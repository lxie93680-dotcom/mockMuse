import asyncio
import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import function_tool
from agents.tool_context import ToolContext
from muse.context_history import RunCheckpoint, close_pending_tools
from muse.loop_guard import LoopGuard, GuardedModel, LoopStopped
import muse.app as app


class ContextHistory(unittest.IsolatedAsyncioTestCase):
    async def test_completed_tool_is_durable_before_next_model_call(self):
        snapshots=[]
        checkpoint=RunCheckpoint([{'role':'user','content':'make route'}], snapshots.append)
        call={'type':'function_call','call_id':'t','name':'write','arguments':'{}'}
        response=SimpleNamespace(to_input_items=lambda:[call])
        guard=LoopGuard(lambda *a:None,checkpoint=checkpoint)
        model=SimpleNamespace(get_response=AsyncMock(return_value=response))
        await GuardedModel(model,guard).get_response()
        self.assertEqual(snapshots[-1][-1],call)
        @function_tool
        async def write() -> str:return 'saved route'
        guard.wrap(write)
        ctx=ToolContext(context=None,tool_name='write',tool_call_id='t',tool_arguments='{}')
        await write.on_invoke_tool(ctx,'{}')
        self.assertEqual(snapshots[-1][-1]['output'],'saved route')
        checkpoint.close()
        self.assertEqual(len(snapshots[-1]),3)
        self.assertNotIn('结果未知',json.dumps(snapshots[-1],ensure_ascii=False))

    async def test_interrupted_tool_is_closed_without_claiming_success(self):
        checkpoint=RunCheckpoint([{'role':'user','content':'task'},
                                  {'type':'function_call','call_id':'t','name':'write','arguments':'{}'}])
        checkpoint.close()
        self.assertIn('是否生效未知',checkpoint.items[-1]['output'])
        self.assertEqual(close_pending_tools(checkpoint.items),checkpoint.items)

    async def test_failure_and_cancel_survive_reload(self):
        for error in (RuntimeError('secret'), LoopStopped('工具调用累计失败达到3次'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                state={'model':'test','status':'running','messages':[{'role':'user','text':'make route'}],'logs':[],
                       'conversation_id':'test','conversation_title':'route'}
                initial=[{'role':'user','content':'make route'}]
                async def fail(browser,prompt,history,record,*args):
                    self.assertEqual(history,[])
                    args[-1](initial+[{'type':'function_call','call_id':'t','name':'write','arguments':'{}'},
                                      {'type':'function_call_output','call_id':'t','output':'route saved'}])
                    raise error
                with patch.object(app,'DATA',Path(directory)), patch.object(app,'state',state), patch.object(app,'history',initial.copy()), patch.object(app,'run_agent',fail), patch.object(app,'publish'):
                    if isinstance(error,asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):await app.execute('make route',False)
                    else:await app.execute('make route',False)
                    with closing(app.database()) as db:
                        stored=json.loads(db.execute('select content from conversations where id="test"').fetchone()[0])
                        self.assertEqual(stored['history'][0]['content'],'make route')
                        self.assertEqual(stored['history'][2]['output'],'route saved')
                        self.assertEqual(stored['history'][-1]['role'],'assistant')
                        self.assertNotIn('secret',json.dumps(stored))
                        app.history=[]
                        app.load_conversation(db,'test')
                        self.assertEqual(app.history,stored['history'])

    async def test_deadline_reason_is_in_model_history(self):
        async def slow(*args):await asyncio.sleep(10)
        state={'model':'test','status':'running','messages':[]}
        with patch.object(app,'history',[]),patch.object(app,'state',state),patch.object(app,'execute',slow),patch.object(app,'record'),patch.object(app,'TASK_TIMEOUT_SECONDS',0.01):
            await app.execute_with_deadline('task',False)
            self.assertIn('30分钟',app.history[-1]['content'])
