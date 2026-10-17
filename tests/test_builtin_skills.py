import os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from muse.skills import BuiltinSkills

class BuiltinSkillTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_document_and_safe_lookup(self):
        reader=BuiltinSkills();text=reader.read('muse-usage')
        self.assertIn('muse mcp add',text);self.assertIn('muse tools search',text)
        self.assertEqual(reader.read('install-mcp'),text)
        with self.assertRaises(ValueError):reader.read('../../.env')
        self.assertEqual([t.name for t in reader.tools()],['skill_list','skill_read'])
    async def test_agent_uses_cli_guidance_without_special_management_tool_or_manager_gate(self):
        from muse.agent import run_agent
        from muse.compression import CheckpointStore
        from muse.models import Provider
        from muse.tool_registry import ToolRegistry
        from muse.host import local_commands_supported
        with tempfile.TemporaryDirectory() as directory:
            registry=ToolRegistry(Path(directory)/'tools.db');store=CheckpointStore(registry.store.path,'cli-test');store.tool_registry=registry
            async def fake_run(agent,**kwargs):
                names={t.name for t in agent.tools}
                self.assertTrue({'skill_list','skill_read','tool_search'}<=names)
                self.assertEqual('exec_command' in names, local_commands_supported())
                self.assertNotIn('manage_mcp',names);self.assertIn('muse-usage',agent.instructions)
                return SimpleNamespace(final_output='done',to_input_list=lambda:[])
            try:
                with patch.dict(os.environ,{'MUSE_LOCAL_WORKSPACE':str(Path(directory)/'workspace'),'MUSE_FILESYSTEM_MCP':'0'}), patch('muse.agent.Runner.run',side_effect=fake_run):
                    answer,_=await run_agent(None,'管理MCP',[],lambda *args:None,provider=Provider('custom','test','test','https://example.invalid','test',{},'CUSTOM_API_KEY'),compression_store=store)
                self.assertEqual(answer,'done')
            finally:await registry.close()
