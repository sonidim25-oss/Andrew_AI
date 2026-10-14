import unittest
from datetime import datetime, timezone

from andrewbot.site_policy import SitePolicy, canonical_origin


class SitePolicyTests(unittest.TestCase):
    def test_canonicalizes_https_host_and_strips_path_query_fragment(self):
        self.assertEqual(canonical_origin("HTTPS://Exämple.com:443/path?q=1#x"), "https://xn--exmple-cua.com")

    def test_rejects_userinfo_ip_literals_non_https_and_unapproved_ports(self):
        for value in ("http://example.com", "https://user@example.com", "https://127.0.0.1", "https://example.com:8443"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_origin(value)

    def test_exact_origin_and_action_grants(self):
        policy = SitePolicy()
        policy.approve("https://example.com/path", {"read", "login"}, datetime.now(timezone.utc))
        self.assertTrue(policy.permits("https://example.com", "login"))
        self.assertFalse(policy.permits("https://sub.example.com", "login"))
        self.assertFalse(policy.permits("https://example.com", "signup"))


if __name__ == "__main__":
    unittest.main()
