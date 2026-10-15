import asyncio
import unittest
from unittest.mock import patch
from muse.progress import Activity
from muse.phases import Phases

class ProgressTests(unittest.TestCase):
    def test_child_phases_tools_and_lifecycle(self):
        events=[]
        with patch('muse.progress.notify',events.append):
            a=Activity('读取文件','delegation','parent-task')
            phases=Phases(a.phase)
            phases.set('replying','等待模型')
            phases.tool_started('call','read_file')
            self.assertEqual(events[-1]['active_tools'],['read_file'])
            self.assertEqual(events[-1]['parent_task_id'],'parent-task')
            phases.tool_finished('call')
            a.finish()
            phases.set('replying')
            self.assertEqual(events[-1]['status'],'completed')
            self.assertEqual(events[-1]['active_tools'],[])
            self.assertIsNotNone(events[-1]['ended_at'])

    def test_compression_and_cancellation(self):
        events=[]
        with patch('muse.progress.notify',events.append):
            a=Activity('上下文压缩','compression','p')
            a.phase('replying')
            a.finish(asyncio.CancelledError())
        self.assertEqual(events[-1]['purpose'],'compression')
        self.assertEqual(events[-1]['status'],'interrupted')

    def test_telemetry_failure_does_not_fail_task(self):
        with patch('muse.progress.notify',side_effect=RuntimeError()):
            a=Activity('任务','delegation');a.finish()
        self.assertEqual(a.event['status'],'completed')
