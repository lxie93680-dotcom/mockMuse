"""Playwright MCP semantic actions, Chrome extension video and manual input."""
import asyncio,json,os,re,shutil
from pathlib import Path
from contextlib import asynccontextmanager,suppress
from agents.mcp import MCPServerStdio
from .browser import Browser
from .tool_diagnostics import safe_text
from .browser_diagnostics import traced_call,traced_lock,background_trace
from .human_handoff import HumanInterventionRequired,human_reason

ROOT=Path(__file__).resolve().parents[1]
ENTRY=ROOT/'vendor/playwright/node_modules/@playwright/mcp/cli.js'

class PlaywrightBrowser(Browser):
    backend='playwright'
    def __init__(self):
        super().__init__();self.server=None;self.process=None
        self.status={'backend':'playwright','ready':False,'error':''}
        self.valid_refs=set();self.extension_attached=False;self.frame_task=None;self.rpc_contexts={}
        self.extension_mode=os.getenv('MUSE_PLAYWRIGHT_CONNECTION','dedicated')=='extension'
        self.status['connection_mode']='extension' if self.extension_mode else 'dedicated'

    @property
    def connected(self):
        return self.server is not None and self.extension_attached if self.extension_mode else super().connected

    async def start(self):
        if self.server is None:raise ConnectionError(self.status['error'] or 'Playwright MCP 尚未就绪')

    async def request(self,action,**params):
        # The extension only supplies frames/manual input; it never grounds Agent clicks.
        if not self.connection:raise ConnectionError('Playwright 画面连接器未连接')
        return await super().request(action,**params)

    @asynccontextmanager
    async def session(self,record):
        node=shutil.which('node')
        child_env={k:v for k,v in os.environ.items() if k in ('HOME','PATH','TMPDIR','LANG','LC_ALL','USER','LOGNAME','DISPLAY')}
        data=Path(os.getenv('MUSE_DATA_DIR',str(ROOT/'data'))).resolve()
        output=data/'playwright-output';output.mkdir(parents=True,exist_ok=True)
        server=None;trace_reader=None
        trace_path=output/('mcp-trace-'+__import__('uuid').uuid4().hex+'.jsonl')
        trace_path.touch(mode=0o600)
        child_env['MUSE_MCP_TRACE_FILE']=str(trace_path)
        try:
            if not node or not ENTRY.is_file():raise RuntimeError('缺少 Playwright MCP 依赖，请运行 npm --prefix vendor/playwright ci')
            endpoint=os.getenv('MUSE_PLAYWRIGHT_CDP_ENDPOINT','')
            if not endpoint and not self.extension_mode:
                executable=os.getenv('MUSE_PLAYWRIGHT_EXECUTABLE','')
                if not executable:
                    matches=list((Path.home()/'Library/Caches/ms-playwright').glob('chromium-*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'))
                    executable=str(max(matches,key=lambda p:int(re.search(r'chromium-(\d+)',str(p))[1]))) if matches else ''
                if not Path(executable).is_file():raise RuntimeError('缺少 Chrome for Testing；请设置 MUSE_PLAYWRIGHT_EXECUTABLE')
                extension=data/'playwright-extension'
                shutil.copytree(ROOT/'extension',extension,dirs_exist_ok=True)
                worker=extension/'worker.js';worker.write_text(worker.read_text().replace('http://127.0.0.1:8000',f'http://127.0.0.1:{os.getenv("MUSE_PORT","8000")}'))
                profile=data/'playwright-profile'
                self.process=await asyncio.create_subprocess_exec(node,str(ROOT/'vendor/playwright/launch.mjs'),str(profile),str(extension),executable,f'http://127.0.0.1:{os.getenv("MUSE_PORT","8000")}/control-test',os.getenv('MUSE_PLAYWRIGHT_HEADLESS','0'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=child_env)
                line=await asyncio.wait_for(self.process.stdout.readline(),30)
                info=json.loads(line);endpoint=info['endpoint'];self.status['browser_version']=info['version']
            connection_args=['--extension'] if self.extension_mode else ['--cdp-endpoint',endpoint]
            if self.extension_mode and os.getenv('MUSE_PLAYWRIGHT_CHROME_EXECUTABLE'):
                connection_args+=['--executable-path',os.environ['MUSE_PLAYWRIGHT_CHROME_EXECUTABLE']]
            if self.extension_mode and os.getenv('MUSE_PLAYWRIGHT_PROFILE_DIR'):
                connection_args+=['--profile-dir-name',os.environ['MUSE_PLAYWRIGHT_PROFILE_DIR']]
            server=MCPServerStdio(name='Playwright Browser',params={'command':node,'args':['--require',str(ROOT/'vendor/playwright/diagnostic-hook.cjs'),str(ENTRY),*connection_args,'--output-dir',str(output),'--timeout-action','5000','--timeout-navigation','20000'],'cwd':str(output),'env':child_env},client_session_timeout_seconds=35,cache_tools_list=True)
            trace_reader=asyncio.create_task(self.read_internal_trace(trace_path))
            await server.connect();self.server=server;self.status.update(ready=True,error='')
            record('Playwright MCP 已连接','浏览器语义操作已切换；实时画面与人工接管使用同一浏览器')
        except Exception as error:
            self.status['error']=safe_text(error);record('Playwright MCP 启动失败',self.status['error'])
        try:yield
        finally:
            await self.stop_stream()
            if trace_reader:
                trace_reader.cancel();await asyncio.gather(trace_reader,return_exceptions=True)
            self.server=None;self.status['ready']=False
            if server:
                with suppress(Exception):await server.cleanup()
            if self.process and self.process.returncode is None:
                with suppress(BrokenPipeError,ConnectionResetError):self.process.stdin.write(b'close\n');await self.process.stdin.drain()
                try:await asyncio.wait_for(self.process.wait(),15)
                except asyncio.TimeoutError:self.process.terminate();await self.process.wait()

    async def read_internal_trace(self,path):
        from .debug import debug
        with path.open() as stream:
            while True:
                for line in stream:
                    try:event=json.loads(line)
                    except ValueError:continue
                    if event.get('background') and not event.get('error'):continue
                    context=self.rpc_contexts.get(event.get('trace_id'))
                    if context:
                        diagnostics,name,call_id=context
                        diagnostics.emit('browser_stage',name,call_id,result=event)
                    else:debug.event('mcp_internal',event,error=bool(event.get('error')))
                await asyncio.sleep(.25)

    async def tool(self,name,args):
        await self.start()
        result=await traced_call(self,name,args)
        text='\n'.join(c.text for c in result.content if hasattr(c,'text'))
        if getattr(result,'is_error',getattr(result,'isError',False)):raise RuntimeError(text)
        if self.extension_mode:self.extension_attached=True
        return text

    async def snapshot(self,ref=None):
        args={'target':str(ref)} if ref is not None else {}
        raw=await self.tool('browser_snapshot',args)
        match=re.search(r'```yaml\n(.*?)```',raw,re.S);tree=match[1].strip() if match else raw
        title=re.search(r'- Page Title: (.*)',raw);url=re.search(r'- Page URL: (.*)',raw)
        self.url=url[1] if url else self.url
        tags={'button':'button','link':'a','textbox':'input','combobox':'select','option':'option','listitem':'li','checkbox':'input','radio':'input'}
        elements=[];parents=[]
        for line in tree.splitlines():
            m=re.search(r'^(\s*)- (\w+)(?: "(.*?)")?.*?\[ref=([^\]]+)\]',line)
            if not m:continue
            depth=len(m[1]);role=m[2];name=m[3] or '';uid=m[4]
            while parents and parents[-1][0]>=depth:parents.pop()
            context=' / '.join(p[1] for p in parents if p[1])
            elements.append({'ref':uid,'role':role,'tag':tags.get(role,role),'text':name,'context':context,'disabled':'[disabled]' in line})
            parents.append((depth,name))
        # Refs are MCP-owned strings; never renumber or recycle them in Muse.
        self.valid_refs={e['ref'] for e in elements}
        result={'title':title[1] if title else '', 'url':self.url,'text':tree,'snapshot':tree,'elements':elements,'width':self.width,'height':self.height,'backend':'playwright'}
        self.last_observation=result
        reason=human_reason(tree)
        if reason:
            await self.sync_video()
            raise HumanInterventionRequired(reason)
        return result

    async def perform(self,action,url='',ref=-1,text='',amount=500):
        if action=='open' and not re.match(r'^https?://',url):raise ValueError('只支持 http/https 网页')
        async with traced_lock(self):
            await self.start()
            has_ref=ref not in (-1,'-1','',None)
            if action=='list_tabs':
                raw=await self.tool('browser_tabs',{'action':'list'});tabs=[]
                for line in raw.splitlines():
                    m=re.search(r'- (\d+):.*?\[(.*?)\]\((.*?)\)',line)
                    if m:tabs.append({'id':int(m[1]),'title':m[2],'url':m[3],'controlled':'(current)' in line})
                return {'title':'Playwright 标签页','tabs':tabs,'raw':raw}
            if action=='switch_tab':
                await self.tool('browser_tabs',{'action':'select','index':int(ref)})
                self.valid_refs.clear()
            elif action=='open':
                await self.tool('browser_navigate',{'url':url});self.valid_refs.clear()
            elif action=='read':return await self.snapshot(ref if has_ref else None)
            elif action=='wait':await asyncio.sleep(max(0,min(amount,3000))/1000)
            elif action=='click_text':
                # Select only a unique observed accessible name; never first()/nth().
                seen=await self.snapshot();matches=[e for e in seen['elements'] if e['text']==text and e['role'] in ('button','link','option','menuitem','listitem')]
                if len(matches)!=1:return {**seen,'status':'needs_observation','action_performed':False,'message':f'可操作文字匹配数量为{len(matches)}','recovery':'根据最新snapshot中的role/context/ref选择唯一目标'}
                await self.tool('browser_click',{'target':matches[0]['ref']})
            elif action in ('click','hover','fill','select','press'):
                if action!='press' or has_ref:
                    if str(ref) not in self.valid_refs:raise ValueError('引用不属于最新 Playwright 快照，请重新读取页面')
                if action in ('click','hover'):await self.tool('browser_'+action,{'target':str(ref)})
                elif action=='fill':await self.tool('browser_type',{'target':str(ref),'text':text})
                elif action=='select':await self.tool('browser_select_option',{'target':str(ref),'values':[text]})
                else:
                    if has_ref:await self.tool('browser_click',{'target':str(ref)})
                    await self.tool('browser_press_key',{'key':text})
            elif action=='scroll':
                # Keep scrolling as ordinary input; semantic grounding remains MCP.
                if self.extension_mode:
                    await self.tool('browser_run_code_unsafe',{'code':f'async (page) => {{ await page.mouse.wheel(0,{int(amount)}); }}'})
                else:await self.request('manual',input={'action':'scroll','y':amount})
            else:raise ValueError('未知浏览器操作')
            result=await self.snapshot()
            await self.sync_video()
            return result

    async def sync_video(self):
        if self.extension_mode or not self.connection:return
        # Match the MCP-selected page to the video connector by observed URL.
        try:
            tabs=await self.request('list_tabs');matches=[t for t in tabs['tabs'] if t['url']==self.url]
            if len(matches)==1:await self.request('switch_tab',ref=matches[0]['id'])
        except (RuntimeError,ConnectionError):pass

    async def observe(self):return await self.perform('read')
    async def screenshot(self,page=None):
        if not self.extension_mode:return await super().screenshot(page)
        import base64
        import io
        from PIL import Image
        result=await traced_call(self,'browser_take_screenshot',{'type':'jpeg','scale':'css','filename':'muse-live.jpg'})
        if getattr(result,'is_error',False):raise RuntimeError('Playwright截图失败')
        blocks=[c for c in result.content if getattr(c,'type',None)=='image']
        if blocks:picture=base64.b64decode(blocks[0].data)
        else:picture=(Path(os.getenv('MUSE_DATA_DIR',str(ROOT/'data')))/'playwright-output'/'muse-live.jpg').read_bytes()
        self.width,self.height=Image.open(io.BytesIO(picture)).size
        self.image_width,self.image_height=self.width,self.height
        meta=await self.tool('browser_run_code_unsafe',{'code':'async (page) => ({url:page.url()})'})
        match=re.search(r'### Result\n(.*?)(?:\n### |$)',meta,re.S)
        if match:self.url=json.loads(match[1])['url']
        self.extension_attached=True
        return picture

    async def ensure_stream(self,on_frame):
        if not self.extension_mode:return await super().ensure_stream(on_frame)
        self.on_frame=on_frame
        if not self.connected or self.frame_task and not self.frame_task.done():return
        async def frames():
            # Background frames must not inherit an Agent call's diagnostic context.
            from .browser_diagnostics import trace_context
            trace_context.set(None)
            background_trace.set(True)
            import base64
            while True:
                if not self.lock.locked():
                    async with traced_lock(self):
                        try:on_frame(base64.b64encode(await self.screenshot()).decode(),self.url)
                        except Exception as error:
                            self.status['stream_error']={'type':type(error).__name__,'message':safe_text(error)}
                            self.extension_attached=False;return
                await asyncio.sleep(1)
        self.frame_task=asyncio.create_task(frames());self.streaming=True

    async def stop_stream(self):
        if not self.extension_mode:return await super().stop_stream()
        if self.frame_task:
            self.frame_task.cancel()
            with suppress(asyncio.CancelledError):await self.frame_task
        self.frame_task=None;self.streaming=False

    async def manual(self,action,x=0,y=0,text='',x2=0,y2=0,button='left',dx=0):
        if not self.extension_mode:return await super().manual(action,x,y,text,x2,y2,button,dx)
        async with traced_lock(self):
            if action=='open':
                if not re.match(r'^https?://',text):raise ValueError('只支持HTTP/HTTPS网页')
                await self.tool('browser_navigate',{'url':text})
            else:
                snippets={'move':f'await page.mouse.move({float(x)},{float(y)})',
                          'down':f'await page.mouse.move({float(x)},{float(y)});await page.mouse.down({{button:{json.dumps(button)}}})',
                          'up':f'await page.mouse.move({float(x)},{float(y)});await page.mouse.up({{button:{json.dumps(button)}}})',
                          'click':f'await page.mouse.click({float(x)},{float(y)},{{button:{json.dumps(button)}}})',
                          'type':f'await page.keyboard.insertText({json.dumps(text)})',
                          'press':f'await page.keyboard.press({json.dumps(text)})',
                          'wheel':f'await page.mouse.wheel({float(dx)},{float(y2)})',
                          'scroll':f'await page.mouse.wheel(0,{float(y)})',
                          'drag':f'await page.mouse.move({float(x)},{float(y)});await page.mouse.down();await page.mouse.move({float(x2)},{float(y2)},{{steps:20}});await page.mouse.up()'}
                if action not in snippets:raise ValueError('未知手动操作')
                await self.tool('browser_run_code_unsafe',{'code':'async (page) => {'+snippets[action]+'}'})
            self.valid_refs.clear()
    async def release_input(self):
        if self.extension_mode:
            if self.server and self.extension_attached:
                with suppress(Exception):await self.tool('browser_run_code_unsafe',{'code':'async (page) => {await page.mouse.up();}'})
        else:await super().release_input()
        self.valid_refs.clear()
