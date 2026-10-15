import asyncio
import unittest
from unittest.mock import patch
import muse.app as module

class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cancels_and_reports(self):
        cancelled=[]
        async def task(*args):
            try:await asyncio.sleep(10)
            finally:cancelled.append(True)
        state={'status':'running','messages':[]}
        with patch.object(module,'execute',task),patch.object(module,'TASK_TIMEOUT_SECONDS',0.01),patch.object(module,'state',state),patch.object(module,'record') as record:
            await module.execute_with_deadline('test',False)
            self.assertEqual(cancelled,[True]);self.assertEqual(state['status'],'stopped')
            self.assertIn('30分钟',state['messages'][0]['text']);record.assert_called_once()
    async def test_user_cancel_is_not_timeout(self):
        async def task(*args):await asyncio.sleep(10)
        with patch.object(module,'execute',task),patch.object(module,'record') as record:
            job=asyncio.create_task(module.execute_with_deadline('test',False));await asyncio.sleep(0);job.cancel()
            with self.assertRaises(asyncio.CancelledError):await job
            record.assert_not_called()
