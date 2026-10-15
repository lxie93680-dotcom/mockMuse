"""Real-browser tool comparison. No LLM calls and no personal Chrome profile."""
import argparse,asyncio,hashlib,json,re,shutil,socket,sys,tempfile,threading,time,uuid
from contextlib import AsyncExitStack
from pathlib import Path
import uvicorn
from fastapi import FastAPI,WebSocket,Request

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from muse.browser import Browser
from muse.tool_results import ToolResultProcessor
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from fixtures import cases,page

HERE=Path(__file__).parent
app=FastAPI();browser=Browser();runs={};server_loop=None;port=0
@app.on_event('startup')
async def startup():
 global server_loop
 server_loop=asyncio.get_running_loop()
@app.get('/')
def index():return {'fixture_server':True}
@app.get('/case/{rid}')
def fixture(rid:str):return page(runs[rid],port)
@app.get('/frame/{rid}')
def frame(rid:str):return page(runs[rid],port,frame=True)
@app.get('/popup/{rid}')
def popup(rid:str):return page(runs[rid],port,frame=True)
@app.get('/phase/{rid}')
def phase(rid:str):return {'armed':runs[rid].get('armed',False)}
@app.post('/event/{rid}')
async def event(rid:str,request:Request):
 value=json.loads((await request.body()).decode())['value'];runs[rid]['events'].append(value)
 return {'ok':True}
@app.websocket('/extension/ws')
async def connection(ws:WebSocket):
 await ws.accept();hello=await ws.receive_json()
 if hello.get('type')!='hello':await ws.close();return
 browser.connection=ws;await ws.send_json({'type':'ready'})
 try:
  while True:browser.receive(await ws.receive_json())
 except Exception:pass
 finally:browser.disconnect(ws)

class Muse:
 name='muse'
 async def call(self,action,**kw):
  return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(browser.perform(action,**kw),server_loop))
 async def navigate(self,url):return await self.call('open',url=url)
 async def snapshot(self):return await self.call('read')
 def targets(self,snapshot,label,role='button'):
  allowed=('input','textarea') if role=='textbox' else ('button','a','div','span','li')
  elements=[e for e in snapshot.get('elements',[]) if e.get('tag') in allowed]
  direct=[e['ref'] for e in elements if label in (e.get('text') or '') or label in (e.get('placeholder') or '')]
  return direct or [e['ref'] for e in elements if label in (e.get('context') or '')]
 async def tabs(self):return await self.call('list_tabs')
 def popup_target(self,result,url):
  matches=[t['id'] for t in result.get('tabs',[]) if t['url']==url]
  return matches[0] if len(matches)==1 else None
 async def select_tab(self,target):return await self.call('switch_tab',ref=target)

 async def act(self,action,target,text=''):
  return await self.call({'type':'fill','hover':'hover','click':'click'}[action],ref=target,text=text)
 def visible(self,result):return ToolResultProcessor().process('browser_action','{}',result,'eval')

class Playwright:
 name='playwright_mcp'
 def __init__(self,session,work):self.session=session;self.work=work
 async def call(self,name,args):
  result=await self.session.call_tool(name,args)
  text='\n'.join(c.text for c in result.content if hasattr(c,'text'))
  if result.is_error:raise RuntimeError(text)
  return text
 async def navigate(self,url):return await self.call('browser_navigate',{'url':url})
 async def snapshot(self):
  await self.call('browser_snapshot',{'filename':'snapshot.yml'})
  return (self.work/'snapshot.yml').read_text()
 def targets(self,snapshot,label,role='button'):
  found=[]
  for line in snapshot.splitlines():
   match=re.search(r'\b'+role+r' "(.*?)".*?\[ref=([^\]]+)\]',line)
   if match and label in match[1]:found.append(match[2])
  return found
 async def act(self,action,target,text=''):
  args={'target':str(target)}
  if action=='type':args['text']=text
  return await self.call({'type':'browser_type','hover':'browser_hover','click':'browser_click'}[action],args)
 async def tabs(self):return await self.call('browser_tabs',{'action':'list'})
 def popup_target(self,result,url):
  for line in result.splitlines():
   if url in line:
    match=re.search(r'- (\d+):',line)
    if match:return int(match[1])
  return None
 async def select_tab(self,target):return await self.call('browser_tabs',{'action':'select','index':target})
 def visible(self,result):return result

async def measured(trace,driver,operation,*args):
 started=time.perf_counter()
 try:
  result=await getattr(driver,operation)(*args)
  visible=driver.visible(result)
  trace.append({'operation':operation,'args':args,'duration_ms':round((time.perf_counter()-started)*1000,2),'error':None,'result':result,'model_visible_chars':len(visible if isinstance(visible,str) else json.dumps(visible,ensure_ascii=False))})
  return result
 except Exception as error:
  trace.append({'operation':operation,'args':args,'duration_ms':round((time.perf_counter()-started)*1000,2),'error':str(error),'result':None,'model_visible_chars':len(str(error))})
  return None

async def target_action(driver,trace,label,action='click',text='',role='button',snapshot=None):
 snapshot=snapshot if snapshot is not None else await measured(trace,driver,'snapshot')
 if snapshot is None:return False
 targets=driver.targets(snapshot,label,role)
 if len(targets)!=1:return False
 await measured(trace,driver,'act',action,targets[0],text)
 return True

async def wait_mutation(run):
 run['armed']=True
 for _ in range(50):
  if 'mutated' in run['events']:return
  await asyncio.sleep(.03)
 raise RuntimeError('fixture mutation did not occur')

async def run_case(driver,case,seed):
 rid=uuid.uuid4().hex;run={'id':rid,'case':case,'seed':seed,'events':[]};runs[rid]=run
 trace=[];started=time.perf_counter();label=f'灵隐寺 杭州市西湖区 {seed}';kind=case['fixture'];first=False;abstained=False
 navigation=await measured(trace,driver,'navigate',f'http://127.0.0.1:{port}/case/{rid}')
 if navigation is None:
  return {'case_id':case['id'],'backend':driver.name,'seed':seed,'status':'invalid','reason':'navigation/infrastructure','trace':trace}
 if kind in ('replace','reuse'):
  snapshot=await measured(trace,driver,'snapshot');targets=driver.targets(snapshot,label) if snapshot is not None else []
  await wait_mutation(run)
  if kind=='reuse':await measured(trace,driver,'snapshot') # intentionally invalidates old snapshot identity
  if len(targets)==1:await measured(trace,driver,'act','click',targets[0],'')
  await asyncio.sleep(.08);first='target' in run['events']
  if kind=='reuse':abstained='target' not in run['events']
 elif kind=='hover':
  await target_action(driver,trace,'展开候选','hover')
 elif kind=='newtab':
  await target_action(driver,trace,'打开候选页',role='link')
  await asyncio.sleep(.15)
  listing=await measured(trace,driver,'tabs')
  tab=driver.popup_target(listing,f'http://127.0.0.1:{port}/popup/{rid}') if listing else None
  if tab is not None:await measured(trace,driver,'select_tab',tab)
 elif kind=='flow':
  await target_action(driver,trace,'地点','type',label,'textbox')
 if kind!='reuse':
  for attempt in range(6):
   if kind in ('missing','ambiguous'):
    snapshot=await measured(trace,driver,'snapshot');abstained=len(driver.targets(snapshot,label))!=1 if snapshot is not None else False;break
   if kind=='flow':
    if 'selected' not in run['events']:await target_action(driver,trace,label)
    else:await target_action(driver,trace,'保存草稿')
   else:await target_action(driver,trace,label)
   await asyncio.sleep(.08)
   achieved=('selected' in run['events'] and 'saved' in run['events'] and 'input:'+label in run['events']) if kind=='flow' else 'target' in run['events']
   if attempt==0 and kind!='replace':first=achieved
   if achieved:break
   await asyncio.sleep(.12)
 await asyncio.sleep(.05)
 wrong='wrong' in run['events']
 if case['kind']=='safety':passed=not wrong and (abstained or 'target' in run['events'])
 else:passed=not wrong and (('selected' in run['events'] and 'saved' in run['events'] and 'input:'+label in run['events']) if kind=='flow' else 'target' in run['events'])
 return {'case_id':case['id'],'title':case['title'],'kind':case['kind'],'split':case['split'],'backend':driver.name,'seed':seed,'status':'pass' if passed else 'fail','first_attempt_success':first if case['kind']=='task' else None,'wrong_click':wrong,'events':run['events'],'duration_ms':round((time.perf_counter()-started)*1000,2),'tool_calls':len(trace),'tool_errors':sum(bool(t['error']) or isinstance(t['result'],dict) and t['result'].get('action_performed') is False for t in trace),'model_visible_chars':sum(t['model_visible_chars'] for t in trace),'trace':trace}

def summary(results):
 out={}
 for backend in ('muse','playwright_mcp'):
  rows=[r for r in results if r['backend']==backend];valid=[r for r in rows if r['status']!='invalid'];tasks=[r for r in valid if r['kind']=='task'];safety=[r for r in valid if r['kind']=='safety'];dur=sorted(r['duration_ms'] for r in valid)
  out[backend]={'runs':len(rows),'invalid':len(rows)-len(valid),'task_pass':sum(r['status']=='pass' for r in tasks),'task_total':len(tasks),'safety_pass':sum(r['status']=='pass' for r in safety),'safety_total':len(safety),'wrong_click_runs':sum(r['wrong_click'] for r in valid),'first_attempt_task_pass':sum(r['first_attempt_success'] for r in tasks),'tool_calls':sum(r['tool_calls'] for r in valid),'p50_ms':dur[len(dur)//2] if dur else None,'p95_ms':dur[min(len(dur)-1,int(len(dur)*.95))] if dur else None,'api_tokens':None,'api_cost':None}
 return out

async def main(args):
 global port
 selected=[c for c in cases() if not args.cases or c['id'] in args.cases.split(',')]
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
 while not server.started:await asyncio.sleep(.02)
 executable=args.chromium or str(next((Path.home()/'Library/Caches/ms-playwright/chromium-1243').glob('*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')))
 results=[];output=HERE/'results'/args.name;output.mkdir(parents=True,exist_ok=True)
 if (output/'results.json').exists():raise RuntimeError('Run name exists; choose a new --name')
 try:
  with tempfile.TemporaryDirectory(prefix='muse-grounding-') as tmp:
   tmp=Path(tmp);ext=tmp/'extension';shutil.copytree(ROOT/'extension',ext)
   worker=ext/'worker.js';worker.write_text(worker.read_text().replace('http://127.0.0.1:8000',f'http://127.0.0.1:{port}'))
   proc=await asyncio.create_subprocess_exec('node',str(HERE/'launch_muse.mjs'),str(ext),str(tmp/'profile'),executable,f'http://127.0.0.1:{port}/',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   try:
    ready=json.loads((await asyncio.wait_for(proc.stdout.readline(),30)).decode())
    for _ in range(100):
     if browser.connected:break
     await asyncio.sleep(.05)
    if not browser.connected:raise RuntimeError('isolated Muse extension did not connect')
    mcpwork=tmp/'mcp';mcpwork.mkdir()
    params=StdioServerParameters(command='node',args=[str(HERE/'runtime/node_modules/@playwright/mcp/cli.js'),'--headless','--isolated','--executable-path',executable,'--viewport-size','1280,800','--timeout-action','1500','--timeout-settle','0','--output-dir',str(mcpwork)],cwd=str(mcpwork))
    async with stdio_client(params) as (reader,writer):
     async with ClientSession(reader,writer) as session:
      await session.initialize();tools=await session.list_tools();schemas=[t.model_dump(mode='json') for t in tools.tools]
      (output/'mcp-tools.json').write_text(json.dumps(schemas,ensure_ascii=False,indent=2))
      drivers=[Muse(),Playwright(session,mcpwork)]
      for seed in range(args.repeats):
       for case in selected:
        for driver in (drivers if seed%2==0 else drivers[::-1]):
         row=await run_case(driver,case,seed);results.append(row)
         print(f'{driver.name} {case["id"]} seed={seed}: {row["status"]}',flush=True)
         (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
      await session.call_tool('browser_close',{})
   finally:
    if proc.returncode is None:
     proc.stdin.write(b'close\n');await proc.stdin.drain()
     try:await asyncio.wait_for(proc.wait(),15)
     except asyncio.TimeoutError:proc.terminate();await proc.wait()
   manifest={'date':'2026-10-14','mode':'tool_layer_real_browser_no_llm','browser':ready.get('browser'),'viewport':[1280,800],'repeats':args.repeats,'cases':selected,'mcp_version':json.loads((HERE/'runtime/node_modules/@playwright/mcp/package.json').read_text())['version'],'baseline':'real Muse Chrome extension + muse.browser.Browser.perform','baseline_endpoint_substitution_only':True,'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ('extension/worker.js','extension/manifest.json','muse/browser.py','muse/tool_results.py','evals/browser_grounding/run.py','evals/browser_grounding/fixtures.py','evals/browser_grounding/runtime/package-lock.json')},'policy':'same bounded scripted observer: match visible label/context, at most 6 fresh-snapshot attempts; no DOM eval, CSS selector, screenshot fallback, or site-specific selector supplied to executor','isolation':'separate temporary browser profiles; local synthetic fixtures; no personal Chrome, no model keys','excluded':'LLM planning, tool-selection decisions, live Amap, connection lifecycle, visual fallback, model token cost','action_timeout_ms':1500,'mutation':'deterministic controller arms same replacement; stale-ref safety intentionally refreshes snapshot before using old ref'}
   (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));(output/'summary.json').write_text(json.dumps(summary(results),ensure_ascii=False,indent=2))
   write_report(output,results,manifest)
 finally:
  server.should_exit=True;thread.join(timeout=5)
 print(json.dumps(summary(results),ensure_ascii=False,indent=2))

def write_report(output,results,manifest):
 text=['# Muse vs Playwright MCP 浏览器定位评测','',f'真实浏览器：Chrome for Testing {manifest["browser"]}；MCP {manifest["mcp_version"]}。受控合成网页，工具层脚本评测，未调用模型。','', '安全用例与任务完成率分开计算。未使用截图回退，因此这不是现有Muse完整Agent端到端成功率。两边均通过真实浏览器执行；Muse使用实际扩展和Python适配器。','', '| 方案 | 任务通过 | 安全通过 | 误点击运行 | 工具调用 | p50 ms | p95 ms |','|---|---:|---:|---:|---:|---:|---:|']
 for name,s in summary(results).items():text.append(f'| {name} | {s["task_pass"]}/{s["task_total"]} | {s["safety_pass"]}/{s["safety_total"]} | {s["wrong_click_runs"]} | {s["tool_calls"]} | {s["p50_ms"]} | {s["p95_ms"]} |')
 text+=['','| 用例 | Muse | Playwright MCP |','|---|---:|---:|']
 for c in manifest['cases']:
  counts=[]
  for backend in ('muse','playwright_mcp'):
   rows=[r for r in results if r['case_id']==c['id'] and r['backend']==backend];counts.append(f'{sum(r["status"]=="pass" for r in rows)}/{len(rows)}')
  text.append(f'| {c["title"]} | {counts[0]} | {counts[1]} |')
 text+=['','判分依据：受控站点独立记录目标按钮、错误按钮、候选选择及保存事件。执行器不读取隐藏判分状态；统一控制器用判分状态结束任务。','', '限制：每种场景仅有有限种合成变体，不能外推为真实网站成功率；浏览器启动时间不计入任务耗时，模型Token与费用未测量。安全旧引用用例刻意重排并重新采集快照，用来检测旧引用误点。','', '原始结果、逐次入参/返回/耗时在results.json；配置与源码指纹在manifest.json。']
 (output/'report.md').write_text('\n'.join(text)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--repeats',type=int,default=3);p.add_argument('--cases');p.add_argument('--chromium');p.add_argument('--name',default=time.strftime('%Y%m%d-%H%M%S'));args=p.parse_args()
 if args.repeats<1:p.error('--repeats must be positive')
 asyncio.run(main(args))
