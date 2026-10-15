import unittest
from dataclasses import dataclass
from muse.debug import Debug,clean

class DebugTests(unittest.TestCase):
    def test_opt_in_redaction_and_response(self):
        d=Debug();self.assertIsNone(d.begin(None,(),{'input':'hello'}));self.assertEqual(d.calls,[])
        d.enabled=True
        call=d.begin(None,(),{'input':[{'role':'user','content':'hello'}],'tools':[]})
        @dataclass
        class Response:
            output:list
        d.end(call,Response([{'role':'assistant','content':'world'}]))
        self.assertEqual(call['response']['output'][0]['content'],'world')
        self.assertEqual(clean({'api_key':'secret','authorization':'Bearer secret'})['api_key'],'[已隐藏]')
        self.assertEqual(clean('data:image/jpeg;base64,abc'),'[图片数据已省略]')
        d.enabled=False;d.calls=[];d.end(call,error=RuntimeError('private'));self.assertEqual(d.calls,[])
    def test_limit_and_safe_error(self):
        d=Debug();d.enabled=True
        for _ in range(35):d.begin(None,(),{'input':'hello'})
        self.assertEqual(len(d.calls),30)
        d.end(d.calls[-1],error=RuntimeError('secret'))
        self.assertEqual(d.calls[-1]['response'],{'error_type':'RuntimeError'})

class HttpDebugTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_http_request_and_response(self):
        import httpx2 as httpx
        from muse.debug import active_http_call,debug_http_client
        d=Debug();d.enabled=True
        call=d.begin(None,(),{'input':[{'id':'__fake_id__'}]})
        payload={'model':'kimi-k2.7-code','messages':[{'role':'user','content':'hello'}],
                 'tools':[{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]}
        def handler(request):
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'ok'}}],'usage':{'prompt_tokens':10}})
        token=active_http_call.set(call)
        try:
            async with debug_http_client(d,httpx.MockTransport(handler)) as client:
                await client.post('https://example.com/v1/chat/completions',json=payload,headers={'Authorization':'Bearer private'})
            d.end(call,result='SDK converted response')
        finally:active_http_call.reset(token)
        self.assertEqual(call['request'],payload)
        self.assertEqual(call['request_source'],'http_json')
        self.assertNotIn('__fake_id__',str(call['request']))
        self.assertNotIn('Bearer private',str(call))
        self.assertEqual(call['response']['usage']['prompt_tokens'],10)

    async def test_sdk_conversion_captured_after_fake_items_removed(self):
        import httpx2 as httpx
        from openai import AsyncOpenAI
        from agents import Agent,Runner,RunConfig,OpenAIChatCompletionsModel
        from muse.debug import debug_http_client,debug
        from muse.loop_guard import GuardedModel,LoopGuard
        from unittest.mock import patch
        d=Debug();d.enabled=True
        def handler(request):
            return httpx.Response(200,json={'id':'chat-test','object':'chat.completion','created':1,'model':'kimi-test',
                'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':1,'total_tokens':11}})
        async with AsyncOpenAI(api_key='not-a-real-key',base_url='https://example.com/v1',http_client=debug_http_client(d,httpx.MockTransport(handler)),max_retries=0) as client:
            model=OpenAIChatCompletionsModel(model='kimi-test',openai_client=client)
            agent=Agent(name='test',model=GuardedModel(model,LoopGuard(lambda *a:None)))
            history=[{'role':'user','content':'hello'}, {'type':'function_call','id':'__fake_id__','call_id':'c','name':'read','arguments':'{}'}, {'type':'function_call_output','call_id':'c','output':'result'}]
            with patch('muse.loop_guard.debug',d):
                await Runner.run(agent,input=history,run_config=RunConfig(tracing_disabled=True))
        self.assertEqual(d.calls[0]['request_source'],'http_json')
        messages=d.calls[0]['request']['messages']
        self.assertEqual(messages[1]['tool_calls'][0]['id'],'c')
        self.assertNotIn('__fake_id__',str(messages))
        self.assertEqual(d.calls[0]['response']['choices'][0]['message']['content'],'ok')
