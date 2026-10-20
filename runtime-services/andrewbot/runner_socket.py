"""Authenticated, bounded Unix-socket frames for private runner receipts."""

import array
import os
import select
import socket
import struct
import time
from contextlib import contextmanager

from .recovery_identity import digest
from .runner_format import FORMATTER, RunnerProtocolError, encode_runner_request
from .runner_protocol import (
    CONTRACT, VERSION, MAX_ENVELOPE_BYTES, MAX_EVIDENCE_BYTES, MAX_METADATA_BYTES,
    DispatchGrant, _hash, _uuid, decode_frame, encode_frame, validate_evidence,
    validate_grant, validate_identity,
)


def _failure():
    return RunnerProtocolError('runner receipt protocol unavailable')


def _credentials(ancillary, flags, expected_uid):
    credentials = []
    invalid = bool(flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors = array.array('i')
            descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            invalid = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == struct.calcsize('3i'):
            credentials.append(struct.unpack('3i', data))
        else:
            invalid = True
    if invalid or len(credentials) != 1 or credentials[0][0] <= 0 or credentials[0][1] != expected_uid:
        raise _failure()


def read_authenticated_frame(connection, *, expected_uid, limit=MAX_EVIDENCE_BYTES,
                             deadline=None, cancel_event=None):
    """Authenticate the actual sender on every chunk, including the size header.

    SO_PASSCRED must be enabled before connecting. Listener-time SO_PEERCRED is
    deliberately not used by the client: a systemd listener may belong to PID 1.
    """
    if type(expected_uid) is not int or expected_uid < 0 or type(limit) is not int or not 0 < limit <= MAX_ENVELOPE_BYTES:
        raise _failure()
    def exact(length):
        result = bytearray()
        while len(result) < length:
            if cancel_event is not None and cancel_event.is_set():
                raise RunnerProtocolError('runner request cancelled')
            if deadline is not None or cancel_event is not None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise _failure()
                if not select.select([connection], [], [], .1 if remaining is None else min(.1, remaining))[0]:
                    continue
            data, ancillary, flags, _ = connection.recvmsg(
                length - len(result), socket.CMSG_SPACE(struct.calcsize('3i')) + socket.CMSG_SPACE(32 * 4),
                socket.MSG_CMSG_CLOEXEC)
            _credentials(ancillary, flags, expected_uid)
            if not data:
                raise _failure()
            result.extend(data)
        return bytes(result)
    try:
        header = exact(4)
        size = struct.unpack('!I', header)[0]
        if not 0 < size <= limit:
            raise _failure()
        return decode_frame(header + exact(size), limit=limit)
    except OSError:
        raise _failure() from None


def envelope(action, **values):
    return dict(contract=CONTRACT, version=VERSION, action=action, **values)


def _expect_end(connection, *, expected_uid, deadline=None, cancel_event=None):
    """Each response ends its connection; extra frames cannot be ignored."""
    deadline = min(time.monotonic() + 5, deadline) if deadline is not None else time.monotonic() + 5
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise RunnerProtocolError('runner request cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _failure()
        if not select.select([connection], [], [], min(.1, remaining))[0]:
            continue
        data, ancillary, flags, _ = connection.recvmsg(
            1, socket.CMSG_SPACE(struct.calcsize('3i')) + socket.CMSG_SPACE(32 * 4), socket.MSG_CMSG_CLOEXEC)
        # Linux may attach an all-zero credential record to EOF. It carries no
        # payload and cannot authenticate data; every preceding byte was checked.
        empty_credentials = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack('3i', 0, 0, 0))]
        if not data and not flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) and ancillary in ([], empty_credentials):
            return
        _credentials(ancillary, flags, expected_uid)  # Close any passed FDs.
        raise _failure()


def validate_envelope(document):
    if (not isinstance(document, dict) or document.get('contract') != CONTRACT
            or type(document.get('version')) is not int or document['version'] != VERSION):
        raise _failure()
    shape = {
        'capabilities': set(), 'lookup': {'identity'}, 'execute': {'identity', 'request'},
        'dispatch': {'grant'}, 'ready': {'operation_sha', 'challenge'},
    }
    action = document.get('action')
    if not isinstance(action, str) or action not in shape or set(document) != {'contract', 'version', 'action'} | shape[action]:
        raise _failure()
    encode_frame(document, limit=MAX_ENVELOPE_BYTES if action == 'execute' else MAX_METADATA_BYTES + 256)
    return action


def validate_request_identity(identity, request):
    identity = validate_identity(identity.to_dict())
    encode_runner_request(request)
    kind = identity.tool_name.split('.')[0]
    key = 'command' if kind == 'shell' else 'code'
    if (request['type'] != kind or set(request) != {'type', key}
            or digest(request) != identity.worker_request_sha):
        raise _failure()
    arguments = {key: request[key]}
    if digest({'name': identity.tool_name, 'arguments': arguments}) != identity.request_sha:
        raise _failure()
    return identity


@contextmanager
def _connection(client, deadline, cancel_event=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RunnerProtocolError('runner request cancelled')
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        connection.settimeout(5 if deadline is None else max(.001, min(5, deadline - time.monotonic())))
        connection.connect(client.socket_path)
        yield connection
    except OSError:
        raise _failure() from None
    finally:
        connection.close()


def receipt_capabilities(client, *, expected_runner_uid):
    deadline = time.monotonic() + 5
    with _connection(client, deadline) as connection:
        connection.sendall(encode_frame(envelope('capabilities')))
        result = read_authenticated_frame(connection, expected_uid=expected_runner_uid,
                                          limit=MAX_METADATA_BYTES, deadline=deadline)
        _expect_end(connection, expected_uid=expected_runner_uid, deadline=deadline)
    expected = {'contract', 'version', 'runner_store_id', 'controller_store_id', 'formatter',
                'records', 'capacity_available', 'execution_enabled'}
    if (set(result) != expected or result['contract'] != CONTRACT
            or type(result['version']) is not int or result['version'] != VERSION
            or not _uuid(result['runner_store_id']) or not _uuid(result['controller_store_id'])
            or result['formatter'] != FORMATTER or type(result['records']) is not int
            or result['records'] < 0 or type(result['capacity_available']) is not bool
            or type(result['execution_enabled']) is not bool):
        raise _failure()
    return result


def lookup_receipt(client, identity, *, expected_runner_uid, timeout_seconds=5):
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 5:
        raise _failure()
    identity = validate_identity(identity.to_dict())
    deadline = time.monotonic() + timeout_seconds
    with _connection(client, deadline) as connection:
        connection.sendall(encode_frame(envelope('lookup', identity=identity.to_dict())))
        result = read_authenticated_frame(connection, expected_uid=expected_runner_uid, deadline=deadline)
        _expect_end(connection, expected_uid=expected_runner_uid, deadline=deadline)
    return validate_evidence(result, identity)


def execute_receipted(client, request, identity, *, expected_runner_uid, grant_factory, cancel_event=None):
    identity = validate_request_identity(identity, request)
    handshake_deadline = time.monotonic() + 5
    with _connection(client, handshake_deadline, cancel_event) as connection:
        connection.sendall(encode_frame(envelope('execute', identity=identity.to_dict(), request=request)))
        ready = read_authenticated_frame(connection, expected_uid=expected_runner_uid,
                                          deadline=handshake_deadline, cancel_event=cancel_event)
        if ready.get('action') != 'ready':
            evidence = validate_evidence(ready, identity)
            _expect_end(connection, expected_uid=expected_runner_uid, deadline=handshake_deadline,
                        cancel_event=cancel_event)
            return evidence
        validate_envelope(ready)
        if ready['operation_sha'] != digest(identity.to_dict()) or not _hash(ready['challenge']):
            raise _failure()
        grant = grant_factory(dict(ready))
        grant = validate_grant(grant.to_dict() if isinstance(grant, DispatchGrant) else grant)
        if grant.operation_sha != ready['operation_sha'] or grant.challenge != ready['challenge']:
            raise _failure()
        if cancel_event is not None and cancel_event.is_set():
            raise RunnerProtocolError('runner request cancelled')
        connection.sendall(encode_frame(envelope('dispatch', grant=grant.to_dict())))
        connection.settimeout(None)
        deadline = None if client.timeout_seconds is None else time.monotonic() + client.timeout_seconds
        result = read_authenticated_frame(connection, expected_uid=expected_runner_uid,
                                          deadline=deadline, cancel_event=cancel_event)
        _expect_end(connection, expected_uid=expected_runner_uid, deadline=deadline, cancel_event=cancel_event)
    return validate_evidence(result, identity)
