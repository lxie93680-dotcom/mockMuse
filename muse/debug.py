"""Opt-in, bounded model-call diagnostics. Never stores client credentials."""
import dataclasses
import inspect
import secrets
import time
import json
from contextvars import ContextVar

active_http_call=ContextVar('muse_debug_http_call',default=None)

SECRET_KEYS={'api_key','authorization','access_token','refresh_token','token','password'}

def clean(value,depth=0):
    if depth>12:return '[深度限制]'
    if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
    elif dataclasses.is_dataclass(value):value={field.name:getattr(value,field.name) for field in dataclasses.fields(value)}
    if isinstance(value,dict):return {str(k):'[已隐藏]' if str(k).lower() in SECRET_KEYS else clean(v,depth+1) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v,depth+1) for v in value[:150]]
    if isinstance(value,str):
        if value.startswith('data:image/'):return '[图片数据已省略]'
        return value[:12000]+('…[截断]' if len(value)>12000 else '')
    if value is None or isinstance(value,(int,float,bool)):return value
    return str(value)[:2000]

class Debug:
    def __init__(self):self.enabled=False;self.calls=[];self.events=[];self.notify=lambda:None
    def event(self,source,detail,error=False):
        if not self.enabled:return
        from .tool_diagnostics import sanitized
        self.events.append({'time':time.time(),'source':source,'error':error,'detail':sanitized(detail)})
        self.events=self.events[-200:];self.notify()

    def begin(self,model,args,kwargs):
        if not self.enabled:return None
        from agents import Model
        try:
            bound=inspect.signature(Model.get_response).bind_partial(model,*args,**kwargs)
            request={k:v for k,v in bound.arguments.items() if k!='self'}
        except TypeError:request={'args':args,**kwargs}
        # Tools contain callables; expose schemas instead of implementation objects.
        if 'tools' in request:request['tools']=[{'name':getattr(t,'name',type(t).__name__),'description':getattr(t,'description',''),'parameters':getattr(t,'params_json_schema',{})} for t in request['tools']]
        call={'id':secrets.token_hex(8),'started':time.time(),'status':'running','request':None,'sdk_request':clean(request),'request_source':'pending_http','response':None}
        self.calls.append(call);self.calls=self.calls[-30:];self.notify();return call
    def end(self,call,result=None,error=None):
        if call is None or not self.enabled or not any(c is call for c in self.calls):return
        if error:
            from .tool_diagnostics import safe_text
            call['error']={'type':type(error).__name__,'message':safe_text(error)}
        call.update(status='error' if error else 'completed',duration=round(time.time()-call['started'],3),response=call.get('http_response',clean(result)) if not error else call.get('http_response',{'error_type':type(error).__name__}))
        self.notify()

debug=Debug()


def debug_http_client(diagnostics=None,transport=None):
    """Capture the actual serialized HTTP body, never authentication headers."""
    import httpx2 as httpx
    d=diagnostics or debug
    async def request_hook(request):
        call=active_http_call.get()
        if not d.enabled or call is None or not any(c is call for c in d.calls):return
        try:
            body=json.loads(await request.aread())
        except (ValueError,UnicodeError):return
        call.update(request=clean(body),request_source='http_json',http_method=request.method,
            http_url=str(request.url.copy_with(query=None)),display_limits='已隐藏凭据字段及图片，单文本最多12000字符；这是实际HTTP JSON的脱敏展示。')
        d.notify()
    async def response_hook(response):
        call=active_http_call.get()
        if not d.enabled or call is None or not any(c is call for c in d.calls):return
        call['http_status']=response.status_code
        if 'application/json' not in response.headers.get('content-type',''):return
        try:
            body=json.loads(await response.aread())
        except (ValueError,UnicodeError):return
        call['http_response']=clean(body);d.notify()
    return httpx.AsyncClient(transport=transport,event_hooks={'request':[request_hook],'response':[response_hook]})
