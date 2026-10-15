import unittest
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from agents import function_tool
from agents.tool_context import ToolContext
from muse.loop_guard import LoopGuard,LoopStopped,GuardedModel,fingerprint
from muse.agent import preserve_browser_images

class GuardTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ,{'TEST_API_KEY':'private-secret'})
    async def test_tool_failure_limit_cannot_be_swallowed(self):
        @function_tool
        async def broken() -> str:raise RuntimeError('private-secret')
        guard=LoopGuard(lambda *a:None);guard.wrap(broken)
        ctx=ToolContext(context=None,tool_name='broken',tool_call_id='t',tool_arguments='{}')
        for _ in range(2):
            result=await broken.on_invoke_tool(ctx,'{}')
            self.assertNotIn('private-secret',result)
        with self.assertRaises(LoopStopped):await broken.on_invoke_tool(ctx,'{}')
        self.assertEqual(guard.failures['tool'],3)
        with self.assertRaises(LoopStopped):await broken.on_invoke_tool(ctx,'{}')
    async def test_error_identity_and_success_reset(self):
        guard=LoopGuard(lambda *a:None)
        error={'_muse_tool_error':True,'message':'not found','error_type':'RuntimeError'}
        guard.observe('click','{"ref":1,"text":"a"}',{**error,'tool_call_id':'a'})
        guard.observe('click',{'text':'a','ref':1},{**error,'tool_call_id':'b'})
        self.assertEqual(guard.failures['tool'],2)
        guard.observe('click',{'text':'a','ref':2},error)
        self.assertEqual(guard.failures['tool'],1)
        guard.observe('click',{'text':'a','ref':2},{**error,'message':'covered'})
        self.assertEqual(guard.failures['tool'],1)
        guard.observe('fill',{'text':'a','ref':2},{**error,'message':'covered'})
        self.assertEqual(guard.failures['tool'],1)
        guard.observe('read',{}, {'title':'success'})
        self.assertEqual(guard.failures['tool'],0)
        for _ in range(20):guard.observe('read',{}, {'title':'success'})
        self.assertIsNone(guard.stopped)
        for _ in range(2):guard.observe('click',{},error)
        with self.assertRaises(LoopStopped):guard.observe('click',{},error)
        self.assertNotEqual(fingerprint('read',{},['a','b']),fingerprint('read',{},['b','a']))

    async def test_wrapped_errors_with_new_call_ids_and_success(self):
        @function_tool
        async def operation(target: int) -> str:
            if target:raise RuntimeError('not found')
            return '{"ok":true}'
        guard=LoopGuard(lambda *a:None);guard.wrap(operation)
        for i,target in enumerate([1,1,2,2,0,2,2]):
            args='{"target":'+str(target)+'}'
            ctx=ToolContext(context=None,tool_name='operation',tool_call_id=str(i),tool_arguments=args)
            await operation.on_invoke_tool(ctx,args)
        self.assertEqual(guard.failures['tool'],2)
        ctx=ToolContext(context=None,tool_name='operation',tool_call_id='last',tool_arguments='{"target":2}')
        with self.assertRaises(LoopStopped):await operation.on_invoke_tool(ctx,'{"target":2}')

    async def test_model_retries_and_cumulative_failure_limit(self):
        model=SimpleNamespace(get_response=AsyncMock(side_effect=[RuntimeError(), 'ok',RuntimeError(),RuntimeError()]))
        guard=LoopGuard(lambda *a:None);wrapped=GuardedModel(model,guard)
        self.assertEqual(await wrapped.get_response(),'ok')
        self.assertEqual(guard.failures['model'],1)
        with self.assertRaises(LoopStopped):await wrapped.get_response()
        self.assertEqual(model.get_response.await_count,4)
        self.assertEqual(guard.failures['model'],3)
    async def test_nonzero_terminal_result_counts(self):
        @function_tool
        async def terminal() -> str:return '{"exit_code":1,"stdout":""}'
        guard=LoopGuard(lambda *a:None);guard.wrap(terminal)
        ctx=ToolContext(context=None,tool_name='terminal',tool_call_id='t',tool_arguments='{}')
        for _ in range(2):await terminal.on_invoke_tool(ctx,'{}')
        with self.assertRaises(LoopStopped):await terminal.on_invoke_tool(ctx,'{}')
