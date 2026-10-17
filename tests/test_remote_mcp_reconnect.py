import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from muse.remote_mcp import RemoteMCPManager
from muse.tool_registry import ToolRegistry
from muse.tool_providers import MCPProvider
from muse.tool_access import ToolAccess


class ConnectionFixture:
    instances=[]
    fail_connect=0
    def __init__(self,name,**kwargs):
        self.name=name;self.dead=False;self.calls=0;self.cleaned=False
        self.instances.append(self)
    async def connect(self):
        self.owner=asyncio.current_task()
        if type(self).fail_connect:
            type(self).fail_connect-=1
            raise ConnectionError('fixture offline')
    async def cleanup(self):
        assert self.owner is asyncio.current_task()
        self.cleaned=True
    async def list_tools(self,**kwargs):
        if self.dead:raise ConnectionError('fixture disconnected')
        return [SimpleNamespace(name='search_hotels',description='search hotels',inputSchema={'type':'object'})]
    def invalidate_tools_cache(self):pass
    async def call_tool(self,tool_name,arguments,meta=None):
        self.calls+=1
        if self.dead:raise ConnectionError('fixture response lost')
        return {'ok':True}


class RemoteReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.registry=ToolRegistry(Path(self.directory.name)/'tools.db')
        self.manager=RemoteMCPManager(Path(self.directory.name)/'remote.json',self.registry)
        self.manager.retry_base=.02;self.manager.retry_max=.08
        self.manager.probe_interval=10
        ConnectionFixture.instances=[];ConnectionFixture.fail_connect=0
        self.patch=patch('muse.remote_mcp.MCPServerStreamableHttp',ConnectionFixture)
        self.patch.start()
    async def asyncTearDown(self):
        await self.manager.close();await self.registry.close()
        self.patch.stop();self.directory.cleanup()
    async def until(self,predicate):
        async with asyncio.timeout(2):
            while not predicate():await asyncio.sleep(.002)
    async def add(self):
        await self.manager.add('travel','https://example.com/mcp')
        return self.manager.servers['travel']

    async def test_manual_retry_replaces_and_cleans_on_owner_task(self):
        old=await self.add()
        await self.manager.connect('travel')
        self.assertIsNot(self.manager.servers['travel'],old)
        self.assertTrue(old.cleaned)
        self.assertTrue(self.manager.listing()[0]['ready'])
        with self.assertRaises(ConnectionError):await old.call_tool('search_hotels',{})
        self.assertEqual(old.calls,0)

    async def test_failed_business_call_reconnects_without_replaying(self):
        old=await self.add();old.dead=True
        row=self.registry.store.list_tools()[0]
        with self.assertRaises(ConnectionError):await old.call_tool('search_hotels',{})
        self.assertFalse(self.manager.listing()[0]['ready'])
        self.assertFalse(ToolAccess(self.registry).search('search_hotels')[0]['executable'])
        await self.until(lambda:self.manager.servers.get('travel') not in (None,old))
        new=self.manager.servers['travel']
        self.assertTrue(old.cleaned)
        self.assertEqual(old.calls,1);self.assertEqual(new.calls,0)
        self.assertEqual(self.registry.store.list_tools()[0]['tool_id'],row['tool_id'])
        self.assertEqual(await new.call_tool('search_hotels',{}),{'ok':True})

    async def test_periodic_probe_rebuilds_and_preserves_disabled_tools(self):
        self.manager.probe_interval=.01
        old=await self.add()
        tid=self.registry.store.list_tools()[0]['tool_id']
        self.registry.set_enabled(tid,False)
        old.dead=True
        await self.until(lambda:self.manager.servers.get('travel') not in (None,old))
        self.assertFalse(self.registry.enabled_states()[tid])
        self.assertEqual(len(self.registry.store.list_tools()),1)
        self.assertTrue(old.cleaned)

    async def test_failed_connect_background_retries_then_recovers(self):
        ConnectionFixture.fail_connect=2
        row=await self.manager.add('travel','https://example.com/mcp')
        self.assertFalse(row['ready']);self.assertEqual(row['state'],'retry_wait')
        self.assertIsNotNone(row['next_retry_at'])
        await self.until(lambda:self.manager.listing()[0]['ready'])
        self.assertEqual(len(ConnectionFixture.instances),3)
        self.assertTrue(all(s.cleaned for s in ConnectionFixture.instances[:-1]))

    async def test_manual_retry_interrupts_backoff_and_close_stops_retries(self):
        self.manager.retry_base=60
        ConnectionFixture.fail_connect=1
        await self.manager.add('travel','https://example.com/mcp')
        await asyncio.wait_for(self.manager.connect('travel'),timeout=1)
        self.assertTrue(self.manager.listing()[0]['ready'])
        await self.manager.close()
        self.assertTrue(all(s.cleaned for s in ConnectionFixture.instances))
        self.assertTrue(all(t.done() for t in self.manager.tasks.values()))
        with self.assertRaises(RuntimeError):await self.manager.connect('travel')

    async def test_retired_provider_cannot_register_again(self):
        old=await self.add();provider=MCPProvider(old)
        await self.manager.connect('travel')
        with self.assertRaises(ConnectionError):await self.registry.register(provider)
        self.assertIs(self.registry.providers[provider.source_id].server,self.manager.servers['travel'])

    async def test_business_error_does_not_rebuild_connection(self):
        old=await self.add()
        async def business_error(*args,**kwargs):raise ValueError('invalid business input')
        # Wrap a separate business-error fixture to exercise classification.
        old.call_tool=business_error
        self.manager._wrap('travel',old)
        with self.assertRaises(ValueError):await old.call_tool('search_hotels',{})
        self.assertTrue(self.manager.listing()[0]['ready'])
        self.assertFalse(self.manager.wakes['travel'].is_set())

class RealTransportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_503_rebuilds_real_sdk_session_without_replay(self):
        import socket
        import uvicorn
        from mcp.server.mcpserver import MCPServer
        from starlette.responses import PlainTextResponse
        mcp=MCPServer('reconnect-fixture',log_level='ERROR')
        calls=[]
        @mcp.tool()
        def read_fixture() -> str:
            calls.append('called')
            return 'local fixture result'
        app=mcp.streamable_http_app(stateless_http=False,json_response=True)
        outage=False
        async def gated(scope,receive,send):
            if scope['type']=='http' and outage:
                return await PlainTextResponse('fixture unavailable',status_code=503)(scope,receive,send)
            await app(scope,receive,send)
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(gated,log_level='critical',lifespan='on'))
        server_task=asyncio.create_task(server.serve(sockets=[sock]))
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db')
            manager=RemoteMCPManager(Path(directory)/'remote.json',registry)
            manager.retry_base=.02
            # Test-only loopback fixture; public add still requires HTTPS.
            manager.entries={'fixture':{'url':f'http://127.0.0.1:{port}/mcp','token_env':''}}
            try:
                async with asyncio.timeout(5):
                    while not server.started:await asyncio.sleep(.01)
                await manager.connect('fixture')
                self.assertTrue(manager.listing()[0]['ready'])
                old=manager.servers['fixture']
                old_session=old.session
                await old.call_tool('read_fixture',{})
                self.assertEqual(calls,['called'])
                outage=True
                with self.assertRaises(Exception):await old.call_tool('read_fixture',{})
                self.assertFalse(manager.listing()[0]['ready'])
                outage=False
                async with asyncio.timeout(5):
                    while manager.servers.get('fixture') in (None,old):await asyncio.sleep(.01)
                self.assertEqual(calls,['called'])
                await manager.servers['fixture'].call_tool('read_fixture',{})
                self.assertEqual(calls,['called','called'])
                self.assertIsNot(manager.servers['fixture'].session,old_session)
                self.assertEqual(len(registry.store.list_tools()),1)
            finally:
                await manager.close();await registry.close()
                server.should_exit=True
                await asyncio.wait_for(server_task,5)
                sock.close()
