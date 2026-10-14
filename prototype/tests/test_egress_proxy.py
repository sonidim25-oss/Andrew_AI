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
