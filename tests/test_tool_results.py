import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from muse.compression import CheckpointStore
from muse.tool_results import ToolResultProcessor,MAX_CHARS,head_tail
from muse.history_tools import HistoryReader

class ToolResults(unittest.TestCase):
    def test_web_cleaning_and_operational_refs(self):
        raw={'title':'路书','url':'https://example.com','html':'<nav>首页菜单</nav><script>steal()</script><div class="advertisement">广告内容</div><main><p>10月23日杭州西湖出发</p><p>北京天安门</p></main>', 'elements':[{'ref':7,'text':'路线'}]}
        result=json.loads(ToolResultProcessor().process('browser_action','{}',json.dumps(raw,ensure_ascii=False),'c'))
        self.assertNotIn('首页菜单',str(result));self.assertNotIn('steal()',str(result));self.assertNotIn('广告内容',str(result))
        self.assertIn('杭州西湖',str(result));self.assertEqual(result['elements'][0]['ref'],7)
        self.assertNotIn('html',result)

    def test_original_stored_and_context_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store=CheckpointStore(Path(root)/'db','s');identity=store.message_store();identity.begin_task()
            user={'role':'user','content':'读取文件'};store.observe([user]);req=identity.request_started();identity.response_received(req)
            call={'type':'function_call','call_id':'a','name':'read_file','arguments':'{}'};store.observe([user,call])
            raw='HEAD'+('数据'*20000)+'TAIL'
            projected=ToolResultProcessor(store).process('read_file','{}',raw,'a')
            self.assertLessEqual(len(projected),MAX_CHARS)
            envelope=json.loads(projected);self.assertTrue(envelope['content'].startswith('HEAD'));self.assertTrue(envelope['content'].endswith('TAIL'))
            store.observe([user,call,{'type':'function_call_output','call_id':'a','output':projected}])
            with closing(store.connect()) as db:
                original=db.execute("SELECT content FROM messages WHERE role='tool'").fetchone()[0]
            self.assertEqual(original,raw)
            read=HistoryReader(store).read(envelope['source_message_id'],offset=len(raw)-4)
            self.assertEqual(read['content'],'TAIL')
            self.assertIn('TAIL',str(HistoryReader(store).search('TAIL')))

    def test_json_escape_budget_and_error_fields(self):
        raw=json.dumps({'exit_code':1,'timed_out':True,'stdout':'"\\\n'*20000})
        rendered=ToolResultProcessor().process('terminal','{}',raw,'a')
        self.assertLessEqual(len(rendered),MAX_CHARS)
        self.assertEqual(json.loads(rendered)['exit_code'],1)
        self.assertTrue(json.loads(rendered)['timed_out'])
        self.assertEqual(head_tail('tiny'),'tiny')

    def test_web_also_uses_universal_head_tail(self):
        page={'title':'网页','url':'https://example.com','html':'<main><p>正文</p></main>',
              'elements':[{'ref':i,'text':'字段'*100} for i in range(120)]}
        output=ToolResultProcessor().process('browser_action','{}',json.dumps(page,ensure_ascii=False),'c')
        self.assertLessEqual(len(output),MAX_CHARS)
        envelope=json.loads(output)
        self.assertTrue(envelope['truncated'])
        self.assertIn('"title": "网页"',envelope['content'])
        self.assertIn('"ref": 119',envelope['content'])
        self.assertIn('工具结果已截断',envelope['content'])

    def test_image_accompanying_text_is_also_bounded(self):
        from agents import ToolOutputImage,ToolOutputText
        image=ToolOutputImage(image_url='https://example.com/image.png')
        result=ToolResultProcessor().process('image','{}',[image,ToolOutputText(text='头'+'内容'*10000+'尾')],'i')
        self.assertIs(result[0],image)
        self.assertLessEqual(len(result[1].text),MAX_CHARS)
        self.assertTrue(result[1].text.endswith('尾'))
