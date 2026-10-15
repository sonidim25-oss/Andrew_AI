#!/usr/bin/env python3
"""Disposable real-Chromium component proof; runs INSIDE --network=none container.
Self-signed localhost fixture and certificate exception exist only in this harness.
No third-party accounts, production credentials or network policy exceptions.
"""
import hashlib
import http.server
import json
import os
import secrets
import ssl
import socket
import socketserver
import select
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs
sys.path.insert(0,'/opt/andrew')
from browser_entry import BrowserEngine
from worker_entry import UnixProxyBridge
from browser_protocol import redact
from playwright.sync_api import sync_playwright

JSON_PAGE = b'''<html><body><input id="user" type="email"><input id="pass" type="password"><button id="submit" onclick="fetch('/json-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:document.querySelector('#user').value,pass:document.querySelector('#pass').value})}).then(r=>r.json()).then(v=>document.body.innerText=JSON.stringify(v))">Login</button></body></html>'''

PAGE = b'''<html><head><title>Andrew controlled fixture</title></head><body>
<form method="POST" action="/signup"><input id="user" name="user"><input id="pass" name="pass" type="password">
<button id="submit">Create account</button></form>
<input id="query"><select id="select"><option value="a">A</option><option value="b">B</option></select>
<input id="file" type="file"><a id="download" href="/download" download="fixture.txt">Download</a>
<script>document.addEventListener('input', e=>fetch('/seen',{method:'POST',body:e.target.value}));</script>
</body></html>'''

class Fixture(http.server.BaseHTTPRequestHandler):
    accounts={}; seen=[]; stolen=[]; evil_cookie=False; encoded_echo=False; encoded_json=False; session_token=secrets.token_urlsafe(24)
    def log_message(self,*a): pass
    def send(self,body,kind='text/html',cookie=None):
        self.send_response(200); self.send_header('Content-Type',kind)
        if cookie is not None: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path=='/logout': return self.send(b'LOGGED OUT',cookie='andrew-session=; Max-Age=0; Secure; HttpOnly; SameSite=Strict')
        if self.path=='/private':
            ok=('andrew-session='+self.session_token) in self.headers.get('Cookie','')
            return self.send(b'ACCOUNT SESSION VERIFIED' if ok else b'SESSION MISSING')
        if self.path=='/json': return self.send(JSON_PAGE)
        if self.path=='/download': return self.send(b'fixture-download','application/octet-stream')
        page=PAGE.replace(b'/signup',b'/login') if self.path=='/login' else PAGE
        self.send(page)
    def do_POST(self):
        body=self.rfile.read(min(int(self.headers.get('Content-Length',0)),65536))
        if self.path=='/seen': self.seen.append(body.decode()); return self.send(b'ok')
        if self.path=='/redirect-local':
            self.send_response(302); self.send_header('Location','/private'); self.send_header('Set-Cookie','andrew-session='+self.session_token+'; Secure; HttpOnly; SameSite=Strict'); self.send_header('Content-Length','0'); self.end_headers(); return
        if self.path=='/redirect':
            self.send_response(307); self.send_header('Location','https://other.example.org/steal'); self.send_header('Content-Length','0'); self.end_headers(); return
        if self.path=='/steal': self.stolen.append(body.decode()); return self.send(b'UNEXPECTED REDIRECT')
        if self.path=='/json-login':
            values=json.loads(body); user=values.get('user',''); password=values.get('pass','')
        else:
            values=parse_qs(body.decode()); user=values.get('user',[''])[0]; password=values.get('pass',[''])[0]
        digest=hashlib.sha256(password.encode()).hexdigest()
        if self.path=='/signup': self.accounts[user]=digest
        ok=self.accounts.get(user)==digest
        # Deliberately hostile echo: it must be redacted from the tool output.
        text=('ACCEPTED '+(''.join('&#'+str(ord(c))+';' for c in password) if self.encoded_echo else password)) if ok else 'REJECTED'
        if self.encoded_json: text='{"echo":"'+''.join('\\u%04x'%ord(c) for c in password)+'"}'
        cookie=('leaked='+password+'; Domain=example.org; Secure; Path=/') if self.evil_cookie else ('andrew-session='+self.session_token+'; Secure; HttpOnly; SameSite=Strict' if ok else None)
        self.send(text.encode(),cookie=cookie)


class FixtureProxy(socketserver.BaseRequestHandler):
    connections=0
    def handle(self):
        raw=b''
        while b'\r\n\r\n' not in raw and len(raw)<4096:
            chunk=self.request.recv(1)
            if not chunk: return
            raw+=chunk
        target=raw.split(b' ',2)[1]
        if target not in (b'fixture.example.org:443',b'other.example.org:443'): return
        FixtureProxy.connections+=1
        upstream=socket.create_connection(('127.0.0.1',443),timeout=5)
        try:
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            while True:
                readers,_,_=select.select([self.request,upstream],[],[],10)
                if not readers: return
                for source in readers:
                    data=source.recv(65536)
                    if not data: return
                    (upstream if source is self.request else self.request).sendall(data)
        except OSError: pass
        finally: upstream.close()

class ProxyServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True


def main():
    with tempfile.TemporaryDirectory() as root:
        key,cert=root+'/key.pem',root+'/cert.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-subj','/CN=fixture.example.org',
                        '-keyout',key,'-out',cert,'-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',443),Fixture)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert,key)
        server.socket=tls.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        proxy_server=ProxyServer(root+'/proxy.sock',FixtureProxy)
        threading.Thread(target=proxy_server.serve_forever,daemon=True).start()
        bridge=UnixProxyBridge(root+'/proxy.sock'); proxy_url=bridge.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,proxy={'server':proxy_url,'bypass':'<-loopback>'},
                    args=['--no-sandbox','--ignore-certificate-errors','--host-resolver-rules=MAP fixture.example.org 127.0.0.1,MAP other.example.org 127.0.0.1'])
                engine=BrowserEngine(browser,request_factory=p.request.new_context,context_options={'ignore_https_errors':True,'proxy':{'server':proxy_url,'bypass':'<-loopback>'}})
                url='https://fixture.example.org'
                def action(**request): return engine.execute(request)
                assert action(action='open',url=url)['title']=='Andrew controlled fixture'
                action(action='fill',selector='#query',value='hello')
                action(action='key',key='Tab'); action(action='select',selector='#select',value='b')
                action(action='scroll',pixels=400)
                assert action(action='screenshot')['artifact']['data']
                action(action='upload',selector='#file',name='test.txt',data='dGVzdA==')
                assert action(action='download',selector='#download')['artifact']['data']=='Zml4dHVyZS1kb3dubG9hZA=='
                action(action='new_tab',url=url)
                assert len(action(action='tabs')['tabs'])==2
                action(action='switch_tab',tab=0); action(action='close_tab')
                secret=secrets.token_urlsafe(32)
                request=dict(action='account_submit',url=url,username='andrew-test',secret=secret,
                    username_selector='#user',password_selector='#pass',submit_selector='#submit')
                result=redact(action(**request),engine.secrets)
                assert result['submitted'] and result['http_status']==200
                assert secret not in json.dumps(result)
                assert secret not in ''.join(Fixture.seen)
                try: action(action='screenshot')
                except ValueError: pass
                else: raise AssertionError('credential screenshot leaked')
                assert 'ACCOUNT SESSION VERIFIED' in action(action='open',url=url+'/private')['text']
                action(action='open',url=url+'/logout')
                assert 'SESSION MISSING' in action(action='open',url=url+'/private')['text']
                engine.context.close()
                engine=BrowserEngine(browser,request_factory=p.request.new_context,context_options={'ignore_https_errors':True,'proxy':{'server':proxy_url,'bypass':'<-loopback>'}})  # Fresh context simulates browser restart.
                action(action='open',url=url+'/login')
                result=redact(action(**request),engine.secrets)
                assert result['submitted'] and result['http_status']==200
                assert secret not in json.dumps(result)
                assert 'ACCOUNT SESSION VERIFIED' in action(action='open',url=url+'/private')['text']
                assert 'SESSION MISSING' in action(action='new_tab',url='https://other.example.org/private')['text']
                action(action='switch_tab',tab=0)
                assert 'ACCOUNT SESSION VERIFIED' in action(action='open',url=url+'/private')['text']
                action(action='reset'); action(action='open',url=url)
                engine.page.locator('form').evaluate("f=>f.action='https://other.example.org/signup'")
                try: action(**request)
                except ValueError: pass
                else: raise AssertionError('cross-origin credential submission allowed')
                action(action='reset'); action(action='open',url=url+'/json')
                result=redact(action(**{**request,'submit_url':url+'/json-login'}),engine.secrets)
                assert result['submitted'] and result['http_status']==200
                assert secret not in json.dumps(result)
                assert 'ACCOUNT SESSION VERIFIED' in action(action='open',url=url+'/private')['text']
                action(action='reset')
                assert 'SESSION MISSING' in action(action='open',url=url+'/private')['text']
                action(action='open',url=url)
                engine.page.locator('body').evaluate("e=>e.insertAdjacentHTML('beforeend','<div data-sitekey=challenge></div>')")
                try: action(**request)
                except ValueError: pass
                else: raise AssertionError('human-verification gate bypassed')
                action(action='reset'); action(action='open',url=url)
                engine.page.locator('form').evaluate("f=>f.action='/redirect'")
                try: action(**request)
                except ValueError: pass
                assert secret not in ''.join(Fixture.stolen),'307 redirect replayed private password'
                action(action='reset'); action(action='open',url=url)
                engine.page.locator('form').evaluate("f=>f.action='/redirect-local'")
                result=action(**request)
                assert result['submitted'] and 'ACCOUNT SESSION VERIFIED' in result['text']
                action(action='reset'); action(action='open',url=url)
                Fixture.evil_cookie=True
                try: action(**request)
                except ValueError: pass
                assert not any(secret in c['value'] for c in engine.context.cookies()),'rejected cookie retained password'
                Fixture.evil_cookie=False
                action(action='reset'); action(action='open',url=url)
                Fixture.encoded_echo=True
                action(**request)
                assert secret not in engine.page.locator('body').inner_text(),'HTML entities exposed password to renderer'
                Fixture.encoded_echo=False
                action(action='reset'); action(action='open',url=url+'/json')
                Fixture.encoded_json=True
                result=action(**{**request,'submit_url':url+'/json-login'})
                assert result['submitted'] and secret not in engine.page.locator('body').inner_text()
                assert 'ACCOUNT SESSION VERIFIED' in action(action='open',url=url+'/private')['text']
                Fixture.encoded_json=False
                browser.close()
            assert FixtureProxy.connections>0
            print(json.dumps({'browser_operations':'passed','account_signup_login_fresh_context':'passed',
                'javascript_json_login':'passed','dom_password_isolation':'passed','hostile_echo_redaction':'passed',
                'cookie_login_logout_reset':'passed','human_verification':'blocked','same_origin_302_login':'passed','rejected_cookie_isolation':'passed','encoded_response_isolation':'passed','credential_307_replay':'blocked','cross_origin_submission':'blocked','external_network':'not used (component fixture only)'}))
        finally:
            bridge.close(); proxy_server.shutdown(); proxy_server.server_close(); server.shutdown(); server.server_close()

if __name__=='__main__': main()
