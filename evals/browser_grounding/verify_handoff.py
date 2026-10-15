"""Synthetic human-verification E2E: real browser, mocked Agent, no real CAPTCHA."""
import os,sys,json,socket,tempfile,subprocess,time,threading,urllib.request
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
ROOT=Path(__file__).resolve().parents[2]
class Fixture(BaseHTTPRequestHandler):
 def do_GET(self):
  text='<p>亲，滑动下方滑块，我们马上回来哦</p>' if self.path=='/challenge' else '<p>人工操作已完成</p>'
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(text.encode())
 def log_message(self,*args):pass
fixture=HTTPServer(('127.0.0.1',0),Fixture);threading.Thread(target=fixture.serve_forever,daemon=True).start();fport=fixture.server_port
s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
def api(path,body=None):
 req=urllib.request.Request(f'http://127.0.0.1:{port}/api/{path}',data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
 return json.load(urllib.request.urlopen(req,timeout=20))
with tempfile.TemporaryDirectory(prefix='muse-handoff-') as tmp:
 env={**os.environ,'MUSE_DATA_DIR':tmp,'MUSE_PORT':str(port),'MUSE_BROWSER_BACKEND':'playwright','MUSE_PLAYWRIGHT_HEADLESS':'1','MUSE_DESKTOP_COMMANDER':'0','MUSE_FILESYSTEM_MCP':'0','LLM_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'synthetic-test-not-a-key'}
 code=f'''import uvicorn,muse.app as app
async def fake(browser,prompt,*args):
 if prompt=='synthetic challenge':await browser.perform('open',url='http://127.0.0.1:{fport}/challenge')
 return '人工处理后已继续',[]
app.run_agent=fake
uvicorn.run(app.app,host='127.0.0.1',port={port},log_level='error')
'''
 with open(Path(tmp)/'service.log','wb') as log:
  proc=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-c',code],cwd=ROOT,env=env,stdout=log,stderr=log)
 ui=None
 try:
  for _ in range(150):
   try:
    if api('state')['browser_connected']:break
   except Exception:pass
   time.sleep(.2)
  else:raise RuntimeError('service failed to start')
  api('tasks',{'prompt':'synthetic challenge'})
  for _ in range(100):
   state=api('state')
   if state['status']=='needs_human':break
   time.sleep(.1)
  else:raise AssertionError(state['status'])
  print('Task paused for human:',state['human_handoff']['reason'],flush=True)
  executable=str(next((Path.home()/'Library/Caches/ms-playwright/chromium-1243').glob('*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')))
  ui=subprocess.Popen(['node',str(ROOT/'evals/browser_grounding/handoff_ui.mjs'),f'http://127.0.0.1:{port}/',executable],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
  print(ui.stdout.readline().strip(),flush=True)
  try:api('resume',{});raise AssertionError('Unresolved challenge resumed')
  except urllib.error.HTTPError as e:assert e.code==409
  # Simulate the human's manual navigation to a ready page in our own fixture.
  api('manual',{'action':'open','text':f'http://127.0.0.1:{fport}/ready'})
  ui.stdin.write('resume\n');ui.stdin.flush();print(ui.stdout.readline().strip(),flush=True);ui.wait(timeout=20);assert ui.returncode==0
  assert api('state')['status']=='completed'
  proc.terminate();proc.wait(20)
  saved=json.loads(Path(tmp,'playwright-profile','muse-tabs.json').read_text())
  assert f'http://127.0.0.1:{fport}/ready' in saved
  with open(Path(tmp)/'restart.log','wb') as restart_log:
   proc=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-c',code],cwd=ROOT,env=env,stdout=restart_log,stderr=restart_log)
  for _ in range(150):
   try:
    if api('state')['browser_connected'] and api('observation')['url']==f'http://127.0.0.1:{fport}/ready':break
   except Exception:pass
   time.sleep(.2)
  else:raise AssertionError('page restoration failed')
  print('Real browser handoff UI, automatic takeover, blocked resume, explicit continuation and restart page restore passed',flush=True)
 finally:
  if ui and ui.poll() is None:ui.terminate();ui.wait()
  proc.terminate()
  try:proc.wait(20)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
  fixture.shutdown()
