import copy
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from muse.tool_store import ToolStore, definition_fingerprint


class ToolStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'muse.db'
        self.store = ToolStore(self.path)
        self.definition = dict(source_id='filesystem', source_kind='mcp', tool_key='read_file',
                               name='read_file', description='读取文件内容 Read document contents',
                               parameters={'type':'object','properties':{'path':{'type':'string','description':'Absolute filename'}},'required':['path']})

    def register(self, **changes):
        return self.store.upsert(**{**self.definition, **changes})

    def test_persistence_and_separate_tables(self):
        result = self.register()
        self.store.set_enabled(result['tool_id'], False)
        self.store.set_availability(result['tool_id'], 'unavailable')
        reopened = ToolStore(self.path)
        row = reopened.get(result['tool_id'])
        self.assertFalse(row['enabled'])
        self.assertEqual(row['availability'], 'unavailable')
        self.assertEqual(row['parameters'], self.definition['parameters'])
        self.assertEqual(row['fingerprint'], result['fingerprint'])
        self.assertEqual(len(reopened.search('document', enabled_only=False)), 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ('tool_catalog', 'tool_fingerprints', 'tool_catalog_fts'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)

    def test_canonical_hash_and_noop_preserve_index_and_timestamps(self):
        initial = self.register()
        row = self.store.get(initial['tool_id'])
        schema = self.definition['parameters']
        reordered = dict(reversed(list(schema.items())))
        self.assertEqual(definition_fingerprint('read_file',self.definition['description'],schema),
                         definition_fingerprint('read_file',self.definition['description'],reordered))
        with closing(sqlite3.connect(self.path)) as db, db:
            before = db.execute('SELECT rowid FROM tool_catalog_fts').fetchone()[0]
        again = self.register(parameters=reordered)
        self.assertEqual(again['tool_id'], initial['tool_id'])
        self.assertFalse(again['changed'])
        self.assertEqual(self.store.get(initial['tool_id'])['updated_at'],row['updated_at'])
        self.assertEqual(self.store.get(initial['tool_id'])['fingerprint_updated_at'],row['fingerprint_updated_at'])
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT rowid FROM tool_catalog_fts').fetchone()[0],before)

    def test_update_changes_hash_and_index_without_resetting_state(self):
        original = self.register()
        self.store.set_enabled(original['tool_id'],False)
        self.store.set_availability(original['tool_id'],'unavailable')
        changed = self.register(name='inspect_file', description='浏览图像 Inspect photographs',
                                parameters={'type':'object','properties':{'image':{'type':'string'}}})
        self.assertTrue(changed['changed'])
        self.assertEqual(changed['tool_id'],original['tool_id'])
        self.assertNotEqual(changed['fingerprint'],original['fingerprint'])
        row = self.store.get(original['tool_id'])
        self.assertFalse(row['enabled'])
        self.assertEqual(row['availability'],'unavailable')
        self.assertEqual(self.store.search('document',enabled_only=False),[])
        self.assertEqual(len(self.store.search('photographs',enabled_only=False)),1)
        self.assertEqual(self.store.search('photographs'),[])

    def test_sources_do_not_collide(self):
        a = self.register()
        b = self.register(source_id='another-service')
        self.assertNotEqual(a['tool_id'],b['tool_id'])
        self.assertEqual(len(self.store.list_tools('filesystem')),1)
        self.assertEqual(len(self.store.list_tools()),2)

    def test_search_chinese_short_parameters_and_literal_syntax(self):
        self.register()
        for query in ('读取文件','文件','Absolute filename','filesystem'):
            self.assertEqual(len(self.store.search(query)),1,query)
        for query in ('" OR "','*','NOT','路径不存在'):
            self.assertEqual(self.store.search(query),[])
        for query in ('',' '*2,'x'*201):
            with self.assertRaises(ValueError):
                self.store.search(query)

    def test_batch_rolls_back_all_tables_on_late_failure(self):
        initial = self.register()
        changed = {**self.definition, 'description':'new-content'}
        # The second definition conflicts with immutable source identity after the
        # first one has updated metadata, fingerprint and the trigger-driven index.
        conflict = {**self.definition, 'source_kind':'http'}
        other = {**self.definition,'tool_key':'other','name':'other'}
        self.store.upsert(**other)
        conflict['tool_key'] = 'other'
        with self.assertRaises(ValueError):
            self.store.upsert_many([changed,conflict])
        self.assertEqual(self.store.get(initial['tool_id'])['description'],self.definition['description'])
        self.assertEqual(self.store.get(initial['tool_id'])['fingerprint'],initial['fingerprint'])
        self.assertEqual(self.store.search('new-content'),[])
        self.assertEqual(len(self.store.search('document')),2)

    def test_index_rebuild_and_delete_consistency(self):
        result = self.register()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM tool_catalog_fts')
        self.assertEqual(self.store.search('document'),[])
        self.store.rebuild_index()
        self.assertEqual(len(self.store.search('document')),1)
        db = self.store.connect()
        try:
            with db:
                db.execute('DELETE FROM tool_catalog WHERE tool_id=?',(result['tool_id'],))
            self.assertEqual(db.execute('SELECT count(*) FROM tool_fingerprints').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM tool_catalog_fts').fetchone()[0],0)
        finally:
            db.close()

    def test_invalid_definition_does_not_write(self):
        bad = copy.deepcopy(self.definition)
        bad['parameters']['invalid'] = float('nan')
        for changes in ({'source_kind':'invalid'},{'tool_key':''},{'description':None},{'parameters':[]},bad):
            with self.assertRaises((ValueError,TypeError)):
                self.register(**changes)
        with self.assertRaises(ValueError):
            self.store.upsert_many([self.definition,self.definition])
        self.assertEqual(self.store.list_tools(),[])

    def test_state_changes_do_not_change_definition_hash(self):
        result = self.register()
        self.store.set_enabled(result['tool_id'],False)
        self.store.set_availability(result['tool_id'],'available')
        self.assertEqual(self.store.get(result['tool_id'])['fingerprint'],result['fingerprint'])
        with self.assertRaises(ValueError):
            self.store.set_enabled(result['tool_id'],1)
        with self.assertRaises(KeyError):
            self.store.set_enabled('nonexistent',True)

    def test_server_id_persists_and_filters_without_changing_tool_identity(self):
        initial=self.register(server_id='travel')
        before=self.store.get(initial['tool_id'])
        self.store.set_enabled(initial['tool_id'],False)
        again=self.register(server_id='travel')
        self.assertEqual(initial['tool_id'],again['tool_id']);self.assertFalse(again['changed'])
        reopened=ToolStore(self.path)
        row=reopened.get(initial['tool_id'])
        self.assertEqual(row['server_id'],'travel');self.assertFalse(row['enabled'])
        self.assertEqual(row['fingerprint'],before['fingerprint'])
        self.assertEqual(row['updated_at'],before['updated_at'])
        self.assertEqual(reopened.list_tools(server_id='other'),[])
        self.assertEqual(reopened.list_tools(server_id='travel')[0]['tool_id'],initial['tool_id'])
        self.assertEqual(reopened.search('document',server_id='travel'),[])
        self.assertEqual(reopened.search('文件',server_id='travel',enabled_only=False)[0]['server_id'],'travel')
        self.assertEqual(reopened.search('document',server_id='other',enabled_only=False),[])

    def test_legacy_schema_migration_and_backfill_preserve_states_and_hash(self):
        initial=self.register();self.store.set_enabled(initial['tool_id'],False)
        self.store.set_availability(initial['tool_id'],'unavailable')
        before=self.store.get(initial['tool_id'])
        with closing(self.store.connect()) as db,db:
            db.execute('DROP INDEX tool_catalog_server')
            db.execute('ALTER TABLE tool_catalog DROP COLUMN server_id')
            db.execute("UPDATE tool_catalog_meta SET value='1' WHERE key='schema_version'")
        migrated=ToolStore(self.path)
        self.assertIsNone(migrated.get(initial['tool_id'])['server_id'])
        migrated.bind_server_id('filesystem','travel')
        reopened=ToolStore(self.path);row=reopened.get(initial['tool_id'])
        self.assertEqual(row['server_id'],'travel')
        for field in ('tool_id','fingerprint','fingerprint_updated_at','updated_at','enabled','availability'):
            self.assertEqual(row[field],before[field],field)
        self.assertEqual(reopened.search('document',enabled_only=False)[0]['server_id'],'travel')

    def test_server_binding_cannot_be_reassigned_or_erased_by_refresh(self):
        initial=self.register(server_id='travel')
        self.register()  # Older provider omits metadata.
        self.assertEqual(self.store.get(initial['tool_id'])['server_id'],'travel')
        new=self.register(tool_key='other',name='other')
        self.assertEqual(self.store.get(new['tool_id'])['server_id'],'travel')
        with self.assertRaises(ValueError):self.register(server_id='wrong',description='must rollback')
        self.assertEqual(self.store.get(initial['tool_id'])['description'],self.definition['description'])
        with self.assertRaises(ValueError):self.store.bind_server_id('filesystem','wrong')
        for server_id in ('../bad','',123):
            with self.assertRaises(ValueError):self.register(server_id=server_id)
