import tempfile
import unittest
from andrewbot.account_broker import AccountBroker
from andrewbot.vault import Vault

class FakeBrowser:
    def __init__(self): self.requests=[]
    def run(self,request):
        self.requests.append(request)
        return {'status':'ok','result':{'submitted':True,'text':request.get('secret','')}}

class AccountBrokerTests(unittest.TestCase):
    def test_signup_persist_login_revoke(self):
        with tempfile.TemporaryDirectory() as root:
            vault=Vault(root); browser=FakeBrowser(); broker=AccountBroker(vault,browser)
            result=broker.execute({'operation':'create','url':'https://example.com/signup','username':'andrew'})
            account=result['account_id']; secret=vault.get_account_secret(account,'https://example.com')
            self.assertNotIn(secret,str(result)); vault.close()
            vault=Vault(root); broker=AccountBroker(vault,browser)
            args={'operation':'submit','url':'https://example.com/login','account_id':account,
                  'username_selector':'#u','password_selector':'#p','submit_selector':'#go'}
            result=broker.execute(args)
            self.assertTrue(result['result']['submitted']); self.assertNotIn(secret,str(result))
            with self.assertRaises(ValueError): broker.execute({**args,'url':'https://other.example/login'})
            broker.execute({'operation':'revoke','account_id':account})
            with self.assertRaises(ValueError): broker.execute(args)
            vault.close()
    def test_unknown_fields_no_password_argument(self):
        with tempfile.TemporaryDirectory() as root:
            vault=Vault(root); broker=AccountBroker(vault,FakeBrowser())
            with self.assertRaises(ValueError): broker.execute({'operation':'create','url':'https://example.com','username':'a','password':'p'})
            vault.close()
