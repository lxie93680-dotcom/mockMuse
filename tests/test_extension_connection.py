import unittest
from types import SimpleNamespace
from unittest.mock import patch
from muse.browser import Browser
import muse.app as app


class WebSocketFixture:
    headers={'origin':'chrome-extension://'+'a'*32}
    def __init__(self,messages):
        self.messages=iter(messages)
        self.closed=[]
        self.sent=[]
    async def accept(self):pass
    async def receive_json(self):return next(self.messages)
    async def send_json(self,value):self.sent.append(value)
    async def close(self,code=1000,reason=''):self.closed.append((code,reason))


class ExtensionConnection(unittest.IsolatedAsyncioTestCase):
    async def test_busy_control_is_retryable_and_does_not_disconnect_owner(self):
        browser=Browser();owner=object();browser.connection=owner
        ws=WebSocketFixture([{'type':'hello','token':app.TOKEN}])
        with patch.object(app,'browser',browser):await app.extension_socket(ws)
        self.assertEqual(ws.closed[0][0],1013)
        self.assertIs(browser.connection,owner)

    async def test_detach_reason_is_retained_in_ui_and_log(self):
        browser=Browser();ws=WebSocketFixture([{'type':'hello','token':app.TOKEN},
                                              {'type':'disconnect','reason':'target_closed'}])
        state={}
        with patch.object(app,'browser',browser),patch.object(app,'state',state),patch.object(app,'job',None),patch.object(app,'record') as record:
            await app.extension_socket(ws)
            self.assertFalse(browser.connected)
            self.assertIn('受控标签页已关闭',state['frame_error'])
            self.assertEqual(record.call_args.args,('浏览器扩展已断开','受控标签页已关闭'))

    async def test_local_extension_needs_no_token(self):
        browser=Browser();ws=WebSocketFixture([{'type':'hello'},{'type':'disconnect','reason':'manual_disconnect'}])
        ws.client=SimpleNamespace(host='127.0.0.1')
        ws.headers={**ws.headers,'host':'127.0.0.1:8000'}
        with patch.object(app,'browser',browser),patch.object(app,'state',{}),patch.object(app,'job',None),patch.object(app,'record'):
            await app.extension_socket(ws)
        self.assertEqual(ws.sent,[{'type':'ready'}])

    async def test_wrong_credentials_remain_non_retryable(self):
        browser=Browser();ws=WebSocketFixture([{'type':'hello','token':'test-invalid-token'}])
        with patch.object(app,'browser',browser):await app.extension_socket(ws)
        self.assertEqual(ws.closed[0][0],1008)
        self.assertFalse(browser.connected)

class LocalAccess(unittest.TestCase):
    def test_loopback_host_and_peer_are_both_required(self):
        for peer,host,expected in [('127.0.0.1','127.0.0.1:8000',True),('::1','[::1]:8000',True),('127.0.0.1','localhost:8000',True),('192.168.1.2','localhost:8000',False),('127.0.0.1','attacker.example:8000',False)]:
            request=SimpleNamespace(client=SimpleNamespace(host=peer),headers={'host':host},cookies={})
            self.assertEqual(app.local_connection(request),expected)
            self.assertEqual(app.authorized(request),expected)
