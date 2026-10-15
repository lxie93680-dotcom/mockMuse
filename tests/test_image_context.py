import unittest
from muse.image_context import project_tool_images
from muse.token_policy import estimate


def tool(call,images=1):
    return {'type':'function_call_output','call_id':call,'output':[{'type':'input_text','text':'actual tool text'}]+[{'type':'input_image','image_url':'data:image/jpeg;base64,'+'a'*10000} for _ in range(images)]}


class ImageContext(unittest.TestCase):
    def test_old_images_removed_latest_batch_bounded_original_unchanged(self):
        raw=[{'role':'user','content':'task'},tool('old'),tool('latest',4)]
        projected=project_tool_images(raw)
        self.assertEqual(sum(p.get('type')=='input_image' for i in projected if i.get('type')=='function_call_output' for p in i['output']),2)
        self.assertEqual(raw[1]['output'][1]['type'],'input_image')
        self.assertIn('tool_call_id=old',projected[1]['output'][1]['text'])
        self.assertEqual(projected[1]['output'][0]['text'],'actual tool text')
        self.assertEqual(project_tool_images(projected),projected)

    def test_new_turn_drops_old_tool_images_but_keeps_user_upload(self):
        upload={'role':'user','content':[{'type':'input_image','image_url':'https://example.com/upload.png'}]}
        raw=[tool('old'),upload]
        result=project_tool_images(raw)
        self.assertEqual(result[1],upload)
        self.assertEqual(result[0]['output'][1]['type'],'input_text')

    def test_image_budget_counts_typed_images_not_base64_bytes(self):
        policy={'image_token_budget':4096,'fallback_utf8_bytes_per_token':2}
        raw=[tool('old'),tool('latest')]
        projected=project_tool_images(raw)
        self.assertLess(estimate(projected,policy),estimate(raw,policy))
        self.assertEqual(estimate(raw[0]['output'][1],policy),4096)
