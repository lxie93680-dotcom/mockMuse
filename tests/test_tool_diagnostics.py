import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from agents import function_tool
from agents.tool_context import ToolContext
from muse.compression import CheckpointStore
from muse.context_history import RunCheckpoint
from muse.loop_guard import LoopGuard,LoopStopped
from muse.tool_diagnostics import ToolDiagnostics,safe_text

class ToolDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_third_error_saved_before_stop(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','s');identity=store.message_store();task=identity.begin_task()
            req=identity.request_started();identity.response_received(req)
            inputs=[{'type':'function_call','call_id':'c'+str(i),'name':'broken','arguments':'{}'} for i in range(3)]
            checkpoint=RunCheckpoint(inputs,store.observe);checkpoint.flush()
            logs=[];guard=LoopGuard(lambda *a:logs.append(a),checkpoint=checkpoint)
            guard.diagnostics=ToolDiagnostics(store,lambda *a:logs.append(a),task)
            @function_tool
            async def broken() -> str:raise RuntimeError('元素被遮挡')
            guard.wrap(broken)
            for i in range(3):
                ctx=ToolContext(context=None,tool_name='broken',tool_call_id='c'+str(i),tool_arguments='{}')
                if i==2:
                    with self.assertRaises(LoopStopped):await broken.on_invoke_tool(ctx,'{}')
                else:await broken.on_invoke_tool(ctx,'{}')
            checkpoint.close()
            outputs=[x for x in checkpoint.items if x.get('type')=='function_call_output']
            self.assertEqual(len(outputs),3)
            self.assertIn('元素被遮挡',outputs[-1]['output'])
            with closing(store.connect()) as db:
                errors=db.execute("SELECT detail,task_id,api_request_id FROM tool_events WHERE event_type='error' ORDER BY rowid").fetchall()
            self.assertEqual(len(errors),3)
            self.assertEqual(json.loads(errors[-1][0])['error_message'],'元素被遮挡')
            self.assertEqual(errors[-1][1:],(task,req))

    def test_credentials_removed(self):
        with patch.dict('os.environ',{'KIMI_API_KEY':'secret-test-key'}):
            self.assertNotIn('secret-test-key',safe_text('failed secret-test-key'))
        self.assertNotIn('abc123',safe_text('Authorization: Bearer abc123'))
