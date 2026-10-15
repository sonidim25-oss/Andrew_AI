"""Trusted, ephemeral browser process. Only private controller RPC reaches stdin."""
import base64
from http.cookies import SimpleCookie
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin
from browser_protocol import MAX_WIRE, MAX_FILE, origin, safe_url, validate_action, redact
from worker_entry import UnixProxyBridge

class BrowserEngine:
    def __init__(self, browser, request_factory, context_options=None):
        self.browser = browser
        self.request_factory = request_factory
        self.context_options = context_options or {}
        self.secrets = []
        self.pending = None
        self.authenticated = False
        self.reset()

    def reset(self):
        if hasattr(self,'context'): self.context.close()
        self.context = self.browser.new_context(service_workers='block', accept_downloads=True, **self.context_options)
        self.context.set_default_timeout(10000)
        self.context.route('**/*', self.route)
        self.context.route_web_socket('**/*', lambda route: route.close())
        self.page = self.context.new_page()
        self.secrets = []
        self.passwords = []
        self.pending = None
        self.authenticated = False
        self.redirect_url = None
        self.credential_blocked = False

    def route(self, route):
        request = route.request
        try:
            request_origin = origin(request.url)
            # Reject credential replay, including redirect bodies. Ordinary
            # public subresources/tabs use the browser's origin-bound cookie jar.
            body = request.post_data or ''
            headers = request.all_headers()
            if any(redact(v,self.passwords) != v for v in [request.url,body,*headers.values()]):
                return route.abort()
            pending = self.pending
            if pending and request.url == pending['target'] and request.method == 'POST':
                if request_origin != pending['origin']: return route.abort()
                content_type = request.headers.get('content-type','').split(';')[0]
                if len(body)>65536: return route.abort()
                counts = {token:0 for token in pending['tokens']}
                def replace(value):
                    if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
                    if isinstance(value,list): return [replace(v) for v in value]
                    if isinstance(value,str) and value in pending['tokens']:
                        counts[value] += 1
                        return pending['tokens'][value]
                    return value
                if content_type == 'application/x-www-form-urlencoded':
                    fields = [(k,replace(v)) for k,v in parse_qsl(body,keep_blank_values=True)]
                    outgoing = urlencode(fields)
                elif content_type == 'application/json':
                    outgoing = json.dumps(replace(json.loads(body)),separators=(',',':'))
                else: return route.abort()
                if any(n != 1 for n in counts.values()): return route.abort()
                self.pending = None
                self.authenticated = True
                self.account_origin = request_origin
                # Browser never receives actual credentials in its DOM.
                # Use a fresh cookie jar: rejected Set-Cookie values must never
                # reach the renderer's shared jar, even briefly.
                options = {k:v for k,v in self.context_options.items() if k in {'proxy','ignore_https_errors'}}
                private = self.request_factory(**options)
                try:
                    outgoing_headers = {k:v for k,v in headers.items() if k.lower() not in {'host','content-length'}}
                    response = private.fetch(request.url,method='POST',data=outgoing,headers=outgoing_headers,
                                             max_redirects=0,timeout=15000)
                    response_headers = dict(response.headers)
                    if any(redact(v,self.passwords) != v for v in response_headers.values()):
                        self.credential_blocked = True
                        return route.fulfill(status=409,content_type='text/plain',body='Credential response blocked')
                    # Cookies are installed only after all validation, scoped to
                    # this exact HTTPS host and hidden from page JavaScript.
                    cookies=[]
                    for header in response.headers_array:
                        if header['name'].lower() != 'set-cookie': continue
                        parsed=SimpleCookie(); parsed.load(header['value'])
                        if not parsed: raise ValueError('unsupported cookie')
                        for name,morsel in parsed.items():
                            path=morsel['path'] or '/'
                            if not path.startswith('/') or len(path)>2048: raise ValueError('invalid cookie path')
                            if morsel['max-age'] == '0':
                                self.context.clear_cookies(name=name,domain=request_origin.removeprefix('https://'),path=path)
                            else:
                                cookies.append({'name':name,'value':morsel.value,'url':request_origin+path,
                                                'httpOnly':True,'secure':True,'sameSite':'Lax'})
                    if 300 <= response.status < 400:
                        target=urljoin(request.url,response_headers.get('location',''))
                        if response.status in {301,302,303} and origin(target)==pending['origin']:
                            self.redirect_url=target
                        else: self.credential_blocked=True
                    if not self.credential_blocked: self.context.add_cookies(cookies)
                    self.credential_status=response.status
                    # No remote credential response body or scripts enter the
                    # page. This also blocks HTML entities/JSON escape echoes.
                    safe=json.dumps({'credential_request_submitted':True,'http_status':response.status,
                                     'verification_required':True})
                    return route.fulfill(status=200,content_type='application/json',body=safe)
                finally: private.dispose()
            return route.continue_()
        except Exception:
            self.credential_blocked = True
            return route.abort()

    def snapshot(self):
        return {'url':safe_url(self.page.url), 'title':self.page.title()[:512],
                'text':self.page.locator('body').inner_text()[:12000],
                'elements': self.page.locator('input,button,select,a,textarea').evaluate_all(
                    "els => els.slice(0,100).map(e => ({tag:e.tagName,id:e.id,name:e.name || '',type:e.type || '',text:(e.innerText || e.getAttribute('aria-label') || '').slice(0,160)}))")}

    def execute(self, request):
        validate_action(request, private=True)
        a = request['action']
        if a == 'reset': self.reset(); return {'reset':True}
        if a in {'open','new_tab'}:
            if a == 'new_tab':
                if len(self.context.pages) >= 8: raise ValueError('tab limit')
                self.page = self.context.new_page()
            self.page.goto(request['url'],wait_until='domcontentloaded',timeout=30000)
        elif a == 'tabs':
            return {'tabs':[{'tab':i,'url':safe_url(p.url)} for i,p in enumerate(self.context.pages)]}
        elif a == 'switch_tab': self.page = self.context.pages[request['tab']]
        elif a == 'close_tab':
            self.page.close()
            self.page = next(iter(self.context.pages), None) or self.context.new_page()
        elif a == 'click': self.page.locator(request['selector']).click()
        elif a == 'fill':
            locator = self.page.locator(request['selector'])
            if locator.get_attribute('type') == 'password':
                raise ValueError('use account broker for passwords')
            locator.fill(request['value'])
        elif a == 'select': self.page.locator(request['selector']).select_option(request['value'])
        elif a == 'key': self.page.keyboard.press(request['key'])
        elif a == 'scroll': self.page.mouse.wheel(0,request['pixels'])
        elif a == 'screenshot':
            if self.secrets: raise ValueError('screenshots unavailable in credential-bearing context; reset first')
            return self.artifact(self.page.screenshot(type='png',full_page=False),'screenshot.png')
        elif a == 'download':
            with self.page.expect_download() as info: self.page.locator(request['selector']).click()
            download = info.value
            try:
                path = Path(download.path())
                if path.stat().st_size > MAX_FILE: raise ValueError('download too large')
                return self.artifact(path.read_bytes(), 'download.bin')
            finally: download.delete()
        elif a == 'upload':
            data = base64.b64decode(request['data'],validate=True)
            if len(data) > MAX_FILE: raise ValueError('upload too large')
            self.page.locator(request['selector']).set_input_files({'name':request['name'],'mimeType':'application/octet-stream','buffer':data})
        elif a == 'account_submit': return self.account_submit(request)
        return self.snapshot()

    @staticmethod
    def artifact(data, name):
        if len(data) > MAX_FILE: raise ValueError('artifact too large')
        return {'artifact':{'name':name,'data':base64.b64encode(data).decode()}}

    def account_submit(self, request):
        expected = origin(request['url'])
        if origin(self.page.url) != expected: raise ValueError('account origin changed')
        # Password form metadata is inspected before any private data is used.
        password = self.page.locator(request['password_selector'])
        form = password.evaluate("e => { const f=e.form; return f ? {method:f.method,action:f.action} : null }")
        target = request.get('submit_url')
        if target is None:
            if not form or form['method'].lower() != 'post':
                raise ValueError('POST form or explicit exact-origin POST endpoint required')
            target = form['action']
        if origin(target) != expected: raise ValueError('exact-origin POST required')
        if self.page.locator('[data-sitekey],iframe[src*="captcha"],input[name*="otp"],input[autocomplete="one-time-code"]').count():
            raise ValueError('human verification required')
        tokens = {secrets.token_urlsafe(20)+'@example.invalid':request['username'],secrets.token_urlsafe(24):request['secret']}
        self.secrets.extend([request['username'],request['secret']])
        self.passwords.append(request['secret'])
        self.pending = {'target':target,'origin':expected,'tokens':tokens,'password':request['secret']}
        self.redirect_url = None
        self.credential_blocked = False
        try:
            username_token, password_token = tokens
            self.page.locator(request['username_selector']).fill(username_token)
            password.fill(password_token)
            with self.page.expect_response(lambda response: response.url == target and response.request.method == 'POST',timeout=15000):
                self.page.locator(request['submit_selector']).click()
            self.page.wait_for_timeout(150)
            if self.pending is not None: raise ValueError('supported credential POST was not submitted')
            if self.credential_blocked: raise ValueError('credential redirect or response blocked')
            if self.redirect_url:
                self.page.goto(self.redirect_url,wait_until='domcontentloaded',timeout=15000)
            if origin(self.page.url) != expected: raise ValueError('account redirect rejected')
            return {'submitted':True,'http_status':self.credential_status,'verification_required':True, **self.snapshot()}
        finally: self.pending = None


def main():
    from playwright.sync_api import sync_playwright
    bridge = UnixProxyBridge('/run/andrew-egress.sock')
    proxy = bridge.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',
                proxy={'server':proxy,'bypass':'<-loopback>'},
                args=['--no-sandbox','--disable-crash-reporter','--disable-breakpad','--disable-quic',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            engine = BrowserEngine(browser,request_factory=p.request.new_context,context_options={'proxy':{'server':proxy,'bypass':'<-loopback>'}})
            while True:
                raw = sys.stdin.buffer.readline(MAX_WIRE + 1)
                if not raw: break
                if len(raw) > MAX_WIRE or not raw.endswith(b'\n'): break
                try:
                    result = {'status':'ok','result':engine.execute(json.loads(raw))}
                except Exception:
                    result = {'status':'error','error':'browser_action_blocked_or_failed'}
                result = redact(result,engine.secrets)
                encoded = json.dumps(result,separators=(',',':')).encode()
                if len(encoded) > MAX_WIRE: encoded = b'{"status":"error","error":"browser_output_limit"}'
                sys.stdout.buffer.write(encoded+b'\n'); sys.stdout.buffer.flush()
            browser.close()
    finally: bridge.close()

if __name__ == '__main__': main()
