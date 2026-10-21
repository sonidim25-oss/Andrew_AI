"""Receipt handshake for RunnerSocketServer; no public listener or fallback."""

import asyncio
import functools
import secrets
import socket
import struct
import threading
import time
from pathlib import Path

from .recovery_identity import digest
from .runner_format import RunnerProtocolError
from .runner_protocol import MAX_METADATA_BYTES, encode_frame, read_frame_async, validate_grant, validate_identity
from .runner_socket import envelope, validate_envelope, validate_request_identity


def _failure():
    return RunnerProtocolError('runner receipt protocol unavailable')


async def _uncancelled(task):
    """Retain ownership while a previously started durable operation finishes."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _durable(function, *args, **kwargs):
    # Keep the executor Future itself. A separately scheduled to_thread Task can
    # be cancelled during loop shutdown while its thread is still running.
    task = asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _uncancelled(task)
        raise


async def _persist(server,function,*args,**kwargs):
    try:return await _durable(function,*args,**kwargs)
    except Exception:
        # Lookup stays available, but failed receipt persistence cannot be
        # followed by another execution in this service process.
        server._receipt_storage_failed=True
        raise


async def _send(writer, document):
    writer.write(encode_frame(document))
    await writer.drain()


def _live_grant(grant, ready):
    now = time.monotonic() * 1000
    if (grant.boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or not now < grant.expires_monotonic_ms <= now + 15500
            or grant.challenge != ready['challenge'] or grant.operation_sha != ready['operation_sha']):
        raise _failure()


async def _no_extra_input(reader):
    try:
        # Pending extra bytes or EOF reject before admission. Later disconnects
        # race an accepted grant and use the normal worker cancellation path.
        await asyncio.wait_for(reader.read(1), .001)
    except asyncio.TimeoutError:
        return
    raise _failure()


async def _run_and_save(server, identity, request, cancel_event):
    try:
        response = await _durable(server.runner.run, request, cancel_event)
        cleanup_verified = getattr(server.runner, 'cleanup_verified', False) is True
        if not cleanup_verified:
            server._cleanup_failed = True
        return await _persist(server,server.receipts.finish, identity, response, cleanup_verified=cleanup_verified)
    except Exception:
        if getattr(server.runner, 'cleanup_verified', False) is not True:
            server._cleanup_failed = True
        await _persist(server,server.receipts.mark_uncertain, identity)
        raise _failure() from None


async def _execute(server, reader, writer, identity, request):
    # Existing operations have an evidence-only path even while another worker
    # owns execution. No duplicate submission enters the grant handshake.
    old = await asyncio.to_thread(server.receipts.lookup, identity)
    if old.state != 'missing':
        await _send(writer, old.to_dict())
        return
    if not server.receipts_enabled or server._busy or not server._worker_available():
        raise _failure()
    server._busy = True
    try:
        prepared = await _persist(server,server.receipts.prepare, identity)
        if not prepared.created:
            await _send(writer, prepared.evidence.to_dict())
            return
        if not server._worker_available():
            raise _failure()
        ready = envelope('ready', operation_sha=digest(identity.to_dict()), challenge=secrets.token_hex(32))
        await _send(writer, ready)
        document = await asyncio.wait_for(read_frame_async(reader, limit=MAX_METADATA_BYTES), 5)
        if validate_envelope(document) != 'dispatch':
            raise _failure()
        grant = validate_grant(document['grant'])
        _live_grant(grant, ready)
        await _no_extra_input(reader)
        if not server._worker_available():
            raise _failure()
        await _persist(server,server.receipts.admit, identity)
        try:
            _live_grant(grant, ready)
            await _no_extra_input(reader)
            if not server._worker_available():
                raise _failure()
        except BaseException:
            await _persist(server,server.receipts.mark_uncertain, identity)
            raise
        cancel_event = threading.Event()
        work = asyncio.create_task(_run_and_save(server, identity, request, cancel_event))
        peer = asyncio.create_task(reader.read(1))
        disconnected = False
        try:
            done, _ = await asyncio.wait((work, peer), return_when=asyncio.FIRST_COMPLETED)
            if peer in done:
                disconnected = True
                cancel_event.set()
            evidence = await asyncio.shield(work)
            if not disconnected:
                await _send(writer, evidence.to_dict())
        except asyncio.CancelledError:
            cancel_event.set()
            await _uncancelled(work)
            raise
        finally:
            cancel_event.set()
            peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)
    finally:
        server._busy = False


async def handle_receipt(server, request, reader, writer):
    if server.receipts is None or type(server.controller_uid) is not int:
        raise _failure()
    connection = writer.get_extra_info('socket')
    if connection is None:
        raise _failure()
    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    if uid != server.controller_uid:
        raise _failure()
    action = validate_envelope(request)
    if action == 'capabilities':
        capabilities = await asyncio.to_thread(server.receipts.capabilities)
        await _send(writer, {**capabilities, 'execution_enabled': server.receipts_enabled and server._worker_available()})
    elif action == 'lookup':
        identity = validate_identity(request['identity'])
        evidence = await asyncio.to_thread(server.receipts.lookup, identity)
        await _send(writer, evidence.to_dict())
    elif action == 'execute':
        identity = validate_identity(request['identity'])
        identity = validate_request_identity(identity, request['request'])
        await _execute(server, reader, writer, identity, request['request'])
    else:
        raise _failure()
