import asyncio
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.mcp import MCPServerStdio
from mcp import types
from muse.catalog_events import CatalogEvents
from muse.filesystem_mcp import LoggedServer
from muse.tool_providers import MCPProvider, ManifestProvider, ToolDefinition, load_configured_providers
from muse.tool_registry import ToolRegistry
from muse.tool_store import ToolStore


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'muse.db'
        self.store=ToolStore(self.path)
        self.registry=ToolRegistry(self.store)

    async def asyncTearDown(self):
        await self.registry.close()

    async def wait_until(self, condition):
        async with asyncio.timeout(2):
            while not condition():await asyncio.sleep(.01)

    async def test_successful_snapshot_removes_and_restores_without_changing_settings(self):
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('a','alpha','Original description',{}),ToolDefinition('b','beta','Other description',{})]))
        first=await self.registry.register(provider)
        a=first['tools'][0]['tool_id'];b=first['tools'][1]['tool_id']
        self.store.set_enabled(a,False)
        provider.discover_tools.return_value=[ToolDefinition('b','beta','Changed description',{})]
        result=await self.registry.refresh('plugin:test')
        self.assertEqual(result['removed'],1)
        self.assertEqual(self.store.get(a)['availability'],'missing')
        self.assertEqual(self.store.search('alpha',enabled_only=False),[])
        self.store.rebuild_index()
        self.assertEqual(self.store.search('alpha',enabled_only=False),[])
        provider.discover_tools.return_value=[ToolDefinition('a','alpha','Original description',{}),ToolDefinition('b','beta','Changed description',{})]
        restored=await self.registry.refresh('plugin:test')
        self.assertEqual(restored['restored'],1)
        self.assertEqual(restored['changed'],0)
        self.assertFalse(self.store.get(a)['enabled'])
        self.assertEqual(self.store.get(a)['availability'],'unknown')
        self.assertEqual(len(self.store.search('alpha',enabled_only=False)),1)
        self.assertEqual(self.store.get(b)['description'],'Changed description')
        provider.discover_tools.return_value=[]
        self.assertEqual((await self.registry.refresh('plugin:test'))['removed'],2)
        self.assertEqual(self.store.search('description',enabled_only=False),[])

    async def test_failed_or_invalid_refresh_does_not_mark_tools_missing(self):
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('a','alpha','old',{})]))
        first=await self.registry.register(provider)
        provider.discover_tools.side_effect=ConnectionError('synthetic secret')
        result=await self.registry.refresh_all()
        self.assertEqual(result['errors'],['plugin:test'])
        self.assertEqual(self.store.get(first['tools'][0]['tool_id'])['availability'],'unknown')
        self.assertEqual(len(self.store.search('alpha')),1)
        provider.discover_tools.side_effect=None
        provider.discover_tools.return_value=[ToolDefinition('bad','bad','',[])]
        with self.assertRaises(ValueError):await self.registry.refresh('plugin:test')
        self.assertEqual(len(self.store.search('alpha')),1)

    async def test_source_reconcile_is_atomic_on_index_failure(self):
        definition=dict(source_id='plugin:test',source_kind='plugin',tool_key='a',name='alpha',description='old',parameters={})
        tid=self.store.upsert(**definition)['tool_id']
        with closing(self.store.connect()) as db, db:
            db.execute("CREATE TRIGGER fail_missing BEFORE UPDATE OF availability ON tool_catalog WHEN new.availability='missing' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):
            self.store.reconcile_source('plugin:test','plugin',[{**definition,'tool_key':'b','name':'beta'}])
        self.assertEqual(self.store.get(tid)['availability'],'unknown')
        self.assertEqual(len(self.store.list_tools()),1)
        self.assertEqual(len(self.store.search('alpha')),1)
        self.assertEqual(self.store.search('beta'),[])

    async def test_actual_handler_coalesces_notifications_and_invalidates_sdk_cache(self):
        server=LoggedServer(lambda *args:None,name='Filesystem',params={'command':'node'})
        current=[SimpleNamespace(name='one',description='first',input_schema={})]
        with patch.object(server,'list_tools',AsyncMock(side_effect=lambda:list(current))),patch.object(server,'invalidate_tools_cache') as invalidate:
            await self.registry.register(MCPProvider(server))
            current[:]=[SimpleNamespace(name='two',description='second',input_schema={})]
            for _ in range(10):
                await server.message_handler(types.ToolListChangedNotification())
            await self.registry.wait_for_refresh()
            self.assertEqual(invalidate.call_count,1)
            self.assertEqual(len(self.store.search('second')),1)
            self.assertEqual(self.store.search('first'),[])
            current[:]=[SimpleNamespace(name='three',description='reconnected',input_schema={})]
            with patch.object(MCPServerStdio,'connect',AsyncMock()):
                await server.connect()
            await self.registry.wait_for_refresh()
            self.assertEqual(invalidate.call_count,2)
            self.assertEqual(len(self.store.search('reconnected')),1)
        await self.registry.close()
        self.assertEqual(server._catalog_listeners,set())

    async def test_notification_during_refresh_is_not_lost(self):
        class Source(CatalogEvents):
            name='Filesystem'
            def __init__(self):self.init_catalog_events();self.counter=0
            async def list_tools(inner):
                inner.counter+=1
                counter=inner.counter
                if counter==2:
                    inner.emit_catalog_event('tools_changed')
                    await asyncio.sleep(.01)
                return [SimpleNamespace(name='tool',description='version '+str(counter),input_schema={})]
        server=Source()
        await self.registry.register(MCPProvider(server))
        server.emit_catalog_event('tools_changed')
        await self.registry.wait_for_refresh()
        self.assertEqual(server.counter,3)
        self.assertEqual(self.store.list_tools()[0]['description'],'version 3')

    async def test_manifest_content_watch_invalid_file_recovery_and_atomic_replace(self):
        path=Path(self.temp.name)/'tools.json'
        def write(description):
            path.write_text(json.dumps({'tools':[{'name':'lookup','description':description,'parameters':{}}]}))
        write('first');provider=ManifestProvider('plugin:test',path)
        result=await self.registry.register(provider);tid=result['tools'][0]['tool_id']
        self.store.set_enabled(tid,False)
        await self.registry.start_watching(.01)
        write('second')
        await self.wait_until(lambda:self.store.get(tid)['description']=='second')
        self.assertFalse(self.store.get(tid)['enabled'])
        path.write_text('{invalid')
        await asyncio.sleep(.12)
        self.assertEqual(self.store.get(tid)['description'],'second')
        replacement=path.with_suffix('.new');replacement.write_text(json.dumps({'tools':[]}));replacement.replace(path)
        await self.wait_until(lambda:self.store.get(tid)['availability']=='missing')
        write('restored')
        await self.wait_until(lambda:self.store.get(tid)['description']=='restored')
        self.assertEqual(self.store.get(tid)['availability'],'unknown')
        self.assertFalse(self.store.get(tid)['enabled'])

    async def test_restart_configuration_and_registration_preserve_identity(self):
        manifest=Path(self.temp.name)/'tools.json'
        manifest.write_text('{"tools":[{"name":"lookup","description":"restart","parameters":{}}]}')
        config=manifest.with_name('sources.json')
        config.write_text(json.dumps({'sources':[{'id':'plugin:test','kind':'plugin','path':'tools.json'}]}))
        first=await self.registry.register_all(load_configured_providers(config))
        row=self.store.list_tools()[0];self.store.set_enabled(row['tool_id'],False)
        await self.registry.close()
        self.registry=ToolRegistry(self.path)
        second=await self.registry.register_all(load_configured_providers(config))
        self.assertEqual(first['registered'],1);self.assertEqual(second['changed'],0)
        self.assertEqual(self.store.list_tools()[0]['tool_id'],row['tool_id'])
        self.assertFalse(self.store.list_tools()[0]['enabled'])

    async def test_close_cancels_refresh_and_watch(self):
        entered=asyncio.Event()
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[]))
        await self.registry.register(provider)
        async def blocked():entered.set();await asyncio.Event().wait()
        provider.discover_tools=blocked
        await self.registry.start_watching(.01)
        self.registry.request_refresh('plugin:test')
        await asyncio.wait_for(entered.wait(),1)
        await self.registry.close()
        self.assertEqual(self.registry._refresh_tasks,{})
        self.assertIsNone(self.registry._watch_task)

    async def test_browser_mcp_actual_reconnect_refreshes_catalog(self):
        from muse.browser_mcp import BrowserMCP
        server=BrowserMCP(SimpleNamespace(),lambda *args:None)
        async with server.session():
            await self.registry.register(MCPProvider(server))
            row=next(row for row in self.store.list_tools() if row['name']=='read_browser')
            await server.cleanup()
            server.functions.pop('read_browser')
            await server.connect()
            await self.registry.wait_for_refresh()
            self.assertEqual(self.store.get(row['tool_id'])['availability'],'missing')
            self.assertEqual(self.store.search('read_browser'),[])


class RefreshAPI(unittest.TestCase):
    def test_config_startup_and_manual_refresh_authentication(self):
        import importlib
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'tools.json';config=Path(root)/'sources.json'
            path.write_text('{"tools":[{"name":"lookup","description":"first","parameters":{}}]}')
            config.write_text(json.dumps({'sources':[{'id':'plugin:fixture','kind':'plugin','path':'tools.json'}]}))
            with patch.dict(os.environ,{'MUSE_DATA_DIR':root,'MUSE_BROWSER_BACKEND':'extension','MUSE_FILESYSTEM_MCP':'0','MUSE_ACCESS_TOKEN':'test-only','MUSE_TOOL_SOURCES_CONFIG':str(config)}):
                import muse.app as app
                app=importlib.reload(app)
                with TestClient(app.app) as client:
                    self.assertEqual(client.post('/api/tools/refresh',json={}).status_code,401)
                    client.post('/api/login',json={'token':'test-only'})
                    self.assertEqual(app.tool_registry.store.list_tools('plugin:fixture')[0]['description'],'first')
                    path.write_text('{"tools":[{"name":"lookup","description":"second","parameters":{}}]}')
                    result=client.post('/api/tools/refresh',json={'source_id':'plugin:fixture'})
                    self.assertEqual(result.status_code,200)
                    self.assertEqual(result.json()['changed'],1)
                    self.assertEqual(app.tool_registry.store.list_tools('plugin:fixture')[0]['description'],'second')
                    path.write_text('{invalid')
                    self.assertEqual(client.post('/api/tools/refresh',json={'source_id':'plugin:fixture'}).status_code,503)
                    self.assertEqual(app.tool_registry.store.list_tools('plugin:fixture')[0]['description'],'second')
                    self.assertEqual(client.post('/api/tools/refresh',json={'source_id':'plugin:missing'}).status_code,404)
                self.assertTrue(app.tool_registry._closed)


class FailedManifestRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_initial_invalid_manifest_can_recover_without_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'tools.json';path.write_text('{invalid')
            registry=ToolRegistry(Path(root)/'muse.db')
            try:
                result=await registry.register_all([ManifestProvider('plugin:recover',path)])
                self.assertEqual(result['errors'],['plugin:recover'])
                await registry.start_watching(.01)
                path.write_text('{"tools":[{"name":"recovered","description":"test","parameters":{}}]}')
                async with asyncio.timeout(2):
                    while not registry.store.search('recovered'):await asyncio.sleep(.01)
                self.assertEqual(len(registry.store.list_tools()),1)
            finally:
                await registry.close()
