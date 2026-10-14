import importlib.util
from pathlib import Path
import unittest
P=Path(__file__).resolve().parents[1]/'build-runtime-policy.py'
spec=importlib.util.spec_from_file_location('runtime_policy',P); policy=importlib.util.module_from_spec(spec); spec.loader.exec_module(policy)
class RuntimePolicyTests(unittest.TestCase):
    def test_scoped_uid_policy_and_connected_public_networks(self):
        text,settings=policy.generate(801,802,803,[{'addr_info':[{'local':'8.8.8.10','prefixlen':24}]}],['127.0.0.53'])
        self.assertEqual(settings['blocked_networks'],['8.8.8.0/24'])
        self.assertIn('8.8.8.0/24',text); self.assertIn('meta skuid { 801, 802 }',text)
        self.assertIn('meta skuid 803 ip daddr 127.0.0.53 udp dport 53 accept',text)
        self.assertNotIn('flush ruleset',text); self.assertNotIn('input {',text)
    def test_never_targets_root_uid(self):
        with self.assertRaises(ValueError): policy.generate(0,802,803,[],[])
    def test_unique_uids_and_dns_required(self):
        for values in ((801,801,803,['127.0.0.53']),(801,802,803,[])):
            with self.assertRaises(ValueError): policy.generate(*values[:3],[],values[3])
