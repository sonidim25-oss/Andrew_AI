"""Strict immutable metadata and bounded frames for runner result receipts.

This module confers no dispatch authority. Peer authentication and a live,
connection-bound grant must be checked by the receiver before admission.
"""

import asyncio
import json
import math
import struct
import uuid
from dataclasses import dataclass, fields
from types import MappingProxyType

from .recovery_identity import HASH, canonical, digest, valid_id, validate_binding
from .runner_format import FORMATTER, RunnerProtocolError, validate_runner_response

CONTRACT = 'runner_result_recovery_v1'
VERSION = 1
MAX_ENVELOPE_BYTES = 80 * 1024
MAX_METADATA_BYTES = 8 * 1024
MAX_EVIDENCE_BYTES = 48 * 1024
TOOLS = {'python.run', 'node.run', 'shell.run'}


def _error():
    return RunnerProtocolError('invalid runner recovery document')


def _hash(value):
    return isinstance(value, str) and bool(HASH.fullmatch(value))


def _uuid(value):
    try:
        return isinstance(value, str) and value in (str(uuid.UUID(value)), uuid.UUID(value).hex)
    except (ValueError, AttributeError):
        return False


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, (dict, MappingProxyType)):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class _Document:
    def to_dict(self):
        return {field.name: (value.to_dict() if isinstance(value := getattr(self, field.name), _Document)
                             else _thaw(value)) for field in fields(self)}


@dataclass(frozen=True)
class OperationIdentity(_Document):
    contract: str
    version: int
    controller_store_id: str
    runner_store_id: str
    binding: object
    binding_sha: str
    admission_id: str
    effect_id: str
    request_sha: str
    operation_id: str
    legacy_attempt_id: str
    tool_name: str
    worker_request_sha: str
    formatter: str
    research_context: object


@dataclass(frozen=True)
class DispatchGrant(_Document):
    operation_sha: str
    challenge: str
    scope_id: str
    fence: int
    boot_id: str
    expires_monotonic_ms: float


@dataclass(frozen=True)
class ReceiptEvidence(_Document):
    contract: str
    version: int
    identity: OperationIdentity
    identity_sha: str
    state: str
    response: object
    response_sha: str | None
    cleanup_verified: bool

    @property
    def importable(self):
        # A completed program error is a saved outcome, including any partial
        # effects. Import its error text; never execute that operation again.
        # Timeout, cancellation and infrastructure failures remain held.
        return (self.state == 'result' and self.cleanup_verified and self.response is not None
                and ((self.response['status'] == 'ok' and self.response['safe_error'] is None)
                     or (self.response['status'] == 'error' and self.response['safe_error'] == 'execution_failed')))


def _shape(value, cls, limit=MAX_METADATA_BYTES):
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        raise _error()
    _json_bytes(value, limit)


def _contract(value):
    if value['contract'] != CONTRACT or type(value['version']) is not int or value['version'] != VERSION:
        raise _error()


def validate_identity(value):
    _shape(value, OperationIdentity)
    _contract(value)
    try:
        validate_binding(value['binding'])
    except ValueError:
        raise _error() from None
    if (not _uuid(value['controller_store_id']) or not _uuid(value['runner_store_id'])
            or value['binding_sha'] != digest(value['binding'])
            or not all(_hash(value[key]) for key in ('request_sha', 'operation_id', 'worker_request_sha'))
            or not all(valid_id(value[key]) for key in ('admission_id', 'effect_id', 'legacy_attempt_id'))
            or not isinstance(value['tool_name'], str) or value['tool_name'] not in TOOLS
            or value['formatter'] != FORMATTER):
        raise _error()
    context = value['research_context']
    if (not isinstance(context, dict) or set(context) != {'session_id', 'cycle_id', 'research_id', 'research_revision'}
            or any(context[key] != value['binding'][key] for key in ('session_id', 'cycle_id'))
            or not isinstance(context['research_id'], str)
            or (context['research_id'] != '' and not valid_id(context['research_id']))
            or type(context['research_revision']) is not int or not 0 <= context['research_revision'] < 2**53):
        raise _error()
    return OperationIdentity(**{**value, 'binding': _freeze(value['binding']), 'research_context': _freeze(context)})


def validate_grant(value):
    _shape(value, DispatchGrant)
    expiry = value['expires_monotonic_ms']
    if (not _hash(value['operation_sha']) or not _hash(value['challenge'])
            or not valid_id(value['scope_id']) or not _uuid(value['boot_id'])
            or type(value['fence']) is not int or not 1 <= value['fence'] < 2**53
            or type(expiry) not in (int, float) or not math.isfinite(expiry) or not 0 < expiry < 2**53):
        raise _error()
    return DispatchGrant(**value)


def validate_evidence(value, expected):
    _shape(value, ReceiptEvidence, MAX_EVIDENCE_BYTES)
    _contract(value)
    # Revalidate even a manually constructed dataclass; callers cannot bypass checks.
    if not isinstance(expected, OperationIdentity):
        raise _error()
    expected = validate_identity(expected.to_dict())
    identity = validate_identity(value['identity'])
    if (identity != expected or value['identity_sha'] != digest(identity.to_dict())
            or not isinstance(value['state'], str)
            or value['state'] not in {'missing', 'prepared', 'admitted', 'uncertain', 'result'}
            or type(value['cleanup_verified']) is not bool):
        raise _error()
    response = value['response']
    if response is None:
        if value['state'] == 'result' or value['response_sha'] is not None or value['cleanup_verified']:
            raise _error()
    else:
        if value['state'] not in {'result', 'uncertain'}:
            raise _error()
        response = validate_runner_response(response)
        if value['response_sha'] != digest(response):
            raise _error()
        if response.get('visit') is not None:
            raise _error()  # Browser receipts are outside this contract.
    return ReceiptEvidence(**{**value, 'identity': identity, 'response': _freeze(response)})


def _json_bytes(document, limit):
    if type(limit) is not int or not 0 < limit <= MAX_ENVELOPE_BYTES or not isinstance(document, dict):
        raise _error()
    try:
        # Canonical validation rejects unsafe numeric types and nonfinite values.
        canonical(document)
        payload = json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error() from None
    if not payload or len(payload) > limit:
        raise RunnerProtocolError('runner recovery document exceeded limits')
    return payload


def encode_frame(document, *, limit=MAX_ENVELOPE_BYTES):
    payload = _json_bytes(document, limit)
    return struct.pack('!I', len(payload)) + payload


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _error()
        result[key] = value
    return result


def _length(header, limit):
    if type(limit) is not int or not 0 < limit <= MAX_ENVELOPE_BYTES or len(header) != 4:
        raise _error()
    size = struct.unpack('!I', header)[0]
    if not 0 < size <= limit:
        raise _error()
    return size


def _decode(payload, limit):
    try:
        document = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(_error()))
        _json_bytes(document, limit)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error() from None
    return document


def decode_frame(frame, *, limit=MAX_ENVELOPE_BYTES):
    size = _length(frame[:4], limit)
    if len(frame) != size + 4:
        raise _error()
    return _decode(frame[4:], limit)


def read_frame(stream, *, limit=MAX_ENVELOPE_BYTES):
    def exact(length):
        chunks = bytearray()
        while len(chunks) < length:
            chunk = stream.recv(length - len(chunks)) if hasattr(stream, 'recv') else stream.read(length - len(chunks))
            if not chunk:
                raise _error()
            chunks.extend(chunk)
        return bytes(chunks)
    size = _length(exact(4), limit)
    return _decode(exact(size), limit)


async def read_frame_async(reader, *, limit=MAX_ENVELOPE_BYTES):
    document, _ = await read_sized_frame_async(reader, limit=limit)
    return document


async def read_sized_frame_async(reader, *, limit=MAX_ENVELOPE_BYTES):
    """Retain wire size when routing legacy frames under a larger envelope cap."""
    try:
        size = _length(await reader.readexactly(4), limit)
        return _decode(await reader.readexactly(size), limit), size
    except asyncio.IncompleteReadError:
        raise _error() from None
