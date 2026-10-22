import importlib
import importlib.util
import socket
import unittest
import asyncio
from unittest.mock import patch


def proxy_module(case):
    case.assertIsNotNone(importlib.util.find_spec("andrewbot.egress_proxy"), "egress proxy is implemented")
    return importlib.import_module("andrewbot.egress_proxy")


def fake_resolver(addresses):
    def resolve(_host, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, type, 6, "", (address, port)) for address in addresses]
    return resolve


class DestinationPolicyTests(unittest.TestCase):
    def test_rejects_transition_documentation_and_host_connected_ranges(self):
        module = proxy_module(self)
        for address in ("64:ff9b::a00:1", "2002:7f00:1::", "3fff::1", "198.51.100.4", "8.8.8.8"):
            with self.subTest(address=address):
                policy = module.DestinationPolicy(fake_resolver([address]), blocked_networks=["8.8.8.0/24"])
                with self.assertRaises(module.ProxyPolicyError):
                    policy.resolve_and_validate("untrusted.example", 443)
    def test_returns_pinned_public_ipv4_and_ipv6_answers(self):
        module = proxy_module(self)
        policy = module.DestinationPolicy(resolver=fake_resolver(["1.1.1.1", "2606:4700:4700::1111"]))
        self.assertEqual(policy.resolve_and_validate("public.example", 443), ("1.1.1.1", "2606:4700:4700::1111"))

    def test_rejects_entire_answer_set_if_any_answer_is_private(self):
        module = proxy_module(self)
        policy = module.DestinationPolicy(resolver=fake_resolver(["1.1.1.1", "10.0.0.4"]))
        with self.assertRaises(module.ProxyPolicyError):
            policy.resolve_and_validate("mixed.example", 443)

    def test_rejects_private_reserved_and_metadata_addresses(self):
        module = proxy_module(self)
        for address in ("127.0.0.1", "10.0.0.4", "169.254.169.254", "100.64.0.2", "224.0.0.1", "::1", "fc00::1", "fe80::1"):
            with self.subTest(address=address):
                policy = module.DestinationPolicy(resolver=fake_resolver([address]))
                with self.assertRaises(module.ProxyPolicyError):
                    policy.resolve_and_validate("blocked.example", 443)

    def test_resolves_again_for_each_request_to_prevent_dns_rebinding(self):
        module = proxy_module(self)
        answers = iter([["1.1.1.1"], ["10.0.0.4"]])
        policy = module.DestinationPolicy(resolver=lambda host, port, type=socket.SOCK_STREAM: fake_resolver(next(answers))(host, port, type))
        self.assertEqual(policy.resolve_and_validate("flip.example", 443), ("1.1.1.1",))
        with self.assertRaises(module.ProxyPolicyError):
            policy.resolve_and_validate("flip.example", 443)

    def test_rejects_bad_host_and_unsupported_port(self):
        module = proxy_module(self)
        policy = module.DestinationPolicy(resolver=fake_resolver(["1.1.1.1"]))
        with self.assertRaises(module.ProxyPolicyError):
            policy.resolve_and_validate("127.0.0.1", 443)
        with self.assertRaises(module.ProxyPolicyError):
            policy.resolve_and_validate("public.example", 22)


class FakeReader:
    def __init__(self, header, chunks=()):
        self.header = header
        self.chunks = list(chunks)

    async def readuntil(self, separator):
        return self.header

    async def readexactly(self, length):
        return b"x" * length

    async def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, value):
        self.data.extend(value)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class EgressProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_concurrent_proxy_connections(self):
        module = proxy_module(self)
        entered, release = asyncio.Event(), asyncio.Event()
        class HoldingReader(FakeReader):
            async def readuntil(self, separator):
                entered.set()
                await release.wait()
                return b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n"
        proxy = module.EgressProxy()
        proxy.MAX_CONNECTIONS = 1
        first = asyncio.create_task(proxy.handle_request(HoldingReader(b""), FakeWriter()))
        try:
            await entered.wait()
            rejected = FakeWriter()
            await proxy.handle_request(FakeReader(b""), rejected)
            self.assertIn(b"503 Service Unavailable", rejected.data)
        finally:
            release.set()
            await first
    async def test_simultaneous_burst_never_exceeds_connection_cap(self):
        module = proxy_module(self)
        proxy = module.EgressProxy()
        full, release = asyncio.Event(), asyncio.Event()
        admitted = []

        class HoldingReader(FakeReader):
            async def readuntil(self, separator):
                admitted.append(1)
                if len(admitted) == proxy.MAX_CONNECTIONS:
                    full.set()
                await release.wait()
                return b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n"

        writers = [FakeWriter() for _ in range(proxy.MAX_CONNECTIONS * 3)]
        tasks = [asyncio.create_task(proxy.handle_request(HoldingReader(b""), writer))
                 for writer in writers]
        try:
            await asyncio.wait_for(full.wait(), 1)
            self.assertEqual(proxy._connections, proxy.MAX_CONNECTIONS)
            self.assertEqual(len(admitted), proxy.MAX_CONNECTIONS)
            rejected = [writer for writer in writers if b"503 Service Unavailable" in writer.data]
            self.assertEqual(len(rejected), proxy.MAX_CONNECTIONS * 2)
            self.assertTrue(all(writer.closed for writer in rejected))
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(proxy._connections, 0)

    async def test_cancellation_releases_slot_for_next_request(self):
        module = proxy_module(self)
        proxy = module.EgressProxy()
        proxy.MAX_CONNECTIONS = 1
        entered = asyncio.Event()

        class HoldingReader(FakeReader):
            async def readuntil(self, separator):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(proxy.handle_request(HoldingReader(b""), FakeWriter()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(proxy._connections, 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(proxy._connections, 0)
        writer = FakeWriter()
        await proxy.handle_request(FakeReader(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n"), writer)
        self.assertNotIn(b"503 Service Unavailable", writer.data)
        self.assertIn(b"502 Bad Gateway", writer.data)
        self.assertEqual(proxy._connections, 0)

    async def test_tunnel_retrieves_both_failures(self):
        import gc
        from unittest.mock import AsyncMock
        module=proxy_module(self)
        events=[]; loop=asyncio.get_running_loop(); original=loop.get_exception_handler()
        loop.set_exception_handler(lambda loop,context:events.append(context))
        try:
            with patch.object(module.EgressProxy,'_copy_bounded',new=AsyncMock(side_effect=ConnectionResetError('test'))):
                with self.assertRaises(ConnectionResetError):
                    await module.EgressProxy._tunnel(None,None,None,None)
            gc.collect(); await asyncio.sleep(0)
            self.assertEqual(events,[])
        finally: loop.set_exception_handler(original)

    async def test_dns_saturation_cannot_queue_more_resolver_jobs(self):
        module=proxy_module(self)
        calls=[]
        async def connector(address,port):
            calls.append(address)
            return FakeReader(b""),FakeWriter()
        proxy=module.EgressProxy(module.DestinationPolicy(fake_resolver(["1.1.1.1"])),connector)
        proxy._resolving=8
        writer=FakeWriter()
        await proxy.handle_request(FakeReader(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n"),writer)
        self.assertEqual(calls,[])
        self.assertIn(b"502 Bad Gateway",writer.data)

    async def test_connect_cannot_tunnel_plain_http_or_non_https_ports(self):
        module = proxy_module(self)
        calls = []
        async def connector(address, port):
            calls.append((address, port))
            return FakeReader(b""), FakeWriter()
        proxy = module.EgressProxy(module.DestinationPolicy(fake_resolver(["1.1.1.1"])), connector)
        writer = FakeWriter()
        await proxy.handle_request(FakeReader(b"CONNECT public.example:80 HTTP/1.1\r\n\r\n"), writer)
        self.assertEqual(calls, [])
        self.assertIn(b"502 Bad Gateway", writer.data)
    async def test_refuses_unmanaged_listener(self):
        module = proxy_module(self)
        with patch.dict("os.environ", {"LISTEN_PID": "0", "LISTEN_FDS": "0"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "socket activation is required"):
                await module.serve_systemd_socket()

    async def test_connect_uses_resolved_pinned_address(self):
        module = proxy_module(self)
        connections = []

        async def connector(address, port):
            connections.append((address, port))
            return FakeReader(b""), FakeWriter()

        proxy = module.EgressProxy(module.DestinationPolicy(fake_resolver(["1.1.1.1"])), connector)
        client = FakeReader(b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n")
        writer = FakeWriter()
        await proxy.handle_request(client, writer)
        self.assertEqual(connections, [("1.1.1.1", 443)])
        self.assertIn(b"200 Connection Established", writer.data)

    async def test_private_connect_is_rejected_before_connecting(self):
        module = proxy_module(self)
        calls = []

        async def connector(address, port):
            calls.append((address, port))
            return FakeReader(b""), FakeWriter()

        proxy = module.EgressProxy(module.DestinationPolicy(fake_resolver(["10.0.0.8"])), connector)
        writer = FakeWriter()
        await proxy.handle_request(FakeReader(b"CONNECT private.example:443 HTTP/1.1\r\n\r\n"), writer)
        self.assertEqual(calls, [])
        self.assertIn(b"502 Bad Gateway", writer.data)

    async def test_http_forwarding_uses_origin_form_and_pinned_address(self):
        module = proxy_module(self)
        connections = []
        upstream_writer = FakeWriter()

        async def connector(address, port):
            connections.append((address, port))
            return FakeReader(b"", [b"HTTP/1.1 200 OK\r\n\r\nhello"]), upstream_writer

        proxy = module.EgressProxy(module.DestinationPolicy(fake_resolver(["1.1.1.1"])), connector)
        request = b"GET http://public.example/a?q=1 HTTP/1.1\r\nHost: public.example\r\n\r\n"
        writer = FakeWriter()
        await proxy.handle_request(FakeReader(request), writer)
        self.assertEqual(connections, [("1.1.1.1", 80)])
        self.assertIn(b"GET /a?q=1 HTTP/1.1", upstream_writer.data)
        self.assertIn(b"hello", writer.data)


if __name__ == "__main__":
    unittest.main()
