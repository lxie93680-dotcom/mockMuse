import importlib,os,tempfile,unittest
from unittest.mock import AsyncMock,patch
from agents import function_tool
from agents.tool_context import ToolContext
from fastapi.testclient import TestClient
from muse.loop_guard import LoopGuard
from muse.human_handoff import HumanInterventionRequired,human_reason
from muse.playwright_browser import PlaywrightBrowser

class Handoff(unittest.IsolatedAsyncioTestCase):
    async def test_captcha_in_iframe_stops_snapshot_before_next_action(self):
        b=PlaywrightBrowser();b.tool=AsyncMock(return_value='### Page\n- Page URL: https://example.com\n```yaml\n- iframe:\n  - generic: 亲，滑动下方滑块，我们马上回来哦\n```');b.sync_video=AsyncMock()
        with self.assertRaises(HumanInterventionRequired):await b.snapshot()
        b.sync_video.assert_awaited_once()
        self.assertIn('滑动下方滑块',b.last_observation['snapshot'])

    async def test_handoff_not_swallowed_or_counted_as_tool_error(self):
        @function_tool
        async def blocked() -> str:raise HumanInterventionRequired('手动验证')
        guard=LoopGuard(lambda *a:None);guard.wrap(blocked)
        ctx=ToolContext(context=None,tool_name='blocked',tool_call_id='h',tool_arguments='{}')
        with self.assertRaises(HumanInterventionRequired):await blocked.on_invoke_tool(ctx,'{}')
        self.assertEqual(guard.failures['tool'],0)
        self.assertIsNone(human_reason('- button "确认"'))

class HandoffState(unittest.TestCase):
    def test_wait_takeover_and_resume_only_after_verification(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-only','LLM_PROVIDER':'deepseek','MUSE_DATA_DIR':directory,'MUSE_BROWSER_BACKEND':'extension','MUSE_DESKTOP_COMMANDER':'0','MUSE_FILESYSTEM_MCP':'0'}):
            import muse.app as app
            app=importlib.reload(app)
            with TestClient(app.app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',1234)) as client:
                client.post('/api/login',json={'token':app.TOKEN})
                app.state.update(status='running',task_id=None)
                with patch.object(app,'run_agent',AsyncMock(side_effect=HumanInterventionRequired('网页出现人机验证'))):
                    client.portal.call(app.execute,'test',False)
                self.assertEqual(app.state['status'],'needs_human')
                self.assertIn('人机验证',app.state['human_handoff']['reason'])
                with client.websocket_connect('/ws',headers={'host':'127.0.0.1:8000','origin':'http://127.0.0.1:8000'}) as ws:
                    ws.receive_json();ws.send_json({'type':'takeover','active':True})
                    while True:
                        msg=ws.receive_json()
                        if msg.get('type')=='control':break
                    self.assertTrue(msg['active'])
                    with patch.object(app.browser,'perform',AsyncMock(side_effect=HumanInterventionRequired('仍有验证'))):
                        self.assertEqual(client.post('/api/resume',json={}).status_code,409)
                    self.assertEqual(app.state['status'],'needs_human')
                    with patch.object(app.browser,'perform',AsyncMock(return_value={'title':'Ready'})),patch.object(app.browser,'release_input',AsyncMock()),patch.object(app,'run_agent',AsyncMock(return_value=('完成',[]))):
                        self.assertEqual(client.post('/api/resume',json={}).status_code,202)
