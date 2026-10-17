import json
import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
from muse.human_handoff import HumanInterventionRequired
from muse.tool_failures import ToolCallFailure
from agents.mcp.util import MCPUtil

from agents.tool_context import ToolContext
from muse.browser_mcp import BrowserMCP
from muse.tool_access import ToolAccess
from muse.tool_providers import MCPProvider, ToolDefinition
from muse.tool_registry import ToolRegistry
from muse.delegation import DENIED_TOOLS, select_child_tools
from muse.loop_guard import LoopGuard
from muse.tool_results import ToolResultProcessor


def connected(test):
    async def run(self):
        async with self.server.session():
            await self.registry.register(MCPProvider(self.server))
            self.rows={r['name']:r for r in self.registry.store.list_tools()}
            await test(self)
    return run


class DeferredToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry=ToolRegistry(Path(self.temp.name)/'tools.db')
        self.browser=SimpleNamespace(perform=AsyncMock(return_value={'title':'fixture','text':'page','elements':[]}))
        self.server=BrowserMCP(self.browser,lambda *args:None)
        self.access=ToolAccess(self.registry)
        self.rows={r['name']:r for r in self.registry.store.list_tools()}
        self.ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='test',tool_arguments='{}')

    async def asyncTearDown(self):
        await self.registry.close()

    async def invoke(self,name,args):
        tid=self.rows[name]['tool_id'];fp=self.access.describe(tid)['fingerprint']
        return await self.access.call(self.ctx,tid,args,fp)

    @connected
    async def test_search_compact_describe_schema_call_real_mcp(self):
        result=self.access.search('read_browser')
        self.assertEqual(len(result),1)
        self.assertNotIn('parameters',result[0])
        info=self.access.describe(result[0]['tool_id'])
        self.assertIn('parameters',info);self.assertTrue(info['fingerprint'])
        output=await self.invoke('read_browser',{})
        self.assertIn('fixture',json.dumps(output))
        self.browser.perform.assert_awaited_once()

    async def test_remote_server_id_in_model_tools_and_scoped_selection(self):
        from tests.test_remote_mcp import FakeServer
        from muse.remote_mcp import RemoteMCPManager
        manager=RemoteMCPManager(Path(self.temp.name)/'remote.json',self.registry)
        with patch('muse.remote_mcp.MCPServerStreamableHttp',FakeServer):
            try:
                await manager.add('first','https://example.com/first')
                await manager.add('second','https://example.com/second')
                tools={tool.name:tool for tool in self.access.tools()}
                selected=json.loads(await tools['tool_search'].on_invoke_tool(self.ctx,json.dumps({'query':'hotel','limit':1,'server_id':'second'})))['tools']
                self.assertEqual(len(selected),1);self.assertEqual(selected[0]['server_id'],'second')
                self.assertEqual(selected[0]['source_kind'],'mcp')
                detail=json.loads(await tools['tool_describe'].on_invoke_tool(self.ctx,json.dumps({'tool_id':selected[0]['tool_id']})))
                self.assertEqual(detail['server_id'],'second')
                child=self.access.child((),[selected[0]['tool_id']])
                self.assertEqual(child.search('hotel',server_id='first'),[])
                self.assertEqual(child.search('hotel',server_id='second')[0]['server_id'],'second')
            finally:await manager.close()

    @connected
    async def test_toggle_blocks_search_describe_and_old_call_then_reenables(self):
        tid=self.rows['read_browser']['tool_id'];fp=self.access.describe(tid)['fingerprint']
        with patch.object(self.registry.store,'permission_rows',wraps=self.registry.store.permission_rows) as read:
            self.registry.set_enabled(tid,False)
            self.assertEqual(self.access.search('read_browser'),[])
            with self.assertRaises(PermissionError):self.access.describe(tid)
            with self.assertRaises(PermissionError):await self.access.call(self.ctx,tid,{},fp)
            self.assertEqual(read.call_count,0)
            self.registry.set_enabled(tid,True)
            self.assertEqual(len(self.access.search('read_browser')),1)
        self.browser.perform.assert_not_awaited()

    @connected
    async def test_invalid_arguments_stale_fingerprint_unknown_id_no_effects(self):
        with self.assertRaises(ValueError):await self.invoke('browser_action',{'action':123})
        tid=self.rows['read_browser']['tool_id']
        with self.assertRaises(ValueError):await self.access.call(self.ctx,tid,{},'old')
        with self.assertRaises(PermissionError):self.access.describe('unknown')
        self.browser.perform.assert_not_awaited()

    @connected
    async def test_child_cannot_use_parent_bridge_or_write_tool(self):
        parent=self.access.tools()
        child=select_child_tools(parent)
        self.assertEqual({t.name for t in child},{'tool_search','tool_describe','tool_call'})
        scope=child[0]._muse_tool_access
        self.assertEqual(scope.search('browser_action'),[])
        with self.assertRaises(PermissionError):scope.describe(self.rows['browser_action']['tool_id'])
        with self.assertRaises(ValueError):select_child_tools(parent,['tool_call'])
        with self.assertRaises(ValueError):select_child_tools(parent,[self.rows['browser_action']['tool_id']])
        self.assertEqual(select_child_tools(parent,[]),[])
        selected=select_child_tools(parent,[self.rows['read_browser']['tool_id']])[0]._muse_tool_access
        self.assertEqual(selected.search('browser_view'),[])
        self.assertEqual(len(selected.search('read_browser')),1)
        with self.assertRaises(PermissionError):selected.describe(self.rows['browser_view']['tool_id'])

    @connected
    async def test_metadata_only_source_cannot_execute_and_refs_do_not_fetch(self):
        schema={'type':'object','properties':{'x':{'$ref':'https://invalid.example/schema'}},'required':['x']}
        provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',discover_tools=AsyncMock(return_value=[ToolDefinition('read','read','metadata only',schema)]))
        tid=(await self.registry.register(provider))['tools'][0]['tool_id']
        info=self.access.describe(tid)
        self.assertFalse(info['executable'])
        with self.assertRaisesRegex(ValueError,'安全验证'):await self.access.call(self.ctx,tid,{'x':1},info['fingerprint'])
        provider.discover_tools.return_value=[ToolDefinition('read','read','metadata only',{'type':'object'})]
        await self.registry.register(provider)
        with self.assertRaisesRegex(RuntimeError,'执行器'):await self.access.call(self.ctx,tid,{},self.access.describe(tid)['fingerprint'])

    @connected
    async def test_removed_tool_and_disconnected_provider_fail_closed(self):
        tid=self.rows['read_browser']['tool_id'];fp=self.access.describe(tid)['fingerprint']
        provider=self.registry.providers.pop('mcp:muse-browser')
        with self.assertRaisesRegex(RuntimeError,'执行器'):await self.access.call(self.ctx,tid,{},fp)
        empty=SimpleNamespace(source_id=provider.source_id,source_kind='mcp',discover_tools=AsyncMock(return_value=[]))
        await self.registry.register(empty)
        self.assertEqual(self.access.search('read_browser'),[])
        with self.assertRaises(PermissionError):self.access.describe(tid)
        self.browser.perform.assert_not_awaited()

    @connected
    async def test_scope_filter_precedes_limit(self):
        scope=self.access.child(DENIED_TOOLS,[self.rows['read_browser']['tool_id']])
        self.assertEqual([r['name'] for r in scope.search('browser',1)],['read_browser'])

    @connected
    async def test_guard_preserves_underlying_browser_result_name(self):
        tool=self.access.tools()[2]
        processor=ToolResultProcessor(None,'read')
        guard=LoopGuard(lambda *args:None);guard.result_processor=processor;guard.wrap(tool)
        info=self.access.describe(self.rows['read_browser']['tool_id'])
        args=json.dumps({'tool_id':info['tool_id'],'arguments':{},'fingerprint':info['fingerprint']})
        with patch.object(processor,'process',wraps=processor.process) as process:
            await tool.on_invoke_tool(self.ctx,args)
            self.assertEqual(process.call_args.args[0],'mcp:muse-browser__read_browser')

    @connected
    async def test_bridge_preserves_images_and_needs_observation(self):
        image=io.BytesIO();Image.new('RGB',(20,10),'white').save(image,format='JPEG')
        self.browser.screenshot=AsyncMock(return_value=image.getvalue())
        self.browser.lock=asyncio.Lock()
        self.browser.active_page=AsyncMock(return_value='page')
        self.browser.width=40;self.browser.height=20
        self.browser.image_width=20;self.browser.image_height=10
        tool=self.access.tools()[2];guard=LoopGuard(lambda *args:None)
        guard.result_processor=ToolResultProcessor();guard.wrap(tool)
        info=self.access.describe(self.rows['browser_view']['tool_id'])
        output=await tool.on_invoke_tool(self.ctx,json.dumps({'tool_id':info['tool_id'],'arguments':{},'fingerprint':info['fingerprint']}))
        self.assertTrue(any(part.get('type')=='image' for part in output))
        self.browser.perform.return_value={'status':'needs_observation','message':'refresh required','action_performed':False,'elements':[{'ref':'a'}]}
        info=self.access.describe(self.rows['browser_action']['tool_id'])
        output=await tool.on_invoke_tool(self.ctx,json.dumps({'tool_id':info['tool_id'],'arguments':{'action':'read'},'fingerprint':info['fingerprint']}))
        self.assertFalse(json.loads(output)['action_performed'])
        self.assertEqual(json.loads(output)['elements'],[{'ref':'a'}])

    @connected
    async def test_human_handoff_not_converted_to_ordinary_error(self):
        self.browser.perform.side_effect=HumanInterventionRequired('人工验证')
        tool=self.access.tools()[2];guard=LoopGuard(lambda *args:None);guard.wrap(tool)
        info=self.access.describe(self.rows['read_browser']['tool_id'])
        with self.assertRaises(HumanInterventionRequired):
            await tool.on_invoke_tool(self.ctx,json.dumps({'tool_id':info['tool_id'],'arguments':{},'fingerprint':info['fingerprint']}))
        self.assertEqual(guard.failures['tool'],0)

    @connected
    async def test_approval_callback_toggle_rechecked_before_execution(self):
        original=MCPUtil.to_function_tool
        tid=self.rows['read_browser']['tool_id']
        async def approval(ctx,args,call_id):
            self.registry.set_enabled(tid,False)
            return False
        def convert(*args,**kwargs):
            tool=original(*args,**kwargs);tool.needs_approval=approval;return tool
        with patch.object(MCPUtil,'to_function_tool',side_effect=convert):
            with self.assertRaises(PermissionError):await self.invoke('read_browser',{})
        self.browser.perform.assert_not_awaited()

    @connected
    async def test_live_source_filter_and_definition_change_reject_execution(self):
        listed=self.server.list_tools
        with patch.object(self.server,'list_tools',AsyncMock(return_value=[])):
            with self.assertRaises(ToolCallFailure):await self.invoke('read_browser',{})
        await self.registry.refresh('mcp:muse-browser')
        tools=await listed()
        tools=next(t for t in tools if t.name=='read_browser')
        tools.description='changed description'
        with patch.object(self.server,'list_tools',AsyncMock(return_value=[tools])):
            with self.assertRaises(ToolCallFailure):await self.invoke('read_browser',{})
        self.browser.perform.assert_not_awaited()
