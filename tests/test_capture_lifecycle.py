import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
import muse.app as app

class CaptureLifecycle(unittest.IsolatedAsyncioTestCase):
    async def check_capture(self,status,owner,viewers,expected,used=False):
        browser=SimpleNamespace(ensure_stream=AsyncMock(),stop_stream=AsyncMock())
        async def end_tick(*args):raise asyncio.CancelledError()
        with patch.object(app,'browser',browser),patch.object(app,'state',{'status':status,'browser_workspace':used}),patch.object(app,'control_owner',owner),patch.object(app,'clients',{1} if viewers else set()),patch('muse.app.asyncio.sleep',end_tick):
            with self.assertRaises(asyncio.CancelledError):await app.capture()
        self.assertEqual(browser.ensure_stream.await_count,int(expected))
        self.assertEqual(browser.stop_stream.await_count,int(not expected))
    async def test_idle_and_terminal_states_stop_frames(self):
        for status in ('idle','completed','stopped','error'):
            await self.check_capture(status,None,True,False)
    async def test_task_and_handoff_keep_frames(self):
        for status in ('running','needs_human'):
            await self.check_capture(status,None,True,True,used=True)
    async def test_manual_control_keeps_frames(self):
        await self.check_capture('completed',object(),True,True)
    async def test_no_viewer_stops_frames_even_when_running(self):
        await self.check_capture('running',None,False,False)

    async def test_file_tasks_do_not_start_frames_even_with_stale_manual_owner(self):
        await self.check_capture('running',None,True,False)
        await self.check_capture('running',object(),True,False)
    async def test_browser_frames_require_viewers_even_after_use(self):
        await self.check_capture('running',None,False,False,used=True)
    async def test_final_frame_only_for_actual_browser_use(self):
        browser=SimpleNamespace(connected=True,lock=asyncio.Lock(),screenshot=AsyncMock(return_value=b'jpeg'),url='https://eval.invalid',width=100,height=80)
        state={'browser_workspace':False,'status':'running','conversation_id':'c','task_id':'t','frame_seq':0}
        with patch.object(app,'state',state),patch.object(app,'browser',browser),patch.object(app,'clients',set()):
            await app.refresh_browser_frame();browser.screenshot.assert_not_awaited()
            app.use_browser_workspace('read_browser');await app.refresh_browser_frame()
            self.assertEqual(state['frame'],'anBlZw==');self.assertEqual(state['browser_use_sequence'],1)
    async def test_late_frames_ignored_without_browser_use_and_tagged_when_used(self):
        state={'status':'running','browser_workspace':False,'frame':None,'frame_seq':0,'conversation_id':'c','task_id':'t'}
        browser=SimpleNamespace(connected=True,width=100,height=80)
        queue=asyncio.Queue();frames=asyncio.Queue(maxsize=1)
        with patch.object(app,'state',state),patch.object(app,'browser',browser),patch.object(app,'clients',{(queue,frames)}),patch.object(app,'control_owner',None):
            app.broadcast_frame('old','old');self.assertIsNone(state['frame']);self.assertTrue(frames.empty())
            app.use_browser_workspace();app.broadcast_frame('new','https://eval.invalid')
            import json
            frame=json.loads(frames.get_nowait());self.assertEqual(frame['conversation_id'],'c');self.assertEqual(frame['task_id'],'t');self.assertTrue(frame['browser_connected'])
