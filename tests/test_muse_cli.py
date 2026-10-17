import asyncio,contextlib,importlib,io,json,os,platform,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from muse import cli

class Handler(BaseHTTPRequestHandler):
    require_auth=False;ready=True;requests=[]
    def log_message(self,*args):pass
    def send(self,status,data,headers=None):
        raw=json.dumps(data).encode();self.send_response(status)
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.handle_request()
    def do_POST(self):self.handle_request()
    def do_DELETE(self):self.handle_request()
    def handle_request(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'null');type(self).requests.append((self.path,self.command,body))
        if self.path=='/api/login':self.send(200,{'ok':True},{'Set-Cookie':'muse_session=test; Path=/; HttpOnly'});return
        if type(self).require_auth and 'muse_session=test' not in self.headers.get('Cookie',''):self.send(401,{'detail':'login required'});return
        if self.command=='DELETE' and self.path=='/api/mcp/travel':self.send(200,{'id':'travel','removed':True,'removed_tools':1});return
        if self.path=='/api/mcp':
            if self.command=='GET':self.send(200,{'servers':[]})
            else:self.send(200,{'id':body['id'],'ready':type(self).ready,'state':'connected' if type(self).ready else 'retry_wait','tool_count':1})
        elif self.path=='/api/state':self.send(200,{'status':'completed','messages':[{'text':'must not appear'}],'mcp':{}})
        elif self.path.startswith('/api/tools/search?'):self.send(200,{'tools':[]})
        elif self.path=='/api/tools':self.send(200,{'tools':[]})
        elif self.path=='/api/mcp/redirect/connect':self.send(302,{}, {'Location':'https://invalid.example/'})
        else:self.send(404,{'detail':'not found'})

class CLITransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Handler.require_auth=False;Handler.ready=True;Handler.requests=[]
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    async def asyncTearDown(self):await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.thread.join()
    def run_cli(self,args):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(['--server',self.url,*args])
        return code,out.getvalue(),err.getvalue()
    def test_add_not_ready_is_saved_but_nonzero_and_status_is_compact(self):
        Handler.ready=False
        code,out,_=self.run_cli(['mcp','add','demo','--url','https://example.com/mcp','--token-env','TEST_TOKEN'])
        data=json.loads(out);self.assertEqual(code,2);self.assertTrue(data['saved']);self.assertFalse(data['ready'])
        self.assertEqual(Handler.requests[-1][2]['token_env'],'TEST_TOKEN')
        code,out,_=self.run_cli(['status']);self.assertEqual(code,0);self.assertNotIn('must not appear',out)
    def test_cookie_auth_is_in_memory_and_secret_is_not_output(self):
        Handler.require_auth=True
        with patch.dict(os.environ,{'MUSE_ACCESS_TOKEN':'test-secret-access'}):code,out,err=self.run_cli(['mcp','list'])
        self.assertEqual(code,0);self.assertNotIn('test-secret-access',out+err)
        self.assertTrue(any(path=='/api/login' for path,_,_ in Handler.requests))
    def test_remove_alias_and_delete_auth_retry(self):
        Handler.require_auth=True
        with patch.dict(os.environ,{'MUSE_ACCESS_TOKEN':'test-secret-access'}):
            for command in ('remove','uninstall'):
                code,out,err=self.run_cli(['mcp',command,'travel'])
                self.assertEqual(code,0,err);self.assertTrue(json.loads(out)['removed'])
                self.assertNotIn('saved',json.loads(out))
                self.assertEqual(Handler.requests[-1],('/api/mcp/travel','DELETE',None))

    def test_no_redirect_and_server_validation(self):
        with self.assertRaises(cli.CLIError):cli.Client(self.url).request('mcp/redirect/connect',{})
        for url in ('http://external.example','https://user:secret@example.com','https://example.com/?key=x'):
            with self.assertRaises(cli.CLIError):cli.Client(url)
    def test_offline_docs_and_invalid_limits_do_not_call_service(self):
        code,out,_=self.run_cli(['docs']);self.assertEqual(code,0);self.assertIn('name: muse-usage',out);self.assertEqual(Handler.requests,[])
        code,_,_=self.run_cli(['tools','search','query','--limit','0']);self.assertEqual(code,1);self.assertEqual(Handler.requests,[])
    async def test_real_native_command_can_find_cli_from_another_workdir(self):
        from muse.native_tools import NativeRuntime
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':directory,'MUSE_URL':self.url,'SECRET_API_KEY':'must-not-be-forwarded'}):
            runtime=NativeRuntime()
            try:
                result=await runtime.exec_command('muse mcp list',yield_time_ms=1000,timeout_seconds=15)
                while result.get('session_id'):result=await runtime.write_stdin(result['session_id'],yield_time_ms=1000)
                self.assertEqual(result['exit_code'],0);self.assertIn('servers',result['stdout'])
                command="[Environment]::GetEnvironmentVariable('SECRET_API_KEY')" if platform.system()=='Windows' else 'printenv SECRET_API_KEY'
                result=await runtime.exec_command(command,yield_time_ms=1000)
                self.assertNotIn('must-not-be-forwarded',result['stdout'])
            finally:await runtime.close()

class CLIServerIntegration(unittest.TestCase):
    def test_cli_reuses_live_manager_and_search_cache_without_restart(self):
        from fastapi.testclient import TestClient
        from tests.test_remote_mcp import FakeServer
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'MUSE_DATA_DIR':directory,'MUSE_ACCESS_TOKEN':'cli-test','MUSE_FILESYSTEM_MCP':'0','MUSE_BROWSER_BACKEND':'extension','MUSE_TOOL_SOURCES_CONFIG':''}),patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
            import muse.app as module
            module=importlib.reload(module)
            with TestClient(module.app) as c:
                c.post('/api/login',json={'token':'cli-test'})
                class APIClient:
                    def request(self,path,body=None,*,method=None):
                        if method=='DELETE':r=c.delete('/api/'+path)
                        else:r=c.get('/api/'+path) if body is None else c.post('/api/'+path,json=body)
                        if r.status_code>=400:raise cli.CLIError('HTTP '+str(r.status_code))
                        return r.json()
                def invoke(args):
                    out=io.StringIO();err=io.StringIO()
                    with patch('muse.cli.Client',return_value=APIClient()),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(args)
                    return code,json.loads(out.getvalue()) if out.getvalue() else {}
                code,added=invoke(['mcp','add','travel','--url','https://example.com/mcp']);self.assertEqual(code,0);self.assertTrue(added['saved']);self.assertTrue(added['ready'])
                persisted=json.loads((Path(directory)/'remote-mcp.json').read_text());self.assertIn('travel',persisted)
                code,found=invoke(['tools','search','hotel_search','--server-id','travel']);self.assertEqual(len(found['tools']),1);tid=found['tools'][0]['tool_id']
                _,info=invoke(['tools','describe',tid]);self.assertIn('fingerprint',info)
                self.assertEqual(found['tools'][0]['server_id'],'travel');self.assertEqual(info['server_id'],'travel')
                self.assertEqual(invoke(['tools','list','--server-id','travel'])[1]['tools'][0]['server_id'],'travel')
                invoke(['tools','disable',tid]);self.assertEqual(invoke(['tools','search','hotel_search'])[1]['tools'],[])
                invoke(['tools','enable',tid]);self.assertEqual(invoke(['tools','search','hotel_search'])[1]['tools'][0]['tool_id'],tid)
                self.assertEqual(invoke(['mcp','add','travel','--url','https://example.com/mcp'])[0],1)
                self.assertEqual(len(json.loads((Path(directory)/'remote-mcp.json').read_text())),1)

                code,removed=invoke(['mcp','remove','travel'])
                self.assertEqual(code,0);self.assertTrue(removed['removed'])
                self.assertEqual(invoke(['tools','search','hotel_search'])[1]['tools'],[])
                self.assertEqual(json.loads((Path(directory)/'remote-mcp.json').read_text()),{})
                self.assertFalse(invoke(['mcp','uninstall','travel'])[1]['removed'])
