import unittest
from andrewbot.browser_protocol import validate_action, redact

class BrowserProtocolTests(unittest.TestCase):
    def test_operations(self):
        for action in [dict(action='open', url='https://example.com/'), dict(action='snapshot'),
                       dict(action='click', selector='#go'), dict(action='fill', selector='#q', value='test'),
                       dict(action='tabs'), dict(action='new_tab', url='https://example.com/'),
                       dict(action='switch_tab', tab=0), dict(action='close_tab'),
                       dict(action='select', selector='#option', value='a'), dict(action='key', key='Enter'),
                       dict(action='scroll', pixels=600), dict(action='screenshot'), dict(action='reset')]:
            self.assertEqual(validate_action(action), action)
    def test_rejects_arbitrary_execution_and_secrets(self):
        for action in [dict(action='evaluate', code='1'), dict(action='open',url='http://localhost/'),
                       dict(action='open',url='https://u:p@example.com/'), dict(action='click',selector='x',extra=1),
                       dict(action='switch_tab', tab=True), dict(action='scroll',pixels=100000),
                       dict(action='fill', selector='x',value='x'*65536)]:
            with self.assertRaises(ValueError): validate_action(action)
    def test_redacts_nested_output(self):
        self.assertEqual(redact({'text':['pass-123','hostile pass-123']}, ['pass-123']),
                         {'text':['[REDACTED]','hostile [REDACTED]']})

class BrowserVisibilityTests(unittest.TestCase):
    def test_enabled_tool_lists_match(self):
        from andrewbot.appserver import AppServerClient
        client=AppServerClient.__new__(AppServerClient)
        client.external_tools_enabled=True
        self.assertIn('browser.control',client.tool_names)
        self.assertIn('browser.account',client.tool_names)
        self.assertIn('shell.run',client.tool_names)
        client.external_tools_enabled=False
        self.assertEqual(client.tool_names,AppServerClient.SAFE_CONTROLLER_TOOL_NAMES)
