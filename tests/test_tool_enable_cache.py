import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import Agent, RunContextWrapper
from agents.tool_context import ToolContext
from muse.browser_mcp import BrowserMCP
from muse.delegation import child_tools
from muse.loop_guard import LoopGuard
from muse.tool_enable_cache import ENABLED_TTL_SECONDS
from muse.tool_providers import MCPProvider, ToolDefinition
from muse.tool_registry import ToolRegistry


class ToolEnablementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry=ToolRegistry(Path(self.temp.name)/'muse.db')
        self.now=1000.0
        self.registry.enabled_cache.clock=lambda:self.now
        self.provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('a','alpha','searchable tool',{}),ToolDefinition('b','beta','searchable tool',{})]))
        self.rows=(await self.registry.register(self.provider))['tools']
        self.a=self.rows[0]['tool_id'];self.b=self.rows[1]['tool_id']

    async def asyncTearDown(self):
        await self.registry.close()

    async def test_warm_write_through_does_not_reload_or_slide_ttl(self):
        cache=self.registry.enabled_cache
        with patch.object(self.registry.store,'permission_rows',wraps=self.registry.store.permission_rows) as read:
            self.assertTrue(self.registry.enabled_states()[self.a])
            loaded_at=cache.loaded_at
            self.now+=6*24*3600
            self.registry.set_enabled(self.a,False)
            self.assertFalse(self.registry.enabled_states()[self.a])
            self.assertEqual(read.call_count,1)
            self.assertEqual(cache.loaded_at,loaded_at)
            self.assertFalse(self.registry.store.get(self.a)['enabled'])
            self.assertEqual(cache.expires_in(),24*3600)
            self.now=loaded_at+ENABLED_TTL_SECONDS
            self.assertFalse(self.registry.enabled_states()[self.a])
            self.assertEqual(read.call_count,2)
            self.assertEqual(cache.loaded_at,self.now)
            self.assertTrue(self.registry.enabled_states()[self.b])
            self.assertEqual(read.call_count,2)

    async def test_external_db_change_is_seen_on_fixed_expiry(self):
        self.assertTrue(self.registry.enabled_states()[self.a])
        self.registry.store.set_enabled(self.a,False)
        self.now+=ENABLED_TTL_SECONDS-1
        self.assertTrue(self.registry.enabled_states()[self.a])
        self.now+=1
        self.assertFalse(self.registry.enabled_states()[self.a])

    async def test_failed_db_write_does_not_change_cache(self):
        self.registry.enabled_states()
        loaded_at=self.registry.enabled_cache.loaded_at
        with patch.object(self.registry.store,'set_enabled',side_effect=sqlite3.OperationalError('simulated write failure')):
            with self.assertRaises(sqlite3.OperationalError):self.registry.set_enabled(self.a,False)
        self.assertTrue(self.registry.enabled_states()[self.a])
        self.assertTrue(self.registry.store.get(self.a)['enabled'])
        self.assertEqual(self.registry.enabled_cache.loaded_at,loaded_at)
        with self.assertRaises(KeyError):self.registry.set_enabled('absent',False)
        with self.assertRaises(ValueError):self.registry.set_enabled(self.a,'false')

    async def test_expired_cache_read_failure_does_not_allow_calls(self):
        self.registry.enabled_states();loaded_at=self.registry.enabled_cache.loaded_at
        self.now+=ENABLED_TTL_SECONDS
        with patch.object(self.registry.store,'permission_rows',side_effect=sqlite3.OperationalError('simulated read failure')):
            with self.assertRaises(sqlite3.OperationalError):self.registry.require_enabled('plugin:test','a')
        self.assertEqual(self.registry.enabled_cache.loaded_at,loaded_at)

    async def test_cold_toggle_rebuilds_whole_cache_and_is_persistent(self):
        self.assertIsNone(self.registry.enabled_cache.loaded_at)
        self.registry.set_enabled(self.a,False)
        self.assertEqual(self.registry.enabled_states(),{self.a:False,self.b:True})
        restarted=ToolRegistry(self.registry.store.path)
        try:self.assertFalse(restarted.enabled_states()[self.a])
        finally:await restarted.close()

    async def test_registration_merges_new_missing_and_restored_without_resetting_ttl(self):
        self.registry.enabled_states();loaded_at=self.registry.enabled_cache.loaded_at
        self.registry.set_enabled(self.a,False)
        self.now+=60
        self.provider.discover_tools.return_value=[ToolDefinition('b','beta','changed',{}),ToolDefinition('c','gamma','new',{})]
        result=await self.registry.refresh('plugin:test')
        c=result['tools'][1]['tool_id']
        self.assertTrue(self.registry.enabled_states()[c])
        with self.assertRaises(PermissionError):self.registry.require_enabled('plugin:test','a')
        self.assertEqual(self.registry.enabled_cache.loaded_at,loaded_at)
        self.provider.discover_tools.return_value=[ToolDefinition('a','alpha','searchable tool',{})]
        await self.registry.refresh('plugin:test')
        self.assertFalse(self.registry.enabled_states()[self.a])
        with self.assertRaises(PermissionError):self.registry.require_enabled('plugin:test','a')
        self.assertEqual(self.registry.enabled_cache.loaded_at,loaded_at)

    async def test_search_filters_before_limit_and_uses_cached_state(self):
        self.provider.discover_tools.return_value=[ToolDefinition(str(i),'search_'+str(i),'searchable tool',{}) for i in range(60)]
        rows=(await self.registry.register(self.provider))['tools']
        for row in rows[:55]:self.registry.set_enabled(row['tool_id'],False)
        result=self.registry.search('searchable',limit=3)
        self.assertEqual(len(result),3)
        self.assertTrue(all(row['enabled'] for row in result))
        active={row['tool_id'] for row in rows[55:]}
        self.assertTrue({row['tool_id'] for row in result}<=active)

    async def test_search_uses_enable_cache_and_sees_toggle_immediately(self):
        with patch.object(self.registry.store,'permission_rows',wraps=self.registry.store.permission_rows) as read:
            self.registry.enabled_states()
            loaded_at=self.registry.enabled_cache.loaded_at
            self.registry.store.set_enabled(self.a,False)
            # Direct DB writes must not silently bypass the agreed cache lifetime.
            self.assertIn(self.a,{row['tool_id'] for row in self.registry.search('searchable')})
            self.registry.set_enabled(self.a,False)
            self.assertNotIn(self.a,{row['tool_id'] for row in self.registry.search('searchable')})
            self.registry.set_enabled(self.a,True)
            self.assertIn(self.a,{row['tool_id'] for row in self.registry.search('searchable')})
            self.assertEqual(read.call_count,1)
            self.assertEqual(self.registry.enabled_cache.loaded_at,loaded_at)

    async def test_mcp_execution_rechecks_old_main_and_child_tool_objects(self):
        browser=SimpleNamespace(perform=AsyncMock(return_value={'title':'fixture','text':'page','elements':[]}))
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            try:
                await self.registry.register(MCPProvider(server))
                agent=Agent(name='test',mcp_servers=[server],mcp_config={'include_server_in_tool_names':True})
                tools=await agent.get_all_tools(RunContextWrapper(context=None))
                allowed=self.registry.filter_agent_tools(tools,[server])
                reader=next(t for t in allowed if t.name.endswith('__read_browser'))
                children=child_tools(allowed)
                child=next(t for t in children if t.name.endswith('__read_browser'))
                tid=self.registry.require_enabled('mcp:muse-browser','read_browser')
                self.registry.set_enabled(tid,False)
                self.assertNotIn(reader.name,{t.name for t in self.registry.filter_agent_tools(tools,[server])})
                self.assertNotIn(reader.name,{t.name for t in child_tools(allowed)})
                with self.assertRaises(PermissionError):await server.call_tool('read_browser',{})
                for tool in (reader,child):
                    guard=LoopGuard(lambda *args:None);guard.wrap(tool)
                    ctx=ToolContext(context=None,tool_name=tool.name,tool_call_id='denied',tool_arguments='{}')
                    result=await tool.on_invoke_tool(ctx,'{}')
                    self.assertIn('已禁用',json.loads(result)['message'])
                browser.perform.assert_not_awaited()
                self.registry.set_enabled(tid,True)
                result=await server.call_tool('read_browser',{})
                self.assertEqual(json.loads(result.content[0].text)['title'],'fixture')
                browser.perform.assert_awaited_once()
            finally:
                await self.registry.close()

    async def test_missing_registration_is_denied(self):
        with self.assertRaises(PermissionError):self.registry.require_enabled('plugin:test','unknown')
        with self.assertRaises(PermissionError):self.registry.require_enabled('muse:builtin','exec_command')

    async def test_agent_task_excludes_disabled_mcp_and_retains_builtins(self):
        from muse.agent import run_agent
        from muse.compression import CheckpointStore
        from muse.models import Provider
        from muse.host import local_commands_supported
        browser=SimpleNamespace(perform=AsyncMock())
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            try:
                await self.registry.register(MCPProvider(server))
                tid=self.registry.require_enabled('mcp:muse-browser','read_browser')
                self.registry.set_enabled(tid,False)
                store=CheckpointStore(self.registry.store.path,'test')
                store.tool_registry=self.registry
                async def fake_run(agent,**kwargs):
                    names={t.name for t in agent.tools}
                    self.assertNotIn('mcp_Muse_Browser__read_browser',names)
                    self.assertEqual('exec_command' in names,local_commands_supported())
                    self.assertIn('request_human',names)
                    return SimpleNamespace(final_output='done',to_input_list=lambda:[])
                provider=Provider('custom','test','test','https://example.invalid/v1','test',{},'CUSTOM_API_KEY')
                with patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':str(Path(self.temp.name)/'workspace'),'MUSE_FILESYSTEM_MCP':'0'}), patch('muse.agent.Runner.run',side_effect=fake_run):
                    answer,_=await run_agent(browser,'test',[],lambda *args:None,provider=provider,compression_store=store,browser_mcp=server)
                self.assertEqual(answer,'done')
                self.assertEqual(self.registry.store.list_tools('muse:builtin'),[])
            finally:
                await self.registry.close()


class EnablementAPI(unittest.TestCase):
    def test_toggle_endpoints_auth_strict_boolean_and_write_through(self):
        import importlib
        import os
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'MUSE_DATA_DIR':root,'MUSE_BROWSER_BACKEND':'extension','MUSE_FILESYSTEM_MCP':'0','MUSE_ACCESS_TOKEN':'test-only','MUSE_TOOL_SOURCES_CONFIG':''}):
            import muse.app as app
            app=importlib.reload(app)
            with TestClient(app.app) as client:
                self.assertEqual(client.get('/api/tools/enabled').status_code,401)
                self.assertEqual(client.post('/api/tools/absent/enabled',json={'enabled':False}).status_code,401)
                client.post('/api/login',json={'token':'test-only'})
                response=client.get('/api/tools/enabled')
                self.assertEqual(response.status_code,200)
                self.assertEqual(len(response.json()['states']),6)
                tid=next(iter(response.json()['states']))
                loaded_at=app.tool_registry.enabled_cache.loaded_at
                result=client.post('/api/tools/'+tid+'/enabled',json={'enabled':False})
                self.assertEqual(result.status_code,200)
                self.assertFalse(result.json()['enabled'])
                self.assertFalse(app.tool_registry.store.get(tid)['enabled'])
                self.assertFalse(client.get('/api/tools/enabled').json()['states'][tid])
                self.assertEqual(app.tool_registry.enabled_cache.loaded_at,loaded_at)
                self.assertEqual(client.post('/api/tools/'+tid+'/enabled',json={'enabled':'false'}).status_code,422)
                self.assertEqual(client.post('/api/tools/not-registered/enabled',json={'enabled':False}).status_code,404)
                client.post('/api/tools/refresh',json={})
                self.assertFalse(client.get('/api/tools/enabled').json()['states'][tid])
                self.assertEqual(client.post('/api/tools/'+tid+'/enabled',json={'enabled':True}).status_code,200)
                self.assertTrue(client.get('/api/tools/enabled').json()['states'][tid])
