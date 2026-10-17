import unittest
from unittest.mock import AsyncMock
from muse.playwright_browser import PlaywrightBrowser
from muse.tool_results import page_projection

SNAPSHOT='''### Page
- Page URL: https://example.com/
- Page Title: Candidate
### Snapshot
```yaml
- region "杭州" [ref=f1e1]:
  - button "选择灵隐寺" [ref=f1e2]
  - textbox "地点" [ref=f1e3]
```
'''

class PlaywrightAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_native_refs_and_frame_context(self):
        b=PlaywrightBrowser();b.tool=AsyncMock(return_value=SNAPSHOT)
        page=await b.snapshot()
        self.assertEqual(page['elements'][1]['ref'],'f1e2')
        self.assertEqual(page['elements'][1]['context'],'杭州')
        self.assertEqual(page_projection(page)['snapshot'],page['snapshot'])

    async def test_stale_ref_rejected_before_click(self):
        b=PlaywrightBrowser();b.server=object();b.valid_refs={'f2e2'};b.tool=AsyncMock()
        with self.assertRaisesRegex(ValueError,'最新'):await b.perform('click',ref='f1e2')
        b.tool.assert_not_awaited()

    async def test_fill_uses_mcp_uid_not_extension_grounding(self):
        b=PlaywrightBrowser();b.server=object();b.valid_refs={'f1e3'};b.tool=AsyncMock(return_value=SNAPSHOT);b.request=AsyncMock()
        result=await b.perform('fill',ref='f1e3',text='灵隐寺')
        self.assertEqual(b.tool.call_args_list[0].args,('browser_type',{'target':'f1e3','text':'灵隐寺'}))
        b.request.assert_not_awaited();self.assertEqual(result['backend'],'playwright')

    async def test_ambiguous_text_returns_observation_without_click(self):
        b=PlaywrightBrowser();b.server=object();b.snapshot=AsyncMock(return_value={'elements':[{'ref':'a','text':'选择','role':'button'},{'ref':'b','text':'选择','role':'button'}]});b.tool=AsyncMock()
        result=await b.perform('click_text',text='选择')
        self.assertFalse(result['action_performed']);b.tool.assert_not_awaited()

    async def test_mcp_error_keeps_actual_error(self):
        from types import SimpleNamespace
        b=PlaywrightBrowser();b.server=SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text='Timeout: element covered')],is_error=True)))
        with self.assertRaisesRegex(RuntimeError,'element covered'):await b.tool('browser_click',{'target':'f1e2'})

    async def test_extension_manual_input_uses_same_mcp_connection(self):
        b=PlaywrightBrowser();b.extension_mode=True;b.server=object();b.tool=AsyncMock(return_value='');b.request=AsyncMock();b.valid_refs={'old'}
        await b.manual('down',x=10,y=20,button='left')
        await b.manual('move',x=30,y=20)
        await b.manual('up',x=30,y=20,button='left')
        self.assertEqual(b.tool.await_count,3)
        self.assertIn('page.mouse.down',b.tool.call_args_list[0].args[1]['code'])
        self.assertIn('page.mouse.up',b.tool.call_args_list[2].args[1]['code'])
        b.request.assert_not_awaited();self.assertFalse(b.valid_refs)

    async def test_extension_connection_state_requires_actual_attach(self):
        b=PlaywrightBrowser();b.extension_mode=True;b.server=object()
        self.assertFalse(b.connected)
        b.extension_attached=True;self.assertTrue(b.connected)

    async def test_extension_navigation_waits_for_readonly_handshake(self):
        from types import SimpleNamespace
        b=PlaywrightBrowser();b.extension_mode=True
        result=SimpleNamespace(content=[SimpleNamespace(text='ok')],is_error=False)
        b.server=SimpleNamespace(call_tool=AsyncMock(return_value=result))
        await b.tool('browser_navigate',{'url':'http://127.0.0.1:8000/control-test'})
        self.assertEqual([c.args[0] for c in b.server.call_tool.call_args_list],['browser_tabs','browser_navigate'])
        self.assertTrue(b.connected)
        await b.tool('browser_click',{'target':'e1'})
        self.assertEqual(b.server.call_tool.call_args.args[0],'browser_click')
        self.assertEqual(b.server.call_tool.await_count,3)

    async def test_extension_timeout_does_not_enqueue_requested_navigation(self):
        from types import SimpleNamespace
        from mcp.shared.exceptions import MCPError
        from mcp.types import REQUEST_TIMEOUT
        b=PlaywrightBrowser();b.extension_mode=True
        b.server=SimpleNamespace(call_tool=AsyncMock(side_effect=MCPError(REQUEST_TIMEOUT,'timeout')))
        with self.assertRaises(MCPError):
            await b.tool('browser_navigate',{'url':'http://127.0.0.1:8000/control-test'})
        self.assertEqual([c.args[0] for c in b.server.call_tool.call_args_list],['browser_tabs'])
        self.assertFalse(b.connected)
        self.assertEqual(b.status['connection_state'],'waiting_for_extension')

    async def test_extension_cancel_does_not_enqueue_requested_input(self):
        import asyncio
        from types import SimpleNamespace
        b=PlaywrightBrowser();b.extension_mode=True;entered=asyncio.Event()
        async def wait(*args):entered.set();await asyncio.Event().wait()
        b.server=SimpleNamespace(call_tool=AsyncMock(side_effect=wait))
        task=asyncio.create_task(b.tool('browser_type',{'target':'e1','text':'fixture'}))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual([c.args[0] for c in b.server.call_tool.call_args_list],['browser_tabs'])
        self.assertFalse(b.connected)
