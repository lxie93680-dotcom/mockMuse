import os,subprocess,tempfile,socket,time,json,urllib.request,asyncio
from pathlib import Path
root=Path(__file__).resolve().parents[2]
def api(port,path,body=None):
 data=json.dumps(body).encode() if body is not None else None
 req=urllib.request.Request(f'http://127.0.0.1:{port}/api/{path}',data=data,headers={'Content-Type':'application/json'})
 return json.load(urllib.request.urlopen(req,timeout=30))
with tempfile.TemporaryDirectory(prefix='muse-playwright-live-') as tmp:
 s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close()
 env={**os.environ,'MUSE_BROWSER_BACKEND':'playwright','MUSE_PLAYWRIGHT_HEADLESS':'1','MUSE_PORT':str(port),'MUSE_DATA_DIR':tmp,'MUSE_DESKTOP_COMMANDER':'0','MUSE_FILESYSTEM_MCP':'0'}
 with open(Path(tmp)/'server.log','wb') as log:
  proc=subprocess.Popen([str(root/'.venv/bin/python'),'-m','uvicorn','muse.app:app','--host','127.0.0.1','--port',str(port)],cwd=root,env=env,stdout=log,stderr=log)
 try:
  for _ in range(160):
   try:
    state=api(port,'state')
    if state['browser_connected']:break
   except Exception:pass
   time.sleep(.15)
  else:
   print(Path(tmp,'server.log').read_text()[-3000:]);raise RuntimeError('Browser not ready')
  print('Driver:',state.get('browser_backend'),state.get('browser_driver'))
  assert state['browser_driver']['ready']
  print('Observation:',api(port,'observation')['title'])
  api(port,'tasks',{'prompt':'浏览器替换验收','demo':True})
  for _ in range(120):
   state=api(port,'state')
   if state['status']!='running':break
   time.sleep(.15)
  print('Demo:',state['status'])
  for l in state['logs'][-8:]:print(l)
  assert state['status']=='completed'
  observation=api(port,'observation')
  print('DOM refs:',[(e['ref'],e['tag'],e['text']) for e in observation['elements'][:8]])
  assert all(isinstance(e['ref'],str) for e in observation['elements'])
  assert 'Muse 浏览器 Demo' in observation['text']
  async def check_video():
   import websockets
   async with websockets.connect(f'ws://127.0.0.1:{port}/ws',origin=f'http://127.0.0.1:{port}',proxy=None) as ws:
    for _ in range(30):
     event=json.loads(await asyncio.wait_for(ws.recv(),5))
     if event.get('type')=='frame':
      assert event.get('frame');print('Live video:',event['width'],event['height']);break
    else:raise AssertionError('no live frame')
    await ws.send(json.dumps({'type':'takeover','active':True}))
    for _ in range(30):
     event=json.loads(await asyncio.wait_for(ws.recv(),5))
     if event.get('type')=='control':assert event['active'];break
    await ws.send(json.dumps({'type':'input','input':{'action':'open','text':f'http://127.0.0.1:{port}/control-test'}}))
    await asyncio.sleep(.5)
    await ws.send(json.dumps({'type':'takeover','active':False}))
   assert api(port,'observation')['title']=='Muse 远程控制测试'
   print('Manual navigation and resumed MCP observation passed')
  asyncio.run(check_video())
  print('Screenshot:',api(port,'screenshot',{}))
 finally:
  proc.terminate()
  try:proc.wait(20)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
