import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from muse.local_files import operate

class LocalFilesTests(unittest.TestCase):
    def test_file_roundtrip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE':root}):
            operate('mkdir','notes')
            operate('write','notes/demo.txt','你好 Muse')
            self.assertEqual(operate('read','notes/demo.txt')['content'],'你好 Muse')
            with self.assertRaises(FileExistsError):
                operate('write','notes/demo.txt','changed')
            operate('write','notes/demo.txt','changed',overwrite=True)
            operate('move','notes/demo.txt',destination='notes/result.txt')
            self.assertEqual(operate('list','notes')['entries'][0]['name'],'result.txt')
            self.assertEqual(operate('read','notes/result.txt')['content'],'changed')
    def test_boundaries(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE':root}):
            for path in ['../outside','/etc/passwd','.env','folder/.secret']:
                with self.assertRaises(ValueError):
                    operate('read',path)
            Path(root,'link').symlink_to('/etc')
            with self.assertRaises(ValueError):
                operate('read','link/passwd')
            with self.assertRaises(ValueError):
                operate('write','large.txt','x'*(256*1024+1))

    def test_explicit_file_authorization(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as desktop:
            target = str(Path(desktop,'hello.txt').resolve())
            with patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE':root,'MUSE_LOCAL_ALLOWED_FILES':target}):
                operate('write',target,'hello')
                self.assertEqual(operate('read',target)['content'],'hello')
                with self.assertRaises(ValueError):
                    operate('write',str(Path(desktop,'other.txt')),'no')
