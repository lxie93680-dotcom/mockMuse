import asyncio
import importlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from muse.browser import Browser
from muse.agent import run_agent


class FixtureHandler(BaseHTTPRequestHandler):
    model_requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        page = (Path(__file__).parents[1] / 'muse/static/demo.html').read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.model_requests.append(body)
        tools_present = any(m.get('role') == 'tool' for m in body['messages'])
        message = {'role': 'assistant', 'content': '模型适配测试完成'} if tools_present else {
            'role': 'assistant', 'content': None, 'reasoning_content': 'Read the current browser page.', 'tool_calls': [{
                'id': 'call_browser_1', 'type': 'function', 'function': {
                    'name': 'browser_action', 'arguments': '{"action":"read"}'}}]}
        payload = json.dumps({'id': 'mock-1', 'object': 'chat.completion', 'created': 1,
                              'model': body['model'], 'choices': [{'index': 0, 'message': message,
                              'finish_reason': 'stop' if tools_present else 'tool_calls'}],
                              'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(payload)


class BrowserIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.browser = Browser()
        self.browser.request = AsyncMock(return_value={'title':'fixture','text':'test','elements':[]})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    async def asyncTearDown(self):
        await self.browser.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.directory.cleanup()

    async def test_real_sdk_tool_loop_with_mock_provider(self):
        await self.browser.perform('open', url=self.url)
        for name, prefix, model in [('kimi', 'KIMI', 'kimi-k2.6'),
                                    ('deepseek', 'DEEPSEEK', 'deepseek-flash'),
                                    ('custom', 'CUSTOM', 'test-model')]:
            with self.subTest(provider=name):
                FixtureHandler.model_requests = []
                with patch.dict(os.environ, {'LLM_PROVIDER': name, f'{prefix}_API_KEY': 'test-only',
                                             f'{prefix}_BASE_URL': self.url, f'{prefix}_MODEL': model,
                                             f'{prefix}_EXTRA_BODY': '{}' if name == 'custom' else '{"thinking":{"type":"disabled"}}'}):
                    logs = []
                    answer, history = await run_agent(self.browser, '读取当前网页', [], lambda *args: logs.append(args))
                self.assertEqual(answer, '模型适配测试完成')
                self.assertEqual(len(FixtureHandler.model_requests), 2)
                first = FixtureHandler.model_requests[0]
                self.assertEqual(first['model'], model)
                if name == 'custom':
                    self.assertNotIn('thinking', first)
                else:
                    self.assertEqual(first['thinking']['type'], 'disabled')
                self.assertTrue(any(m['role'] == 'tool' for m in FixtureHandler.model_requests[1]['messages']))
                self.assertTrue(any(m.get('reasoning_content') == 'Read the current browser page.' for m in FixtureHandler.model_requests[1]['messages']))
                self.assertGreater(len(history), 1)
                self.assertTrue(logs)



class APIIntegration(unittest.TestCase):
    def test_auth_exclusivity_cancellation_persistence_and_websocket(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'MUSE_DESKTOP_COMMANDER':'0', 'MUSE_FILESYSTEM_MCP':'0', 'MUSE_BROWSER_BACKEND':'extension','MUSE_DATA_DIR': directory, 'MUSE_ACCESS_TOKEN': 'test-access', 'DEEPSEEK_API_KEY': '',
            'KIMI_API_KEY': '', 'MOONSHOT_API_KEY': '', 'LLM_PROVIDER': 'kimi'}):
            import muse.app as module
            module = importlib.reload(module)
            module.browser.connection = object()
            module.browser.request = AsyncMock(return_value={})
            module.browser.close = AsyncMock()
            async def slow_execute(prompt, demo):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    module.state['status'] = 'stopped'
                    module.save()
                    module.publish()
                    raise
            with patch.object(module, 'execute', slow_execute), TestClient(module.app) as client:
                self.assertEqual(client.get('/api/state').status_code, 401)
                self.assertEqual(client.post('/api/login', json={'token': 'bad'}).status_code, 401)
                response = client.post('/api/login', json={'token': 'test-access'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('HttpOnly', response.headers['set-cookie'])
                module.history = [{'role':'user','content':'old context'}]
                self.assertEqual(client.post('/api/provider', json={'provider':'deepseek'}).status_code, 200)
                self.assertEqual(module.history, [])
                self.assertEqual(client.get('/api/state').json()['provider'], 'deepseek')
                self.assertEqual(client.post('/api/provider', json={'provider':'unknown'}).status_code, 400)
                self.assertEqual(client.post('/api/tasks', json={'prompt': 'test'}).status_code, 400)
                self.assertEqual(client.post('/api/tasks', json={'prompt': 'test', 'demo': True},
                                            headers={'origin': 'https://other.example'}).status_code, 403)
                with client.websocket_connect('/ws', headers={'origin': 'http://testserver'}) as ws:
                    self.assertEqual(ws.receive_json()['status'], 'idle')
                    self.assertEqual(client.post('/api/tasks', json={'prompt': 'test', 'demo': True}).status_code, 202)
                    self.assertEqual(ws.receive_json()['status'], 'running')
                    self.assertEqual(client.post('/api/provider', json={'provider':'kimi'}).status_code, 409)
                    self.assertEqual(client.post('/api/tasks', json={'prompt': 'test', 'demo': True}).status_code, 409)
                    self.assertEqual(client.post('/api/manual', json={'action': 'click'}).status_code, 409)
                    self.assertEqual(client.post('/api/stop').status_code, 200)
                    for _ in range(10):
                        stopped = ws.receive_json()
                        if stopped.get('status')=='stopped' and stopped.get('phase') is None:break
                    self.assertEqual(stopped['status'], 'stopped')
                    self.assertIsNone(stopped.get('phase'))
                self.assertEqual(client.get('/api/state').json()['status'], 'stopped')
                self.assertTrue((Path(directory) / 'muse.db').exists())
            with TestClient(module.app) as client:
                client.post('/api/login', json={'token': 'test-access'})
                self.assertEqual(client.get('/api/state').json()['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
