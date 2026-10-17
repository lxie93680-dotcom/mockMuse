import asyncio
import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from agents import Agent, RunContextWrapper
from agents.mcp.util import MCPUtil
from agents.tool_context import ToolContext
from muse.browser_mcp import BrowserMCP
from muse.browser_diagnostics import trace_context
from muse.human_handoff import HumanInterventionRequired
from muse.delegation import child_tools
from muse.tool_results import ToolResultProcessor
from muse.loop_guard import LoopGuard, normalize


class BrowserMCPTests(unittest.IsolatedAsyncioTestCase):
    def setup_browser(self):
        image=io.BytesIO();Image.new('RGB',(200,100),'white').save(image,format='JPEG')
        browser=SimpleNamespace(lock=asyncio.Lock(),perform=AsyncMock(return_value={'title':'fixture','text':'hello','elements':[]}),
            active_page=AsyncMock(return_value='page'),screenshot=AsyncMock(return_value=image.getvalue()),
            manual=AsyncMock(),width=400,height=200,image_width=200,image_height=100)
        return browser

    async def test_protocol_catalog_and_sdk_registration(self):
        browser=self.setup_browser()
        server=BrowserMCP(browser,lambda *args:None,lambda picture:'/api/images/test.jpg')
        async with server.session():
            tools=await server.list_tools()
            self.assertEqual({t.name for t in tools},{'browser_action','browser_view','browser_click','browser_send_image','browser_crop_image','read_browser'})
            from muse.agent import make_agent
            from muse.models import Provider
            provider=Provider('custom','test','test','https://example.com/v1','test',{},'CUSTOM_API_KEY')
            agent,client=make_agent(browser,lambda *args:None,provider,browser_mcp=server)
            try:
                self.assertTrue({'browser_action','browser_view','browser_click','browser_send_image','browser_crop_image'}.isdisjoint(t.name for t in agent.tools))
                self.assertEqual(agent.mcp_servers,[server])
                functions=await agent.get_all_tools(RunContextWrapper(context=None))
            finally:
                await client.close()
            functions=[t for t in functions if t.name.startswith('mcp_')]
            self.assertTrue(all(t.name.startswith('mcp_Muse_Browser__') for t in functions))
            readonly={t.name for t in child_tools(functions)}
            self.assertEqual(readonly,{'mcp_Muse_Browser__read_browser','mcp_Muse_Browser__browser_view'})
        self.assertFalse(server.status['ready'])
        self.assertIsNone(server.worker)

    async def test_screenshot_and_publishing_use_same_browser(self):
        browser=self.setup_browser();sent=[]
        server=BrowserMCP(browser,lambda *args:None,lambda picture:sent.append(picture) or '/api/images/test.jpg')
        async with server.session():
            result=await server.call_tool('browser_view',{})
            self.assertEqual(result.content[1].type,'image')
            with Image.open(io.BytesIO(base64.b64decode(result.content[1].data))) as image:
                self.assertEqual(image.size,(200,100))
            result=await server.call_tool('browser_crop_image',{'x':10,'y':10,'width':40,'height':30,'scale':2})
            self.assertTrue(json.loads(result.content[0].text)['sent'])
            with Image.open(io.BytesIO(sent[0])) as image:
                self.assertEqual(image.size,(80,60))
            await server.call_tool('browser_send_image',{})
            self.assertEqual(len(sent),2)
            await server.call_tool('browser_click',{'x':50,'y':25})
            browser.manual.assert_awaited_once_with('click',100.0,50.0)

    async def test_context_diagnostics_and_handoff_cross_protocol(self):
        browser=self.setup_browser()
        marker=object()
        async def perform(*args):
            self.assertIs(trace_context.get(),marker)
            raise HumanInterventionRequired('网页出现人机验证')
        browser.perform=perform
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            token=trace_context.set(marker)
            try:
                with self.assertRaises(HumanInterventionRequired):
                    await server.call_tool('read_browser',{})
                browser.perform=AsyncMock(side_effect=HumanInterventionRequired('网页出现人机验证'))
                functions=await MCPUtil.get_function_tools(server,False,RunContextWrapper(context=None),Agent(name='test'),include_server_in_tool_names=True)
                tool=next(t for t in functions if t.name.endswith('__browser_action'))
                guard=LoopGuard(lambda *args:None);guard.wrap(tool)
                ctx=ToolContext(context=None,tool_name=tool.name,tool_call_id='human',tool_arguments='{}')
                with self.assertRaises(HumanInterventionRequired):
                    await tool.on_invoke_tool(ctx,'{"action":"read"}')
                self.assertEqual(guard.failures['tool'],0)
            finally:
                trace_context.reset(token)
            self.assertEqual(server.calls,{})

    async def test_mcp_projection_keeps_images_and_page_metadata(self):
        browser=self.setup_browser();server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            functions=await MCPUtil.get_function_tools(server,False,RunContextWrapper(context=None),Agent(name='test'),include_server_in_tool_names=True)
            view=next(t for t in functions if t.name.endswith('__browser_view'))
            ctx=ToolContext(context=None,tool_name=view.name,tool_call_id='image',tool_arguments='{}')
            result=await view.on_invoke_tool(ctx,'{}')
            projected=ToolResultProcessor().process(view.name,'{}',result,'image')
            self.assertIsInstance(projected,list)
            self.assertTrue(any(isinstance(p,dict) and p.get('type')=='image' for p in projected))
            raw={'type':'text','text':json.dumps({'text':'hello world','elements':[{'ref':'e1'}],'status':'needs_observation','action_performed':False})}
            projected=ToolResultProcessor().process('mcp_Muse_Browser__browser_action','{}',raw,'read')
            self.assertEqual(json.loads(projected)['elements'],[{'ref':'e1'}])
            self.assertEqual(normalize(raw)['status'],'needs_observation')

    async def test_cancel_rpc_stops_action_and_releases_call_context(self):
        browser=self.setup_browser();started=asyncio.Event();finished=asyncio.Event()
        async def slow(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        browser.perform=slow
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            task=asyncio.create_task(server.call_tool('read_browser',{}))
            await asyncio.wait_for(started.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            await asyncio.wait_for(finished.wait(),1)
            self.assertEqual(server.calls,{})

    async def test_browser_workspace_not_notified_by_discovery_only_real_invocation(self):
        from unittest.mock import Mock
        notify=Mock();browser=self.setup_browser();server=BrowserMCP(browser,lambda *args:None,on_use=notify)
        async with server.session():
            await server.list_tools();notify.assert_not_called()
            await server.call_tool('read_browser',{})
            notify.assert_called_once_with('read_browser');browser.perform.assert_awaited_once_with('read')
