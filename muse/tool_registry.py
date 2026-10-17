"""Source-neutral, transactional registration for external tool definitions."""
import asyncio
import json
import re
import time

from .tool_providers import ToolDefinition
from .tool_enable_cache import ToolEnableCache
from .tool_store import KINDS, ToolStore, canonical_json


class ToolRegistry:
    def __init__(self, store, record=lambda *args:None):
        self.store = store if isinstance(store, ToolStore) else ToolStore(store)
        self.record = record
        self.enabled_cache = ToolEnableCache(self.store)
        self.providers = {}
        self._desired_providers = {}
        self._locks = {}
        self._subscriptions = {}
        self._access_bindings = {}
        self._refresh_tasks = {}
        self._pending = set()
        self._revisions = {}
        self._watch_task = None
        self._closed = False
        self._failure_refresh_tasks = {}
        self._failure_refresh_at = {}

    async def register(self, provider, *, force=False):
        if self._closed:
            raise RuntimeError('Tool registry is closed')
        source_id = provider.source_id
        kind = provider.source_kind
        if kind not in KINDS or not isinstance(source_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,199}',source_id):
            raise ValueError('Unsupported external tool source')
        if not source_id.startswith(kind+':'):
            raise ValueError('Source ID must use its kind namespace')
        lock = self._locks.setdefault(source_id,asyncio.Lock())
        async with lock:
            if not getattr(getattr(provider,'server',None),'_muse_connection_available',True):
                raise ConnectionError('远程 MCP 连接已退役，等待重建')
            existing = self.store.list_tools(source_id)
            if any(row['source_kind']!=kind for row in existing):
                raise ValueError('Registered source kind cannot change')
            if self._desired_providers.get(source_id) is not provider:
                self._desired_providers[source_id]=provider
                self._bind_provider(provider)
            if force and hasattr(provider,'invalidate'):
                provider.invalidate()
            definitions = await provider.discover_tools()
            if not isinstance(definitions,list) or len(definitions)>1000:
                raise ValueError('Provider must return at most 1000 tool definitions')
            rows = []
            names = set()
            for definition in definitions:
                if not isinstance(definition,ToolDefinition):
                    raise ValueError('Provider must return ToolDefinition objects')
                if not isinstance(definition.name,str) or definition.name in names:
                    raise ValueError('Tool names must be unique within a source')
                names.add(definition.name)
                rows.append({'source_id':source_id,'source_kind':kind,'server_id':getattr(provider,'server_id',None),'tool_key':definition.key,
                             'name':definition.name,'description':definition.description,
                             'parameters':definition.parameters})
            # Copy the full discovered snapshot before committing; executable objects
            # and source credentials never enter the persistence layer.
            payload = canonical_json(rows)
            if len(payload.encode('utf-8')) > 2*1024*1024:
                raise ValueError('Discovered tool definitions exceed 2 MiB')
            snapshot = self.store.reconcile_source(source_id,kind,json.loads(payload))
            registered = snapshot['tools']
            if any(row['changed'] for row in registered) or snapshot['removed'] or snapshot['restored']:
                self.enabled_cache.sync_source(source_id,snapshot['permissions'])
            self.providers[source_id] = provider
            self._bind_provider(provider,bind_access=True)
            return {'source_id':source_id,'server_id':getattr(provider,'server_id',None),'registered':len(registered),
                    'changed':sum(row['changed'] for row in registered),'removed':snapshot['removed'],
                    'restored':snapshot['restored'],'tools':registered}

    async def unregister_source(self,source_id):
        """Remove executors and tombstone definitions, retaining IDs/audit history."""
        async with self._locks.setdefault(source_id,asyncio.Lock()):
            for providers in (self.providers,self._desired_providers):
                provider=providers.pop(source_id,None)
                server=getattr(provider,'server',None)
                if server is not None:server._muse_connection_available=False
            unsubscribe=self._subscriptions.pop(source_id,None)
            if unsubscribe:unsubscribe()
            for key in tuple(self._access_bindings):
                if key[0]==source_id:self._access_bindings.pop(key)()
            self._pending.discard(source_id)
            self._revisions.pop(source_id,None)
            result=self.store.reconcile_source(source_id,'mcp',[])
            self.enabled_cache.sync_source(source_id,result['permissions'])
            return result['removed']

    async def register_all(self, providers):
        providers = list(providers)
        identities = [provider.source_id for provider in providers]
        if len(set(identities))!=len(identities):
            raise ValueError('Duplicate tool source in registration batch')
        result = {'registered':0,'changed':0,'sources':{},'errors':[]}
        for provider in providers:
            try:
                row = await self.register(provider)
            except Exception:
                # Source identifiers are the only error detail sent to UI/logs;
                # URLs, headers and arbitrary exception contents may hold secrets.
                label = provider.source_id if re.fullmatch(r'[A-Za-z0-9:._-]{1,200}',str(provider.source_id)) else 'invalid-source'
                result['errors'].append(label)
                self.record('工具目录注册失败',label+'；原有定义保留')
                continue
            result['registered'] += row['registered']
            result['changed'] += row['changed']
            result['sources'][row['source_id']] = row['registered']
        return result


    def _bind_provider(self, provider, *, bind_access=False):
        source_id=provider.source_id
        unsubscribe=self._subscriptions.pop(source_id,None)
        if unsubscribe:unsubscribe()
        cleanups=[]
        if hasattr(provider,'subscribe'):
            cleanups.append(provider.subscribe(lambda reason:self.request_refresh(source_id,reason)))
        if bind_access and hasattr(provider,'bind_access'):
            target=getattr(provider,'server',provider)
            binding_key=(source_id,id(target))
            if binding_key not in self._access_bindings:
                def check(key):
                    active=self.providers.get(source_id)
                    if active is None or getattr(active,'server',active) is not target:
                        raise PermissionError('工具来源连接已替换，请重新描述后调用')
                    return self.require_enabled(source_id,key)
                self._access_bindings[binding_key]=provider.bind_access(check)
        if cleanups:
            self._subscriptions[source_id]=lambda:[cleanup() for cleanup in reversed(cleanups)]
        if hasattr(provider,'watch_revision'):
            self._revisions[source_id]=getattr(provider,'revision',None)

    def enabled_states(self):
        return self.enabled_cache.states()

    def set_enabled(self, tool_id, enabled):
        if self._closed:raise RuntimeError('Tool registry is closed')
        return self.enabled_cache.set_enabled(tool_id,enabled)

    def require_enabled(self, source_id, tool_key):
        if self._closed:raise RuntimeError('Tool registry is closed')
        return self.enabled_cache.require(source_id,tool_key)

    def search(self, query, limit=10):
        rows=self.store.search(query,limit,enabled_only=False,tool_ids=self.enabled_cache.active_ids())
        for row in rows:row['enabled']=True
        return rows

    def filter_agent_tools(self, tools, servers):
        """Use SDK source bindings, never model-visible name/prefix guesses."""
        from .tool_providers import mcp_source_id
        names={server.name:mcp_source_id(server) for server in servers}
        result=[]
        for tool in tools:
            binding=getattr(tool,'_mcp_tool_binding',None)
            if binding is None:
                if tool.name.startswith('mcp_'):
                    raise ValueError('MCP tool has no source binding')
                result.append(tool)
                continue
            source_id=names.get(binding[0])
            if source_id is None:
                raise ValueError('MCP tool source is not registered')
            key=binding[1]
            check=lambda sid=source_id,key=key:self.enabled_cache.allows(sid,key)
            tool._muse_enabled_check=check
            if check():result.append(tool)
        return result

    def has_source(self, source_id):
        return source_id in self._desired_providers

    async def refresh(self, source_id):
        provider=self._desired_providers.get(source_id) or self.providers.get(source_id)
        if provider is None:
            raise KeyError(source_id)
        return await self.register(provider,force=True)

    async def rediscover_on_failure(self, source_id):
        """At most one bounded discovery per source per five seconds; no business RPC."""
        if self._closed or not self.has_source(source_id):return 'unavailable'
        task=self._failure_refresh_tasks.get(source_id)
        if task is not None:
            return await asyncio.shield(task)
        if time.monotonic()-self._failure_refresh_at.get(source_id,float('-inf'))<5:
            return 'throttled'
        self._failure_refresh_at[source_id]=time.monotonic()
        async def discover():
            try:
                async with asyncio.timeout(5):
                    await self.refresh(source_id)
                status='refreshed'
            except Exception:
                status='failed'
            self.record('调用失败后的工具重新发现',source_id+'；'+status+'；未重试业务调用')
            return status
        task=asyncio.create_task(discover())
        self._failure_refresh_tasks[source_id]=task
        task.add_done_callback(lambda done:self._failure_refresh_tasks.pop(source_id,None))
        return await asyncio.shield(task)

    async def refresh_all(self):
        result={'registered':0,'changed':0,'removed':0,'restored':0,'sources':{},'errors':[]}
        for source_id in tuple(self._desired_providers):
            try:
                row=await self.refresh(source_id)
            except Exception:
                result['errors'].append(source_id)
                self.record('工具目录刷新失败',source_id+'；原有定义保留')
                continue
            for key in ('registered','changed','removed','restored'):
                result[key]+=row[key]
            result['sources'][source_id]=row['registered']
        return result

    def request_refresh(self, source_id, reason='manual'):
        if self._closed or source_id not in self._desired_providers:
            return
        self._pending.add(source_id)
        if source_id not in self._refresh_tasks:
            task=asyncio.create_task(self._drain_refresh(source_id))
            self._refresh_tasks[source_id]=task

    async def _drain_refresh(self, source_id):
        try:
            # Coalesce rapid notifications; never list tools inside the RPC handler.
            await asyncio.sleep(.05)
            while source_id in self._pending and not self._closed:
                self._pending.discard(source_id)
                try:
                    row=await self.refresh(source_id)
                    self.record('工具目录已刷新',source_id+f"；变更{row['changed']}，移除{row['removed']}，恢复{row['restored']}")
                except Exception:
                    self.record('工具目录刷新失败',source_id+'；原有定义保留')
        finally:
            self._refresh_tasks.pop(source_id,None)

    async def wait_for_refresh(self):
        while self._refresh_tasks:
            await asyncio.gather(*tuple(self._refresh_tasks.values()))

    async def start_watching(self, interval=1.0):
        if self._closed:
            raise RuntimeError('Tool registry is closed')
        if not .01<=interval<=60:
            raise ValueError('Watch interval must be .01–60 seconds')
        if self._watch_task is None:
            self._watch_task=asyncio.create_task(self._watch_files(interval))

    async def _watch_files(self, interval):
        while True:
            await asyncio.sleep(interval)
            for source_id,provider in tuple(self._desired_providers.items()):
                if not hasattr(provider,'watch_revision'):
                    continue
                revision=await provider.watch_revision()
                if revision!=self._revisions.get(source_id):
                    self._revisions[source_id]=revision
                    self.request_refresh(source_id,'file_changed')

    async def close(self):
        self._closed=True
        for unsubscribe in self._subscriptions.values():unsubscribe()
        self._subscriptions.clear()
        for unbind in self._access_bindings.values():unbind()
        self._access_bindings.clear()
        tasks=list(self._refresh_tasks.values())+list(self._failure_refresh_tasks.values())+([self._watch_task] if self._watch_task else [])
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self._refresh_tasks.clear()
        self._pending.clear()
        self._watch_task=None
