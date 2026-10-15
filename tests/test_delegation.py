import unittest,json
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from agents import function_tool
from agents.tool_context import ToolContext
from muse.delegation import child_tools,delegate_tool
from muse.loop_guard import LoopGuard

class DelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_and_no_recursion(self):
        @function_tool(name_override='mcp_Filesystem__read_text_file')
        async def read(path:str) -> str:return 'hello'
        @function_tool(name_override='mcp_Filesystem__write_file')
        async def write(path:str) -> str:return 'written'
        guard=LoopGuard(lambda *a:None);guard.wrap(read)
        children=child_tools([read,write]);self.assertEqual([t.name for t in children],['mcp_Filesystem__read_text_file'])
        self.assertIs(children[0].on_invoke_tool,read._muse_original_invoke)
        provider=SimpleNamespace(api_key='test',base_url='https://example.com/v1',model='test',extra_body={})
        tool=delegate_tool(None,provider,[read,write],lambda *a:None,lambda d:d)
        ctx=ToolContext(context=None,tool_name='delegate_task',tool_call_id='d',tool_arguments='{}')
        async def fake_run(agent,**kwargs):
            names=[t.name for t in agent.tools]
            self.assertNotIn('delegate_task',names);self.assertNotIn(write.name,names)
            self.assertEqual(kwargs['max_turns'],100)
            self.assertNotIn('history',json.loads(kwargs['input']))
            return SimpleNamespace(final_output='核查完成')
        with patch('muse.delegation.Runner.run',side_effect=fake_run):
            result=await tool.on_invoke_tool(ctx,json.dumps({'task':'检查文件','instructions':'只读验证','context':'只检查hello'}))
            self.assertEqual(json.loads(result)['result'],'核查完成')
        result=await tool.on_invoke_tool(ctx,json.dumps({'task':'写文件','tools':[write.name]}))
        self.assertIn('error',result.lower())
    async def test_generic_without_role_and_empty_tools(self):
        provider=SimpleNamespace(api_key='test',base_url='https://example.com/v1',model='test',extra_body={})
        tool=delegate_tool(None,provider,[],lambda *a:None,lambda d:d)
        self.assertNotIn('role',tool.params_json_schema['properties'])
        ctx=ToolContext(context=None,tool_name='delegate_task',tool_call_id='test',tool_arguments='{}')
        async def fake_run(agent,**kwargs):
            self.assertEqual(agent.tools,[])
            self.assertIn('保留目标和进度',agent.instructions)
            self.assertEqual(kwargs['max_turns'],5)
            return SimpleNamespace(final_output='摘要')
        with patch('muse.delegation.Runner.run',side_effect=fake_run):
            result=await tool.on_invoke_tool(ctx,json.dumps({'task':'压缩上下文','context':'历史内容','instructions':'保留目标和进度','tools':[],'max_turns':5}))
            result=json.loads(result);self.assertEqual(result['tools'],[]);self.assertEqual(result['result'],'摘要')
        result=await tool.on_invoke_tool(ctx,json.dumps({'task':'扩大权限','tools':['local_terminal']}))
        self.assertIn('error',result.lower())

    async def test_new_tools_inherit_without_allowlist_changes(self):
        @function_tool
        async def future_lookup(query:str) -> str:return 'new capability'
        @function_tool(name_override='mcp_NewServer__write_file')
        async def write() -> str:return 'blocked'
        @function_tool(name_override='delegate_task')
        async def delegate() -> str:return 'blocked'
        self.assertEqual([t.name for t in child_tools([future_lookup,write,delegate])],['future_lookup'])
