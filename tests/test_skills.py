import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.tool_context import ToolContext
from muse.skills import Skills
from muse import cli


def write_skill(root,name='sample',description='测试技能简介',body='正文不应出现在发现结果中'):
    package=root/name;package.mkdir(parents=True,exist_ok=True)
    (package/'SKILL.md').write_text(f'---\nname: {name}\ndescription: {description}\n---\n{body}',encoding='utf-8')
    return package


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.builtin=self.root/'builtin';self.installed=self.root/'user/skills'
        self.skills=Skills(self.builtin,self.installed)
    def test_two_sources_and_demand_loading(self):
        write_skill(self.builtin,'guide');write_skill(self.installed,'travel')
        rows=self.skills.catalog()
        self.assertEqual([r['skill_id'] for r in rows],['builtin/guide','installed/travel'])
        self.assertNotIn('正文',json.dumps(rows,ensure_ascii=False))
        self.assertIn('正文',self.skills.read('travel'))
        self.assertIn('正文',self.skills.read('installed/travel'))
    def test_same_name_requires_explicit_source(self):
        write_skill(self.builtin,body='builtin body');write_skill(self.installed,body='installed body')
        with self.assertRaisesRegex(ValueError,'同名'):self.skills.read('sample')
        self.assertTrue(self.skills.read('builtin/sample').endswith('builtin body'))
        self.assertTrue(self.skills.read('installed/sample').endswith('installed body'))
    def test_next_read_observes_create_update_and_removal(self):
        self.assertEqual(self.skills.catalog(),[])
        package=write_skill(self.installed)
        self.assertEqual(len(self.skills.catalog()),1)
        write_skill(self.installed,body='new body');self.assertTrue(self.skills.read('sample').endswith('new body'))
        (package/'SKILL.md').unlink();self.assertEqual(self.skills.catalog(),[])
        with self.assertRaises(ValueError):self.skills.read('sample')
    def test_resources_and_unsafe_paths(self):
        package=write_skill(self.installed);(package/'references').mkdir()
        (package/'references/info.md').write_text('reference text')
        self.assertEqual(self.skills.read('sample','references/info.md'),'reference text')
        for name,path in (('../sample','SKILL.md'),('sample','../secret'),('sample','/etc/hosts'),('sample','.env'),('sample','references/./info.md'),('sample','a\\b')):
            with self.assertRaises(ValueError):self.skills.read(name,path)
        (package/'ref').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):self.skills.read('sample','ref')
        (package/'references').rename(package/'original');(package/'references').symlink_to(package/'original',target_is_directory=True)
        with self.assertRaises(ValueError):self.skills.read('sample','references/info.md')
    def test_invalid_and_linked_skills_are_not_discovered(self):
        self.installed.mkdir(parents=True)
        outside=write_skill(self.root/'external','linked')
        (self.installed/'linked').symlink_to(outside,target_is_directory=True)
        invalid=write_skill(self.installed,'invalid');(invalid/'SKILL.md').write_text('missing metadata')
        mismatch=write_skill(self.installed,'mismatch');(mismatch/'SKILL.md').write_text('---\nname: other\ndescription: no\n---\nbody')
        self.assertEqual(self.skills.catalog(),[])
        for name in ('linked','invalid','mismatch'):
            with self.assertRaises(ValueError):self.skills.read(name)
    def test_binary_and_large_text_rejected_and_no_script_execution(self):
        package=write_skill(self.installed)
        (package/'binary').write_bytes(b'\xff\x00')
        (package/'large').write_bytes(b'a'*(256*1024+1))
        (package/'script.py').write_text('raise RuntimeError("never execute")')
        for path in ('binary','large'):
            with self.assertRaises(ValueError):self.skills.read('sample',path)
        self.assertIn('never execute',self.skills.read('sample','script.py'))
    def test_agent_tools_list_and_read_share_catalog(self):
        write_skill(self.installed)
        tools={t.name:t for t in self.skills.tools()}
        ctx=ToolContext(context=None,tool_name='skill_list',tool_call_id='fixture',tool_arguments='{}')
        rows=json.loads(asyncio.run(tools['skill_list'].on_invoke_tool(ctx,'{}')))['skills']
        self.assertEqual(rows[0]['source'],'installed')
        text=asyncio.run(tools['skill_read'].on_invoke_tool(ctx,json.dumps({'name':rows[0]['skill_id']})))
        self.assertIn('正文',text)
    def test_cli_lists_and_reads_both_sources_offline(self):
        write_skill(self.installed);write_skill(self.builtin,'guide')
        def invoke(args):
            out=io.StringIO()
            with patch('muse.cli.Skills',return_value=self.skills),patch('muse.cli.Client',side_effect=AssertionError('offline')),contextlib.redirect_stdout(out):
                code=cli.main(args)
            return code,out.getvalue()
        code,out=invoke(['skills','list']);self.assertEqual(code,0);self.assertEqual(len(json.loads(out)['skills']),2)
        code,out=invoke(['skills','read','installed/sample']);self.assertEqual(code,0);self.assertIn('正文',out)
