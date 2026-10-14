"""Fixed entrypoint for one untrusted request inside the disposable worker."""

import http.server
import json
import os
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_ARTIFACTS = 32
REQUEST_FIELDS = {"python": {"type", "code"}, "node": {"type", "code"}, "shell": {"type", "command"}, "browser_open": {"type", "url"}}


def _result(status="ok", stdout="", stderr="", artifacts=None, safe_error=None, visit=None):
    return {"status": status, "stdout": stdout, "stderr": stderr, "artifacts": artifacts or [], "safe_error": safe_error, "visit": visit}


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            remote.settimeout(15)
            remote.connect(self.server.unix_path)
            remote.settimeout(None)
            self.request.settimeout(None)
            selector = selectors.DefaultSelector()
            selector.register(self.request, selectors.EVENT_READ, remote)
            selector.register(remote, selectors.EVENT_READ, self.request)
            while True:
                for key, _ in selector.select(timeout=120):
                    chunk = key.fileobj.recv(64 * 1024)
                    if not chunk:
                        return
                    key.data.sendall(chunk)
        except OSError:
            return
        finally:
            remote.close()


class UnixProxyBridge:
    def __init__(self, unix_path):
        self.unix_path = unix_path
        self.server = None
        self.thread = None

    def create_server(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _BridgeHandler, bind_and_activate=True)
        server.daemon_threads = True
        server.unix_path = self.unix_path
        self.server = server
        return server

    def start(self):
        server = self.server or self.create_server()
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        host, port = server.server_address
        return f"http://{host}:{port}"

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=1)


def _artifacts(workspace):
    root = Path(workspace).resolve()
    found = []
    for item in root.iterdir():
        try:
            if item.is_file() and not item.is_symlink():
                found.append(item.relative_to(root).as_posix())
        except OSError:
            continue
        if len(found) >= MAX_ARTIFACTS:
            break
    return found


def _run_child(command, environment, cwd, timeout=120):
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in outputs:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        for key, _ in selector.select(timeout=min(remaining, 0.25)):
            try:
                chunk = key.fileobj.read(4096)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            output = outputs[key.fileobj]
            if len(output) < MAX_OUTPUT_BYTES:
                output.extend(chunk[:MAX_OUTPUT_BYTES - len(output)])
        if process.poll() is not None and not selector.get_map():
            break
    if timed_out:
        process.wait(timeout=5)
        return -1, bytes(outputs[process.stdout]), bytes(outputs[process.stderr]), True
    return process.wait(timeout=5), bytes(outputs[process.stdout]), bytes(outputs[process.stderr]), False


def _browser(url, proxy_url, environment, cwd):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return _result("error", safe_error="invalid_request")
    script = """import json,os,sys
from urllib.parse import urlsplit,urlunsplit
from playwright.sync_api import sync_playwright
url, proxy = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, proxy={\"server\": proxy}, executable_path=os.environ[\"CHROMIUM_EXECUTABLE_PATH\"], args=[\"--no-sandbox\", \"--disable-crash-reporter\", \"--disable-breakpad\"])
    page = browser.new_page()
    page.goto(url, wait_until=\"domcontentloaded\", timeout=30000)
    title = page.title()[:512]
    text = page.locator(\"body\").inner_text(timeout=5000)[:15000]
    final = urlsplit(page.url)
    safe_url = urlunsplit((final.scheme, final.netloc, final.path, \"\", \"\"))
    print(json.dumps({\"url\": safe_url, \"title\": title, \"text\": text}, ensure_ascii=False))
    browser.close()
"""
    try:
        browser_environment = dict(environment)
        browser_environment["HOME"] = "/tmp"
        code, stdout, _stderr, timed_out = _run_child([sys.executable, "-c", script, url, proxy_url], browser_environment, cwd, timeout=35)
    except OSError:
        return _result("error", safe_error="worker_unavailable")
    if timed_out:
        return _result("error", safe_error="timeout")
    if code != 0:
        return _result("error", safe_error="execution_failed")
    try:
        page = json.loads(stdout.decode("utf-8"))
        if not isinstance(page, dict) or set(page) != {"url", "title", "text"}:
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _result("error", safe_error="execution_failed")
    visit = {"url": page["url"], "title": page["title"]}
    output = "URL: " + visit["url"] + "\nTITLE: " + visit["title"] + "\n--- PAGE TEXT ---\n" + page["text"]
    return _result(stdout=output, visit=visit)


def execute_request(request, proxy_url=None, workspace=None, timeout=120):
    if not isinstance(request, dict) or request.get("type") not in REQUEST_FIELDS or set(request) != REQUEST_FIELDS.get(request.get("type"), set()):
        return _result("error", safe_error="invalid_request")
    kind = request["type"]
    field = "url" if kind == "browser_open" else "command" if kind == "shell" else "code"
    value = request[field]
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
        return _result("error", safe_error="invalid_request")
    cwd = str(workspace or (Path("/workspace") if Path("/workspace").is_dir() else Path.cwd()))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": cwd, "TMPDIR": "/tmp", "PWD": cwd}
    if proxy_url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy_url
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = env["NO_PROXY"]
        env["npm_config_proxy"] = proxy_url
        env["npm_config_https_proxy"] = proxy_url
        env["NODE_USE_ENV_PROXY"] = "1"
    if kind == "browser_open":
        if not proxy_url:
            return _result("error", safe_error="proxy_unavailable")
        return _browser(value, proxy_url, env, cwd)
    command = [sys.executable, "-c", value] if kind == "python" else ["node", "-e", value] if kind == "node" else ["/bin/sh", "-lc", value]
    try:
        code, stdout, stderr, timed_out = _run_child(command, env, cwd, timeout=timeout)
    except OSError:
        return _result("error", safe_error="unsupported_tool" if kind == "node" else "execution_failed")
    if timed_out:
        return _result("error", stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), safe_error="timeout")
    artifacts = _artifacts(cwd)
    return _result("ok" if code == 0 else "error", stdout.decode("utf-8", errors="replace"),
                   stderr.decode("utf-8", errors="replace"), artifacts,
                   None if code == 0 else "execution_failed")


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        response = _result("error", safe_error="invalid_request")
    else:
        bridge = UnixProxyBridge("/run/andrew-egress.sock")
        proxy_url = bridge.start()
        try:
            response = execute_request(request, proxy_url=proxy_url)
        finally:
            bridge.close()
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
