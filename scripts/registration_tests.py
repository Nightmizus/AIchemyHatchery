"""Registration bridge over real HTTP and isolated PostgreSQL; no real emails."""
from contextlib import ExitStack
import functools
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bcrypt
import groovin_registration as registration
import server
from local_postgres import LocalPostgres

PAYLOAD = {'campusId':'fixture-campus','name':'公开昵称','realName':'测试姓名','email':'student@example.test',
           'grade':'高二','classGroup':'24班','password':'fixture-password-123','code':'123456'}


class RegistrationFixture:
    def __enter__(self):
        self.stack=ExitStack();self.calls=[];self.override=None;self.otp_entered=None;self.otp_release=None
        self.pg=self.stack.enter_context(LocalPostgres())
        with self.pg.db() as conn,conn.cursor() as cur:
            cur.execute('''CREATE TABLE "User"(id TEXT PRIMARY KEY,name TEXT,email TEXT,"campusId" TEXT,password TEXT,
                "realName" TEXT,grade TEXT,"classGroup" TEXT,"isAdmin" BOOLEAN DEFAULT false,
                "bannedUntil" TEXT,"createdAt" TEXT,"lastLoginAt" TEXT);
                CREATE TABLE console_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT,created_at TEXT,
                expires_at TEXT,last_seen_at TEXT,ip_address TEXT,user_agent TEXT);
                CREATE TABLE hatchery_user_extras(user_id TEXT,preview_id TEXT);
                CREATE TABLE hatchery_published_sites(user_id TEXT,slug TEXT,listed_in_gallery BOOLEAN);''')
        fixture=self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls.append({'path':self.path,'body':body,'headers':dict(self.headers)})
                status,value,headers=200,{'ok':True},{}
                if fixture.override:
                    status,value,headers=fixture.override
                elif self.path.endswith('/campus'):
                    if body['campusId']=='unknown':status,value=404,{'error':'该数字校园号未开放注册'}
                elif self.path.endswith('/send-otp'):
                    if fixture.otp_entered:fixture.otp_entered.set()
                    if fixture.otp_release:fixture.otp_release.wait(10)
                    value={'ok':True,'devCode':'123456','user':{'private':'not public'}}
                elif self.path.endswith('/register'):
                    if body['code']!='123456':status,value=400,{'error':'验证码无效或已过期'}
                    else:
                        digest=bcrypt.hashpw(body['password'].encode()[:72],bcrypt.gensalt(rounds=4)).decode()
                        with fixture.pg.db() as conn,conn.cursor() as cur:
                            cur.execute('''INSERT INTO "User"(id,name,email,"campusId",password,"realName",grade,"classGroup")
                                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                                ('fixture-user',body['name'],body['email'],body['campusId'],digest,body['realName'],body['grade'],body['classGroup']))
                raw=json.dumps(value).encode() if isinstance(value,dict) else value
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.send_header('Set-Cookie','upstream-private=fixture')
                for name,value in headers.items():self.send_header(name,value)
                self.end_headers();self.wfile.write(raw)
        self.upstream=self.start(Upstream)
        self.stack.enter_context(patch.object(registration.http.client,'HTTPSConnection',http.client.HTTPConnection))
        self.stack.enter_context(patch.object(server,'SDSZ_BASE_URL',f'https://127.0.0.1:{self.upstream.server_port}'))
        self.stack.enter_context(patch.object(server,'neon_db',self.pg.db))
        self.stack.enter_context(patch.object(server,'user_column_name',lambda name:name))
        self.stack.enter_context(patch.object(server,'SECURE_COOKIES',False))
        self.stack.enter_context(patch.object(server,'rate_limit_hit',return_value=False))
        self.stack.enter_context(patch.object(server,'LOGIN_ATTEMPTS',{}))
        server.invalidate_console_sessions()
        class Handler(server.AIchemyHatcheryHandler):
            def log_message(self,*args):pass
            def proxy_site_backend(self):return False
            def _do_GET(self):
                if self.path=='/fixture':
                    source=(server.ROOT/'frontend/index.html').read_text().split('  <main')[0]
                    source+='<script src="/auth.js"></script><script src="/registration.js"></script></body></html>'
                    body=source.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
                super()._do_GET()
        self.http=self.start(functools.partial(Handler,directory=str(server.ROOT)))
        self.base=f'http://127.0.0.1:{self.http.server_port}'
        return self

    def start(self,handler):
        http=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.stack.callback(http.server_close);self.stack.callback(http.shutdown)
        return http

    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=5)
        try:
            conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,
                         {'Content-Type':'application/json','Origin':self.base,**(headers or {})})
            response=conn.getresponse();raw=response.read()
            value=json.loads(raw) if response.getheader('Content-Type','').startswith('application/json') else raw
            return response.status,value,dict(response.getheaders())
        finally:conn.close()

    def __exit__(self,*args):
        if self.otp_release:self.otp_release.set()
        self.stack.close()


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.f=RegistrationFixture().__enter__()
    @classmethod
    def tearDownClass(cls):cls.f.__exit__()
    def setUp(self):
        self.f.calls.clear();self.f.override=None
        server.invalidate_console_sessions()
        with self.f.pg.db() as conn,conn.cursor() as cur:cur.execute('TRUNCATE "User",console_sessions')

    def test_full_contract_and_shared_account_login(self):
        f=self.f
        self.assertEqual(f.request('/api/auth/campus',{'campusId':PAYLOAD['campusId']})[:2],(200,{'ok':True}))
        status,value,headers=f.request('/api/auth/send-otp',PAYLOAD,{'Cookie':'console-secret=private','Authorization':'Bearer private'})
        self.assertEqual((status,value),(200,{'ok':True}))
        self.assertNotIn('Set-Cookie',headers);self.assertNotIn('devCode',value)
        self.assertEqual(f.calls[-1]['body'],{key:PAYLOAD[key] for key in ('campusId','email')})
        for key in ('Cookie','Authorization'):self.assertNotIn(key,f.calls[-1]['headers'])
        self.assertEqual(f.request('/api/auth/register',PAYLOAD)[:2],(200,{'ok':True}))
        self.assertEqual(f.calls[-1]['body'],PAYLOAD)
        status,value,headers=f.request('/api/auth/login',{'identifier':PAYLOAD['campusId'],'password':PAYLOAD['password']})
        self.assertEqual(status,200);self.assertEqual(value['user']['username'],PAYLOAD['name'])
        self.assertIn('alchemy_hatchery_console_session=',headers['Set-Cookie'])

    def test_long_shared_bcrypt_password_can_log_in(self):
        password='汉'*30
        self.assertEqual(self.f.request('/api/auth/register',{**PAYLOAD,'password':password})[0],200)
        self.assertEqual(self.f.request('/api/auth/login',{'identifier':PAYLOAD['campusId'],'password':password})[0],200)

    def test_validation_and_origin_rejection_never_reach_upstream(self):
        f=self.f
        for path,body in [('/api/auth/campus',{}),('/api/auth/send-otp',{'email':PAYLOAD['email']}),
                          ('/api/auth/register',[]),('/api/auth/register',{**PAYLOAD,'grade':'invalid'}),
                          ('/api/auth/register',{**PAYLOAD,'classGroup':'25班'}),('/api/auth/register',{**PAYLOAD,'name':''}),
                          ('/api/auth/register',{**PAYLOAD,'realName':''}),('/api/auth/register',{**PAYLOAD,'code':'000'})]:
            with self.subTest(path=path,body=body):self.assertEqual(f.request(path,body)[0],400)
        self.assertEqual(f.request('/api/auth/register',PAYLOAD,{'Origin':'https://evil.test'})[0],403)
        self.assertFalse(f.calls)

    def test_failed_eligibility_or_code_does_not_create_user(self):
        self.assertEqual(self.f.request('/api/auth/campus',{'campusId':'unknown'})[0],404)
        status,value,_=self.f.request('/api/auth/register',{**PAYLOAD,'code':'000000'})
        self.assertEqual(status,400);self.assertIn('验证码',value['error'])
        with self.f.pg.db() as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM "User"');self.assertEqual(cur.fetchone()[0],0)

    def test_upstream_rate_limit_is_preserved(self):
        self.f.override=(429,{'error':'请稍后重试'},{'Retry-After':'60'})
        status,value,headers=self.f.request('/api/auth/send-otp',PAYLOAD)
        self.assertEqual(status,429);self.assertEqual(headers['Retry-After'],'60')
        self.assertEqual(len(self.f.calls),1)

    def test_local_rate_limit_blocks_before_forwarding(self):
        with patch.object(server,'rate_limit_hit',return_value=True):
            self.assertEqual(self.f.request('/api/auth/send-otp',PAYLOAD)[0],429)
        self.assertFalse(self.f.calls)

    def test_redirect_html_oversized_and_ambiguous_results_never_retry(self):
        for status,body in [(302,{}),(500,{'error':'private stack'}),(200,b'<html>gateway</html>'),
                            (200,{'error':'not registered'}),(200,{'blob':'x'*70000})]:
            self.f.override=(status,body,{'Location':'https://evil.test'})
            before=len(self.f.calls)
            result=self.f.request('/api/auth/register',PAYLOAD)
            self.assertEqual(result[0],502);self.assertTrue(result[1]['uncertain'])
            self.assertNotIn('private stack',str(result));self.assertEqual(len(self.f.calls),before+1)

    def test_upstream_errors_cannot_echo_password_or_code(self):
        self.f.override=(400,{'error':PAYLOAD['password']+' '+PAYLOAD['code']},{})
        value=self.f.request('/api/auth/register',PAYLOAD)[1]
        self.assertEqual(value['error'],'*** ***')

    def test_only_fixed_https_registration_routes_are_allowed(self):
        with self.assertRaises(ValueError):registration.forward('https://example.test','../../admin',{})
        for url in ('http://example.test','https://user:pass@example.test','https://example.test/path','https://example.test?url=elsewhere','https://example.test:invalid'):
            self.assertEqual(registration.forward(url,'campus',{})[0],503)


if __name__=='__main__':unittest.main()
