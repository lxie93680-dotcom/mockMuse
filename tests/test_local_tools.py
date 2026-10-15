import os
import tempfile
import unittest
from unittest.mock import patch
from muse.local_files import operate
from muse.local_terminal import run
from muse.documents import process

class LocalTools(unittest.IsolatedAsyncioTestCase):
    async def test_terminal(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':root,'KIMI_API_KEY':'hidden'}):
            r=await run("printf hello; printf error >&2; exit 3")
            self.assertEqual((r['stdout'],r['stderr'],r['exit_code']),('hello','error',3))
            r=await run('sleep 3',timeout=1)
            self.assertTrue(r['timed_out'])
            r=await run('printf "%s" "$KIMI_API_KEY"')
            self.assertEqual(r['stdout'],'')
    async def test_file_edit_search(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':root}):
            operate('write','demo.txt','hello world')
            operate('edit','demo.txt','Muse',query='world')
            self.assertEqual(operate('read','demo.txt')['content'],'hello Muse')
            self.assertTrue(operate('search',query='Muse')['matches'])
            with self.assertRaises(ValueError): operate('edit','demo.txt','x',query='missing')
    async def test_documents(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':root}):
            for path,content in [('demo.docx','hello Muse'),('demo.pdf','hello Muse'),('demo.xlsx','[["name","value"],["Muse",1]]')]:
                process('create',path,content)
                result=process('read',path)
                self.assertIn('Muse',str(result))
                with self.assertRaises(FileExistsError): process('create',path,content)
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
            agent,client=make_agent(browser,lambda *a:None,get_provider(),lambda b:sent.append(b) or '/api/images/test.jpg')
        try:
            tools={t.name:t for t in agent.tools}
            from agents.tool_context import ToolContext
            ctx=ToolContext(context=None,tool_name='browser_crop_image',tool_call_id='test',tool_arguments='{}')
            self.assertNotIn('linux_terminal',tools)
            result=await tools['browser_crop_image'].on_invoke_tool(ctx,json.dumps({'x':10,'y':10,'width':40,'height':30,'scale':2}))
            self.assertTrue(json.loads(result)['sent'])
            with Image.open(BytesIO(sent[0])) as picture:self.assertEqual(picture.size,(80,60))
            result=await tools['browser_crop_image'].on_invoke_tool(ctx,json.dumps({'x':190,'y':10,'width':40,'height':30}))
            self.assertEqual(len(sent),1)
            self.assertIn('error',result.lower())
        finally: await client.close()
