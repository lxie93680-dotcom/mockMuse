import asyncio
import base64
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from agents.tool_context import ToolContext
from muse.native_tools import NativeRuntime, apply_patch_text, load_image, native_tools
from muse.delegation import child_tools


class NativeToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE':str(self.root),'MUSE_LOCAL_ALLOWED_FILES':'','KIMI_API_KEY':'fake-secret-not-inherited'})
        self.env.start()
        self.runtime = NativeRuntime()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.env.stop()
        self.temp.cleanup()

    async def test_command_exit_code_output_and_environment(self):
        result = await self.runtime.exec_command('printf hello; printf error >&2; exit 3')
        self.assertEqual((result['stdout'],result['stderr'],result['exit_code']),('hello','error',3))
        self.assertIsNone(result['session_id'])
        result = await self.runtime.exec_command('printf "%s" "$KIMI_API_KEY"')
        self.assertEqual(result['stdout'],'')
        result = await self.runtime.exec_command('pwd',str(self.root))
        self.assertEqual(Path(result['stdout'].strip()).resolve(),self.root.resolve())

    async def test_interactive_command_session(self):
        result = await self.runtime.exec_command('read -r line; printf "received:%s" "$line"',yield_time_ms=1)
        sid = result['session_id']
        self.assertIsNotNone(sid)
        self.assertEqual(result['status'],'running')
        result = await self.runtime.write_stdin(sid,'Muse\n')
        self.assertEqual(result['stdout'],'received:Muse')
        self.assertEqual(result['exit_code'],0)
        self.assertIsNone(result['session_id'])
        with self.assertRaises(ValueError):
            await self.runtime.write_stdin(sid)

    async def test_timeout_and_cleanup(self):
        result = await self.runtime.exec_command('sleep 20',yield_time_ms=1,timeout_seconds=1)
        sid = result['session_id']
        result = await self.runtime.write_stdin(sid,yield_time_ms=2000)
        self.assertTrue(result['timed_out'])
        self.assertNotEqual(result['exit_code'],0)
        result = await self.runtime.exec_command('sleep 20',yield_time_ms=1)
        session = self.runtime.sessions[result['session_id']]
        await self.runtime.close()
        self.assertIsNotNone(session.process.returncode)
        self.assertEqual(self.runtime.sessions,{})

    async def test_cancel_exec_cleans_session(self):
        task = asyncio.create_task(self.runtime.exec_command('sleep 20',yield_time_ms=10000))
        for _ in range(100):
            if self.runtime.sessions:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.runtime.sessions)
        session = next(iter(self.runtime.sessions.values()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(session.process.returncode)
        self.assertEqual(self.runtime.sessions,{})

    async def test_output_is_bounded_and_paginated(self):
        command = shlex.quote(sys.executable) + ' -c ' + shlex.quote('print("x"*100000)')
        result = await self.runtime.exec_command(command)
        self.assertLessEqual(len(result['stdout']),6000)
        self.assertTrue(result['truncated'])
        self.assertGreater(result['dropped_bytes'],0)
        total = len(result['stdout'])
        while result['session_id']:
            result = await self.runtime.write_stdin(result['session_id'])
            total += len(result['stdout'])
        self.assertLessEqual(total,65536)

    async def test_patch_add_update_delete_and_absolute_workspace_path(self):
        result = apply_patch_text('*** Begin Patch\n*** Add File: notes/a.txt\n+hello\n+world\n*** End Patch')
        target = self.root/'notes/a.txt'
        self.assertTrue(result['applied'])
        self.assertEqual(target.read_text(),'hello\nworld\n')
        apply_patch_text('*** Begin Patch\n*** Update File: '+str(target)+'\n@@\n hello\n-world\n+Muse\n*** End Patch')
        self.assertEqual(target.read_text(),'hello\nMuse\n')
        apply_patch_text('*** Begin Patch\n*** Delete File: notes/a.txt\n*** End Patch')
        self.assertFalse(target.exists())

    async def test_patch_failure_preflights_entire_batch(self):
        target = self.root/'a.txt';target.write_text('one\n')
        with self.assertRaises(ValueError):
            apply_patch_text('*** Begin Patch\n*** Add File: new.txt\n+new\n*** Update File: a.txt\n@@\n-missing\n+two\n*** End Patch')
        self.assertFalse((self.root/'new.txt').exists())
        self.assertEqual(target.read_text(),'one\n')
        with self.assertRaises(FileExistsError):
            apply_patch_text('*** Begin Patch\n*** Add File: a.txt\n+overwrite\n*** End Patch')

    async def test_patch_ambiguous_hunks_and_newline_preservation(self):
        target = self.root/'a.txt';target.write_bytes(b'one\r\ntwo\r\n')
        apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-two\n+three\n*** End Patch')
        self.assertEqual(target.read_bytes(),b'one\r\nthree\r\n')
        target.write_text('same\nsame\n')
        with self.assertRaises(ValueError):
            apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-same\n+other\n*** End Patch')
        apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-same\n+last\n*** End of File\n*** End Patch')
        self.assertEqual(target.read_text(),'same\nlast\n')

    async def test_patch_rolls_back_write_failure(self):
        a=self.root/'a.txt';b=self.root/'b.txt'
        a.write_text('a\n');b.write_text('b\n')
        original=Path.write_bytes
        failed=False
        def fail_once(path,data):
            nonlocal failed
            if path==b and not failed:
                failed=True
                raise OSError('simulated disk failure')
            return original(path,data)
        with patch.object(Path,'write_bytes',fail_once),self.assertRaises(OSError):
            apply_patch_text('*** Begin Patch\n*** Update File: a.txt\n@@\n-a\n+new a\n*** Update File: b.txt\n@@\n-b\n+new b\n*** End Patch')
        self.assertEqual(a.read_text(),'a\n');self.assertEqual(b.read_text(),'b\n')

    async def test_file_image_and_cwd_boundaries(self):
        (self.root/'link').symlink_to(self.root/'real')
        for path in ('../outside.txt','.env','link/a.txt','/etc/passwd'):
            with self.assertRaises(ValueError):
                apply_patch_text('*** Begin Patch\n*** Add File: '+path+'\n+data\n*** End Patch')
            with self.assertRaises(ValueError):
                load_image(path)
        with self.assertRaises(ValueError):
            await self.runtime.exec_command('pwd',workdir='../')

    async def test_real_image_pixels_and_size(self):
        target=self.root/'image.png'
        Image.new('RGB',(3000,1000),'red').save(target)
        result=load_image(str(target))
        metadata=json.loads(result[0].text)
        self.assertEqual(metadata['original_size'],[3000,1000])
        pixels=base64.b64decode(result[1].image_url.split(',',1)[1])
        with Image.open(io.BytesIO(pixels)) as image:
            self.assertEqual(image.size[0],2048)
            self.assertGreater(image.getpixel((0,0))[0],240)
        self.assertIn('不是浏览器截图',metadata['notice'])

    async def test_native_registration_with_and_without_mcp(self):
        from muse.agent import make_agent
        from muse.models import get_provider
        from muse.staged_workspace import StagedWorkspace
        import shutil
        transaction=StagedWorkspace(self.root).begin()
        try:
            for servers in ([],[object()]):
                agent,client=make_agent(None,lambda *args:None,get_provider('kimi'),mcp_servers=servers)
                try:
                    tools={tool.name:tool for tool in agent.tools}
                    self.assertTrue({'exec_command','write_stdin','apply_patch','view_image'}<=tools.keys())
                    children={tool.name for tool in child_tools(agent.tools)}
                    self.assertTrue({'exec_command','write_stdin','apply_patch'}.isdisjoint(children))
                    self.assertIn('view_image',children)
                    ctx=ToolContext(context=None,tool_name='exec_command',tool_call_id='command',tool_arguments='{}')
                    result=await tools['exec_command'].on_invoke_tool(ctx,json.dumps({'command':'printf native'}))
                    self.assertEqual(json.loads(result)['stdout'],'native')
                finally:
                    await agent._muse_native_runtime.close()
                    await client.close()
        finally:
            transaction.abandon();transaction.release()
            shutil.rmtree(transaction.control,ignore_errors=True)

    async def test_agent_task_closes_commands_on_runner_failure(self):
        from muse.agent import run_agent
        from muse.models import get_provider
        captured=[]
        async def fail(agent,**kwargs):
            runtime=agent._muse_native_runtime
            result=await runtime.exec_command('sleep 20',yield_time_ms=1)
            captured.append(runtime.sessions[result['session_id']])
            raise RuntimeError('simulated model failure')
        with patch('muse.agent.Runner.run',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'simulated model failure'):
                await run_agent(None,'test',[],lambda *args:None,provider=get_provider('kimi'))
        self.assertIsNotNone(captured[0].process.returncode)

    async def test_local_image_transport_is_not_labeled_as_browser_screenshot(self):
        from types import SimpleNamespace
        from agents.models.chatcmpl_converter import Converter
        from muse.agent import preserve_browser_images
        Image.new('RGB',(4,4),'blue').save(self.root/'small.png')
        parts=load_image('small.png')
        items=[{'role':'user','content':'看图片'}, {'type':'function_call_output','call_id':'local-image','output':[
            {'type':'input_text','text':parts[0].text},
            {'type':'input_image','image_url':parts[1].image_url,'detail':'high'}]}]
        projected=preserve_browser_images(SimpleNamespace(model_data=SimpleNamespace(input=items,instructions='test')))
        messages=Converter.items_to_messages(projected.input)
        self.assertIn('本地文件图片不是浏览器截图',projected.input[-1]['content'][0]['text'])
        self.assertTrue(any(p.get('type')=='image_url' for m in messages if m['role']=='user' for p in m['content'] if isinstance(p,dict)))


class NativeTaskAdmission(unittest.TestCase):
    def test_optional_backends_do_not_block_native_task(self):
        import importlib
        from types import SimpleNamespace
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as data,patch.dict(os.environ,{
            'MUSE_DATA_DIR':data,'MUSE_BROWSER_BACKEND':'extension',
            'MUSE_FILESYSTEM_MCP':'0','DEEPSEEK_API_KEY':'test-only','LLM_PROVIDER':'deepseek'}):
            import muse.app as app
            app=importlib.reload(app)
            with TestClient(app.app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',1234)) as client:
                with patch.object(app,'browser',SimpleNamespace(status={'ready':False,'error':'offline'},connected=False)),patch.object(app,'run_agent',AsyncMock(return_value=('native completed',[]))):
                    app.filesystem.status.update(enabled=True,ready=False,error='MCP offline')
                    self.assertEqual(client.post('/api/tasks',json={'prompt':'执行本地命令'}).status_code,202)
                    if app.job is not None:
                        async def settle():
                            await app.job
                        client.portal.call(settle)
                    self.assertEqual(app.state['status'],'completed')
