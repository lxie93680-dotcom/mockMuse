import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from muse import evaluation_center as ec
from muse.search_annotations import Annotations,AnnotationConflict
from evals.tool_search import run

class SearchAnnotations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'tool_search';self.root.mkdir()
        self.cases=[{'id':'one','query':'save text','relevant':['write_file'],'category':'text'}, {'id':'two','query':'nothing','relevant':[],'category':'none'}]
        self.tools=[dict(source_id='plugin:test',name=n,description=n,parameters={'type':'object','properties':{}}) for n in ('write_file','read_file')]
        (self.root/'cases.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in self.cases));(self.root/'catalog.json').write_text(json.dumps(self.tools));(self.root/'protocol.json').write_text('{}')
        self.store=Annotations(self.root);self.digest=self.store.load()['dataset_hash']
    def tearDown(self):self.temp.cleanup()
    def save(self,id='one',**kwargs):
        body=dict(dataset_hash=self.digest,base_revision=0,status='confirmed',relevant=['write_file'],no_match=False,note='本人判断');body.update(kwargs)
        return self.store.save(id,**body)
    def test_labels_blank_until_saved_and_history_preserved(self):
        self.assertEqual(self.store.load()['annotations'],{})
        draft=self.save(status='draft');self.assertEqual(self.store.load()['progress']['draft'],1)
        confirmed=self.save(base_revision=draft['revision'],relevant=['read_file','write_file'])
        self.assertEqual(Annotations(self.root).load()['annotations']['one'],confirmed)
        with sqlite3.connect(self.store.path) as db:self.assertEqual(db.execute('select count(*) from judgments').fetchone()[0],2)
        self.assertEqual(json.loads((self.root/'cases.jsonl').read_text().splitlines()[0])['relevant'],['write_file'])
    def test_empty_requires_explicit_no_match_and_unknown_tools_rejected(self):
        for kwargs in [dict(relevant=[]),dict(relevant=['unknown']),dict(relevant=['write_file','write_file']),dict(no_match=True)]:
            with self.assertRaises(ValueError):self.save(**kwargs)
        self.save('two',relevant=[],no_match=True)
        self.assertEqual(self.store.load()['progress']['confirmed'],1)
    def test_optimistic_lock_and_dataset_changes(self):
        self.save()
        with self.assertRaises(AnnotationConflict):self.save()
        tools=json.loads((self.root/'catalog.json').read_text());tools[0]['description']='changed';(self.root/'catalog.json').write_text(json.dumps(tools))
        self.assertEqual(self.store.load()['annotations'],{})
        with self.assertRaises(AnnotationConflict):self.save()
    def test_api_validation_and_isolation(self):
        app=FastAPI();app.include_router(ec.router)
        with patch.object(ec,'EVAL_ROOT',self.root.parent),TestClient(app) as client:
            self.assertEqual(client.get('/api/evals/tool-search/annotations').status_code,200)
            body=dict(dataset_hash=self.digest,base_revision=0,status='confirmed',relevant=[],no_match=False)
            path='/api/evals/tool-search/annotations/two'
            self.assertEqual(client.post(path,json=body).status_code,422)
            body['no_match']=True
            self.assertEqual(client.post(path,json=body).status_code,200)
            self.assertEqual(client.post(path,json=body).status_code,409)
            self.assertEqual(client.post(path,json={**body,'no_match':'true'}).status_code,422)
            self.assertEqual(client.post('/api/evals/tool-search/annotations/unknown',json=body).status_code,422)
    def test_symlink_database_refused(self):
        self.store.path.unlink();self.store.path.symlink_to(self.root/'catalog.json')
        with self.assertRaises(ValueError):self.store.load()

    async def test_confirmed_only_and_snapshot_is_reproducible(self):
        self.save();self.save('two',status='draft',relevant=[])
        with patch.object(run,'ROOT',self.root):
            with self.assertRaises(ValueError):await run.evaluate(self.root/'results'/'incomplete','human')
            with self.assertRaises(ValueError):await run.evaluate(self.root/'results'/'none','unknown')
            partial=await run.evaluate(self.root/'results'/'partial','human',True)
            self.assertEqual(partial['total'],1);self.assertTrue(partial['partial'])
            snapshot=json.loads((self.root/'results'/'partial'/'labels.json').read_text())
            self.assertEqual(set(snapshot['annotations']),{'one'})
            draft=self.store.load()['annotations']['two']
            self.save('two',base_revision=draft['revision'],relevant=[],no_match=True)
            full=await run.evaluate(self.root/'results'/'full','human')
            self.assertEqual(full['total'],2);self.assertFalse(full['partial'])
            self.assertEqual(full['label_source'],'human')
            self.assertEqual(json.loads((self.root/'results'/'partial'/'labels.json').read_text()),snapshot)
