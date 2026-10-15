import tempfile
import unittest
from pathlib import Path
from muse.compression import CheckpointStore
from muse.history_tools import HistoryReader

class HistoryTests(unittest.TestCase):
    def test_chinese_search_and_archived_read_after_compaction(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','a')
            original=[{'id':'m1','role':'user','content':'杭州西湖，10月23日出发'},{'id':'m2','role':'user','content':'改为10月24日'}]
            store.observe(original);store.compact(original,1,'旅行目标')
            reader=HistoryReader(store)
            mid=reader.search('杭州西湖')['results'][0]['message_id']
            self.assertTrue(mid.startswith('message_'))
            self.assertEqual(reader.search('西湖')['results'][0]['message_id'],mid)
            self.assertIn('10月23日',reader.read(mid)['content'])
            other=HistoryReader(CheckpointStore(Path(root)/'db','b'))
            self.assertEqual(other.search('西湖')['results'],[])
            self.assertFalse(other.read(mid)['found'])
            part=reader.read(mid,max_chars=3)
            self.assertTrue(part['truncated']);self.assertEqual(part['next_offset'],3)
            self.assertNotIn('23日',part['content'])

    def test_fake_sdk_ids_do_not_drop_tool_calls(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','a')
            rows=store.observe([{'id':'__fake_id__','type':'function_call','arguments':'first'}, {'id':'__fake_id__','type':'function_call','arguments':'second'}])
            self.assertNotEqual(rows[0]['item_id'],rows[1]['item_id'])
            self.assertEqual(len(HistoryReader(store).search('second')['results']),1)
