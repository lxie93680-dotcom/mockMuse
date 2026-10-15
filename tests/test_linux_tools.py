import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from muse.linux_worker import app, path_for
from muse.linux_client import linux_request

class LinuxToolsTests(unittest.TestCase):
    def test_non_linux_refused(self):
        with patch('muse.linux_worker.platform.system', return_value='Darwin'):
            with self.assertRaises(RuntimeError):
                with TestClient(app):
                    pass

    def test_files_auth_and_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MUSE_WORKSPACE':directory,'MUSE_LINUX_TOKEN':'a'*32}), patch('muse.linux_worker.platform.system', return_value='Linux'):
            # File operations only; no local terminal commands are executed by this test.
            with TestClient(app) as c:
                self.assertEqual(c.post('/execute',json={'action':'list'}).status_code,401)
                def call(**body):
                    return c.post('/execute',headers={'Authorization':'Bearer '+'a'*32},json=body)
                self.assertEqual(call(action='mkdir',path='notes').status_code,200)
                self.assertEqual(call(action='write',path='notes/demo.txt',content='你好 Linux').status_code,200)
                self.assertEqual(call(action='read',path='notes/demo.txt').json()['content'],'你好 Linux')
                self.assertEqual(call(action='move',path='notes/demo.txt',destination='notes/result.txt').status_code,200)
                self.assertEqual(call(action='list',path='notes').json()['entries'][0]['name'],'result.txt')
                self.assertEqual(call(action='read',path='../escape').status_code,400)
                self.assertEqual(call(action='read',path='/etc/passwd').status_code,400)
                Path(directory,'link').symlink_to('/etc')
                self.assertEqual(call(action='read',path='link/passwd').status_code,400)
                self.assertEqual(call(action='terminal',command='echo test',timeout=61).status_code,422)

class LinuxClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_local_fallback(self):
        with patch.dict(os.environ, {'MUSE_LINUX_URL':'','MUSE_LINUX_TOKEN':''}):
            with self.assertRaisesRegex(RuntimeError,'不会使用本地环境'):
                await linux_request('terminal',command='uname -a')
    async def test_remote_plaintext_refused(self):
        with patch.dict(os.environ, {'MUSE_LINUX_URL':'http://cloud.example:8765','MUSE_LINUX_TOKEN':'a'*32}):
            with self.assertRaisesRegex(RuntimeError,'HTTPS'):
                await linux_request('list')
