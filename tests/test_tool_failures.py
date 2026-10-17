import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT, CONNECTION_CLOSED
from agents.tool_context import ToolContext
from muse.tool_access import ToolAccess
from muse.tool_failures import classify, ToolBusinessError, ToolCallFailure
from muse.tool_registry import ToolRegistry
from muse.tool_providers import ToolDefinition, MCPProvider
from muse.browser_mcp import BrowserMCP
from muse.loop_guard import LoopGuard


class ClassificationTests(unittest.TestCase):
    def test_typed_causes_not_message_matching(self):
        for error,expected in [(ConnectionError('secret'),'connection'),(TimeoutError(),'timeout'),
            (httpx2.ConnectError('secret'),'connection'),(FileNotFoundError(),'execution'),
            (PermissionError(),'permission'),(ValueError(),'arguments'),
            (RuntimeError('timeout connection tool not found'),'execution'),
            (MCPError(-32601,'secret'),'definition'),(MCPError(REQUEST_TIMEOUT,'opaque'),'timeout'),
            (MCPError(CONNECTION_CLOSED,'opaque'),'connection'),(MCPError(-32602,'secret'),'execution')]:
            with self.subTest(expected=expected):self.assertEqual(classify(error),expected)
        try:
            try:raise ConnectionError('secret')
            except Exception as cause:raise RuntimeError('SDK wrapper') from cause
        except Exception as error:self.assertEqual(classify(error),'connection')
        try:
            try:raise TimeoutError()
            except Exception as cause:raise ToolBusinessError('business') from cause
        except Exception as error:self.assertEqual(classify(error),'business')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry=ToolRegistry(Path(self.temp.name)/'tools.db')
        self.provider=SimpleNamespace(source_id='plugin:test',source_kind='plugin',
            discover_tools=AsyncMock(return_value=[ToolDefinition('old','old','old description',{'type':'object'})]))
        self.tid=(await self.registry.register(self.provider))['tools'][0]['tool_id']

    async def asyncTearDown(self):await self.registry.close()

    async def test_source_neutral_refresh_fingerprint_index_and_enablement_preserved(self):
        old=self.registry.store.get(self.tid)['fingerprint']
        self.registry.set_enabled(self.tid,False)
        self.provider.discover_tools.return_value=[ToolDefinition('old','old','new description',{'type':'object','properties':{'x':{'type':'string'}}})]
        self.assertEqual(await self.registry.rediscover_on_failure('plugin:test'),'refreshed')
        row=self.registry.store.get(self.tid)
        self.assertNotEqual(old,row['fingerprint']);self.assertFalse(row['enabled'])
        self.assertFalse(self.registry.enabled_states()[self.tid])
        self.assertEqual(len(self.registry.store.search('new description',enabled_only=False)),1)
        self.assertEqual(self.registry.search('new description'),[])

    async def test_concurrent_refresh_coalesces_and_throttles(self):
        self.provider.discover_tools.reset_mock()
        self.assertEqual(await asyncio.gather(*[self.registry.rediscover_on_failure('plugin:test') for _ in range(10)]),['refreshed']*10)
        self.provider.discover_tools.assert_awaited_once()
        self.assertEqual(await self.registry.rediscover_on_failure('plugin:test'),'throttled')

    async def test_failed_discovery_preserves_snapshot(self):
        old=self.registry.store.get(self.tid)
        self.provider.discover_tools.side_effect=ConnectionError('secret credential')
        self.assertEqual(await self.registry.rediscover_on_failure('plugin:test'),'failed')
        self.assertEqual(self.registry.store.get(self.tid),old)
        self.assertEqual(await self.registry.rediscover_on_failure('unknown'),'unavailable')

    async def test_discovery_timeout_is_bounded(self):
        async def slow():await asyncio.Event().wait()
        self.provider.discover_tools.side_effect=slow
        real_timeout=asyncio.timeout
        with patch('muse.tool_registry.asyncio.timeout',side_effect=lambda seconds:real_timeout(.01)):
            self.assertEqual(await self.registry.rediscover_on_failure('plugin:test'),'failed')
        self.assertIsNotNone(self.registry.store.get(self.tid))

    async def test_close_cancels_owned_discovery(self):
        started=asyncio.Event()
        async def slow():started.set();await asyncio.Event().wait()
        self.provider.discover_tools.side_effect=slow
        task=asyncio.create_task(self.registry.rediscover_on_failure('plugin:test'))
        await started.wait()
        await self.registry.close()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_execute_failure_rediscovers_without_replaying_business_rpc(self):
        browser=SimpleNamespace(perform=AsyncMock(side_effect=ConnectionError('secret-token-value')))
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            await self.registry.register(MCPProvider(server))
            access=ToolAccess(self.registry)
            row=access.search('read_browser')[0];info=access.describe(row['tool_id'])
            ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='error',tool_arguments='{}')
            tool=access.tools()[2];guard=LoopGuard(lambda *args:None);guard.wrap(tool)
            with patch.object(self.registry,'refresh',wraps=self.registry.refresh) as refresh:
                result=json.loads(await tool.on_invoke_tool(ctx,json.dumps({'tool_id':info['tool_id'],'arguments':{},'fingerprint':info['fingerprint']})))
            self.assertEqual(result['category'],'connection')
            self.assertEqual(result['rediscovery'],'refreshed')
            self.assertTrue(result['call_may_have_executed'])
            self.assertFalse(result['business_call_retried'])
            self.assertNotIn('secret-token-value',json.dumps(result))
            browser.perform.assert_awaited_once();refresh.assert_awaited_once()

    async def test_preflight_failure_no_business_effect_and_refresh_failure_reported(self):
        browser=SimpleNamespace(perform=AsyncMock())
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            await self.registry.register(MCPProvider(server));access=ToolAccess(self.registry)
            info=access.describe(access.search('read_browser')[0]['tool_id'])
            ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='preflight',tool_arguments='{}')
            with patch.object(server,'list_tools',AsyncMock(side_effect=ConnectionError('secret'))):
                with self.assertRaises(ToolCallFailure) as caught:
                    await access.call(ctx,info['tool_id'],{},info['fingerprint'])
            self.assertFalse(caught.exception.details['call_may_have_executed'])
            self.assertEqual(caught.exception.details['rediscovery'],'failed')
            browser.perform.assert_not_awaited()

    async def test_business_argument_and_cancellation_never_trigger_refresh(self):
        browser=SimpleNamespace(perform=AsyncMock(side_effect=ToolBusinessError('business')))
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            await self.registry.register(MCPProvider(server));access=ToolAccess(self.registry)
            info=access.describe(access.search('read_browser')[0]['tool_id'])
            ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='business',tool_arguments='{}')
            with patch.object(self.registry,'rediscover_on_failure',AsyncMock()) as discover:
                with self.assertRaises(ValueError):await access.call(ctx,info['tool_id'],{},'old-fingerprint')
                with self.assertRaises(ToolCallFailure) as caught:await access.call(ctx,info['tool_id'],{},info['fingerprint'])
                self.assertEqual(caught.exception.details['category'],'business')
                started=asyncio.Event()
                async def slow(*args):
                    started.set();await asyncio.Event().wait()
                browser.perform.side_effect=slow
                task=asyncio.create_task(access.call(ctx,info['tool_id'],{},info['fingerprint']))
                await started.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                discover.assert_not_awaited()

    async def test_sdk_protocol_timeout_from_browser_triggers_discovery_without_replay(self):
        browser=SimpleNamespace(perform=AsyncMock(side_effect=MCPError(REQUEST_TIMEOUT,'opaque')))
        server=BrowserMCP(browser,lambda *args:None)
        async with server.session():
            await self.registry.register(MCPProvider(server));access=ToolAccess(self.registry)
            info=access.describe(access.search('read_browser')[0]['tool_id'])
            ctx=ToolContext(context=None,tool_name='tool_call',tool_call_id='protocol-timeout',tool_arguments='{}')
            with self.assertRaises(ToolCallFailure) as caught:
                await access.call(ctx,info['tool_id'],{},info['fingerprint'])
            self.assertEqual(caught.exception.details['category'],'timeout')
            self.assertEqual(caught.exception.details['rediscovery'],'refreshed')
            self.assertTrue(caught.exception.details['call_may_have_executed'])
            browser.perform.assert_awaited_once()
