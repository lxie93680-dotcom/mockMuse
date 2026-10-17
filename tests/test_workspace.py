import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from muse.workspace import checked_path, workspace


class WorkspaceTests(unittest.TestCase):
    def test_document_path_boundaries(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE': root, 'MUSE_LOCAL_ALLOWED_FILES': ''}):
            self.assertEqual(checked_path('demo.docx'), workspace() / 'demo.docx')
            for path in ['../outside', '/etc/passwd', '.env', 'folder/.secret']:
                with self.assertRaises(ValueError):
                    checked_path(path)
            Path(root, 'link').symlink_to('/etc')
            with self.assertRaises(ValueError):
                checked_path('link/passwd')

    def test_explicit_file_authorization(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as external:
            target = Path(external, 'demo.docx').resolve()
            with patch.dict(os.environ, {'MUSE_LOCAL_WORKSPACE': root, 'MUSE_LOCAL_ALLOWED_FILES': str(target)}):
                self.assertEqual(checked_path(str(target)), target)
                with self.assertRaises(ValueError):
                    checked_path(str(target.with_name('other.docx')))
