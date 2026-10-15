"""Per-task model budget and consecutive identical tool-error detection."""
import asyncio
import hashlib
import json
import os
import time
from .model_errors import model_failure
from .tool_diagnostics import safe_text,record_model_error
from contextvars import ContextVar
from .phases import Phases
from .debug import debug,active_http_call
from agents import Model
from agents.tool import set_function_tool_failure_error_function

active_guard=ContextVar('muse_loop_guard',default=None)

class LoopStopped(RuntimeError):
    pass


def normalize(value):
    if hasattr(value,'model_dump'):
        value=value.model_dump(mode='json',exclude_none=True)
    if isinstance(value,str):
        try: return normalize(json.loads(value))
        except (ValueError,TypeError): return value.replace('\r\n','\n').replace('\r','\n').strip()
    if isinstance(value,dict):return {str(k):normalize(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [normalize(v) for v in value]
    return value


def fingerprint(name,arguments,result):
    value=json.dumps([name,normalize(arguments),normalize(result)],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(value.encode()).hexdigest()

class LoopGuard:
    def __init__(self,record,phase_changed=None,checkpoint=None):
        self.record=record
        self.result_processor=None
        self.diagnostics=None
        self.tool_started_at={}
        self.checkpoint=checkpoint
        self.phases=Phases(phase_changed)
        self.model_failure_reason=None
        self.failures={'tool':0,'model':0}
        self.last_tool_error_key=None;self.pending=[];self.stopped=None
    def check(self):
        if self.stopped:raise LoopStopped(self.stopped)
    def failure(self,kind):
        self.check();self.failures[kind]+=1
        self.record('调用失败计数',f'{kind}: {self.failures[kind]}/3')
        if self.failures[kind]>=3:
            self.stopped=('模型无法使用：'+self.model_failure_reason+' 本轮任务已结束，已保存的进度保留。' if kind=='model' and self.model_failure_reason else ('工具' if kind=='tool' else '模型')+'调用累计失败达到3次，本轮任务已结束。已保存的修改保留。')
            raise LoopStopped(self.stopped)
    def observe(self,name,arguments,result):
        self.check()
        value=normalize(result)
        failed=isinstance(value,dict) and (
            value.get('_muse_tool_error') or value.get('is_error') or value.get('isError')
            or value.get('timed_out') or isinstance(value.get('exit_code'),int) and value['exit_code']!=0
            or value.get('status')=='needs_observation' and value.get('action_performed') is False)
        if not failed:
            self.last_tool_error_key=None;self.failures['tool']=0
            return
        # SDK correlation ids differ on each invocation; they are not error content.
        error_result=dict(value)
        if error_result.get('_muse_tool_error'):
            error_result.pop('tool_call_id',None)
        key=fingerprint(name,arguments,error_result)
        self.failures['tool']=self.failures['tool']+1 if key==self.last_tool_error_key else 1
        self.last_tool_error_key=key
        count=self.failures['tool']
        self.record('重复工具报错计数',f'{name}：名称、入参、错误返回值相同，连续 {count}/3 次')
        if count>=3:
            self.stopped=f'工具 {name} 的名称、入参和错误返回值相同，连续失败达到3次，本轮任务已结束。已保存的修改保留。'
            self.record('重复工具报错上限触发',self.stopped)
            raise LoopStopped(self.stopped)
    def filter(self,data,image_filter):
        self.phases.set('preparing_context', '整理上下文和工具结果')
        if self.checkpoint:
            self.checkpoint.model_input(data.model_data.input)
        result=image_filter(data)
        if self.pending:
            warnings='\n'.join(self.pending);self.pending.clear()
            result.instructions=(result.instructions or '')+'\n'+warnings
        return result
    def wrap(self,tool):
        original=tool.on_invoke_tool
        tool._muse_original_invoke=original
        def on_error(ctx,error):
            if isinstance(error,LoopStopped):raise error
            call_id=getattr(ctx,'tool_call_id',None) or str(id(ctx))
            duration=time.monotonic()-self.tool_started_at.get(call_id,time.monotonic())
            arguments=getattr(ctx,'tool_arguments',None)
            if self.diagnostics:self.diagnostics.emit('error',tool.name,call_id,arguments,error=error,duration=duration)
            else:self.record('工具原始错误',tool.name+' · '+type(error).__name__+': '+safe_text(error))
            output=json.dumps({'_muse_tool_error':True,'error_type':type(error).__name__,'message':safe_text(error),'tool_name':tool.name,'tool_call_id':call_id},ensure_ascii=False)
            # Persist evidence BEFORE the third failure aborts the SDK invocation.
            processed=self.result_processor.process(tool.name,arguments,output,call_id) if self.result_processor else output
            if self.checkpoint:self.checkpoint.tool_output(call_id,processed)
            self.observe(tool.name,arguments,json.loads(output))
            return processed
        set_function_tool_failure_error_function(tool,on_error)
        async def invoke(ctx,arguments):
            self.check()
            call_id=getattr(ctx,'tool_call_id',None) or str(id(ctx))
            started=time.monotonic()
            self.tool_started_at[call_id]=started
            if self.diagnostics:self.diagnostics.emit('start',tool.name,call_id,arguments)
            self.phases.tool_started(call_id,tool.name)
            from .browser_diagnostics import trace_context
            trace_token=trace_context.set((self.diagnostics,tool.name,call_id) if self.diagnostics else None)
            try:
                raw_result=await original(ctx,arguments)
                result=self.result_processor.process(tool.name,arguments,raw_result,call_id) if self.result_processor else raw_result
                if self.checkpoint and not (isinstance(normalize(raw_result),dict) and normalize(raw_result).get('_muse_tool_error')):
                    self.checkpoint.tool_output(call_id,result)
                if self.diagnostics and not (isinstance(normalize(raw_result),dict) and normalize(raw_result).get('_muse_tool_error')):
                    self.diagnostics.emit('completed',tool.name,call_id,arguments,result=raw_result,duration=time.monotonic()-self.tool_started_at[call_id])
            except asyncio.CancelledError:
                if self.diagnostics:self.diagnostics.emit('cancelled',tool.name,call_id,arguments,duration=time.monotonic()-self.tool_started_at[call_id])
                raise
            finally:
                trace_context.reset(trace_token)
                self.phases.tool_finished(call_id)
                self.tool_started_at.pop(call_id,None)
            value=normalize(raw_result)
            if isinstance(value,dict) and not value.get('_muse_tool_error') and (value.get('timed_out') or isinstance(value.get('exit_code'),int) and value['exit_code']!=0):
                if self.diagnostics:self.diagnostics.emit('invalid_result',tool.name,call_id,arguments,result=raw_result,duration=time.monotonic()-started)
            if not (isinstance(value,dict) and value.get('_muse_tool_error')):
                self.observe(tool.name,arguments,raw_result)
            return result
        tool.on_invoke_tool=invoke

class GuardedModel(Model):
    def __init__(self,model,guard,usage_observer=None):self.model=model;self.guard=guard;self.usage_observer=usage_observer;self.identity=None
    async def get_response(self,*args,**kwargs):
        request_id=self.identity.request_started() if self.identity else None
        retry_index=0
        if request_id:self.guard.record('模型请求',request_id)
        while True:
            self.guard.check()
            self.guard.phases.set('replying','等待模型响应，包含模型推理时间')
            call=debug.begin(self.model,args,kwargs)
            if call is not None:call.update(api_request_id=request_id,retry_index=retry_index,task_id=getattr(self.guard,'task_id',None))
            http_token=active_http_call.set(call)
            try:
                result=await self.model.get_response(*args,**kwargs)
            except asyncio.CancelledError as error:
                debug.end(call,error=error)
                raise
            except LoopStopped as error:
                debug.end(call,error=error)
                raise
            except Exception as error:
                debug.end(call,error=error)
                record_model_error(self.guard,self.identity,error,request_id,retry_index)
                reason,permanent=model_failure(error)
                self.guard.model_failure_reason=reason
                if permanent:
                    self.guard.failures['model']+=1
                    self.guard.stopped='模型无法使用：'+reason+' 本轮任务已结束，已保存的进度保留。'
                    self.guard.record('模型无法使用',self.guard.stopped)
                    raise LoopStopped(self.guard.stopped) from None
                self.guard.failure('model')
                retry_index+=1
                if self.identity:self.identity.retry(request_id,retry_index)
                continue
            finally:
                active_http_call.reset(http_token)
            debug.end(call,result=result)
            if self.identity:self.identity.response_received(request_id)
            if self.usage_observer:
                import inspect
                bound=inspect.signature(self.model.get_response).bind(*args,**kwargs)
                self.usage_observer(result,bound.arguments.get('input',[]),bound.arguments.get('system_instructions'))
            if not self.usage_observer and getattr(getattr(result,'usage',None),'requests',0):
                usage=result.usage
                self.guard.record('模型实际Token用量',f'输入 {usage.input_tokens}，输出 {usage.output_tokens}（供应商返回）')
            if self.guard.checkpoint:
                self.guard.checkpoint.model_output(result)
            self.guard.phases.set('checking_result','检查模型返回和下一步调用')
            return result
    async def stream_response(self,*args,**kwargs):
        self.guard.check()
        try:
            async for event in self.model.stream_response(*args,**kwargs):yield event
        except Exception:
            self.guard.failure('model');raise


def max_turns():
    value=int(os.getenv('MUSE_MAX_TURNS','100'))
    if not 1<=value<=1000:raise ValueError('MUSE_MAX_TURNS必须为1到1000')
    return value
