import asyncio
import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from muse.browser import Browser


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlated_commands_errors_and_disconnect(self):
        browser = Browser()
        class Connection:
            async def send_json(self, message):
                self.last = message
        connection = Connection()
        browser.connection = connection
        task = asyncio.create_task(browser.perform('read'))
        await asyncio.sleep(0)
        browser.receive({'type':'result','id':connection.last['id'],
                         'result':{'title':'test','width':1600,'height':900}})
        self.assertEqual((await task)['title'], 'test')
        self.assertEqual(browser.width, 1600)
        task = asyncio.create_task(browser.perform('read'))
        await asyncio.sleep(0)
        browser.disconnect(connection)
        with self.assertRaises(ConnectionError):
            await task
        self.assertFalse(browser.pending)
        with self.assertRaises(ConnectionError):
            await browser.start()
        with self.assertRaises(ValueError):
            await browser.perform('open', url='file:///etc/passwd')


class ExtensionSocketTests(unittest.TestCase):
    def test_image_messages_are_persisted_and_require_authentication(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'MUSE_DESKTOP_COMMANDER':'0', 'MUSE_FILESYSTEM_MCP':'0', 'MUSE_BROWSER_BACKEND':'extension','MUSE_DATA_DIR':directory,'MUSE_ACCESS_TOKEN':'test-access'}):
            import muse.app as module
            module = importlib.reload(module)
            with TestClient(module.app) as client:
                url = module.send_browser_image(b'\xff\xd8fixture')
                self.assertEqual(client.get(url).status_code,401)
                client.post('/api/login',json={'token':'test-access'})
                response = client.get(url)
                self.assertEqual(response.content,b'\xff\xd8fixture')
                self.assertEqual(response.headers['cache-control'],'private, no-store')
                self.assertEqual(client.get('/api/images/not-an-image').status_code,404)
                self.assertEqual(client.get('/api/state').json()['messages'][-1]['image_url'],url)
            with TestClient(module.app) as client:
                client.post('/api/login',json={'token':'test-access'})
                self.assertEqual(client.get('/api/state').json()['messages'][-1]['image_url'],url)

    def test_auth_commands_frame_dimensions_and_disconnect(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'MUSE_DESKTOP_COMMANDER':'0', 'MUSE_FILESYSTEM_MCP':'0', 'MUSE_BROWSER_BACKEND':'extension','MUSE_DATA_DIR':directory,'MUSE_ACCESS_TOKEN':'test-access'}):
            import muse.app as module
            module = importlib.reload(module)
            origin = {'origin':'chrome-extension://'+'a'*32}
            with TestClient(module.app) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect('/extension/ws', headers={'origin':'https://evil.example'}):
                        pass
                with client.websocket_connect('/extension/ws',headers=origin) as rejected:
                    rejected.send_json({'type':'hello','token':'bad'})
                    with self.assertRaises(WebSocketDisconnect):
                        rejected.receive_json()
                client.post('/api/login',json={'token':'test-access'})
                self.assertEqual(client.post('/api/tasks',json={'prompt':'test','demo':True}).status_code,409)
                with client.websocket_connect('/extension/ws',headers=origin) as extension:
                    extension.send_json({'type':'hello','token':'test-access'})
                    self.assertEqual(extension.receive_json()['type'],'ready')
                    with client.websocket_connect('/extension/ws',headers=origin) as duplicate:
                        duplicate.send_json({'type':'hello','token':'test-access'})
                        with self.assertRaises(WebSocketDisconnect):
                            duplicate.receive_json()
                    with client.websocket_connect('/ws',headers={'origin':'http://testserver'}) as viewer:
                        viewer.receive_json()
                        # A page read travels out through the actual server bridge.
                        future = client.portal.start_task_soon(module.browser.perform, 'read')
                        command = extension.receive_json()
                        while command['action'] != 'read':
                            extension.send_json({'type':'result','id':command['id'],'result':{}})
                            command = extension.receive_json()
                        extension.send_json({'type':'result','id':command['id'],
                            'result':{'title':'fixture','width':1600,'height':900}})
                        self.assertEqual(future.result(timeout=5)['title'],'fixture')
                        module.browser.on_frame = module.broadcast_frame
                        extension.send_json({'type':'frame','data':'/9g=','url':'https://example.com',
                                             'width':1600,'height':900})
                        for _ in range(10):
                            event=viewer.receive_json()
                            if event.get('type')=='frame':break
                        self.assertEqual(event['width'],1600)
                        self.assertEqual(event['height'],900)
                        viewer.send_json({'type':'input','input':{'action':'click','x':10,'y':20}})
                        while True:
                            event=viewer.receive_json()
                            if event.get('type')=='control':break
                        self.assertFalse(event['active'])
                        # Complete any stream request so shutdown cannot block.
                        module.browser.streaming = False
                self.assertFalse(module.browser.connected)
