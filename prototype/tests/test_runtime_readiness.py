import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from andrewbot.runtime_readiness import code_digest, check_proof

class ReadinessTests(unittest.TestCase):
    def test_digest_detects_modified_module(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/'a.py').write_text('one')
            before=code_digest(p); (p/'a.py').write_text('two')
            self.assertNotEqual(before,code_digest(p))
    def test_proof_exact_bindings(self):
        expected={'code':'abc','image':'sha256:123','policy':'def'}
        proof={**expected,'schema':1,'checks':{k:'passed' for k in ('network','limits','browser','accounts','letta')}}
        check_proof(proof,expected)
        for key in expected:
            with self.assertRaises(ValueError): check_proof({**proof,key:'stale'},expected)
        with self.assertRaises(ValueError): check_proof({**proof,'checks':{'network':'passed'}},expected)
    def test_runtime_and_autonomy_require_proof(self):
        from andrewbot.lifecycle_policy import load_lifecycle_policy
        for autonomous in ('0','1'):
            env={'ANDREW_AUTONOMOUS_ENABLED':autonomous,'ANDREW_ISOLATED_RUNTIME_ENABLED':'1'}
            with patch('andrewbot.lifecycle_policy.validate_runtime_proof',side_effect=ValueError('missing')):
                with self.assertRaises(ValueError): load_lifecycle_policy(env)
            with patch('andrewbot.lifecycle_policy.validate_runtime_proof'):
                self.assertEqual(load_lifecycle_policy(env).autonomous_enabled,autonomous=='1')
