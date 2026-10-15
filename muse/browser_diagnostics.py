"""Correlated MCP boundary evidence; does not infer unobservable browser internals."""
import asyncio
import contextvars
import time
import uuid
from .tool_diagnostics import sanitized

trace_context = contextvars.ContextVar('browser_trace', default=None)
background_trace = contextvars.ContextVar('browser_background_trace', default=False)

async def traced_call(browser, name, arguments):
    trace_id = uuid.uuid4().hex
    started = time.monotonic()
    context = trace_context.get()
    if not hasattr(browser,'rpc_contexts'):browser.rpc_contexts={}
    browser.rpc_contexts[trace_id]=context
    if len(browser.rpc_contexts)>300:browser.rpc_contexts.pop(next(iter(browser.rpc_contexts)))
    arguments={**arguments,'_meta':{**arguments.get('_meta',{}),'muse_trace_id':trace_id,'muse_background':background_trace.get()}}
    def emit(stage, **extra):
        evidence = {'trace_id':trace_id,'stage':stage,'mcp_tool':name,
                    'elapsed_ms':round((time.monotonic()-started)*1000),
                    'connection_mode':browser.status.get('connection_mode'),
                    'mcp_ready':browser.server is not None,
                    'extension_last_attached':browser.extension_attached,
                    'owned_browser_exit_code':getattr(browser.process,'returncode',None),
                    'action_timeout_ms':5000,'rpc_timeout_seconds':35,
                    'target':arguments.get('target'), **extra}
        browser.status['last_rpc']=sanitized(evidence)
        if context:
            diagnostics, tool_name, call_id=context
            diagnostics.emit('browser_stage',tool_name,call_id,result=evidence)
    emit('rpc_dispatch')
    async def watchdog():
        while True:
            await asyncio.sleep(5)
            emit('rpc_pending', interpretation='已进入SDK调用，未返回；SDK排队、传输、扩展及页面内部尚无法区分')
    watcher=asyncio.create_task(watchdog())
    try:
        result=await browser.server.call_tool(name,arguments)
        texts='\n'.join(c.text for c in result.content if hasattr(c,'text'))
        failed=getattr(result,'is_error',getattr(result,'isError',False))
        emit('rpc_error_result' if failed else 'rpc_returned', response_chars=len(texts),
             error_evidence=texts if failed else None)
        return result
    except asyncio.CancelledError:
        emit('rpc_cancelled');raise
    except Exception as error:
        emit('rpc_exception',error_type=type(error).__name__,error_message=str(error),
             interpretation='请求未正常返回；不能仅凭超时认定扩展断连')
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)

from contextlib import asynccontextmanager

@asynccontextmanager
async def traced_lock(browser):
    context=trace_context.get()
    started=time.monotonic()
    def emit(stage):
        if context:
            diagnostics,name,call_id=context
            diagnostics.emit('browser_stage',name,call_id,result={'stage':stage,
                'elapsed_ms':round((time.monotonic()-started)*1000),
                'lock_locked':browser.lock.locked()})
    emit('browser_lock_wait')
    async def watch():
        while True:
            await asyncio.sleep(5);emit('browser_lock_pending')
    watcher=asyncio.create_task(watch())
    acquired=False
    try:
        await browser.lock.acquire();acquired=True;emit('browser_lock_acquired')
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        yield
    finally:
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
        if acquired:browser.lock.release();emit('browser_lock_released')
