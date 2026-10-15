from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from agents.run import ModelInputData
from muse.compression import CheckpointStore, Compressor, split_tail
from muse.loop_guard import LoopGuard


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    def test_tail_keeps_tool_pair(self):
        items=[{'role':'user','content':'old'},
               {'type':'function_call','call_id':'a','name':'read','arguments':'{}'},
               {'type':'function_call_output','call_id':'a','output':'result'},
               {'role':'assistant','content':'done'}]
        # No later user: protects original user, and never leaves an orphan result.
        self.assertEqual(split_tail(items,10),0)
        items[0]={'role':'assistant','content':'old'}
        cut=split_tail(items,25)
        self.assertFalse(cut==2)

    def test_checkpoint_persists_and_archives(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'db';store=CheckpointStore(path,'one')
            items=[{'id':'user_1','role':'user','content':'hello'}, {'role':'assistant','content':'done'}]
            store.observe(items[:1])
            self.assertEqual(store.latest()['item_id'],'user_1')
            self.assertIsNone(store.latest()['context_summary'])
            old=store.observe(items)
            kept=store.compact(items,1,'summary one')
            first=store.latest()
            self.assertTrue(first['item_id'].startswith('summary_'))
            self.assertEqual(first['covered_through_item_id'],'user_1')
            self.assertEqual(CheckpointStore(path,'one').latest(),first)
            store.compact(kept,1,'summary two')
            self.assertNotEqual(store.latest()['item_id'],first['item_id'])
            with closing(store.connect()) as db:
                self.assertEqual(db.execute('SELECT content FROM context_items WHERE item_id=?',('user_1',)).fetchone()[0], '{"id": "user_1", "role": "user", "content": "hello"}')
            self.assertIsNone(CheckpointStore(path,'two').latest())

    async def test_compress_overlay_does_not_restore_raw_history(self):
        received=[]
        async def summarize(items):received.append(items);return '任务目标和已完成结果'
        agent=SimpleNamespace(model=SimpleNamespace(model='kimi-k2.7-code'),tools=[],model_settings=SimpleNamespace(extra_body=None))
        guard=LoopGuard(lambda *args:None)
        comp=Compressor(agent,guard,lambda *args:None,summarize=summarize,window=100000)
        raw=[{'role':'assistant','content':'旧历史'*1000} for _ in range(40)]+[{'role':'user','content':'继续'}]
        data=SimpleNamespace(model_data=ModelInputData(input=raw,instructions='system'))
        result=await comp.filter(data,lambda data:data.model_data)
        self.assertEqual(received[0],raw[:len(received[0])])
        self.assertGreater(len(received[0]),0)
        self.assertEqual(result.input[-1],raw[-1])
        later=raw+[{'role':'assistant','content':'新结果'}]
        effective=comp.effective(later)
        self.assertEqual(effective[-1],later[-1])
        self.assertTrue(effective[0]['content'].startswith('[Muse累计上下文摘要'))
        self.assertLess(len(effective),len(later))

    async def test_failed_summary_preserves_input(self):
        async def fail(items):raise ValueError('bad')
        agent=SimpleNamespace(model=SimpleNamespace(model='kimi-k2.7-code'),tools=[])
        comp=Compressor(agent,LoopGuard(lambda *args:None),lambda *args:None,summarize=fail,window=100000)
        raw=[{'role':'assistant','content':'历史'*1000} for _ in range(40)]+[{'role':'user','content':'继续'}]
        result=await comp.filter(SimpleNamespace(model_data=ModelInputData(input=raw,instructions='')),lambda data:data.model_data)
        self.assertEqual(result.input,raw)
        self.assertTrue(comp.disabled)

    async def test_real_sdk_runs_separate_compression_child(self):
        from agents import Agent, Runner, RunConfig, Model, ModelSettings
        from agents.items import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText
        class TestModel(Model):
            model='kimi-k2.7-code'
            def __init__(self):self.calls=[]
            async def get_response(self,*args,**kwargs):
                self.calls.append(kwargs)
                text='保留用户目标、完成事项、后续待办' if len(self.calls)==1 else '继续执行'
                return ModelResponse(output=[ResponseOutputMessage(id='reply_'+str(len(self.calls)),role='assistant',status='completed',content=[ResponseOutputText(type='output_text',text=text,annotations=[])],type='message')],usage=Usage(),response_id=None)
            async def stream_response(self,*args,**kwargs):
                raise NotImplementedError
                yield
        model=TestModel();agent=Agent(name='main',model=model,tools=[],model_settings=ModelSettings())
        comp=Compressor(agent,LoopGuard(lambda *a:None),lambda *a:None,window=100000)
        raw=[{'role':'assistant','content':'历史记录'*1000} for _ in range(40)]+[{'role':'user','content':'继续'}]
        result=await Runner.run(agent,input=raw,run_config=RunConfig(tracing_disabled=True,call_model_input_filter=lambda d:comp.filter(d,lambda d:d.model_data)))
        self.assertEqual(len(model.calls),2)
        self.assertEqual(model.calls[0]['tools'],[])
        self.assertIn('累计上下文摘要',str(model.calls[1]['input'][0]))
        self.assertEqual(comp.effective(result.to_input_list())[-1]['id'],'reply_2')
