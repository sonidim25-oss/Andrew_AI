import tempfile
import unittest
from pathlib import Path
from andrewbot.browser_tools import BrowserTools

class FakeBrowser:
    def __init__(self): self.requests=[]
    def run(self,request):
        self.requests.append(request)
        return {'status':'ok','result':{'url':'https://example.com/','title':'fixture','text':'untrusted'}}

class BrowserToolsTests(unittest.TestCase):
    def test_history_before_navigation_and_after_response(self):
        with tempfile.TemporaryDirectory() as root:
            browser=FakeBrowser(); events=[]
            tools=BrowserTools(browser,lambda a:None,Path(root),Path(root)/'artifacts',lambda u,t:events.append((u,t)))
            text=tools.execute('browser.control',{'action':'open','url':'https://example.com/'})
            self.assertIn('untrusted',text); self.assertEqual(len(events),2)
            self.assertEqual(browser.requests[0]['action'],'open')
    def test_reject_upload_symlink_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/'link').symlink_to('/etc/passwd'); browser=FakeBrowser()
            tools=BrowserTools(browser,None,p,p/'artifacts',lambda *a:None)
            for path in ('link','../passwd','/etc/passwd'):
                with self.assertRaises(ValueError):
                    tools.execute('browser.control',{'action':'upload','selector':'input','path':path})
            self.assertFalse(browser.requests)
