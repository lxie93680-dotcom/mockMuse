import os
import unittest
from unittest.mock import patch

class LocalTools(unittest.IsolatedAsyncioTestCase):
    async def test_crop_tool(self):
        import asyncio,json
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from muse.agent import make_agent
        from muse.models import get_provider
        output=BytesIO();Image.new('RGB',(200,100),'white').save(output,format='JPEG')
        browser=SimpleNamespace(lock=asyncio.Lock(),screenshot=AsyncMock(return_value=output.getvalue()))
        sent=[]
        with patch.dict(os.environ,{'KIMI_API_KEY':'test'}):
            agent,client=make_agent(browser,lambda *a:None,get_provider('kimi'),lambda b:sent.append(b) or '/api/images/test.jpg')
        try:
            from agents import RunContextWrapper
            await agent._muse_browser_mcp.connect()
            tools={t.name:t for t in await agent.get_all_tools(RunContextWrapper(context=None))}
            from agents.tool_context import ToolContext
            ctx=ToolContext(context=None,tool_name='browser_crop_image',tool_call_id='test',tool_arguments='{}')
            self.assertTrue({'local_files','local_terminal','linux_files','linux_terminal'}.isdisjoint(tools))
            self.assertNotIn('local_documents',tools)
            result=await tools['mcp_Muse_Browser__browser_crop_image'].on_invoke_tool(ctx,json.dumps({'x':10,'y':10,'width':40,'height':30,'scale':2}))
            self.assertTrue(json.loads(result['text'])['sent'])
            with Image.open(BytesIO(sent[0])) as picture:self.assertEqual(picture.size,(80,60))
            result=await tools['mcp_Muse_Browser__browser_crop_image'].on_invoke_tool(ctx,json.dumps({'x':190,'y':10,'width':40,'height':30}))
            self.assertEqual(len(sent),1)
            self.assertIn('error',str(result).lower())
        finally:
            await agent._muse_browser_mcp.cleanup()
            await client.close()
