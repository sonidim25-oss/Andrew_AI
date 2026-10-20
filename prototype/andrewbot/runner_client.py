"""Bounded, secret-free client for the isolated Andrew tool runner."""

import json
import select
import time
from contextvars import ContextVar
import socket
import struct


TOOL_CANCELLATION = ContextVar("runner_tool_cancellation", default=None)


from .runner_format import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, REQUEST_FIELDS as _REQUEST_FIELDS,
    SAFE_ERRORS as _SAFE_ERRORS, RunnerProtocolError, encode_runner_request,
    validate_runner_response,
)


def _read_exactly(connection, length, cancel_event=None):
    result = bytearray()
    timeout = connection.gettimeout()
    deadline = None if timeout is None else time.monotonic() + timeout
    while len(result) < length:
        if cancel_event is not None:
            if cancel_event.is_set():
                raise RunnerProtocolError("runner request cancelled")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise socket.timeout()
            if not select.select([connection], [], [], 0.25 if remaining is None else min(0.25, remaining))[0]:
                continue
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise RunnerProtocolError("runner response was incomplete")
        result.extend(chunk)
        if timeout is not None:
            deadline = time.monotonic() + timeout
    return bytes(result)


class RunnerClient:
    def __init__(self, socket_path: str, timeout_seconds: int | None = None):
        if timeout_seconds is not None and (not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0):
            raise ValueError("timeout must be positive or None")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def run(self, request: dict) -> dict:
        payload = self._encode_request(request)
        cancel_event = TOOL_CANCELLATION.get()
        if cancel_event is not None and cancel_event.is_set():
            raise RunnerProtocolError("runner request cancelled")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(5)
            connection.connect(self.socket_path)
            if cancel_event is not None and cancel_event.is_set():
                raise RunnerProtocolError("runner request cancelled")
            connection.sendall(struct.pack("!I", len(payload)) + payload)
            connection.settimeout(self.timeout_seconds)
            size = struct.unpack("!I", _read_exactly(connection, 4, cancel_event))[0]
            if size == 0 or size > MAX_RESPONSE_BYTES:
                raise RunnerProtocolError("runner response exceeded protocol limits")
            raw = _read_exactly(connection, size, cancel_event)
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RunnerProtocolError("runner returned an invalid response") from None
            return self._validate_result(result)
        except socket.timeout:
            raise RunnerProtocolError("runner request timed out") from None
        except OSError:
            raise RunnerProtocolError("runner unavailable") from None
        finally:
            connection.close()

    _encode_request = staticmethod(encode_runner_request)
    _validate_result = staticmethod(validate_runner_response)

    def receipt_capabilities(self, *, expected_runner_uid):
        from .runner_socket import receipt_capabilities
        return receipt_capabilities(self, expected_runner_uid=expected_runner_uid)

    def lookup_receipt(self, identity, *, expected_runner_uid, timeout_seconds=5):
        from .runner_socket import lookup_receipt
        return lookup_receipt(self, identity, expected_runner_uid=expected_runner_uid,
                              timeout_seconds=timeout_seconds)

    def execute_receipted(self, request, identity, *, expected_runner_uid, grant_factory, cancel_event=None):
        from .runner_socket import execute_receipted
        if cancel_event is None:
            cancel_event = TOOL_CANCELLATION.get()
        return execute_receipted(self, request, identity, expected_runner_uid=expected_runner_uid,
                                 grant_factory=grant_factory, cancel_event=cancel_event)
