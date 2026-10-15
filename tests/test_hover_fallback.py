import unittest
from unittest.mock import AsyncMock
from muse.browser import Browser

class HoverFallback(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_hover_uses_mouse_move(self):
        b=Browser();e={'ref':8,'tag':'a','text':'账户','context':'用户','href':'https://example.com','x':100,'y':50};snapshot={'elements':[e]}
        b.last_observation=snapshot
        b.request=AsyncMock(side_effect=[RuntimeError('未知操作'),snapshot,{},snapshot])
        result=await b.perform('hover',ref=8)
        self.assertEqual(result,snapshot)
        self.assertEqual(b.request.call_args_list[2].kwargs['input'],{'action':'move','x':100,'y':50})
    async def test_changed_element_is_not_hovered(self):
        b=Browser();b.last_observation={'elements':[{'ref':8,'tag':'a','text':'账户'}]}
        b.request=AsyncMock(side_effect=[RuntimeError('未知操作'),{'elements':[{'ref':8,'tag':'a','text':'删除'}]}])
        with self.assertRaisesRegex(RuntimeError,'已变化'):await b.perform('hover',ref=8)
        self.assertEqual(b.request.await_count,2)
