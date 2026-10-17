"""Evidence-based initial grades and append-only human review of run results."""
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime,timezone

RUBRIC_VERSION='evidence-v1'
class ReviewConflict(ValueError):pass

def initial_grade(suite,row):
    grade={'score':None,'scale':100,'rubric_version':RUBRIC_VERSION,
           'method':'规则初评（非模型裁判）','rubric':'未定义','reason':'缺少可适用的评分规则或执行证据，待人工复核。','dimensions':{}}
    if row.get('error') or row.get('compression_error') or row.get('status') in ('invalid','error'):
        grade['reason']='本次运行异常或未有效执行，不把基础设施错误当作能力零分。';return grade
    if suite=='tool_search' and isinstance(row.get('relevant'),list) and isinstance(row.get('retrieved'),list):
        if any(not isinstance(n,str) for n in row['relevant']+row['retrieved']):return grade
        expected=set(row['relevant']);actual=row['retrieved'][:5];found=expected.intersection(actual)
        if expected:
            rank=next((i+1 for i,n in enumerate(actual) if n in expected),None)
            grade.update(score=round(100*len(found)/len(expected),2),rubric='搜索召回 Recall@5 × 100',
                reason=f'前5项找回 {len(found)}/{len(expected)} 个标注相关工具。排名指标单列，不把单次命中等同完整召回。',
                dimensions={'relevant':sorted(expected),'retrieved':actual,'missing':sorted(expected-found),
                            'first_relevant_rank':rank,'mrr_at_5':1/rank if rank else 0,
                            'precision_at_5':len(found)/len(actual) if actual else 0})
        else:
            grade.update(score=100 if not actual else 0,rubric='无匹配查询是否返回空',
                         reason='标注为无合适工具；'+('正确返回空。' if not actual else '仍返回了候选工具。'),dimensions={'retrieved':actual})
        return grade
    if suite=='tool_lifecycle' and row.get('status') in ('pass','fail') and (row.get('checks') or row.get('failure')):
        grade.update(score=100 if row['status']=='pass' else 0,rubric='系统断言全部通过=100，否则=0',
            reason='仅反映该次生命周期场景的系统正确性，不是搜索质量分。',
            dimensions={'passed_checks':row.get('checks',[]),'failure':row.get('failure'),
                        'file_content_exact':row.get('observations',{}).get('file_content',{}).get('content_exact')})
        if grade['dimensions']['file_content_exact'] is False:grade['reason']+=' 文件包含目标内容，但逐字精确度未通过。'
        return grade
    if suite=='browser_grounding' and row.get('status') in ('pass','fail') and isinstance(row.get('events'),list):
        grade.update(score=100 if row['status']=='pass' and not row.get('wrong_click') else 0,
            rubric='浏览器任务原判定通过且无误点=100，否则=0',
            reason='依据已记录的任务判定和页面事件；这是二元任务分，不评价语言风格。',
            dimensions={k:row.get(k) for k in ('events','wrong_click','first_attempt_success','tool_errors')})
        return grade
    if suite.startswith('compression/') and isinstance(row.get('grade'),dict) and isinstance(row['grade'].get('exact'),bool):
        grade.update(score=100 if row['grade']['exact'] else 0,rubric='原严格正确判定通过=100，否则=0',
                     reason='沿用本次运行严格正确性标准。',dimensions={'grade':row['grade']})
    return grade

class RunReviews:
    def __init__(self,root):self.path=root/'run-reviews.sqlite3'
    def connect(self):
        if self.path.is_symlink():raise ValueError('复核数据库不能是符号链接')
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS initial_grades (
          id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,artifact_hash TEXT NOT NULL,row_index INTEGER NOT NULL,
          rubric_version TEXT NOT NULL,grade_json TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(run_id,artifact_hash,row_index,rubric_version));
          CREATE TABLE IF NOT EXISTS human_reviews (
          revision INTEGER PRIMARY KEY AUTOINCREMENT,grade_id INTEGER NOT NULL,
          verdict TEXT NOT NULL,score REAL,note TEXT NOT NULL,created_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS review_grade ON human_reviews(grade_id,revision);''')
        return db
    @staticmethod
    def decode_review(row):
        return {k:row[k] for k in ('revision','verdict','score','note','created_at')}
    def load(self,run_id,path,rows,suite):
        raw=path.read_bytes();data=json.loads(raw) if path.suffix!='.jsonl' else [json.loads(s) for s in raw.splitlines() if s.strip()]
        actual=data if isinstance(data,list) else data.get('results',[]) if isinstance(data,dict) else []
        if actual!=rows:raise ReviewConflict('运行文件已变化，请重新读取后复核')
        digest=hashlib.sha256(raw).hexdigest()
        with closing(self.connect()) as db,db:
            existing={r['row_index']:r for r in db.execute('SELECT * FROM initial_grades WHERE run_id=? AND artifact_hash=? AND rubric_version=?',(run_id,digest,RUBRIC_VERSION))}
            now=datetime.now(timezone.utc).isoformat()
            for index,row in enumerate(rows):
                if index not in existing:
                    db.execute('INSERT OR IGNORE INTO initial_grades(run_id,artifact_hash,row_index,rubric_version,grade_json,created_at) VALUES(?,?,?,?,?,?)',
                        (run_id,digest,index,RUBRIC_VERSION,json.dumps(initial_grade(suite,row),ensure_ascii=False),now))
            grades=db.execute('SELECT * FROM initial_grades WHERE run_id=? AND artifact_hash=? AND rubric_version=?',(run_id,digest,RUBRIC_VERSION)).fetchall()
            reviews={r['grade_id']:self.decode_review(r) for r in db.execute('''SELECT h.* FROM human_reviews h JOIN initial_grades g ON h.grade_id=g.id
                WHERE g.run_id=? AND g.artifact_hash=? AND g.rubric_version=? AND h.revision=(SELECT MAX(h2.revision) FROM human_reviews h2 WHERE h2.grade_id=h.grade_id)''',(run_id,digest,RUBRIC_VERSION))}
            return [{'initial_grade':{**json.loads(r['grade_json']),'grade_id':r['id'],'created_at':r['created_at']},
                     'human_review':reviews.get(r['id']),'artifact_hash':digest} for r in sorted(grades,key=lambda r:r['row_index'])]
    def save(self,grade_id,*,base_revision,verdict,score,note):
        if verdict not in ('agree','adjust','unscorable'):raise ValueError('复核结论无效')
        if not isinstance(note,str) or len(note)>4000:raise ValueError('复核理由最多4000字')
        if verdict!='agree' and not note.strip():raise ValueError('改分或标记待评时请填写理由')
        if score is not None and (type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=100):raise ValueError('分数必须为0到100')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            grade=db.execute('SELECT grade_json FROM initial_grades WHERE id=?',(grade_id,)).fetchone()
            if not grade:raise ValueError('初评分不存在')
            original=json.loads(grade['grade_json'])['score']
            if verdict=='agree':
                if original is None:raise ValueError('待评任务不能认可数值分，请改分或标记证据不足')
                if score!=original:raise ValueError('认可初评分时不能修改分数')
            elif verdict=='adjust' and score is None:raise ValueError('请填写复核分数')
            elif verdict=='unscorable' and score is not None:raise ValueError('证据不足时分数必须为空')
            old=db.execute('SELECT revision FROM human_reviews WHERE grade_id=? ORDER BY revision DESC LIMIT 1',(grade_id,)).fetchone()
            if (old['revision'] if old else 0)!=base_revision:raise ReviewConflict('该任务已在其他页面复核，请重新打开后核对；未覆盖已有结果')
            cursor=db.execute('INSERT INTO human_reviews(grade_id,verdict,score,note,created_at) VALUES(?,?,?,?,?)',
                (grade_id,verdict,score,note,datetime.now(timezone.utc).isoformat()))
            return self.decode_review(db.execute('SELECT * FROM human_reviews WHERE revision=?',(cursor.lastrowid,)).fetchone())
