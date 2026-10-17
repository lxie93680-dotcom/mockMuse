import tempfile
import unittest
from pathlib import Path
from evals.tool_search.run import grade,aggregate,evaluate

class SearchEvaluation(unittest.IsolatedAsyncioTestCase):
    def test_rank_recall_and_negative_denominators(self):
        score=grade(['noise','a'],['a','b'])
        self.assertEqual(score['mrr_at_5'],.5);self.assertEqual(score['recall_at_5'],.5)
        self.assertFalse(score['hit_at_1']);self.assertTrue(score['hit_at_3'])
        summary=aggregate([{'relevant':['a','b'],'metrics':score},{'relevant':[],'metrics':grade([],[])}])
        self.assertEqual(summary['mrr_at_5'],.5);self.assertEqual(summary['no_match_correct_rate'],1)
        self.assertFalse(grade(['noise'],[])['no_match_correct'])
    async def test_repeated_benchmark_keeps_scores_and_separate_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            a=await evaluate(Path(temp)/'a');b=await evaluate(Path(temp)/'b')
            self.assertEqual(a['metrics'],b['metrics']);self.assertEqual(a['total'],40)
            self.assertTrue(a['temporary_namespace_removed']);self.assertFalse(a['product_state_modified'])
            with self.assertRaises(FileExistsError):await evaluate(Path(temp)/'a')
