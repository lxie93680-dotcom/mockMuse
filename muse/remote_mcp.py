"""Persist remote MCP definitions; keep each SDK connection in its owning task."""
import asyncio
import json
import os
import re
import time
import tempfile
import httpx2
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from agents.mcp import MCPServerStreamableHttp
from .tool_providers import MCPProvider, mcp_source_id
from .tool_failures import classify


def connection_failure(error):
    if classify(error) in ('connection','timeout'):return True
    status=getattr(getattr(error,'response',None),'status_code',None)
    if status in (401,403,404,410) or isinstance(status,int) and status>=500:return True
    return any(connection_failure(child) for child in getattr(error,'exceptions',()))


class RemoteMCPManager:
    def __init__(self, path, registry):
        self.path = Path(path)
        self.registry = registry
        self.entries = {}
        self.servers = {}
        self.tasks = {}
        self.stops = {}
        self.status = {}
        self.lock = asyncio.Lock()
        self.wakes = {}
        self.waiters = {}
        self.connect_locks = {}
        self.closed = False
        self.retry_base = 30.0
        self.retry_max = 600.0
        self.probe_interval = 30.0

    def listing(self):
        return [{'id':key, 'url':entry['url'], 'token_env':entry.get('token_env',''),
                 **self.status.get(key, {'ready':False, 'error':''})}
                for key,entry in self.entries.items()]

    @staticmethod
    def validate(key, url, token_env=''):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',key):
            raise ValueError('名称限 1–64 位字母、数字、下划线或短横线')
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment):
            raise ValueError('请填写不含账号密码和片段的 HTTPS MCP 地址')
        if parsed.query:
            raise ValueError('请勿在 URL 中填写凭据或查询参数')
        if token_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',token_env):
            raise ValueError('Token 环境变量名无效')

    async def start(self):
        if self.path.exists():
            data=json.loads(self.path.read_text(encoding='utf-8'))
            for key,entry in data.items():
                self.validate(key,entry['url'],entry.get('token_env',''))
            self.entries=data
        for key in self.entries:self.registry.store.bind_server_id(self.source_id(key),key)
        configured={self.source_id(key) for key in self.entries}
        # Complete removals interrupted after the config commit but before DB cleanup.
        for source in {row['source_id'] for row in self.registry.store.list_tools()}:
            if source.startswith('mcp:Remote-') and source not in configured:
                await self.registry.unregister_source(source)
        for key in self.entries:
            await self.connect(key)

    async def add(self, key, url, token_env=''):
        self.validate(key,url,token_env)
        async with self.lock:
            if key in self.entries:
                raise ValueError('该名称已存在，请使用其他名称')
            entries={**self.entries,key:{'url':url,'token_env':token_env}}
            self._save(entries)
            self.entries=entries
            await self._connect(key, force=False)
        return next(row for row in self.listing() if row['id']==key)

    @staticmethod
    def source_id(key):
        return mcp_source_id(SimpleNamespace(name='Remote-'+key))

    def _save(self,entries):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix=self.path.name+'.',suffix='.tmp',dir=self.path.parent)
        temporary=Path(name)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(entries,stream,ensure_ascii=False,indent=2)
                stream.flush();os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def remove(self,key):
        self.validate(key,'https://placeholder.invalid/mcp')
        async with self.lock:
            if self.closed:raise RuntimeError('MCP manager closed')
            existed=key in self.entries
            entries={name:entry for name,entry in self.entries.items() if name!=key}
            self._save(entries)  # Failure leaves the live connection untouched.
            self.entries=entries
            server=self.servers.get(key)
            if server:server._muse_connection_available=False
            if key in self.stops:
                self.stops[key].set();self.wakes[key].set()
            task=self.tasks.get(key)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            # Wait for pending connect requests to observe removal and finish.
            async with self.connect_locks.setdefault(key,asyncio.Lock()):
                removed=await self.registry.unregister_source(self.source_id(key))
                self._complete_attempt(key)
                for mapping in (self.servers,self.tasks,self.stops,self.wakes,self.status):mapping.pop(key,None)
            self.registry.record('远程 MCP 已卸载',key+'；配置及可调用工具已移除')
            return {'id':key,'removed':existed,'removed_tools':removed}

    async def connect(self, key):
        """Explicit retries always replace an existing session, not just its cache."""
        await self._connect(key, force=True)

    async def _connect(self, key, *, force):
        if self.closed:raise RuntimeError('MCP manager closed')
        if key not in self.entries:raise KeyError(key)
        async with self.connect_locks.setdefault(key,asyncio.Lock()):
            if key not in self.entries:raise KeyError(key)
            ready=asyncio.get_running_loop().create_future()
            self.waiters.setdefault(key,[]).append(ready)
            task=self.tasks.get(key)
            if task is None or task.done():
                self.wakes[key]=asyncio.Event()
                self.stops[key]=asyncio.Event()
                self.tasks[key]=asyncio.create_task(self._run(key))
            elif force:
                self.status[key]={**self.status.get(key,{}),'ready':False,'state':'reconnecting','error':'正在重建连接','next_retry_at':None}
                old=self.servers.get(key)
                if old:old._muse_connection_available=False
                self.wakes[key].set()
            await asyncio.shield(ready)

    def _complete_attempt(self,key,waiters=None):
        for future in self.waiters.pop(key,[]) if waiters is None else waiters:
            if not future.done():future.set_result(None)

    def _signal_failure(self,key,server):
        if self.closed or not getattr(server,'_muse_connection_available',False):return
        server._muse_connection_available=False
        self.status[key]={**self.status.get(key,{}),'ready':False,'state':'reconnecting',
                          'error':'连接不可用，正在重建','next_retry_at':None}
        self.wakes[key].set()

    def _client_factory(self,key,server):
        manager=self
        class ObservedClient(httpx2.AsyncClient):
            async def send(self,*args,**kwargs):
                try:return await super().send(*args,**kwargs)
                except httpx2.TransportError:
                    manager._signal_failure(key,server)
                    raise
        async def response_hook(response):
            # GET SSE may be unsupported on otherwise healthy HTTP servers.
            if response.request.method=='POST' and (response.status_code in (401,403,404,410) or response.status_code>=500):
                self._signal_failure(key,server)
        def factory(headers=None,timeout=None,auth=None):
            return ObservedClient(headers=headers,timeout=timeout or httpx2.Timeout(10,read=30),
                                  auth=auth,event_hooks={'response':[response_hook]})
        return factory

    def _wrap(self,key,server):
        server._muse_connection_available=True
        for method in ('list_tools','call_tool'):
            original=getattr(server,method)
            def wrap(original):
                async def checked(*args,**kwargs):
                    if not server._muse_connection_available:
                        raise ConnectionError('远程 MCP 连接正在恢复')
                    try:return await original(*args,**kwargs)
                    except Exception as error:
                        if connection_failure(error):self._signal_failure(key,server)
                        raise
                return checked
            setattr(server,method,wrap(original))

    async def _retire(self,server):
        server._muse_connection_available=False
        source=mcp_source_id(server)
        # Serialize removal with directory refreshes; never drop a replacement.
        async with self.registry._locks.setdefault(source,asyncio.Lock()):
            for providers in (self.registry.providers,self.registry._desired_providers):
                provider=providers.get(source)
                if provider is not None and getattr(provider,'server',None) is server:
                    providers.pop(source,None)
            unsubscribe=self.registry._subscriptions.pop(source,None)
            if unsubscribe:unsubscribe()
            unbind=self.registry._access_bindings.pop((source,id(server)),None)
            if unbind:unbind()
        try:
            async with asyncio.timeout(10):await server.cleanup()
        except Exception:pass

    async def _run(self,key):
        delay=self.retry_base
        wake=self.wakes[key]
        stop=self.stops[key]
        attempt_waiters=[]
        try:
            while not stop.is_set():
                attempt_waiters=self.waiters.pop(key,[])
                wake.clear()
                server=None
                failed=False
                self.status[key]={'ready':False,'state':'connecting','error':'正在连接',
                                  'tool_count':self.status.get(key,{}).get('tool_count',0),'next_retry_at':None}
                try:
                    entry=self.entries[key]
                    headers={}
                    variable=entry.get('token_env','')
                    if variable:
                        token=os.environ.get(variable)
                        if not token:raise ValueError('missing token')
                        headers['Authorization']='Bearer '+token
                    params={'url':entry['url'],'headers':headers,'timeout':10,'sse_read_timeout':30}
                    server=MCPServerStreamableHttp(name='Remote-'+key,
                        params=params,
                        cache_tools_list=True,client_session_timeout_seconds=30,max_retry_attempts=0)
                    params['httpx_client_factory']=self._client_factory(key,server)
                    await server.connect()
                    self._wrap(key,server)
                    result=await self.registry.register(MCPProvider(server,server_id=key))
                    self.servers[key]=server
                    self.status[key]={'ready':True,'state':'connected','error':'',
                                      'tool_count':result['registered'],'next_retry_at':None}
                    delay=self.retry_base
                    self.registry.record('远程 MCP 已连接',key+'；工具目录已绑定')
                    self._complete_attempt(key,attempt_waiters)
                    while not stop.is_set() and not wake.is_set():
                        try:
                            await asyncio.wait_for(wake.wait(),timeout=self.probe_interval)
                        except TimeoutError:
                            # Probe the transport, not the cached list. No business RPC.
                            result=await self.registry.refresh(mcp_source_id(server))
                            self.status[key]['tool_count']=result['registered']
                except Exception:
                    failed=True
                    self.status[key]={**self.status.get(key,{}),'ready':False,'state':'retry_wait',
                        'error':'连接或工具注册失败，将自动重试；请检查地址、凭据和服务状态',
                        'next_retry_at':time.time()+delay}
                    self.registry.record('远程 MCP 等待重连',key+'；'+str(int(delay))+' 秒后重试')
                finally:
                    self.servers.pop(key,None)
                    if server:await self._retire(server)
                    if failed:self._complete_attempt(key,attempt_waiters)
                if stop.is_set():break
                if failed:
                    # A manual retry interrupts backoff; shutdown does too.
                    wake.clear()
                    if self.waiters.get(key):wake.set()
                    try:await asyncio.wait_for(wake.wait(),timeout=delay)
                    except TimeoutError:pass
                    delay=min(delay*2,self.retry_max)
        finally:
            self._complete_attempt(key,attempt_waiters)
            self._complete_attempt(key)
            self.status[key]={**self.status.get(key,{}),'ready':False,'state':'closed','next_retry_at':None}

    async def close(self):
        self.closed=True
        for key,stop in self.stops.items():
            stop.set()
            self.wakes[key].set()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)
