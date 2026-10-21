"""Fixed-configuration rootless Podman runner for untrusted tool requests."""

import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from .process_limits import run_bounded, child_environment


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 40 * 1024
MAX_STREAM_BYTES = 16 * 1024
_REQUEST_FIELDS = {"python": {"type", "code"}, "node": {"type", "code"}, "shell": {"type", "command"}, "browser_open": {"type", "url"}}


@dataclass(frozen=True)
class WorkerConfig:
    image: str
    workspace: str
    proxy_socket: str
    podman_binary: str = "/usr/bin/podman"
    timeout_seconds: int | None = None
    cgroup_parent: str | None = None

    def __post_init__(self):
        if not re.search(r"@sha256:[0-9a-f]{64}$", self.image):
            raise ValueError("worker image must be pinned by digest")
        workspace = Path(self.workspace)
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir() or workspace.name != "projects":
            raise ValueError("worker workspace must be the isolated projects directory")
        if not Path(self.proxy_socket).is_absolute():
            raise ValueError("proxy socket path must be absolute")
        if self.timeout_seconds is not None and (type(self.timeout_seconds) is not int or self.timeout_seconds <= 0):
            raise ValueError("worker timeout must be positive or None")
        if self.cgroup_parent is not None and (not isinstance(self.cgroup_parent,str)
                or not self.cgroup_parent.startswith('/') or not self.cgroup_parent.endswith('.service')
                or any(p in {'.','..',''} for p in self.cgroup_parent[1:].split('/'))):
            raise ValueError('worker cgroup parent must be an absolute service cgroup')


class RunnerService:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.healthy = True
        self.cleanup_verified = False

    def _podman_args(self, container_name, timeout=None):
        args = [
            self.config.podman_binary, "--runtime=/usr/bin/crun", "run", "--rm", "--name", container_name, "-i",
            "--log-driver=none", "--pull=never", "--label=org.andrew.runtime=managed",
            "--group-add=keep-groups", "--network=none", "--userns=keep-id:uid=1000,gid=1000", "--read-only", "--user=1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=2g", "--cpus=2",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--mount", f"type=bind,src={self.config.workspace},dst=/workspace,rw=true",
            "--mount", f"type=bind,src={self.config.proxy_socket},dst=/run/andrew-egress.sock,rw=true",
            self.config.image,
        ]
        if timeout is not None:
            args.insert(args.index("--log-driver=none"), "--timeout=" + str(timeout + 5))
        if self.config.cgroup_parent is not None:
            # Rootless systemd scopes cannot move a PID out of a system service
            # into user@.service. Keep every worker inside our delegated subtree.
            current = Path('/proc/self/cgroup').read_text().strip()
            if current != '0::'+self.config.cgroup_parent+'/supervisor':
                raise ValueError('launcher is outside its delegated supervisor cgroup')
            args.insert(args.index('run'),'--cgroup-manager=cgroupfs')
            args.insert(args.index('run')+1,'--cgroup-parent='+self.config.cgroup_parent)
        return args

    @staticmethod
    def _validate_request(request):
        if not isinstance(request, dict) or request.get("type") not in _REQUEST_FIELDS:
            raise ValueError("unsupported tool request")
        kind = request["type"]
        if set(request) != _REQUEST_FIELDS[kind]:
            raise ValueError("invalid tool request")
        field = "url" if kind == "browser_open" else "command" if kind == "shell" else "code"
        value = request[field]
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("invalid tool request")
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("serialized tool request exceeds the limit")
        return payload

    @staticmethod
    def _worker_result(stdout):
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        required = {"status", "stdout", "stderr", "artifacts", "safe_error"}
        if (not isinstance(value, dict) or not required <= set(value) <= required | {"visit", "output_truncated"}
                or ("output_truncated" in value and type(value["output_truncated"]) is not bool)):
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        if value["status"] not in {"ok", "error"} or not isinstance(value["stdout"], str) or not isinstance(value["stderr"], str):
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        for stream in ('stdout', 'stderr'):
            original = value[stream]
            value[stream] = original.encode('utf-8')[:MAX_STREAM_BYTES].decode('utf-8', errors='ignore')
            if value[stream] != original:
                value['output_truncated'] = True
        value["artifacts"] = [p for p in value["artifacts"] if isinstance(p, str) and not p.startswith("/") and ".." not in p.split("/")][:32] if isinstance(value["artifacts"], list) else []
        if value["safe_error"] not in {None, "invalid_request", "unsupported_tool", "execution_failed", "timeout", "cancelled", "proxy_unavailable", "policy_rejected", "worker_unavailable"}:
            value["safe_error"] = "worker_unavailable"
        visit = value.get("visit")
        if visit is not None and (not isinstance(visit, dict) or set(visit) != {"url", "title"}
                                  or not isinstance(visit["url"], str) or not isinstance(visit["title"], str)
                                  or len(visit["url"]) > 2048 or len(visit["title"]) > 512):
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable", "visit": None}
        value["visit"] = visit
        return value

    def run(self, request: dict, cancel_event=None) -> dict:
        self.cleanup_verified = False
        payload = self._validate_request(request)
        if not self.healthy:
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        container_name = "andrew-tool-" + uuid.uuid4().hex
        try:
            timeout = 120 if request["type"] == "browser_open" else self.config.timeout_seconds
            process = run_bounded(self._podman_args(container_name, timeout=timeout), payload=payload,
                                  timeout=timeout, output_limit=MAX_RESPONSE_BYTES, cancel_event=cancel_event)
        except OSError:
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        finally:
            self.cleanup_verified = self._remove_container(container_name) is True
            if not self.cleanup_verified:
                self.healthy = False
        if process.cancelled:
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "cancelled"}
        if process.timed_out:
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "timeout"}
        if process.output_exceeded or process.returncode != 0:
            return {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
        return self._worker_result(process.stdout)

    def _remove_container(self, container_name):
        try:
            result = subprocess.run([self.config.podman_binary, "rm", "--force", "--ignore", container_name],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False, env=child_environment())
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def recover(self):
        result = run_bounded([self.config.podman_binary,"ps","-aq","--filter","label=org.andrew.runtime=managed"],
                             timeout=15, output_limit=8192)
        if result.returncode or result.timed_out or result.output_exceeded:
            raise RuntimeError("cannot inspect interrupted workers")
        for container_id in result.stdout.decode('ascii').splitlines():
            if not re.fullmatch(r'[0-9a-f]{12,64}',container_id) or not self._remove_container(container_id):
                raise RuntimeError("cannot clean interrupted worker")


class RunnerSocketServer:
    def __init__(self, runner, *, receipts=None, controller_uid=None, receipts_enabled=False):
        if type(receipts_enabled) is not bool or (receipts_enabled and
                (receipts is None or type(controller_uid) is not int or controller_uid < 0)):
            raise ValueError('invalid runner receipt configuration')
        self.runner = runner
        self.receipts = receipts
        self.controller_uid = controller_uid
        self.receipts_enabled = receipts_enabled
        self._busy = False
        self._receipt_storage_failed = False
        self._cleanup_failed = False

    def _worker_available(self):
        return (not self._receipt_storage_failed and not self._cleanup_failed
                and getattr(self.runner, 'healthy', True) is True)

    async def handle(self, reader, writer):
        try:
            from .runner_protocol import MAX_ENVELOPE_BYTES, encode_frame, read_sized_frame_async
            request, wire_size = await asyncio.wait_for(read_sized_frame_async(reader, limit=MAX_ENVELOPE_BYTES), timeout=5)
            if 'contract' in request or 'action' in request:
                from .runner_receipt_server import handle_receipt
                await handle_receipt(self, request, reader, writer)
                return
            # Legacy requests retain their worker-frame limit and behavior.
            if wire_size > MAX_REQUEST_BYTES:
                raise ValueError('runner request exceeded protocol limits')
            encode_frame(request, limit=MAX_REQUEST_BYTES)
            if self._busy or not self._worker_available():
                result = {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}
            else:
                self._busy = True
                # Keep ownership until the executor actually finishes, even if
                # the peer disconnects or its coroutine is cancelled.
                cancel_event = threading.Event()
                future = asyncio.get_running_loop().run_in_executor(None, self.runner.run, request, cancel_event)
                future.add_done_callback(lambda _: setattr(self, "_busy", False))
                peer = asyncio.create_task(reader.read(1))
                try:
                    done, _ = await asyncio.wait((future, peer), return_when=asyncio.FIRST_COMPLETED)
                    if peer in done:
                        # Each connection carries one request. EOF or extra input
                        # ends ownership, but cleanup must finish before reuse.
                        cancel_event.set()
                        await asyncio.shield(future)
                        return
                    result = await asyncio.shield(future)
                except (ValueError, UnicodeError):
                    result = {"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "invalid_request"}
                finally:
                    cancel_event.set()
                    peer.cancel()
                    await asyncio.gather(peer, return_exceptions=True)
            response = json.dumps(result, separators=(",", ":")).encode("utf-8")
            if len(response) > MAX_RESPONSE_BYTES:
                response = json.dumps({"status": "error", "stdout": "", "stderr": "", "artifacts": [], "safe_error": "worker_unavailable"}).encode("utf-8")
            writer.write(struct.pack("!I", len(response)) + response)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, UnicodeError, json.JSONDecodeError, ValueError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass


async def serve_systemd_socket(runner, **receipt_options):
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("systemd socket activation is required")
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    listener.setblocking(False)
    server = await asyncio.start_unix_server(RunnerSocketServer(runner, **receipt_options).handle, sock=listener, limit=MAX_REQUEST_BYTES)
    async with server:
        await server.serve_forever()


def main():
    config_path = os.environ.get("ANDREW_RUNNER_CONFIG", "/etc/andrew/runner.conf")
    try:
        values = {}
        with open(config_path, encoding="utf-8") as configuration:
            for line in configuration:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
        config = WorkerConfig(values["IMAGE"], values["WORKSPACE"], values["PROXY_SOCKET"],
                              cgroup_parent=os.environ.get('ANDREW_CGROUP_PARENT'))
        runner = RunnerService(config)
        from .runner_adoption import startup_record, open_receiver
        record = startup_record('runner')
        receipts = None
        try:
            options = {}
            if record is not None:
                receipts = open_receiver(record, os.environ.get('CREDENTIALS_DIRECTORY',''))
                options = dict(receipts=receipts,controller_uid=record['controller_uid'],receipts_enabled=record['mode']=='enabled')
            runner.recover()
            asyncio.run(serve_systemd_socket(runner, **options))
        finally:
            if receipts is not None:receipts.close()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"runner service failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
