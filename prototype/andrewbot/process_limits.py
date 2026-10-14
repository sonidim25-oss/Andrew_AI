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


def run_bounded(argv, payload=b"", timeout=120, output_limit=40 * 1024, stderr_limit=4096):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True)
    outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
    limits = {process.stdout: output_limit, process.stderr: stderr_limit}
    pending = memoryview(payload)
    deadline = time.monotonic() + timeout
    timed_out = output_exceeded = False
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.25)):
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
        if not timed_out and not output_exceeded:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        selector.close()
        if timed_out or output_exceeded or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        for stream in streams:
            if not stream.closed:
                stream.close()
    return ProcessResult(process.returncode, bytes(outputs[process.stdout]), bytes(outputs[process.stderr]),
                         timed_out, output_exceeded)
