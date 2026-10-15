import unittest
from types import SimpleNamespace
from agents.models.chatcmpl_converter import Converter
from muse.agent import preserve_browser_images


class ScreenshotTransport(unittest.TestCase):
    def test_tool_image_reaches_chat_completions_user_message(self):
        image = {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,dGVzdA==', 'detail': 'high'}
        original = [{'type': 'function_call_output', 'call_id': 'view', 'output': [
            {'type': 'input_text', 'text': '截图'}, image]}]
        result = preserve_browser_images(SimpleNamespace(model_data=SimpleNamespace(input=original, instructions='test')))
        messages = Converter.items_to_messages(result.input)
        user = next(m for m in messages if m['role'] == 'user')
        self.assertEqual(user['content'][-1]['image_url']['url'], image['image_url'])
        self.assertEqual(len(original), 1)
        self.assertEqual(result.instructions, 'test')
        self.assertFalse(any(p.get('type')=='input_image' for p in result.input[0]['output']))
        self.assertEqual(original[0]['output'][-1],image)
