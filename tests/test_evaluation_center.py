import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from muse import evaluation_center as ec

class EvaluationCenter(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patch=patch.object(ec,'EVAL_ROOT',self.root);self.patch.start()
        p=self.root/'suite';p.mkdir();(p/'cases.jsonl').write_text('{"id":"one"}\n')
        self.run=p/'results'/'run-1';self.run.mkdir(parents=True)
        (self.run/'results.json').write_text(json.dumps([{'case_id':'one','status':'fail','trace':[{'operation':'click','args':['e1'],'error':'timeout','result':None}]},{'case_id':'two','status':'pass'}]))
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def test_catalog_counts_and_failure_trace(self):
        c=ec.catalog();self.assertEqual(c['suites'][0]['case_count'],1)
        r=c['runs'][0];self.assertEqual(r['counts']['fail'],1)
        rows=ec.run_rows(r['id'],status='fail');self.assertEqual(rows['total'],1)
        d=ec.detail(r['id'],rows['rows'][0]['index']);self.assertEqual(d['trace'][0]['error'],'timeout')
    def test_invalid_id_and_pagination_rejected(self):
        with self.assertRaises(HTTPException):ec.lookup('../../.env')
        with self.assertRaises(HTTPException):ec.cases('../')
        with self.assertRaises(HTTPException):ec.run_rows('x',limit=201)
    def test_symlink_not_read(self):
        p=self.root/'escape.json';p.symlink_to('/etc/passwd')
        with self.assertRaises(HTTPException):ec.read(p)
    def test_compression_grade_and_redaction(self):
        (self.run/'results.json').write_text(json.dumps([{'variant_id':'v','mode':'compressed','grade':{'exact':False},'error':{'token':'do-not-expose','message':'bad'}}]))
        r=ec.catalog()['runs'][0];d=ec.detail(r['id'],0)
        self.assertEqual(d['status'],'error');self.assertNotIn('do-not-expose',json.dumps(d))
    def test_validation_not_counted_as_model_runs(self):
        (self.run/'results.json').unlink();(self.run/'validation.json').write_text('{"status":"offline_passed","model_calls":0}')
        r=ec.catalog()['runs'][0];self.assertEqual(r['total'],0);self.assertEqual(r['kind'],'验证/基础设施记录')
    def test_suite_classification_inherited_by_historical_runs(self):
        (self.root/'suite'/'protocol.json').write_text(json.dumps({'suite_type':'test','title':'系统回归'}))
        c=ec.catalog();self.assertEqual(c['suites'][0]['suite_type'],'test');self.assertEqual(c['suites'][0]['title'],'系统回归')
        self.assertEqual(c['runs'][0]['suite_type'],'test')
        (self.root/'suite'/'protocol.json').write_text(json.dumps({'suite_type':'evaluation'}))
        self.assertEqual(ec.catalog()['runs'][0]['suite_type'],'evaluation')
    def test_legacy_classification_defaults(self):
        self.assertEqual(ec.suite_type('tool_lifecycle'),'test')
        self.assertEqual(ec.suite_type('browser_grounding'),'evaluation')
        self.assertEqual(ec.suite_type('unknown',{'suite_type':'invalid'}),'evaluation')
