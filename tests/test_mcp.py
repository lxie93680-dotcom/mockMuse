import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from muse.desktop_commander import Commander
from muse.agent import make_agent
from muse.models import get_provider

class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_and_tool_registration(self):
        fake=SimpleNamespace(connect=AsyncMock(),cleanup=AsyncMock(),list_tools=AsyncMock(return_value=[SimpleNamespace(name='read_file',description='read')]))
        manager=Commander();records=[]
        with patch.dict(os.environ,{'MUSE_DESKTOP_COMMANDER':'1','KIMI_API_KEY':'test'}), patch('muse.desktop_commander.Path.is_file',return_value=True),patch('muse.desktop_commander.LoggedServer',return_value=fake):
            import tempfile
            with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,{'MUSE_MCP_HOME':home}):
                async with manager.session(lambda *args:records.append(args)):
                    self.assertTrue(manager.status['ready']);self.assertIs(manager.server,fake)
                    agent,client=make_agent(None,lambda *args:None,get_provider(),mcp_servers=[fake])
                    try:
                        self.assertEqual(agent.mcp_servers,[fake])
                        self.assertNotIn('local_terminal',[t.name for t in agent.tools])
                    finally: await client.close()
                fake.cleanup.assert_awaited_once();self.assertFalse(manager.status['ready'])
    async def test_disabled_and_unavailable(self):
        manager=Commander()
        with patch.dict(os.environ,{'MUSE_DESKTOP_COMMANDER':'0'}):
            async with manager.session(lambda *args:None):self.assertFalse(manager.status['enabled'])
        with patch.dict(os.environ,{'MUSE_DESKTOP_COMMANDER':'1'}),patch('muse.desktop_commander.Path.is_file',return_value=False):
            async with manager.session(lambda *args:None):
                self.assertFalse(manager.status['ready']);self.assertTrue(manager.status['error'])
