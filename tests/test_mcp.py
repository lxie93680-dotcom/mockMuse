import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from muse.filesystem_mcp import Filesystem
from muse.agent import make_agent
from muse.models import get_provider


class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_and_tool_registration(self):
        fake=SimpleNamespace(connect=AsyncMock(),cleanup=AsyncMock(),list_tools=AsyncMock(return_value=[SimpleNamespace(name='read_file',description='read')]))
        manager=Filesystem();records=[]
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'1','MUSE_LOCAL_WORKSPACE':root,'KIMI_API_KEY':'test'}), patch('muse.filesystem_mcp.Path.is_file',return_value=True),patch('muse.filesystem_mcp.LoggedServer',return_value=fake):
            async with manager.session(lambda *args:records.append(args)):
                self.assertTrue(manager.status['ready']);self.assertIs(manager.server,fake)
                agent,client=make_agent(None,lambda *args:None,get_provider('kimi'),mcp_servers=[fake])
                try:
                    self.assertEqual(agent.mcp_servers,[fake])
                    self.assertTrue({'local_files','local_terminal'}.isdisjoint(t.name for t in agent.tools))
                    self.assertIn('exec_command',[t.name for t in agent.tools])
                finally:
                    await client.close()
            fake.cleanup.assert_awaited_once();self.assertFalse(manager.status['ready'])

    async def test_disabled_and_unavailable(self):
        manager=Filesystem()
        with patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'0'}):
            async with manager.session(lambda *args:None):self.assertFalse(manager.status['enabled'])
        with patch.dict(os.environ,{'MUSE_FILESYSTEM_MCP':'1'}),patch('muse.filesystem_mcp.Path.is_file',return_value=False):
            async with manager.session(lambda *args:None):
                self.assertFalse(manager.status['ready']);self.assertTrue(manager.status['error'])
