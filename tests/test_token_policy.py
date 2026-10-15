import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from muse.token_policy import model_policy, TokenTracker
from muse.compression import CheckpointStore


class TokenPolicyTests(unittest.TestCase):
    def test_exact_usage_survives_restart_and_is_not_accumulated(self):
        with tempfile.TemporaryDirectory() as directory:
            store=CheckpointStore(Path(directory)/'db','session')
            policy=model_policy('kimi','kimi-k2.7-code')
            tracker=TokenTracker('kimi','kimi-k2.7-code',policy,store)
            items=[{'role':'user','content':'hello'}]
            response=SimpleNamespace(usage=SimpleNamespace(input_tokens=140000,output_tokens=100,total_tokens=140100,requests=1))
            tracker.observe(response,items,'system',[])
            tracker.observe(response,items,'system',[])
            self.assertEqual(tracker.pressure(items,'system',[]),(140000,'provider_usage'))
            restored=TokenTracker('kimi','kimi-k2.7-code',policy,store)
            self.assertEqual(restored.pressure(items,'system',[])[0],140000)
            larger=items+[{'role':'assistant','content':'result'}]
            count,source=restored.pressure(larger,'system',[])
            self.assertGreater(count,140000)
            self.assertEqual(source,'provider_usage_plus_estimated_delta')
            restored.reset()
            self.assertIsNone(store.get_usage('kimi','kimi-k2.7-code'))

    def test_unknown_model_does_not_inherit_kimi_capacity(self):
        self.assertEqual(model_policy('custom','kimi-unverified')['context_window'],0)
        self.assertEqual(model_policy('kimi','kimi-k2.7-code')['context_window'],262144)

    def test_missing_usage_is_not_recorded_as_zero(self):
        policy=model_policy('kimi','kimi-k2.7-code')
        tracker=TokenTracker('kimi','kimi-k2.7-code',policy)
        tracker.observe(SimpleNamespace(usage=SimpleNamespace(input_tokens=0,requests=0)),[],'',[])
        self.assertIsNone(tracker.latest)
        self.assertEqual(tracker.pressure([],'',[])[1],'fallback_estimate')

    def test_changed_system_uses_new_estimate(self):
        policy=model_policy('kimi','kimi-k2.7-code')
        tracker=TokenTracker('kimi','kimi-k2.7-code',policy)
        tracker.observe(SimpleNamespace(usage=SimpleNamespace(input_tokens=100,requests=1)),[],'old',[])
        self.assertEqual(tracker.pressure([],'new',[])[1],'fallback_estimate')

    def test_compaction_replaces_baseline_with_nonzero_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            store=CheckpointStore(Path(directory)/'db','session')
            policy=model_policy('kimi','kimi-k2.7-code')
            tracker=TokenTracker('kimi','kimi-k2.7-code',policy,store)
            response=SimpleNamespace(usage=SimpleNamespace(input_tokens=140000,output_tokens=100,total_tokens=140100,requests=1))
            tracker.observe(response,[{'role':'user','content':'old'}],'system',[])
            kept=[{'role':'user','content':'摘要'},{'role':'user','content':'最近原文'}]
            count=tracker.after_compaction(kept,'system',[])
            self.assertGreater(count,0)
            self.assertLess(count,140000)
            restored=TokenTracker('kimi','kimi-k2.7-code',policy,store)
            self.assertEqual(restored.pressure(kept,'system',[]),(count,'post_compression_estimate'))
            restored.observe(response,kept,'system',[])
            self.assertEqual(restored.pressure(kept,'system',[]),(140000,'provider_usage'))
            from contextlib import closing
            with closing(store.connect()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM context_usage_events').fetchone()[0],2)
