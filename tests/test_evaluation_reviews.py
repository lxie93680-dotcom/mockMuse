import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from muse import evaluation_center as ec
from muse.evaluation_reviews import RunReviews,ReviewConflict,initial_grade

class InitialGrading(unittest.TestCase):
    def test_search_partial_recall_and_rank_are_separate(self):
        g=initial_grade('tool_search',{'relevant':['a','b'],'retrieved':['noise','a'],'status':'pass'})
        self.assertEqual(g['score'],50);self.assertEqual(g['dimensions']['first_relevant_rank'],2)
        self.assertEqual(g['dimensions']['mrr_at_5'],.5)
        self.assertEqual(initial_grade('tool_search',{'relevant':[],'retrieved':[]})['score'],100)
        self.assertEqual(initial_grade('tool_search',{'relevant':[],'retrieved':['noise']})['score'],0)
    def test_insufficient_evidence_is_not_capability_failure(self):
        for row in ({'status':'error','error':'timeout'},{'status':'invalid'},{'status':'pass'}):
            self.assertIsNone(initial_grade('unknown',row)['score'])
        self.assertIsNone(initial_grade('tool_lifecycle',{'status':'pass'})['score'])
        grade=initial_grade('tool_lifecycle',{'status':'pass','checks':['triggered'],'observations':{'file_content':{'content_exact':False}}})
        self.assertEqual(grade['score'],100);self.assertIn('逐字精确度未通过',grade['reason'])
        self.assertEqual(initial_grade('browser_grounding',{'status':'pass','events':['target'],'wrong_click':True})['score'],0)

class RunReviewAPI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.suite=self.root/'tool_search';self.path=self.suite/'results'/'run1'/'results.jsonl';self.path.parent.mkdir(parents=True)
        (self.suite/'cases.jsonl').write_text('{"id":"one","query":"save"}\n')
        self.row={'case_id':'one','query':'save','relevant':['a','b'],'retrieved':['a'],'status':'pass'}
        self.path.write_text(json.dumps(self.row)+'\n');self.original=self.path.read_bytes()
        self.patch=patch.object(ec,'EVAL_ROOT',self.root);self.patch.start();app=FastAPI();app.include_router(ec.router);self.client=TestClient(app)
        self.id=ec.key(self.path);self.url=f'/api/evals/runs/{self.id}/cases/0'
    def tearDown(self):self.client.close();self.patch.stop();self.temp.cleanup()
    def body(self):
        d=self.client.get(self.url).json()
        return dict(artifact_hash=d['artifact_hash'],grade_id=d['initial_grade']['grade_id'],base_revision=d['human_review']['revision'] if d['human_review'] else 0,verdict='adjust',score=75,note='部分完成，补充人工判断')
    def test_review_survives_reload_does_not_overwrite_initial_or_artifact(self):
        body=self.body();response=self.client.post(self.url+'/review',json=body);self.assertEqual(response.status_code,200)
        d=self.client.get(self.url).json();self.assertEqual(d['initial_grade']['score'],50);self.assertEqual(d['human_review']['score'],75)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(self.client.post(self.url+'/review',json=body).status_code,409)
        body=self.body();body.update(verdict='agree',score=50,note='确认')
        self.assertEqual(self.client.post(self.url+'/review',json=body).status_code,200)
        with RunReviews(self.root).connect() as db:self.assertEqual(db.execute('select count(*) from human_reviews').fetchone()[0],2)
        self.assertEqual(ec.run_rows(self.id,review='reviewed')['total'],1)
        self.assertEqual(ec.run_rows(self.id,review='unreviewed')['total'],0)
    def test_invalid_score_and_reason_rejected(self):
        body=self.body()
        for delta in ({'score':101},{'score':None},{'note':''},{'verdict':'agree','score':99},{'verdict':'unscorable','score':75}):
            self.assertEqual(self.client.post(self.url+'/review',json={**body,**delta}).status_code,422)
        self.assertIsNone(self.client.get(self.url).json()['human_review'])
    def test_changed_artifact_invalidates_old_review_binding(self):
        body=self.body();self.assertEqual(self.client.post(self.url+'/review',json=body).status_code,200)
        self.row['retrieved']=[];self.path.write_text(json.dumps(self.row)+'\n')
        self.assertEqual(self.client.post(self.url+'/review',json=body).status_code,409)
        current=self.client.get(self.url).json();self.assertIsNone(current['human_review']);self.assertEqual(current['initial_grade']['score'],0)
    def test_row_order_and_snapshot_race_are_not_misattributed(self):
        self.body()
        with self.assertRaises(ReviewConflict):RunReviews(self.root).load(self.id,self.path,[{**self.row,'query':'different'}],'tool_search')
