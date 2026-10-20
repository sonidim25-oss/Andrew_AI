"""Private browser launcher, independent of arbitrary-code runner identity."""
import asyncio
import json
import os
import selectors
import socket
import struct
import subprocess
import sys
import time
import threading
import uuid
from .browser_protocol import MAX_WIRE, validate_action
from .runner_service import WorkerConfig, RunnerService
from .process_limits import child_environment

class BrowserProcess:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.name = None
        self.deadline = 0
        self.healthy = True
        self._lifecycle_lock = threading.RLock()

    def close(self):
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self):
        process, name = self.process, self.name
        self.process = self.name = None
        try:
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.stdin.close()
                process.stdout.close()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.healthy = False
            raise
        finally:
            if name and not RunnerService(self.config)._remove_container(name):
                self.healthy = False

    def reap_idle(self):
        with self._lifecycle_lock:
            # A request may have replaced an expired session before the reaper
            # acquired ownership. Recheck the current identity under the lock.
            if self.process and time.monotonic() >= self.deadline:
                self._close_locked()

    def run(self, request):
        with self._lifecycle_lock:
            return self._run_locked(request)

    def _run_locked(self, request):
        validate_action(request,private=True)
        if self.process and (time.monotonic() >= self.deadline or self.process.poll() is not None): self.close()
        if not self.healthy: raise ValueError('browser cleanup requires administrator recovery')
        if not self.process:
            self.name = 'andrew-browser-' + uuid.uuid4().hex
            args = RunnerService(self.config)._podman_args(self.name)
            # Trusted browser has no shared projects volume.
            mount = args.index('--mount')
            del args[mount:mount+2]
            args[-1:-1] = ['--entrypoint','python3','--workdir','/tmp','--env','HOME=/tmp']
            args += ['/opt/andrew/browser_entry.py']
            self.process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL,close_fds=True,env=child_environment())
            self.deadline = time.monotonic() + 120
        p = self.process
        payload = json.dumps(request,separators=(',',':')).encode() + b'\n'
        deadline = min(self.deadline,time.monotonic()+45)
        result = bytearray()
        selector = selectors.DefaultSelector()
        os.set_blocking(p.stdin.fileno(),False); os.set_blocking(p.stdout.fileno(),False)
        selector.register(p.stdin,selectors.EVENT_WRITE)
        selector.register(p.stdout,selectors.EVENT_READ)
        offset = 0
        try:
            while time.monotonic() < deadline:
                for key,_ in selector.select(min(0.2,max(0,deadline-time.monotonic()))):
                    if key.fileobj is p.stdin:
                        try: offset += os.write(p.stdin.fileno(),payload[offset:offset+4096])
                        except BlockingIOError: continue
                        if offset == len(payload): selector.unregister(p.stdin)
                    else:
                        chunk = os.read(p.stdout.fileno(),65536)
                        if not chunk: raise ValueError('browser unavailable')
                        result.extend(chunk)
                        if len(result) > MAX_WIRE: raise ValueError('browser output limit')
                        if b'\n' in result:
                            if not result.endswith(b'\n') or result.count(b'\n') != 1:
                                raise ValueError('invalid browser framing')
                            return json.loads(result)
            raise ValueError('browser deadline exceeded')
        except Exception:
            self.close()
            raise ValueError('browser session failed safely') from None
        finally: selector.close()

class BrowserServer:
    def __init__(self, process):
        self.process = process
        self.busy = False

    async def reap_idle(self):
        if self.busy or not self.process.process or time.monotonic() < self.process.deadline:
            return False
        # Reserve admission before scheduling cleanup, just like a request.
        self.busy = True
        future = asyncio.get_running_loop().run_in_executor(None, self.process.reap_idle)
        future.add_done_callback(lambda _: setattr(self, 'busy', False))
        await asyncio.shield(future)
        return True

    async def handle(self, reader, writer):
        try:
            size = struct.unpack('!I',await asyncio.wait_for(reader.readexactly(4),5))[0]
            if not 0 < size <= MAX_WIRE: raise ValueError()
            request = json.loads(await asyncio.wait_for(reader.readexactly(size),5))
            if self.busy: raise ValueError()
            validate_action(request,private=True)
            self.busy = True
            future = asyncio.get_running_loop().run_in_executor(None,self.process.run,request)
            future.add_done_callback(lambda _:setattr(self,'busy',False))
            try: result = await asyncio.shield(future)
            except Exception: result = {'status':'error','error':'browser_unavailable'}
            data = json.dumps(result,separators=(',',':')).encode()
            if len(data)>MAX_WIRE: raise ValueError()
            writer.write(struct.pack('!I',len(data))+data)
            await asyncio.wait_for(writer.drain(),5)
        except (OSError,ValueError,asyncio.IncompleteReadError,asyncio.TimeoutError): pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass

async def serve(process):
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('systemd activation required')
    listener = socket.fromfd(3,socket.AF_UNIX,socket.SOCK_STREAM)
    listener.setblocking(False)
    handler = BrowserServer(process)
    # Independent reaper enforces idle lifetime as well as request deadlines.
    async def reap():
        while True:
            await asyncio.sleep(1)
            await handler.reap_idle()
    reaper = asyncio.create_task(reap())
    try:
        server = await asyncio.start_unix_server(handler.handle,sock=listener,limit=MAX_WIRE)
        async with server: await server.serve_forever()
    finally:
        reaper.cancel()
        await asyncio.to_thread(process.close)

def main():
    try:
        values = dict(line.strip().split('=',1) for line in open('/etc/andrew/runner.conf') if '=' in line and not line.startswith('#'))
        config = WorkerConfig(values['IMAGE'],'/var/lib/andrew/browser/projects',values['PROXY_SOCKET'],
                              cgroup_parent=os.environ.get('ANDREW_CGROUP_PARENT'))
        RunnerService(config).recover()
        asyncio.run(serve(BrowserProcess(config)))
    except KeyboardInterrupt: return 0
    except Exception:
        print('browser service startup failed',file=sys.stderr)
        return 1

if __name__ == '__main__': raise SystemExit(main())
