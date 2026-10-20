"""Destination validation for Andrew's policy-enforcing egress proxy."""

import ipaddress
import socket
import re
import asyncio
import os
import sys
import json
from .network_policy import excluded_networks, is_public
from urllib.parse import urlsplit


class ProxyPolicyError(RuntimeError):
    """A destination or request was rejected by egress policy."""


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class DestinationPolicy:
    def __init__(self, resolver=None, blocked_networks=()):
        self.resolver = resolver or socket.getaddrinfo
        self.exclusions = excluded_networks(blocked_networks)

    def resolve_and_validate(self, host: str, port: int) -> tuple[str, ...]:
        if not isinstance(host, str) or not host or len(host) > 253:
            raise ProxyPolicyError("invalid destination")
        if isinstance(port, bool) or port not in {80, 443}:
            raise ProxyPolicyError("destination port is not allowed")
        host = host.rstrip(".")
        if not host or "%" in host:
            raise ProxyPolicyError("invalid destination")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not is_public(literal, self.exclusions):
                raise ProxyPolicyError("destination is not public")
            return (str(literal),)
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ProxyPolicyError("invalid destination") from None
        labels = ascii_host.split(".")
        if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ProxyPolicyError("invalid destination")
        try:
            records = self.resolver(ascii_host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise ProxyPolicyError("destination could not be resolved") from None
        addresses = []
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
            except (ValueError, IndexError, TypeError):
                raise ProxyPolicyError("destination resolution was invalid") from None
            if not is_public(address, self.exclusions):
                raise ProxyPolicyError("destination is not public")
            canonical = str(address)
            if canonical not in addresses:
                addresses.append(canonical)
        if not addresses:
            raise ProxyPolicyError("destination has no addresses")
        return tuple(addresses)


class EgressProxy:
    """Single-request HTTP proxy that connects only to validated pinned IPs."""

    HEADER_LIMIT = 32 * 1024
    BODY_LIMIT = 8 * 1024 * 1024
    TUNNEL_LIMIT = 256 * 1024 * 1024
    CONNECT_TIMEOUT = 15
    SESSION_TIMEOUT = 120
    MAX_CONNECTIONS = 32

    def __init__(self, policy=None, connector=None):
        self.policy = policy or DestinationPolicy()
        self.connector = connector or self._connect_pinned
        self._connections = 0
        self._resolving = 0

    async def _resolve(self, host, port):
        if self._resolving >= 8:
            raise ProxyPolicyError("resolver capacity reached")
        self._resolving += 1
        future = asyncio.get_running_loop().run_in_executor(None, self.policy.resolve_and_validate, host, port)
        def finished(done):
            self._resolving -= 1
            if not done.cancelled(): done.exception()
        future.add_done_callback(finished)
        # Timeout/cancellation must not release a slot while libc still resolves.
        return await asyncio.wait_for(asyncio.shield(future), self.CONNECT_TIMEOUT)

    async def handle_request(self, reader, writer):
        if self._connections >= self.MAX_CONNECTIONS:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
            return
        self._connections += 1
        try:
            await asyncio.wait_for(self._handle_request(reader, writer), self.SESSION_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            writer.close()
        finally:
            self._connections -= 1

    async def _handle_request(self, reader, writer):
        upstream_writer = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.CONNECT_TIMEOUT)
            if len(header) > self.HEADER_LIMIT:
                raise ProxyPolicyError("request headers exceeded limit")
            request_line, headers = self._parse_header(header)
            method, target, _version = request_line
            if method == "CONNECT":
                host, port = self._parse_authority(target)
                if port != 443:
                    raise ProxyPolicyError("CONNECT requires HTTPS port 443")
                address = (await self._resolve(host, port))[0]
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self.connector(address, port), timeout=self.CONNECT_TIMEOUT
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
                return
            parsed = urlsplit(target)
            if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
                raise ProxyPolicyError("only absolute public HTTP requests are accepted")
            port = parsed.port or 80
            address = (await self._resolve(parsed.hostname, port))[0]
            content_length = self._content_length(headers)
            if content_length > self.BODY_LIMIT:
                raise ProxyPolicyError("request body exceeded limit")
            body = await reader.readexactly(content_length) if content_length else b""
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self.connector(address, port), timeout=self.CONNECT_TIMEOUT
            )
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            rewritten = self._rewrite_headers(request_line, headers, path, parsed.netloc, content_length)
            upstream_writer.write(rewritten + body)
            await upstream_writer.drain()
            await self._copy_bounded(upstream_reader, writer, self.TUNNEL_LIMIT)
        except (ProxyPolicyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ValueError):
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (OSError, ConnectionError):
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    @staticmethod
    async def _connect_pinned(address, port):
        ip = ipaddress.ip_address(address)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        return await asyncio.open_connection(str(ip), port, family=family)

    @staticmethod
    def _parse_header(header):
        try:
            lines = header[:-4].decode("iso-8859-1").split("\r\n")
            request_line = lines[0].split(" ", 2)
            if len(request_line) != 3 or request_line[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError
            headers = []
            for line in lines[1:]:
                if not line or ":" not in line or line[:1].isspace():
                    raise ValueError
                name, value = line.split(":", 1)
                if not name.strip() or "\r" in value or "\n" in value:
                    raise ValueError
                if name.lower().strip() in {"proxy-authorization", "proxy-authenticate"}:
                    raise ValueError
                headers.append((name.strip(), value.strip()))
            return request_line, headers
        except (UnicodeError, IndexError, ValueError):
            raise ProxyPolicyError("malformed proxy request") from None

    @staticmethod
    def _parse_authority(target):
        try:
            parsed = urlsplit("//" + target)
            if parsed.username or parsed.password or not parsed.hostname or parsed.port is None:
                raise ValueError
            return parsed.hostname, parsed.port
        except ValueError:
            raise ProxyPolicyError("malformed CONNECT authority") from None

    @classmethod
    def _content_length(cls, headers):
        values = [value for name, value in headers if name.lower() == "content-length"]
        if any(name.lower() == "transfer-encoding" for name, _ in headers):
            raise ProxyPolicyError("transfer-encoded HTTP request is not accepted")
        if len(values) > 1:
            raise ProxyPolicyError("ambiguous content length")
        try:
            length = int(values[0]) if values else 0
        except ValueError:
            raise ProxyPolicyError("invalid content length") from None
        if length < 0:
            raise ProxyPolicyError("invalid content length")
        return length

    @staticmethod
    def _rewrite_headers(request_line, headers, path, authority, content_length):
        method = request_line[0]
        lines = [f"{method} {path} HTTP/1.1", f"Host: {authority}", "Connection: close"]
        for name, value in headers:
            lower = name.lower()
            if lower in {"host", "connection", "proxy-connection", "content-length"}:
                continue
            lines.append(f"{name}: {value}")
        if content_length:
            lines.append(f"Content-Length: {content_length}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    @classmethod
    async def _copy_bounded(cls, source, destination, maximum):
        copied = 0
        while True:
            chunk = await asyncio.wait_for(source.read(64 * 1024), timeout=cls.SESSION_TIMEOUT)
            if not chunk:
                return
            copied += len(chunk)
            if copied > maximum:
                raise ProxyPolicyError("proxy transfer exceeded limit")
            destination.write(chunk)
            await destination.drain()

    @classmethod
    async def _tunnel(cls, client_reader, client_writer, upstream_reader, upstream_writer):
        tasks = {
            asyncio.create_task(cls._copy_bounded(client_reader, upstream_writer, cls.TUNNEL_LIMIT)),
            asyncio.create_task(cls._copy_bounded(upstream_reader, client_writer, cls.TUNNEL_LIMIT)),
        }
        try:
            done, pending = await asyncio.wait(tasks, timeout=cls.SESSION_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise asyncio.TimeoutError
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve_systemd_socket(proxy=None):
    """Serve the systemd-activated Unix listener; refuse direct standalone binds."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("systemd socket activation is required")
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    listener.setblocking(False)
    server = await asyncio.start_unix_server((proxy or EgressProxy()).handle_request, sock=listener, limit=32 * 1024)
    async with server:
        await server.serve_forever()


def main():
    try:
        from .runtime_readiness import protected_read
        settings = json.loads(protected_read('/etc/andrew/egress.conf'))
        if set(settings) != {'blocked_networks'} or not settings['blocked_networks']:
            raise ValueError('host-network exclusions required')
        asyncio.run(serve_systemd_socket(EgressProxy(DestinationPolicy(blocked_networks=settings['blocked_networks']))))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # Only the exception class is safe to log; no request or URL data is retained.
        print(f"egress proxy failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
