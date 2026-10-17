import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from muse.tool_registration import bootstrap_catalog, register_catalog
from muse.tool_store import ToolStore
from muse.browser_mcp import BrowserMCP


class ToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_external_browser_schemas_persist_without_model(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'muse.db'
            browser=SimpleNamespace()
            server=BrowserMCP(browser,lambda *args:None,lambda data:'/image')
            async with server.session():
                result=await bootstrap_catalog(path,server,None,lambda *args:None)
            store=ToolStore(path)
            self.assertEqual(result['sources'],{'mcp:muse-browser':6})
            self.assertEqual(result['errors'],[])
            self.assertEqual(store.list_tools('muse:builtin'),[])
            for row in store.list_tools():
                self.assertEqual(len(store.get(row['tool_id'])['fingerprint']),64)
            self.assertEqual(len(store.search('browser_crop_image')),1)
            self.assertEqual(store.search('exec_command'),[])

    async def test_repeated_registration_preserves_identity_and_enabled(self):
        server=SimpleNamespace(name='Filesystem',list_tools=AsyncMock(return_value=[SimpleNamespace(name='read_file',description='Read contents',input_schema={'type':'object','properties':{'path':{'type':'string'}}})]))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'muse.db'
            first=await register_catalog(path,[server],lambda *args:None)
            store=ToolStore(path)
            row=store.list_tools('mcp:filesystem')[0]
            store.set_enabled(row['tool_id'],False)
            second=await register_catalog(path,[server],lambda *args:None)
            self.assertEqual(first['changed'],1)
            self.assertEqual(second['changed'],0)
            self.assertEqual(store.list_tools('mcp:filesystem')[0]['tool_id'],row['tool_id'])
            self.assertFalse(store.get(row['tool_id'])['enabled'])
            server.list_tools.side_effect=ConnectionError('private endpoint credentials')
            logs=[]
            failed=await register_catalog(path,[server],lambda *args:logs.append(args))
            self.assertEqual(failed['errors'],['mcp:filesystem'])
            self.assertEqual(len(store.list_tools('mcp:filesystem')),1)
            self.assertNotIn('private endpoint',str(logs))

    async def test_legacy_builtin_rows_are_purged_with_fingerprint_and_index(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'muse.db'
            store=ToolStore(path)
            with closing(store.connect()) as db, db:
                db.execute("INSERT INTO tool_catalog(tool_id,source_id,source_kind,tool_key,name,description,parameters_json,created_at,updated_at,checked_at) VALUES ('old','muse:builtin','builtin','exec_command','exec_command','execute','{}',0,0,0)")
                db.execute("INSERT INTO tool_fingerprints VALUES ('old','sha256','old-digest',0)")
            store=ToolStore(path)
            self.assertEqual(store.list_tools(),[])
            with closing(store.connect()) as db, db:
                self.assertEqual(db.execute('SELECT count(*) FROM tool_fingerprints').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM tool_catalog_fts').fetchone()[0],0)
            with self.assertRaises(ValueError):
                store.upsert(source_id='muse:builtin',source_kind='builtin',tool_key='test',name='test',description='',parameters={})
