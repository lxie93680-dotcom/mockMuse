import contextlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from evals.compression.v1.build import templates,materialize
from evals.compression.v1.score import grade,efficiency,report
from evals.compression.v1.run import live

class CompressionEval(unittest.IsolatedAsyncioTestCase):
    def test_scorer_rejects_wrong_types_and_extra_answers(self):
        self.assertFalse(grade({'days':[True]},{'days':[1]},['days'])['exact'])
        self.assertFalse(grade({'id':'good','extra':'bad'},{'id':'good'},[])['exact'])
        self.assertTrue(grade({'id':None},{'id':None},['id'])['exact'])
        self.assertTrue(grade('bad',{'id':'x'},['id'])['invalid_json'])
    def test_templates_disjoint_and_positions_change(self):
        cs=list(templates());self.assertEqual(len(cs),24)
        self.assertFalse({c['family'] for c in cs if c['split']=='dev'}&{c['family'] for c in cs if c['split']=='test'})
        a=materialize(cs[0],8000,'front');b=materialize(cs[0],8000,'back')
        self.assertEqual(a['history'][0]['item_id'],cs[0]['evidence_item_ids'][0])
        self.assertNotEqual(a['history'][0],b['history'][0])
    def test_efficiency_charges_compressor(self):
        common={'variant_id':'v','round':1,'repeat':0}
        rows=[{**common,'mode':'full','answer_usage':[{'input_tokens':100,'total_tokens':110}]},
              {**common,'mode':'compressed','answer_usage':[{'input_tokens':20,'total_tokens':30}], 'compression_applied':True,'compression_usage':[{'total_tokens':120}],'compression_ms':10}]
        m=efficiency(rows);self.assertEqual(m['request_input_reduction_mean_actual'],.8)
        self.assertLess(m['one_probe_net_total_token_reduction_mean_actual'],0)
    async def test_runner_three_groups_gold_not_passed_to_model(self):
        class Fake:
            metadata={'synthetic_test_adapter':True}
            def __init__(self,*args):self.model=SimpleNamespace(usage=[])
            async def compress(self,history):
                self.model.usage.append({'input_tokens':100,'output_tokens':10,'total_tokens':110})
                return '摘要 rb-7F2A，https://maps.example.test/plan/rb-7F2A',self.model.usage[-1:]
            async def answer(self,history,question,keys):
                assert all('gold' not in i for i in history)
                return json.dumps({'id':'rb-7F2A','url':'https://maps.example.test/plan/rb-7F2A'}),[{'input_tokens':20,'output_tokens':10,'total_tokens':30}]
            async def close(self):pass
        with tempfile.TemporaryDirectory() as root,patch('evals.compression.v1.muse_adapter.MuseAdapter',Fake),contextlib.redirect_stdout(io.StringIO()):
            args=SimpleNamespace(provider=None,split='dev',limit=1,output=str(Path(root)/'run'),lengths=[8000],positions=['front'],repeats=1,tail_tokens=500)
            await live(args)
            rows=[json.loads(l) for l in (Path(args.output)/'results.jsonl').read_text().splitlines()]
            self.assertEqual({r['mode'] for r in rows},{'full','compressed','tail'})
            self.assertEqual(len(rows),3);self.assertTrue(report(rows)['quality']['compressed']['strict_accuracy'])
    async def test_cumulative_pipeline_recompresses_five_rounds(self):
        calls=[]
        class Fake:
            metadata={'synthetic_test_adapter':True}
            def __init__(self,*args):self.model=SimpleNamespace(usage=[])
            async def compress(self,history):
                calls.append(history);return '保留版本与约束的合成测试摘要',[]
            async def answer(self,history,question,keys):return '{}',[]
            async def close(self):pass
        case=next(c for c in templates() if c['id']=='round_route')
        with tempfile.TemporaryDirectory() as root,patch('evals.compression.v1.muse_adapter.MuseAdapter',Fake),patch('evals.compression.v1.run.templates',lambda:iter([case])),contextlib.redirect_stdout(io.StringIO()):
            args=SimpleNamespace(provider=None,split='dev',limit=1,output=str(Path(root)/'run'),lengths=[8000],positions=['front'],repeats=1,tail_tokens=500)
            await live(args)
            self.assertEqual(len(calls),5)
            self.assertTrue(any('累计上下文摘要' in str(i) for i in calls[-1]))
            self.assertEqual(json.loads((Path(args.output)/'run.json').read_text())['status'],'completed')
