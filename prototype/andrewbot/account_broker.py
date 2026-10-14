"""Owner-authorized public-site accounts; exact-origin secrets, no approval prompts."""
import secrets
from .browser_protocol import origin, redact

class AccountBroker:
    def __init__(self,vault,browser):
        self.vault = vault
        self.browser = browser

    def execute(self,args):
        if not isinstance(args,dict): raise ValueError('invalid account action')
        op = args.get('operation')
        fields = {'create':{'operation','url','username'},'list':{'operation'},
                  'submit':{'operation','url','account_id','username_selector','password_selector','submit_selector'},
                  'revoke':{'operation','account_id'},'delete':{'operation','account_id'}}
        if op == 'submit' and 'submit_url' in args: fields[op] = fields[op] | {'submit_url'}
        if op not in fields or set(args) != fields[op]: raise ValueError('invalid account fields')
        if any(not isinstance(v,str) or not v or len(v)>2048 for v in args.values()):
            raise ValueError('invalid account argument')
        if op == 'create':
            account = self.vault.save_account(origin(args['url']),args['username'],secrets.token_urlsafe(32))
            return {'account_id':account,'origin':origin(args['url']),'stored':True,
                    'note':'Credential stored. Use submit on a signup or login form; site acceptance must be checked separately.'}
        if op == 'list': return {'accounts':self.vault.account_metadata()}
        if op in {'revoke','delete'}:
            # Reset first: failure must not leave a revoked authenticated session.
            response = self.browser.run({'action':'reset'})
            if response.get('status') != 'ok': raise ValueError('browser reset required')
            changed = getattr(self.vault,op+'_account')(args['account_id'])
            return {'changed':changed}
        try:
            expected = origin(args['url'])
            if 'submit_url' in args and origin(args['submit_url']) != expected:
                raise ValueError('credential endpoint origin changed')
            secret = self.vault.get_account_secret(args['account_id'],expected)
            record = next(r for r in self.vault.account_metadata() if r['account_id']==args['account_id'])
        except (KeyError,PermissionError,StopIteration):
            raise ValueError('account unavailable for this exact origin') from None
        response = self.browser.run({'action':'account_submit','url':args['url'],
            'username':record['username'],'secret':secret,
            **({'submit_url':args['submit_url']} if 'submit_url' in args else {}),
            **{k:args[k] for k in ('username_selector','password_selector','submit_selector')}})
        return redact(response,[secret,record['username']])

class StoredAccountBroker:
    def __init__(self,directory,browser):
        self.directory,self.browser = directory,browser
    def execute(self,args):
        from .vault import Vault
        vault = Vault(self.directory)
        try: return AccountBroker(vault,self.browser).execute(args)
        finally: vault.close()
