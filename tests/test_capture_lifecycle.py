import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
import muse.app as app

class CaptureLifecycle(unittest.IsolatedAsyncioTestCase):
    async def check_capture(self,status,owner,viewers,expected):
        browser=SimpleNamespace(ensure_stream=AsyncMock(),stop_stream=AsyncMock())
        async def end_tick(*args):raise asyncio.CancelledError()
        with patch.object(app,'browser',browser),patch.object(app,'state',{'status':status}),patch.object(app,'control_owner',owner),patch.object(app,'clients',{1} if viewers else set()),patch('muse.app.asyncio.sleep',end_tick):
            with self.assertRaises(asyncio.CancelledError):await app.capture()
        self.assertEqual(browser.ensure_stream.await_count,int(expected))
        self.assertEqual(browser.stop_stream.await_count,int(not expected))
    async def test_idle_and_terminal_states_stop_frames(self):
        for status in ('idle','completed','stopped','error'):
            await self.check_capture(status,None,True,False)
    async def test_task_and_handoff_keep_frames(self):
        for status in ('running','needs_human'):
            await self.check_capture(status,None,True,True)
    async def test_manual_control_keeps_frames(self):
        await self.check_capture('completed',object(),True,True)
    async def test_no_viewer_stops_frames_even_when_running(self):
        await self.check_capture('running',None,False,False)
