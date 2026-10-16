"""Bounded, secret-free client for the isolated Andrew tool runner."""

import json
import select
import time
from contextvars import ContextVar
import socket
import struct


TOOL_CANCELLATION = ContextVar("runner_tool_cancellation", default=None)


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 40 * 1024
_REQUEST_FIELDS = {
    "python": {"type", "code"},
    "node": {"type", "code"},
    "shell": {"type", "command"},
    "browser_open": {"type", "url"},
}
_SAFE_ERRORS = {"invalid_request", "unsupported_tool", "execution_failed", "timeout", "cancelled", "proxy_unavailable", "policy_rejected", "worker_unavailable"}


class RunnerProtocolError(RuntimeError):
    """A safe, bounded runner transport or response error."""


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

    @staticmethod
    def _encode_request(request):
        if not isinstance(request, dict) or not isinstance(request.get("type"), str):
            raise RunnerProtocolError("invalid runner request")
        request_type = request["type"]
        if request_type not in _REQUEST_FIELDS or set(request) != _REQUEST_FIELDS[request_type]:
            raise RunnerProtocolError("unsupported runner request")
        value_key = "url" if request_type == "browser_open" else "command" if request_type == "shell" else "code"
        value = request.get(value_key)
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise RunnerProtocolError("invalid runner request")
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise RunnerProtocolError("runner request exceeded protocol limits")
        return payload

    @staticmethod
    def _validate_result(result):
        required = {"status", "stdout", "stderr", "artifacts", "safe_error"}
        if not isinstance(result, dict) or set(result) not in (required, required | {"visit"}):
            raise RunnerProtocolError("runner returned an invalid response")
        if result["status"] not in {"ok", "error"}:
            raise RunnerProtocolError("runner returned an invalid response")
        if not isinstance(result["stdout"], str) or not isinstance(result["stderr"], str):
            raise RunnerProtocolError("runner returned an invalid response")
        if len(result["stdout"].encode("utf-8")) > MAX_RESPONSE_BYTES or len(result["stderr"].encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise RunnerProtocolError("runner returned an oversized response")
        if not isinstance(result["artifacts"], list) or len(result["artifacts"]) > 32:
            raise RunnerProtocolError("runner returned an invalid response")
        if any(not isinstance(path, str) or path.startswith("/") or ".." in path.split("/") for path in result["artifacts"]):
            raise RunnerProtocolError("runner returned an invalid response")
        safe_error = result["safe_error"]
        if safe_error is not None and safe_error not in _SAFE_ERRORS:
            raise RunnerProtocolError("runner returned an invalid response")
        visit = result.get("visit")
        if visit is not None and (not isinstance(visit, dict) or set(visit) != {"url", "title"}
                                  or not isinstance(visit["url"], str) or not isinstance(visit["title"], str)
                                  or len(visit["url"]) > 2048 or len(visit["title"]) > 512):
            raise RunnerProtocolError("runner returned an invalid response")
        result["visit"] = visit
        return result
