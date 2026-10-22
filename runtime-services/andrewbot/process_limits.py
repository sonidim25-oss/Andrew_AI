"""Bound pipe capture and wall time outside an untrusted process/container."""
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False
    cancelled: bool = False


def child_environment():
    """Activation belongs to our listener, never a spawned tool or Podman."""
    return {key:value for key,value in os.environ.items()
            if key not in {'LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES'}}


def run_bounded(argv, payload=b"", timeout=120, output_limit=40 * 1024, stderr_limit=4096, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return ProcessResult(-9, b"", b"", cancelled=True)
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True, env=child_environment())
    outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
    limits = {process.stdout: output_limit, process.stderr: stderr_limit}
    pending = memoryview(payload)
    deadline = None if timeout is None else time.monotonic() + timeout
    timed_out = output_exceeded = cancelled = False
    selector = selectors.DefaultSelector()
    streams = (process.stdin, process.stdout, process.stderr)
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        if pending:
            selector.register(process.stdin, selectors.EVENT_WRITE)
        else:
            process.stdin.close()
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(0.25 if remaining is None else min(remaining, 0.25)):
                stream = key.fileobj
                if stream is process.stdin:
                    try:
                        written = os.write(stream.fileno(), pending[:4096])
                        pending = pending[written:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                remaining_output = limits[stream] - len(outputs[stream])
                outputs[stream].extend(chunk[:remaining_output])
                if len(chunk) > remaining_output:
                    output_exceeded = True
                    break
            if output_exceeded:
                break
        while not timed_out and not output_exceeded and not cancelled and process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=0.25 if remaining is None else min(0.25, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            selector.close()
            if timed_out or output_exceeded or cancelled or process.poll() is None:
                try:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                finally:
                    # A failed signal does not prove the child is still alive;
                    # attempt to reap it, retaining the bounded cleanup wait.
                    process.wait(timeout=5)
        finally:
            # Signal/wait failures remain errors, but must not leak pipe handles.
            for stream in streams:
                if not stream.closed:
                    stream.close()
    return ProcessResult(process.returncode, bytes(outputs[process.stdout]), bytes(outputs[process.stderr]),
                         timed_out, output_exceeded, cancelled)
