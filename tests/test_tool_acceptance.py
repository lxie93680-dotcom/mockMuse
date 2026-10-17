"""Acceptance across replacement, discovery, caching and persisted restart."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import types
from agents.tool_context import ToolContext
from muse.tool_access import ToolAccess
from muse.tool_providers import MCPProvider, ManifestProvider
from muse.tool_registry import ToolRegistry
from muse.tool_store import definition_fingerprint
from muse.browser_mcp import BrowserMCP
from muse.filesystem_mcp import Filesystem, ROOT


class ToolAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'muse.db'
        self.registry=ToolRegistry(self.path)

    async def asyncTearDown(self):await self.registry.close()

    def server(self,description='fixture'):
        tool=types.Tool(name='read',description=description,inputSchema={'type':'object'})
        return SimpleNamespace(name='Replacement Fixture',list_tools=AsyncMock(return_value=[tool]),
            call_tool=AsyncMock(return_value=types.CallToolResult(content=[types.TextContent(type='text',text='ok')])),
            invalidate_tools_cache=lambda:None)

    async def test_failed_replacement_keeps_old_connection_permission_guard(self):
        old=self.server();original=old.call_tool
        row=(await self.registry.register(MCPProvider(old)))['tools'][0]
        self.registry.enabled_states()
        new=self.server();new.list_tools.side_effect=ConnectionError('fixture')
        with self.assertRaises(ConnectionError):await self.registry.register(MCPProvider(new))
        self.registry.set_enabled(row['tool_id'],False)
        with self.assertRaises(PermissionError):await old.call_tool('read',{})
        original.assert_not_awaited()
        self.registry.set_enabled(row['tool_id'],True)
        await old.call_tool('read',{})
        original.assert_awaited_once()

    async def test_successful_replacement_rejects_retired_objects_and_unbinds_on_close(self):
        old=self.server();original=old.call_tool
        row=(await self.registry.register(MCPProvider(old)))['tools'][0]
        new=self.server();new_original=new.call_tool
        result=await self.registry.register(MCPProvider(new))
        self.assertEqual(result['tools'][0]['tool_id'],row['tool_id'])
        with self.assertRaises(PermissionError):await old.call_tool('read',{})
        original.assert_not_awaited()
        await new.call_tool('read',{});new_original.assert_awaited_once()
        # Same runtime server repeatedly registered must not accumulate nested guards.
        await self.registry.register(MCPProvider(new))
        self.assertEqual(len(self.registry._access_bindings),2)
        await self.registry.close()
        self.assertIs(old.call_tool,original);self.assertIs(new.call_tool,new_original)

    async def test_discovery_cannot_strip_guard_while_waiting_or_cancelled(self):
        old=self.server();original=old.call_tool
        tid=(await self.registry.register(MCPProvider(old)))['tools'][0]['tool_id']
        self.registry.set_enabled(tid,False)
        new=self.server();entered=asyncio.Event()
        async def blocked():entered.set();await asyncio.Event().wait()
        new.list_tools.side_effect=blocked
        task=asyncio.create_task(self.registry.register(MCPProvider(new)))
        await entered.wait()
        try:
            with self.assertRaises(PermissionError):await old.call_tool('read',{})
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        with self.assertRaises(PermissionError):await old.call_tool('read',{})
        original.assert_not_awaited()

    async def test_persisted_restart_refresh_fingerprint_index_cache_and_dynamic_scope(self):
        manifest=Path(self.temp.name)/'tools.json'
        def write(items):manifest.write_text(json.dumps({'tools':items}))
        write([{'name':'lookup','description':'first version','parameters':{'type':'object'}}])
        provider=ManifestProvider('plugin:acceptance',manifest)
        row=(await self.registry.register(provider))['tools'][0];tid=row['tool_id']
        access=ToolAccess(self.registry);child=access.child((),[tid])
        self.assertEqual(len(access.search('first version')),1)
        self.registry.set_enabled(tid,False)
        self.assertEqual(access.search('first version'),[])
        self.assertEqual(child.search('first version'),[])
        await self.registry.close()
        self.registry=ToolRegistry(self.path)
        await self.registry.register(ManifestProvider('plugin:acceptance',manifest))
        self.assertFalse(self.registry.enabled_states()[tid])
        self.assertEqual(self.registry.store.get(tid)['fingerprint'],row['fingerprint'])
        self.registry.set_enabled(tid,True)
        access=ToolAccess(self.registry);child=access.child((),[tid])
        write([{'name':'lookup','description':'changed version','parameters':{'type':'object','properties':{'q':{'type':'string'}}}},
               {'name':'new_tool','description':'new capability','parameters':{}}])
        await self.registry.refresh('plugin:acceptance')
        current=access.describe(tid)
        self.assertNotEqual(current['fingerprint'],row['fingerprint'])
        self.assertEqual(access.search('first version'),[])
        self.assertEqual(len(access.search('new capability')),1)
        self.assertEqual(child.search('new capability'),[])
        self.assertEqual(current['fingerprint'],definition_fingerprint(current['name'],current['description'],current['parameters']))
        ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='stale',tool_arguments='{}')
        with self.assertRaises(ValueError):await access.call(ctx,tid,{},row['fingerprint'])
        write([]);await self.registry.refresh('plugin:acceptance')
        self.assertEqual(access.search('changed version'),[])
        with self.assertRaises(PermissionError):access.describe(tid)
        self.registry.store.rebuild_index()
        self.assertEqual(self.registry.store.search('changed version',enabled_only=False),[])

    @unittest.skipUnless(shutil.which('node') and (ROOT/'vendor/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js').is_file(),
                         'Requires installed local Filesystem MCP')
    async def test_installed_filesystem_protocol_search_describe_readonly_call_and_disable(self):
        work=(Path(self.temp.name)/'workspace').resolve();work.mkdir()
        (work/'acceptance.txt').write_text('synthetic acceptance fixture')
        with patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'1','MUSE_LOCAL_WORKSPACE':str(work)}):
            fs=Filesystem()
            async with fs.session(lambda *args:None):
                self.assertIsNotNone(fs.server)
                browser=BrowserMCP(SimpleNamespace(),lambda *args:None,lambda picture:'/fixture-image')
                async with browser.session():
                    try:
                        await self.registry.register_all([MCPProvider(fs.server),MCPProvider(browser)])
                        rows=self.registry.store.list_tools()
                        self.assertEqual(len(rows),20)
                        self.assertEqual(sum(r['source_id']=='mcp:filesystem' for r in rows),14)
                        access=ToolAccess(self.registry)
                        row=next(row for row in access.search('list_directory') if row['name']=='list_directory')
                        info=access.describe(row['tool_id'])
                        ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='real-read',tool_arguments='{}')
                        result=await access.call(ctx,info['tool_id'],{'path':str(work)},info['fingerprint'])
                        self.assertIn('acceptance.txt',json.dumps(result))
                        self.registry.set_enabled(info['tool_id'],False)
                        self.assertNotIn(info['tool_id'],{r['tool_id'] for r in access.search('list_directory')})
                        with self.assertRaises(PermissionError):
                            await access.call(ctx,info['tool_id'],{'path':str(work)},info['fingerprint'])
                    finally:
                        await self.registry.close()
