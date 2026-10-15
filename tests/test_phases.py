import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agents import function_tool
from agents.tool_context import ToolContext
from muse.phases import Phases
from muse.loop_guard import LoopGuard,GuardedModel

class PhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_tool_stages(self):
        events=[];guard=LoopGuard(lambda *a:None,lambda *a:events.append(a))
        guard.phases.set('thinking')
        model=GuardedModel(SimpleNamespace(get_response=AsyncMock(return_value='ok')),guard)
        self.assertEqual(await model.get_response(),'ok')
        @function_tool
        async def sample() -> str:return 'result'
        guard.wrap(sample)
        ctx=ToolContext(context=None,tool_name='sample',tool_call_id='one',tool_arguments='{}')
        await sample.on_invoke_tool(ctx,'{}')
        self.assertEqual([e[0] for e in events],['thinking','replying','checking_result','executing_tool','checking_result'])
        self.assertEqual(events[-1][2],[])
    async def test_parallel_tools_and_failure_clear(self):
        events=[];phases=Phases(lambda *a:events.append(a))
        phases.tool_started('a','read');phases.tool_started('b','write')
        phases.tool_finished('a')
        self.assertEqual(events[-1][0],'executing_tool');self.assertEqual(events[-1][2],['write'])
        phases.tool_finished('b');self.assertEqual(events[-1][0],'checking_result')
        guard=LoopGuard(lambda *a:None,lambda *a:events.append(a))
        @function_tool
        async def broken() -> str:raise RuntimeError('failed')
        guard.wrap(broken)
        ctx=ToolContext(context=None,tool_name='broken',tool_call_id='bad',tool_arguments='{}')
        await broken.on_invoke_tool(ctx,'{}')
        self.assertEqual(events[-1][0],'checking_result');self.assertEqual(guard.phases.active_tools,{})
