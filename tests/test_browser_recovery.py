import json
import unittest
from unittest.mock import AsyncMock
from agents import function_tool
from agents.tool_context import ToolContext
from muse.browser import Browser
from muse.loop_guard import LoopGuard,LoopStopped
from muse.tool_results import ToolResultProcessor,MAX_CHARS


class BrowserRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_target_errors_refresh_without_replaying_input(self):
        for action,message in [('click_text','需要唯一可见文字'),('click','元素已失效，请重新读取页面'),('fill','元素被遮挡')]:
            with self.subTest(action=action):
                b=Browser();snapshot={'title':'Fresh','elements':[{'ref':8,'text':'候选','context':'目标地区'}]}
                b.request=AsyncMock(side_effect=[RuntimeError('Error: '+message+'\n at point'),snapshot])
                result=await b.perform(action,ref=28,text='简称')
                self.assertFalse(result['action_performed'])
                self.assertEqual(result['status'],'needs_observation')
                self.assertEqual(result['elements'],snapshot['elements'])
                self.assertEqual(b.request.await_count,2)
                self.assertEqual(b.request.call_args_list[-1].args,('read',))

    async def test_refresh_and_connection_errors_still_propagate(self):
        b=Browser();b.request=AsyncMock(side_effect=[RuntimeError('元素已失效，请重新读取页面'),ConnectionError('disconnected')])
        with self.assertRaises(ConnectionError):await b.perform('click',ref=8)
        for action,message in [('read','元素被遮挡'),('click','Input failed'),('click','untrusted text 需要唯一可见文字')]:
            b.request=AsyncMock(side_effect=RuntimeError(message))
            with self.assertRaises(RuntimeError):await b.perform(action)
            self.assertEqual(b.request.await_count,1)

    async def test_identical_recovery_is_a_failed_action_not_a_success(self):
        b=Browser();snapshot={'title':'Fresh','elements':[]}
        async def request(action,**params):
            if action=='read':return snapshot
            raise RuntimeError('需要唯一可见文字')
        b.request=request
        @function_tool
        async def browser_action() -> str:return json.dumps(await b.perform('click_text',text='简称'),ensure_ascii=False)
        guard=LoopGuard(lambda *a:None);guard.result_processor=ToolResultProcessor();guard.wrap(browser_action)
        for i in range(2):
            ctx=ToolContext(context=None,tool_name='browser_action',tool_call_id=str(i),tool_arguments='{}')
            result=json.loads(await browser_action.on_invoke_tool(ctx,'{}'))
            self.assertFalse(result['action_performed'])
        self.assertEqual(guard.failures['tool'],2)
        with self.assertRaises(LoopStopped):await browser_action.on_invoke_tool(ctx,'{}')

    def test_recovery_flags_survive_large_page_projection(self):
        page={'title':'Fresh','elements':[{'ref':i,'text':'字段'*100} for i in range(120)],
              'status':'needs_observation','action_performed':False,'message':'元素被遮挡','recovery':'重新选择目标'}
        rendered=ToolResultProcessor().process('browser_action','{}',json.dumps(page,ensure_ascii=False),'c')
        self.assertLessEqual(len(rendered),MAX_CHARS)
        result=json.loads(rendered)
        self.assertFalse(result['action_performed'])
        self.assertEqual(result['status'],'needs_observation')
        self.assertEqual(result['recovery'],'重新选择目标')
