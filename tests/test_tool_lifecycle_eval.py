import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from evals.tool_lifecycle.run import CASES,evaluate,fixture_case,ROOT,unpack,file_observation


class LifecycleEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_suite_is_repeatable_and_marks_model_cases_unrun(self):
        with tempfile.TemporaryDirectory() as root:
            for repeat in range(2):
                rows=await evaluate(Path(root)/str(repeat))
                self.assertEqual(len(rows),len(CASES))
                self.assertEqual(sum(r['status']=='pass' for r in rows),len(CASES)-8)
                self.assertEqual({r['case_id'] for r in rows if r['status']=='invalid'},{c['id'] for c in CASES if c.get('scenario',c['id']) in ('natural_write','natural_browser')})
                self.assertTrue(all(r['temporary_namespace_removed'] for r in rows))
                self.assertTrue(all(not r['dialogue_tested'] and r['model_requests']==0 for r in rows))
                for row in rows:
                    cleanups=[s['result'] for s in row['trace'] if s['operation']=='cleanup']
                    self.assertTrue(cleanups)
                    self.assertTrue(all(c['restored'] for c in cleanups))

    @unittest.skipUnless(shutil.which('node') and (ROOT.parents[1]/'vendor/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js').exists(),
                         'Requires installed Filesystem MCP')
    async def test_model_prose_cannot_pass_without_actual_file_effect(self):
        case=next(c for c in CASES if c['id']=='natural_write');trace=[]
        with tempfile.TemporaryDirectory() as root,patch('evals.tool_lifecycle.run.Conversation.ask',
                AsyncMock(return_value={'answer':'已保存','events':[],'outputs':[]})):
            with self.assertRaisesRegex(AssertionError,'actual_fixture_file_contains_expected'):
                await fixture_case(case,Path(root),object(),trace)
            self.assertTrue(trace[-1]['result']['restored'])
            self.assertFalse((Path(root)/'note.txt').exists())

    async def test_failure_after_mutation_restores_definition_and_enablement(self):
        case=next(c for c in CASES if c['id']=='warm_cache_update');trace=[]
        calls=0
        async def answer(prompt):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('Injected model failure after mutation')
            return {'outputs':[{'tool':'tool_describe','value':{'description':'LOOKUP_VERSION_ONE'}}]}
        with tempfile.TemporaryDirectory() as root,patch('evals.tool_lifecycle.run.Conversation.ask',side_effect=answer):
            with self.assertRaises(RuntimeError):await fixture_case(case,Path(root),object(),trace)
            self.assertTrue(trace[-1]['result']['restored'])
            self.assertTrue(trace[-1]['result']['new_test_rows_only_missing'])

    async def test_evaluation_failure_still_removes_temporary_namespace(self):
        async def fail(case,root,provider,trace):
            (root/'leftover.txt').write_text('fixture')
            raise RuntimeError('Injected failure')
        with tempfile.TemporaryDirectory() as root,patch('evals.tool_lifecycle.run.fixture_case',side_effect=fail):
            rows=await evaluate(Path(root)/'failure',selected=['delete_visibility'])
            self.assertEqual(rows[0]['status'],'error')
            self.assertTrue(rows[0]['temporary_namespace_removed'])

    def test_catalog_covers_six_categories_and_unique_cases(self):
        self.assertEqual({c['category'] for c in CASES},{str(i) for i in range(1,14)})
        self.assertGreaterEqual(len(CASES),50)
        self.assertEqual(len(CASES),len({c['id'] for c in CASES}))

    def test_plain_mcp_text_and_json_are_both_preserved(self):
        self.assertEqual(unpack('{"type":"text","text":"Successfully wrote file"}'),'Successfully wrote file')
        self.assertEqual(unpack('{"type":"text","text":"{\\"tools\\": []}"}'),{'tools':[]})

    def test_file_trigger_and_exact_content_are_separate_observations(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'note.txt'
            self.assertFalse(file_observation(path,'旅行')['contains_expected'])
            path.write_text('旅行\\\\n')
            observed=file_observation(path,'旅行')
            self.assertTrue(observed['contains_expected'])
            self.assertFalse(observed['content_exact'])
            path.write_text('旅行')
            self.assertTrue(file_observation(path,'旅行')['content_exact'])

    async def test_category_selection_repeat_schedule_and_nonempty_checks(self):
        with tempfile.TemporaryDirectory() as root:
            rows=await evaluate(Path(root)/'repeat',categories=['7'],repeats=2)
            self.assertEqual(len(rows),14)
            self.assertEqual({r['repeat'] for r in rows},{0,1})
            self.assertTrue(all(r['status']=='pass' and r['checks'] for r in rows))
            self.assertTrue(all(r['temporary_namespace_removed'] for r in rows))

    async def test_unknown_extended_case_cannot_silently_pass(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError,'Unknown extended case'):
                await fixture_case({'id':'unknown-case'},Path(root),None,[])
