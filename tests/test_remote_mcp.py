import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from muse.remote_mcp import RemoteMCPManager
from muse.tool_registry import ToolRegistry


class FakeServer:
    def __init__(self, name, **kwargs):self.name=name
    async def connect(self):self.owner=asyncio.current_task()
    async def cleanup(self):assert self.owner is asyncio.current_task()
    async def list_tools(self):
        return [SimpleNamespace(name='hotel_search',description='Hotel search',inputSchema={'type':'object'})]
    async def call_tool(self, tool_name, arguments, meta=None):return {'ok':True}


class RemoteMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_discover_bind_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'remote.json'
            registry=ToolRegistry(Path(directory)/'tools.db')
            with patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
                manager=RemoteMCPManager(path,registry)
                try:
                    row=await manager.add('travel','https://example.com/mcp')
                    self.assertTrue(row['ready'])
                    self.assertEqual(row['tool_count'],1)
                    tools=registry.store.list_tools()
                    self.assertEqual(tools[0]['name'],'hotel_search')
                    self.assertEqual(tools[0]['server_id'],'travel')
                    server=manager.servers['travel']
                    self.assertEqual(await server.call_tool('hotel_search',{}),{'ok':True})
                    self.assertEqual(path.stat().st_mode & 0o777,0o600)
                    with self.assertRaises(ValueError):await manager.add('travel','https://other.com/mcp')
                finally:await manager.close()
                restored=RemoteMCPManager(path,registry)
                try:
                    await restored.start()
                    self.assertTrue(restored.listing()[0]['ready'])
                finally:await restored.close()
            await registry.close()

    async def test_offline_startup_backfills_config_key_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db');path=Path(directory)/'remote.json'
            manager=RemoteMCPManager(path,registry);source=manager.source_id('travel')
            row=registry.store.upsert(source_id=source,source_kind='mcp',tool_key='hotel_search',name='hotel_search',description='hotel',parameters={})
            path.write_text(json.dumps({'travel':{'url':'https://example.com/mcp','token_env':'MUSE_TEST_ABSENT_TOKEN_8472'}}))
            try:
                await manager.start()
                self.assertFalse(manager.listing()[0]['ready'])
                self.assertEqual(registry.store.get(row['tool_id'])['server_id'],'travel')
                from muse.tool_access import ToolAccess
                self.assertEqual(ToolAccess(registry).search('hotel')[0]['server_id'],'travel')
                self.assertFalse(ToolAccess(registry).search('hotel')[0]['executable'])
            finally:await manager.close();await registry.close()

    async def test_invalid_url_and_missing_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db')
            manager=RemoteMCPManager(Path(directory)/'remote.json',registry)
            try:
                for url in ['file:///tmp/x','http://example.com/mcp','https://user:secret@example.com/mcp','https://example.com/mcp?key=secret']:
                    with self.assertRaises(ValueError):await manager.add('bad',url)
                row=await manager.add('missing','https://example.com/mcp','MUSE_TEST_ABSENT_TOKEN_8472')
                self.assertFalse(row['ready'])
                self.assertNotIn('Authorization',json.dumps(manager.listing()))
                self.assertEqual(registry.store.list_tools(),[])
            finally:
                await manager.close()
                await registry.close()

    async def test_remove_live_service_refresh_race_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'remote.json';registry=ToolRegistry(Path(directory)/'tools.db')
            with patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
                manager=RemoteMCPManager(path,registry)
                try:
                    await manager.add('travel','https://example.com/mcp')
                    await manager.add('keep','https://example.com/other')
                    source=manager.source_id('travel');server=manager.servers['travel']
                    provider=registry.providers[source]
                    self.assertTrue(registry.search('hotel_search'))  # warm cache
                    registry.request_refresh(source)
                    result=await manager.remove('travel')
                    await registry.wait_for_refresh()
                    self.assertTrue(result['removed']);self.assertEqual(result['removed_tools'],1)
                    self.assertNotIn(source,registry.providers);self.assertFalse(registry.has_source(source))
                    self.assertFalse(server._muse_connection_available)
                    with self.assertRaises(ConnectionError):await registry.register(provider)
                    with self.assertRaises(ConnectionError):await server.call_tool('hotel_search',{})
                    self.assertTrue(all(row['source_id']!=source for row in registry.search('hotel_search')))
                    self.assertTrue(all(row['availability']=='missing' for row in registry.store.list_tools(source)))
                    self.assertEqual(registry.store.list_tools(source)[0]['server_id'],'travel')
                    self.assertNotIn('travel',manager.tasks);self.assertTrue(manager.servers['keep']._muse_connection_available)
                    self.assertFalse((await manager.remove('travel'))['removed'])
                    with self.assertRaises(KeyError):await manager.connect('travel')
                finally:await manager.close()
                restored=RemoteMCPManager(path,registry)
                try:
                    await restored.start();self.assertEqual([r['id'] for r in restored.listing()],['keep'])
                    await restored.add('travel','https://example.com/new')
                    self.assertTrue(registry.search('hotel_search'))
                finally:await restored.close()
            await registry.close()

    async def test_remove_retrying_server_and_save_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db')
            manager=RemoteMCPManager(Path(directory)/'remote.json',registry)
            try:
                await manager.add('retry','https://example.com/mcp','MUSE_TEST_ABSENT_TOKEN_8472')
                task=manager.tasks['retry']
                with patch.object(manager,'_save',side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):await manager.remove('retry')
                self.assertIn('retry',manager.entries);self.assertFalse(task.done())
                self.assertTrue((await manager.remove('retry'))['removed']);self.assertTrue(task.done())
                self.assertEqual(manager.listing(),[])
            finally:await manager.close();await registry.close()

    async def test_remove_interrupts_connect_and_waiting_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db')
            manager=RemoteMCPManager(Path(directory)/'remote.json',registry)
            entered=asyncio.Event()
            class SlowReconnect(FakeServer):
                attempts=0
                async def connect(self):
                    await super().connect();type(self).attempts+=1
                    if type(self).attempts>1:
                        entered.set();await asyncio.Event().wait()
            with patch('muse.remote_mcp.MCPServerStreamableHttp',SlowReconnect):
                try:
                    await manager.add('travel','https://example.com/mcp')
                    reconnect=asyncio.create_task(manager.connect('travel'))
                    await asyncio.wait_for(entered.wait(),1)
                    queued=asyncio.create_task(manager.connect('travel'))
                    result=await asyncio.wait_for(manager.remove('travel'),2)
                    outcomes=await asyncio.gather(reconnect,queued,return_exceptions=True)
                    self.assertTrue(result['removed']);self.assertIsInstance(outcomes[1],KeyError)
                    self.assertEqual(manager.listing(),[]);self.assertEqual(registry.search('hotel_search'),[])
                finally:await manager.close();await registry.close()

    async def test_restart_finishes_interrupted_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db');path=Path(directory)/'remote.json'
            with patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
                manager=RemoteMCPManager(path,registry)
                await manager.add('travel','https://example.com/mcp');await manager.close()
                path.write_text('{}')  # Crash after config replacement, before catalog cleanup.
                restored=RemoteMCPManager(path,registry)
                try:
                    await restored.start();self.assertEqual(registry.search('hotel_search'),[])
                    self.assertEqual(restored.listing(),[])
                finally:await restored.close();await registry.close()

class RemoteMCPAPITests(unittest.TestCase):
    def test_add_and_restart_through_api(self):
        import importlib
        import os
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{
            'MUSE_DATA_DIR':directory,'MUSE_ACCESS_TOKEN':'remote-test',
            'MUSE_FILESYSTEM_MCP':'0','MUSE_BROWSER_BACKEND':'extension','MUSE_TOOL_SOURCES_CONFIG':''
        }), patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
            import muse.app as module
            module=importlib.reload(module)
            with TestClient(module.app) as client:
                self.assertEqual(client.post('/api/mcp',json={}).status_code,401)
                client.post('/api/login',json={'token':'remote-test'})
                response=client.post('/api/mcp',json={'id':'travel','url':'https://example.com/mcp'})
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json()['ready'])
                selected=client.get('/api/tools/search',params={'query':'hotel','server_id':'travel','limit':1}).json()['tools']
                self.assertEqual(selected[0]['server_id'],'travel')
                tid=selected[0]['tool_id']
                self.assertEqual(client.get('/api/tools/'+tid+'/detail').json()['server_id'],'travel')
                catalog=client.get('/api/tools',params={'server_id':'travel'}).json()
                self.assertEqual(catalog['tools'][0]['server_id'],'travel')
                self.assertEqual(catalog['sources'][0]['server_id'],'travel')
                self.assertEqual(client.get('/api/tools/search',params={'query':'hotel','server_id':'other'}).json()['tools'],[])
                self.assertEqual(client.get('/api/tools',params={'server_id':'other'}).json(),{'tools':[],'sources':[]})
                self.assertTrue(any(t['name']=='hotel_search' for t in client.get('/api/tools').json()['tools']))
                self.assertEqual(client.post('/api/mcp',json={'id':'travel','url':'https://example.com/mcp'}).status_code,400)
                self.assertEqual(client.post('/api/mcp/travel/connect',json={}).status_code,200)
            with TestClient(module.app) as client:
                client.post('/api/login',json={'token':'remote-test'})
                self.assertTrue(client.get('/api/mcp').json()['servers'][0]['ready'])
                client.cookies.clear()
                self.assertEqual(client.delete('/api/mcp/travel').status_code,401)
                client.post('/api/login',json={'token':'remote-test'})
                self.assertTrue(client.delete('/api/mcp/travel').json()['removed'])
                self.assertEqual(client.get('/api/mcp').json()['servers'],[])
                self.assertEqual(client.post('/api/mcp/travel/connect',json={}).status_code,404)
            with TestClient(module.app) as client:
                client.post('/api/login',json={'token':'remote-test'})
                self.assertEqual(client.get('/api/mcp').json()['servers'],[])
