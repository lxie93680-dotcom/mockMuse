import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock
from muse.compression import CheckpointStore
from muse.message_store import MessageStore
from muse.loop_guard import GuardedModel,LoopGuard
from muse.context_history import RunCheckpoint
from muse.history_tools import HistoryReader

class Messages(unittest.IsolatedAsyncioTestCase):
    async def test_multicall_stores_one_assistant_and_separate_results(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','session');messages=MessageStore(store)
            task=messages.begin_task();store.observe([{'role':'user','content':'执行两个检查'}])
            request=messages.request_started();turn=messages.response_received(request)
            items=[{'role':'user','content':'执行两个检查'},
                {'role':'assistant','content':'检查文件'},
                {'type':'function_call','id':'__fake_id__','call_id':'a','name':'read','arguments':'{"path":"甲"}'},
                {'type':'function_call','id':'__fake_id__','call_id':'b','name':'read','arguments':'{"path":"乙"}'}]
            store.observe(items)
            items += [{'type':'function_call_output','call_id':'a','output':'成功甲'}, {'type':'function_call_output','call_id':'b','output':'成功乙'}]
            store.observe(items)
            with closing(store.connect()) as db:
                rows=db.execute('SELECT role,content,tool_calls,tool_call_id,task_id,turn_id,api_request_id FROM messages ORDER BY rowid').fetchall()
            self.assertEqual([r[0] for r in rows],['user','assistant','tool','tool'])
            self.assertEqual(len(json.loads(rows[1][2])),2)
            self.assertEqual(rows[1][1],'检查文件')
            self.assertEqual(rows[1][4:],(task,turn,request))
            self.assertEqual(rows[2][4:],(task,turn,request))
            mid=HistoryReader(store).search('成功甲')['results'][0]['message_id']
            self.assertEqual(HistoryReader(store).read(mid)['tool_call_id'],'a')
            store.compact(items,4,'用户希望检查甲乙文件')
            self.assertEqual(store.latest()['covered_through_message_id'],HistoryReader(store).search('检查文件')['results'][0]['message_id'])

    async def test_retries_share_request_and_next_call_increments(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','s');identity=store.message_store();task=identity.begin_task()
            response=SimpleNamespace(to_input_items=lambda:[{'role':'assistant','content':'回答'}])
            checkpoint=RunCheckpoint([],store.observe)
            wrapped=GuardedModel(SimpleNamespace(get_response=AsyncMock(side_effect=[RuntimeError(),response,response])),LoopGuard(lambda *a:None,checkpoint=checkpoint))
            wrapped.identity=identity
            await wrapped.get_response();await wrapped.get_response()
            with closing(store.connect()) as db:
                requests=db.execute('SELECT api_request_id,retry_index,turn_id FROM model_requests ORDER BY rowid').fetchall()
            self.assertEqual(requests[0][:2],(task+':api:1',1))
            self.assertEqual(requests[1][:2],(task+':api:2',0))
            self.assertNotEqual(requests[0][2],requests[1][2])
