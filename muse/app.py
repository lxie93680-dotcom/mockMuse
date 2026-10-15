import asyncio
import base64
import json
import ipaddress
import os
import secrets
import re
import sqlite3
import time
from contextlib import asynccontextmanager, suppress, closing, AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from .compression import CheckpointStore
from .context_history import close_pending_tools
from .agent import run_agent
from .loop_guard import LoopStopped
from .desktop_commander import Commander, Filesystem
from .browser import Browser
from .human_handoff import HumanInterventionRequired
from .debug import debug
from . import progress
from .models import get_provider, public_providers

load_dotenv()
ROOT = Path(__file__).parent
DATA = Path(os.getenv('MUSE_DATA_DIR', 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = os.getenv('MUSE_ACCESS_TOKEN', '')
if not TOKEN or TOKEN == 'replace-with-a-long-random-token':
    token_path = DATA / 'access-token.txt'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    TOKEN = token_path.read_text().strip()

if os.getenv('MUSE_BROWSER_BACKEND','extension') == 'playwright':
    from .playwright_browser import PlaywrightBrowser
    browser = PlaywrightBrowser()
else:
    browser = Browser()
commander = Commander()
filesystem = Filesystem()
sessions = set()
clients = set()
control_owner = None
state = {'browser_backend':getattr(browser,'backend','extension'),'browser_driver':getattr(browser,'status',{}),'phase':None,'phase_detail':'','active_tools':[],'activities':[], 'status': 'idle', 'logs': [], 'messages': [], 'url': '', 'frame': None, 'frame_seq': 0, 'frame_error': '',
         'model_ready': bool(get_provider().api_key), 'model': get_provider().model,
         'provider': get_provider().id, 'provider_name': get_provider().name, 'providers': public_providers()}
provider_id = get_provider().id
job = None
history = []


def database():
    db = sqlite3.connect(DATA / 'muse.db')
    db.execute('CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT, updated REAL, content TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS conversation_meta (id INTEGER PRIMARY KEY, active TEXT)')
    return db


def refresh_conversations(db):
    state['conversations'] = [{'id': row[0], 'title': row[1]} for row in db.execute(
        'SELECT id,title FROM conversations ORDER BY updated DESC')]


def save():
    with closing(database()) as db:
        db.execute('CREATE TABLE IF NOT EXISTS demo_state (id INTEGER PRIMARY KEY, content TEXT)')
        content = {k: v for k, v in state.items() if k in ('status', 'logs', 'messages','task_id','activities','human_handoff')}
        db.execute('INSERT OR REPLACE INTO demo_state VALUES (1, ?)', (json.dumps(content, ensure_ascii=False),))
        session_id = state.setdefault('conversation_id', secrets.token_hex(16))
        title = state.setdefault('conversation_title', '历史会话' if state['messages'] else '新会话')
        content.update(history=history, provider=provider_id)
        db.execute('INSERT OR REPLACE INTO conversations VALUES (?,?,?,?)',
                   (session_id, title, time.time(), json.dumps(content, ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO conversation_meta VALUES (1,?)', (session_id,))
        if state.get('task_id'):
            db.execute('CREATE TABLE IF NOT EXISTS task_runs (task_id TEXT PRIMARY KEY, session_id TEXT, parent_task_id TEXT, status TEXT, api_sequence INTEGER DEFAULT 0, current_turn_id TEXT, current_api_request_id TEXT)')
            db.execute('UPDATE task_runs SET status=? WHERE task_id=?',(state['status'],state['task_id']))
        refresh_conversations(db)
        db.commit()


def load_conversation(db, session_id):
    global history, provider_id
    row = db.execute('SELECT title,content FROM conversations WHERE id=?', (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, '会话不存在')
    old = json.loads(row[1])
    history = close_pending_tools(old.get('history', []))
    provider_id = old.get('provider', get_provider().id)
    provider = get_provider(provider_id)
    state.update(conversation_id=session_id, conversation_title=row[0],task_id=old.get('task_id'),human_handoff=old.get('human_handoff'),
                 activities=old.get('activities',[]),status=old.get('status','idle'), messages=old.get('messages',[]), logs=old.get('logs',[]),
                 provider=provider.id, provider_name=provider.name, model=provider.model,
                 model_ready=bool(provider.api_key.strip()))
    if state['status']=='running':
        state['status']='interrupted'
    for activity in state['activities']:
        if activity['status']=='running':activity.update(status='interrupted',phase=None,active_tools=[],ended_at=time.time(),detail='进程重启')
    state.update(phase=None,phase_detail='',active_tools=[])
    refresh_conversations(db)


def publish():
    payload = json.dumps(state, ensure_ascii=False)
    for queue, _ in list(clients):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(payload)


def publish_debug():
    state['debug']={'enabled':debug.enabled,'calls':debug.calls,'events':debug.events}
    publish()

debug.notify=publish_debug


def publish_activity(event):
    if not state.get('task_id') or event.get('parent_task_id')!=state['task_id']:
        return
    activities=state.setdefault('activities',[])
    for i,activity in enumerate(activities):
        if activity['task_id']==event['task_id']:
            activities[i]=event
            break
    else:activities.append(event)
    state['activities']=activities[-20:]
    save()
    publish()

progress.notify=publish_activity


def set_phase(phase, detail='', active_tools=None):
    if state['status'] != 'running':
        return
    state.update(phase=phase, phase_detail=detail, active_tools=active_tools or [],
                 phase_started_at=time.time())
    publish()


def record(title, detail=''):
    if state['status']!='running':
        state.update(phase=None,phase_detail='',active_tools=[])
    state['logs'].append({'title': title, 'detail': detail[:2000], 'time': time.strftime('%H:%M:%S')})
    state['logs'] = state['logs'][-200:]
    save()
    publish()


def send_browser_image(picture):
    directory = DATA / 'chat-images'
    directory.mkdir(exist_ok=True)
    name = secrets.token_hex(16) + '.jpg'
    (directory / name).write_bytes(picture)
    url = '/api/images/' + name
    state['messages'].append({'role': 'assistant', 'text': '当前浏览器截图', 'image_url': url})
    state['messages'] = state['messages'][-40:]
    save()
    publish()
    return url


def broadcast_frame(data, url):
    state.update(frame=data, url=url, browser_connected=browser.connected, frame_error='', frame_seq=state['frame_seq'] + 1,
                 width=browser.width, height=browser.height)
    payload = json.dumps({'type':'frame', 'frame':data, 'url':url,
                          'status':state['status'], 'seq':state['frame_seq'],
                          'width':browser.width, 'height':browser.height})
    for _, queue in list(clients):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(payload)


async def capture():
    while True:
        try:
            if clients and (state['status'] in ('running','needs_human') or control_owner is not None):
                await browser.ensure_stream(broadcast_frame)
            else:
                await browser.stop_stream()
        except Exception as error:
            await browser.stop_stream()
            state['frame_error'] = ('画面传输暂时失败：' + str(error)[:200] if isinstance(error, (RuntimeError, ConnectionError)) else '画面传输暂时失败：' + type(error).__name__)
            publish()
        await asyncio.sleep(.5)


@asynccontextmanager
async def lifespan(app):
    if (DATA / 'muse.db').exists():
        with closing(sqlite3.connect(DATA / 'muse.db')) as db:
            row = db.execute('SELECT content FROM demo_state WHERE id=1').fetchone()
            if row:
                old = json.loads(row[0])
                state.update(old)
                if state['status'] == 'running':
                    state['status'] = 'interrupted'
    with closing(database()) as db:
        active = db.execute('SELECT active FROM conversation_meta WHERE id=1').fetchone()
        if active:
            load_conversation(db, active[0])
    save()
    context_store().message_store()
    state.update(frame=None, browser_connected=False, width=1280, height=800,
                 frame_error='Chrome 连接器尚未连接，请确认扩展已更新并启用自动连接')
    task = asyncio.create_task(capture())
    try:
        async with AsyncExitStack() as stack:
            if hasattr(browser,'session'):
                await stack.enter_async_context(browser.session(record))
            await stack.enter_async_context(commander.session(record))
            await stack.enter_async_context(filesystem.session(record))
            state['mcp'] = {'desktop_commander':commander.status,'filesystem':filesystem.status}
            publish()
            try:
                yield
            finally:
                if job and not job.done():
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job
    finally:
        if job and not job.done():
            job.cancel()
            with suppress(asyncio.CancelledError):
                await job
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await browser.close()


app = FastAPI(lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


def local_connection(request):
    client=getattr(request,'client',None)
    host=request.headers.get('host','').split(':')[0] if not request.headers.get('host','').startswith('[') else request.headers.get('host','').split(']')[0]+']'
    try:
        peer_local=bool(client and ipaddress.ip_address(client.host).is_loopback)
    except ValueError:
        peer_local=False
    return peer_local and host in ('127.0.0.1','localhost','[::1]')


def authorized(request):
    return local_connection(request) or request.cookies.get('muse_session') in sessions


@app.middleware('http')
async def authentication(request: Request, call_next):
    if request.url.path.startswith('/api/') and request.url.path != '/api/login':
        if not authorized(request):
            return JSONResponse({'detail': '请先输入访问口令'}, status_code=401)
        if request.method != 'GET':
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '来源不匹配'}, status_code=403)
    return await call_next(request)


class Login(BaseModel):
    token: str


@app.post('/api/login')
async def login(body: Login, request: Request):
    if not secrets.compare_digest(body.token, TOKEN):
        raise HTTPException(401, '访问口令错误')
    session = secrets.token_urlsafe(32)
    sessions.add(session)
    response = JSONResponse({'ok': True})
    response.set_cookie('muse_session', session, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https')
    return response


@app.get('/')
async def index():
    return FileResponse(ROOT / 'static/index.html')


@app.get('/demo')
async def fixture():
    return FileResponse(ROOT / 'static/demo.html')


@app.get('/control-test')
async def control_fixture():
    return FileResponse(ROOT / 'static/control-test.html')


@app.get('/api/state')
async def get_state():
    return {**state, 'browser_backend':getattr(browser,'backend','extension'), 'browser_driver':getattr(browser,'status',{}), 'browser_connected': browser.connected,
            'width': browser.width, 'height': browser.height}


class DebugSettings(BaseModel):
    enabled: bool

@app.get('/api/tool-events')
async def get_tool_events(task_id: str | None = None, limit: int = 100):
    if not 1<=limit<=500:raise HTTPException(400,'limit须为1至500')
    store=context_store()
    from .tool_diagnostics import ToolDiagnostics
    ToolDiagnostics(store,record,state.get('task_id'))
    with closing(store.connect()) as db:
        db.row_factory=sqlite3.Row
        params=[state['conversation_id']]
        sql='SELECT * FROM tool_events WHERE session_id=?'
        if task_id:sql+=' AND task_id=?';params.append(task_id)
        sql+=' ORDER BY created_at DESC,rowid DESC LIMIT ?';params.append(limit)
        events=[dict(row) for row in db.execute(sql,params)]
    for event in events:event['detail']=json.loads(event['detail'])
    return {'events':events,'scope':'current_session'}


@app.post('/api/debug')
async def configure_debug(body: DebugSettings):
    debug.enabled=body.enabled
    if not body.enabled:debug.calls=[];debug.events=[]
    publish_debug()
    return state['debug']

@app.post('/api/debug/clear')
async def clear_debug():
    debug.calls=[];debug.events=[]
    publish_debug()
    return state['debug']

@app.post('/api/conversations')
async def create_conversation():
    global history
    if job and not job.done():
        raise HTTPException(409, '请先停止当前任务，再新建会话')
    save()
    history = []
    state.update(conversation_id=secrets.token_hex(16), conversation_title='新会话',task_id=None,human_handoff=None,
                 messages=[], logs=[],activities=[], status='idle', phase=None, phase_detail='', active_tools=[])
    save()
    publish()
    return await get_state()


@app.post('/api/conversations/{conversation_id}/activate')
async def activate_conversation(conversation_id: str):
    if job and not job.done():
        raise HTTPException(409, '请先停止当前任务，再切换会话')
    if conversation_id != state['conversation_id']:
        with closing(database()) as db:
            if not db.execute('SELECT 1 FROM conversations WHERE id=?',(conversation_id,)).fetchone():
                raise HTTPException(404,'会话不存在')
        save()
        with closing(database()) as db:
            load_conversation(db, conversation_id)
        save()
        publish()
    return await get_state()


@app.websocket('/extension/ws')
async def extension_socket(ws: WebSocket):
    # A normal website cannot use the extension control socket, even with a cookie.
    if not re.fullmatch(r'chrome-extension://[a-p]{32}', ws.headers.get('origin', '')):
        await ws.close(code=1008)
        return
    await ws.accept()
    disconnect_detail='连接中断（未收到扩展断开原因）'
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 8)
        token = hello.get('token') if isinstance(hello, dict) else None
        valid_token=isinstance(token,str) and secrets.compare_digest(token,TOKEN)
        if (not isinstance(hello,dict) or hello.get('type')!='hello'
                or not (local_connection(ws) or valid_token)):
            await ws.close(code=1008)
            return
        if browser.connected:
            await ws.close(code=1013, reason='control_connection_busy')
            return
        browser.connection = ws
        state.update(frame=None, frame_error='', browser_connected=True)
        record('浏览器扩展已连接', '按任务自动连接 Chrome 网页')
        await ws.send_json({'type': 'ready'})
        while True:
            message = await ws.receive_json()
            if not isinstance(message, dict):
                raise ValueError('无效消息')
            if message.get('type')=='disconnect':
                disconnect_detail={'manual_disconnect':'用户断开控制','target_closed':'受控标签页已关闭','debugger_detached':'Chrome 调试控制已结束（可能关闭了调试提示或打开了开发者工具）','auth_rejected':'工作台拒绝授权'}.get(message.get('reason'),'扩展主动断开')
                break
            if message.get('type') != 'ping':
                browser.receive(message)
    except WebSocketDisconnect as error:
        disconnect_detail=f'控制连接断开，WebSocket 状态码 {error.code}'
    except (asyncio.TimeoutError, ValueError, RuntimeError) as error:
        disconnect_detail='控制连接异常：'+type(error).__name__
    finally:
        if browser.connection is ws:
            browser.disconnect(ws)
            if job and not job.done():
                job.cancel()
            state.update(frame=None, browser_connected=False,
                         frame_error=disconnect_detail+'；请在 Chrome 中重新连接 Muse 扩展')
            record('浏览器扩展已断开',disconnect_detail)


from .evaluation_center import router as evaluation_router
app.include_router(evaluation_router)

@app.get('/evals')
async def evaluation_page():
    return FileResponse(ROOT / 'static' / 'evals.html')


@app.get('/api/observation')
async def observation(action: Literal['read','list_tabs'] = 'read'):
    if job and not job.done():
        raise HTTPException(409, 'Agent 正在操作，请先停止再检查页面')
    try:
        return await browser.perform(action)
    except (RuntimeError, ConnectionError) as error:
        raise HTTPException(409, str(error)[:200]) from error


@app.get('/api/images/{name}')
async def chat_image(name: str):
    if not re.fullmatch(r'[0-9a-f]{32}\.jpg', name):
        raise HTTPException(404, '图片不存在')
    path = DATA / 'chat-images' / name
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})


@app.post('/api/screenshot')
async def send_screenshot():
    if job and not job.done():
        raise HTTPException(409, '请先停止 Agent，或让 Agent 发送截图')
    if not browser.connected:
        raise HTTPException(409, '请先连接 Chrome 扩展')
    async with browser.lock:
        picture = await browser.screenshot()
    return {'image_url': send_browser_image(picture)}


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=6000)
    demo: bool = False


def context_store():
    store=CheckpointStore(DATA / 'muse.db', state['conversation_id'])
    store.task_id=state.get('task_id')
    return store


@app.get('/api/context/checkpoint')
async def get_context_checkpoint():
    store=context_store()
    store.observe(history)
    from .token_policy import model_policy
    provider=get_provider(provider_id)
    return {'checkpoint':store.latest(),'policy':model_policy(provider.id,provider.model),'usage':store.get_usage(provider.id,provider.model)}


def checkpoint_history(items):
    global history
    history = items
    context_store().observe(items)
    save()


checkpoint_history.store_factory = context_store


def retain_outcome(message):
    history.append({'role':'assistant','content':message})


async def execute(prompt, demo):
    global history
    try:
        if demo:
            async with browser.lock:
                await browser.start()
        if demo:
            set_phase('executing_tool','执行浏览器演示')
            record('演示模式', '固定流程，不调用模型')
            await browser.perform('open', url=f'http://127.0.0.1:{os.getenv("MUSE_PORT", "8000")}/demo')
            record('打开演示网页')
            await asyncio.sleep(1.2)
            result = await browser.perform('read')
            ref = next(e['ref'] for e in result['elements'] if e['tag'] == 'input')
            await browser.perform('fill', ref=ref, text='Muse 浏览器 Demo')
            record('填写搜索框', 'Muse 浏览器 Demo')
            await asyncio.sleep(1.2)
            result = await browser.perform('read')
            ref = next(e['ref'] for e in result['elements'] if e['tag'] == 'button')
            result = await browser.perform('click', ref=ref)
            record('点击搜索并读取结果')
            answer = '演示完成：已打开测试网页、输入内容并点击搜索。右侧显示实际浏览器截图。本次没有调用模型。'
        else:
            record('Agent 开始执行', state['model'])
            answer, new_history = await run_agent(browser, prompt, history[:-1], record,
                                                  get_provider(provider_id), send_browser_image,
                                                  [manager.server for manager in (commander,filesystem) if manager.server], set_phase, checkpoint_history)
            history = new_history
        set_phase('checking_result','保存执行结果与会话上下文')
        state['messages'].append({'role': 'assistant', 'text': answer})
        if demo:
            retain_outcome(answer)
        state['status'] = 'completed'
        record('任务完成')
    except asyncio.CancelledError:
        state['status'] = 'stopped'
        message='任务已停止。本轮已记录的执行进度保留；未返回的工具是否生效未知，继续前请检查实际状态。'
        state['messages'].append({'role':'assistant','text':message})
        retain_outcome(message)
        record('任务已停止', '可以手动接管浏览器')
        raise
    except HumanInterventionRequired as error:
        message='需要你操作：'+str(error)+' 自动操作已暂停，请在右侧画面接管；处理完成后点击“已处理，继续”。'
        state.update(status='needs_human',human_handoff={'reason':str(error),'url':browser.url,'task_id':state.get('task_id')})
        state['messages'].append({'role':'assistant','text':message})
        retain_outcome(message)
        record('等待人工操作',str(error))
    except LoopStopped as error:
        state['status']='stopped'
        state['messages'].append({'role':'assistant','text':str(error)})
        retain_outcome(str(error))
        record('失败上限触发，任务已结束',str(error))
    except Exception as error:
        state['status'] = 'error'
        # Avoid leaking provider error bodies or credentials into the UI.
        record('执行失败', type(error).__name__)
        state['messages'].append({'role': 'assistant', 'text': (
            '达到本轮操作上限，已停止。之前保存的页面修改仍保留；请查看执行记录确认进度。'
            if type(error).__name__ == 'MaxTurnsExceeded' else
            '执行失败。请检查模型配置、网络和浏览器环境。错误类型：' + type(error).__name__)})
        retain_outcome(state['messages'][-1]['text'])
    finally:
        history=close_pending_tools(history)
        checkpoint_history(history)
        state.update(phase=None,phase_detail='',active_tools=[])
        state['messages'] = state['messages'][-40:]
        save()
        publish()


TASK_TIMEOUT_SECONDS = 1800


async def execute_with_deadline(prompt, demo):
    try:
        await asyncio.wait_for(execute(prompt, demo), timeout=TASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        state['status'] = 'stopped'
        message = '任务总处理时长达到30分钟，已停止继续执行。之前保存的修改和文件仍保留。'
        state['messages'].append({'role':'assistant','text':message})
        state['messages'] = state['messages'][-40:]
        retain_outcome(message)
        record('任务总时长超限', '30分钟上限已触发')


@app.post('/api/tasks', status_code=202)
async def start_task(body: TaskRequest):
    global job, history
    if job and not job.done():
        raise HTTPException(409, '已有任务正在执行')
    if body.demo and not browser.connected:
        raise HTTPException(409, 'Chrome 连接器尚未连接，请确认扩展已更新并启用自动连接')
    if hasattr(browser,'status') and not browser.status['ready']:
        raise HTTPException(503,browser.status['error'] or 'Playwright MCP 尚未就绪')
    for manager in (commander, filesystem):
        if not body.demo and manager.status['enabled'] and not manager.status['ready']:
            raise HTTPException(503, manager.status['error'] or 'MCP 未就绪')
    provider = get_provider(provider_id)
    if not body.demo and not provider.api_key.strip():
        raise HTTPException(400, f'请在服务端 .env 中配置 {provider.key_variable}，然后重启服务')
    if not body.demo and (not provider.model or not provider.base_url):
        raise HTTPException(400, '请配置模型名称和 API 地址')
    if not body.prompt.strip():
        raise HTTPException(400, '任务不能为空')
    state['task_id']=context_store().message_store().begin_task()
    state['status'] = 'running'
    state['human_handoff']=None
    set_phase('preparing_context','准备上下文与执行计划')
    if not any(item.get('role')=='user' for item in state['messages']):
        state['conversation_title'] = ('浏览器演示' if body.demo else body.prompt.strip().splitlines()[0])[:28]
    state['messages'].append({'role': 'user', 'text': '运行浏览器演示' if body.demo else body.prompt})
    history=close_pending_tools(history)
    history.append({'role':'user','content':'运行浏览器演示' if body.demo else body.prompt})
    save()
    publish()
    job = asyncio.create_task(execute_with_deadline(body.prompt, body.demo))
    return {'ok': True,'task_id':state['task_id']}


class ProviderSelection(BaseModel):
    provider: str


@app.post('/api/resume',status_code=202)
async def resume_human():
    global control_owner
    if state['status']!='needs_human':
        raise HTTPException(409,'当前没有等待人工处理的任务')
    try:
        await browser.perform('read')
    except HumanInterventionRequired as error:
        raise HTTPException(409,'验证尚未完成：'+str(error)) from error
    control_owner=None
    await browser.release_input()
    return await start_task(TaskRequest(prompt='人工操作已完成，继续之前未完成的任务。先重新观察当前页面，确认验证已解除和已有进度，再继续，不重复已保存的修改。'))


@app.post('/api/provider')
async def select_provider(body: ProviderSelection):
    global provider_id, history
    if job and not job.done():
        raise HTTPException(409, '请先停止当前任务再切换模型')
    try:
        provider = get_provider(body.provider)
    except ValueError as error:
        raise HTTPException(400, '无效的模型配置') from error
    if provider_id != provider.id:
        history = []
        provider_id = provider.id
        state.update(provider=provider.id, provider_name=provider.name, model=provider.model,
                     model_ready=bool(provider.api_key.strip()))
        record('切换模型', f'{provider.name} · {provider.model}；模型上下文已重置')
    return provider.public()


@app.post('/api/stop')
async def stop():
    if job and not job.done():
        job.cancel()
        with suppress(asyncio.CancelledError):
            await job
        if state['status']=='running':
            state.update(status='stopped',phase=None,phase_detail='',active_tools=[])
            message='任务在执行开始前已取消，用户输入已保存。'
            state['messages'].append({'role':'assistant','text':message})
            retain_outcome(message)
            record('任务已停止','执行尚未开始或已取消')
        state.update(phase=None,phase_detail='',active_tools=[])
        save()
        publish()
    return {'ok': True}


class Manual(BaseModel):
    action: Literal['open','move','down','up','wheel','click','drag','type','press','scroll']
    button: Literal['left','right','middle'] = 'left'
    dx: float = Field(default=0, allow_inf_nan=False)
    x: float = Field(default=0, allow_inf_nan=False)
    y: float = Field(default=0, allow_inf_nan=False)
    x2: float = Field(default=0, allow_inf_nan=False)
    y2: float = Field(default=0, allow_inf_nan=False)
    text: str = Field(default='', max_length=4000)


@app.post('/api/manual')
async def manual(body: Manual):
    if job and not job.done():
        raise HTTPException(409, '请先停止 Agent 再接管')
    try:
        await browser.manual(body.action, body.x, body.y, body.text, body.x2, body.y2, body.button, body.dx)
    except Exception as error:
        raise HTTPException(400, '操作失败：' + type(error).__name__) from error
    record('手动接管', body.action)
    return {'ok': True}


@app.websocket('/ws')
async def websocket(ws: WebSocket):
    global control_owner
    origin = ws.headers.get('origin')
    expected = ('https' if ws.url.scheme == 'wss' else 'http') + '://' + ws.headers.get('host', '')
    if not authorized(ws) or origin != expected:
        await ws.close(code=1008)
        return
    await ws.accept()
    queue = asyncio.Queue(maxsize=4)
    frames = asyncio.Queue(maxsize=1)
    subscription = (queue, frames)
    clients.add(subscription)
    queue.put_nowait(json.dumps(state, ensure_ascii=False))

    async def respond(message):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(json.dumps(message, ensure_ascii=False))

    async def sender():
        pending = [asyncio.create_task(queue.get()), asyncio.create_task(frames.get())]
        try:
            while True:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for index, task in enumerate(pending):
                    if task in done:
                        await ws.send_text(task.result())
                        pending[index] = asyncio.create_task((queue if index == 0 else frames).get())
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    sending = asyncio.create_task(sender())
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 20000:
                await respond({'type':'error','message':'输入内容过长'})
                continue
            try:
                message = json.loads(raw)
                if message.get('type') == 'takeover':
                    if message.get('active') is False:
                        if control_owner is ws:
                            control_owner = None
                            await browser.release_input()
                        await respond({'type':'control','active':False})
                    elif job and not job.done() and state['status']!='needs_human':
                        await respond({'type':'error','message':'请先停止 Agent 再接管'})
                    elif control_owner and control_owner is not ws:
                        await respond({'type':'error','message':'另一页面正在接管，请先在该页面退出接管'})
                    else:
                        control_owner = ws
                        await respond({'type':'control','active':True})
                elif message.get('type') == 'input':
                    if control_owner is not ws or (job and not job.done() and state['status']!='needs_human'):
                        if control_owner is ws:
                            control_owner = None
                        await respond({'type':'control','active':False})
                        continue
                    body = Manual.model_validate(message.get('input', {}))
                    await browser.manual(body.action, body.x, body.y, body.text,
                                         body.x2, body.y2, body.button, body.dx)
                    # Do not write every mouse move or typed character to disk/logs.
                else:
                    await respond({'type':'error','message':'未知消息类型'})
            except Exception as error:
                await respond({'type':'error','message':'操作失败：' + type(error).__name__})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(subscription)
        if control_owner is ws:
            control_owner = None
            await browser.release_input()
        sending.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await sending
