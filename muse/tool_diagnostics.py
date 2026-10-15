"""Durable tool events with sanitized parameters and exception evidence."""
import json
import os
import re
import time
import uuid
import traceback
from contextlib import closing
from .debug import clean


def safe_text(value):
    text=str(value)
    for key,value in os.environ.items():
        if any(word in key.upper() for word in ('API_KEY','TOKEN','PASSWORD','SECRET')) and len(value)>=8:
            text=text.replace(value,'[已隐藏]')
    text=re.sub(r'(?i)Bearer\s+[A-Za-z0-9._\-]+','Bearer [已隐藏]',text)
    text=re.sub(r'''(?i)(api[_-]?key|password|access_token|refresh_token|authorization|cookie|playwright_mcp_extension_token)(["']?\s*[:=]\s*["']?)[^\s,"'}&]+''',r'\1\2[已隐藏]',text)
    return text[:2000]+('…[日志截断]' if len(text)>2000 else '')


def sanitized(value):
    value=clean(value)
    if isinstance(value,str):return safe_text(value)
    if isinstance(value,list):return [sanitized(v) for v in value]
    if isinstance(value,dict):return {k:sanitized(v) for k,v in value.items()}
    return value


class ToolDiagnostics:
    def __init__(self,store,record,task_id=None):
        self.store=store;self.record=record;self.task_id=task_id or getattr(store,'task_id',None)
        with closing(store.connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_events (
                event_id TEXT PRIMARY KEY,session_id TEXT,task_id TEXT,turn_id TEXT,api_request_id TEXT,
                tool_call_id TEXT,tool_name TEXT,event_type TEXT,created_at REAL,duration_ms INTEGER,detail TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS tool_events_task ON tool_events(task_id,created_at)');db.commit()

    def emit(self,kind,name,call_id,arguments=None,result=None,error=None,duration=None):
        try:params=json.loads(arguments) if isinstance(arguments,str) else arguments
        except (ValueError,TypeError):params=arguments
        detail={'arguments':sanitized(params)}
        if kind=='browser_stage':detail['browser']=sanitized(result)
        if result is not None:
            text=result if isinstance(result,str) else json.dumps(clean(result),ensure_ascii=False)
            detail['result_preview']=safe_text(text[:1000]);detail['result_chars']=len(text)
        if error is not None:
            detail.update(error_type=type(error).__name__,error_message=safe_text(error),
                stack=[{'file':frame.filename,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(error.__traceback__)[-8:]])
        with closing(self.store.connect()) as db:
            identity=db.execute('SELECT current_turn_id,current_api_request_id FROM task_runs WHERE task_id=?',(self.task_id,)).fetchone()
            db.execute('INSERT INTO tool_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',('event_'+uuid.uuid4().hex,self.store.session_id,self.task_id,*(identity or (None,None)),call_id,name,kind,time.time(),round(duration*1000) if duration is not None else None,json.dumps(detail,ensure_ascii=False)));db.commit()
        from .debug import debug
        debug.event('tool',{'tool':name,'call_id':call_id,'event':kind,**detail},error=error is not None or kind=='invalid_result' or bool(isinstance(result,dict) and (result.get('error') or 'exception' in result.get('stage','') or 'error' in result.get('stage',''))))
        summary=f'{name} · call={call_id}'
        if duration is not None:summary+=f' · {duration:.2f}秒'
        if error is not None:summary+=' · '+detail['error_type']+': '+detail['error_message']
        if kind=='start':summary+=' · '+json.dumps(detail['arguments'],ensure_ascii=False)[:500]
        self.record({'start':'工具调用开始','completed':'工具调用完成','error':'工具原始错误','cancelled':'工具调用取消','invalid_result':'工具返回失败'}.get(kind,kind),summary)


def record_model_error(guard,identity,error,request_id,retry_index):
    """Persist provider/local model failures before the cumulative limit fires."""
    store=getattr(identity,'store',None) or getattr(getattr(guard,'diagnostics',None),'store',None)
    detail={'error_type':type(error).__name__,'message':safe_text(error),
            'http_status':getattr(error,'status_code',None),'provider_request_id':getattr(error,'request_id',None),
            'provider_body':sanitized(getattr(error,'body',None)),
            'retry_index':retry_index,'api_request_id':request_id}
    if store:
        with closing(store.connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS model_errors (
                event_id TEXT PRIMARY KEY,session_id TEXT,task_id TEXT,api_request_id TEXT,
                retry_index INTEGER,created_at REAL,detail TEXT)''')
            db.execute('INSERT INTO model_errors VALUES (?,?,?,?,?,?,?)',('event_'+uuid.uuid4().hex,store.session_id,getattr(guard,'task_id',None),request_id,retry_index,time.time(),json.dumps(detail,ensure_ascii=False)));db.commit()
    guard.record('模型原始错误',f"{request_id or '模型请求'} · 尝试{retry_index+1} · {detail['error_type']} · HTTP {detail['http_status']} · {detail['message']}")
    return detail
