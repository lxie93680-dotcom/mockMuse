import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from muse.browser_diagnostics import traced_call,trace_context

class BrowserDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def browser(self,call):
        return SimpleNamespace(server=SimpleNamespace(call_tool=call),status={'connection_mode':'extension'},extension_attached=True,process=None)
    async def test_timeout_correlates_without_claiming_disconnect(self):
        d=Mock();token=trace_context.set((d,'browser_action','call-test'))
        b=self.browser(AsyncMock(side_effect=TimeoutError('request timed out')))
        try:
            with self.assertRaises(TimeoutError):await traced_call(b,'browser_snapshot',{})
        finally:trace_context.reset(token)
        stages=[c.kwargs['result'] for c in d.emit.call_args_list]
        self.assertEqual([s['stage'] for s in stages],['rpc_dispatch','rpc_exception'])
        self.assertEqual(stages[0]['trace_id'],stages[1]['trace_id'])
        self.assertEqual(d.emit.call_args.args[:3],('browser_stage','browser_action','call-test'))
        self.assertTrue(stages[-1]['extension_last_attached'])
    async def test_returned_page_error_keeps_evidence(self):
        d=Mock();token=trace_context.set((d,'browser_action','id'))
        r=SimpleNamespace(is_error=True,content=[SimpleNamespace(text='waiting for element visible and stable')])
        try:await traced_call(self.browser(AsyncMock(return_value=r)),'browser_click',{'target':'e3'})
        finally:trace_context.reset(token)
        e=d.emit.call_args.kwargs['result'];self.assertEqual(e['stage'],'rpc_error_result');self.assertIn('stable',e['error_evidence'])
    async def test_cancellation_recorded_and_watchdog_cleaned(self):
        d=Mock();token=trace_context.set((d,'browser_view','id'))
        async def slow(*args):await asyncio.sleep(100)
        try:
            task=asyncio.create_task(traced_call(self.browser(slow),'browser_take_screenshot',{}));await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:trace_context.reset(token)
        self.assertEqual(d.emit.call_args.kwargs['result']['stage'],'rpc_cancelled')
    async def test_lock_wait_cancellation_does_not_release_other_owner(self):
        from muse.browser_diagnostics import traced_lock
        b=SimpleNamespace(lock=asyncio.Lock());await b.lock.acquire()
        d=Mock();token=trace_context.set((d,'browser_action','id'))
        async def wait():
            async with traced_lock(b):pass
        try:
            t=asyncio.create_task(wait());await asyncio.sleep(0);t.cancel()
            with self.assertRaises(asyncio.CancelledError):await t
        finally:trace_context.reset(token)
        self.assertTrue(b.lock.locked());b.lock.release()
        self.assertEqual(d.emit.call_args.kwargs['result']['stage'],'browser_lock_wait')
    async def test_debug_events_redact_secrets_and_bound_size(self):
        from muse.debug import Debug
        d=Debug();d.enabled=True
        for i in range(205):d.event('mcp',{'token':'private-value','stage':'error'},error=True)
        self.assertEqual(len(d.events),200);self.assertTrue(d.events[-1]['error'])
        self.assertEqual(d.events[-1]['detail']['token'],'[已隐藏]')
