"""Versioned human relevance judgments, isolated from generated labels and runs."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

class AnnotationConflict(ValueError):pass

def dataset(root):
    root=Path(root)
    for name in ('cases.jsonl','catalog.json'):
        if (root/name).is_symlink():raise ValueError('标注源文件不能是符号链接')
    cases=[json.loads(s) for s in (root/'cases.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    tools=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    if len({r['id'] for r in cases})!=len(cases):raise ValueError('查询ID重复')
    if len({r['name'] for r in tools})!=len(tools):raise ValueError('工具名重复')
    payload={'cases':cases,'tools':tools}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return cases,tools,digest

class Annotations:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'annotations.sqlite3'
    def connect(self):
        if self.path.is_symlink():raise ValueError('标注数据库不能是符号链接')
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS judgments (
          revision INTEGER PRIMARY KEY AUTOINCREMENT,dataset_hash TEXT NOT NULL,
          case_id TEXT NOT NULL,status TEXT NOT NULL,relevant_json TEXT NOT NULL,
          no_match INTEGER NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL)''')
        db.commit();return db
    @staticmethod
    def decode(row):
        return {'revision':row['revision'],'case_id':row['case_id'],'status':row['status'],
                'relevant':json.loads(row['relevant_json']),'no_match':bool(row['no_match']),
                'note':row['note'],'updated_at':row['created_at']}
    def current(self,digest,db):
        return {row['case_id']:self.decode(row) for row in db.execute('''SELECT * FROM judgments WHERE revision IN
          (SELECT MAX(revision) FROM judgments WHERE dataset_hash=? GROUP BY case_id)''',(digest,))}
    def load(self):
        cases,tools,digest=dataset(self.root)
        with closing(self.connect()) as db:labels=self.current(digest,db)
        return {'dataset_hash':digest,'cases':cases,'tools':tools,'annotations':labels,
                'progress':{state:sum(labels.get(c['id'],{}).get('status','unreviewed')==state for c in cases)
                            for state in ('unreviewed','draft','confirmed')},'total':len(cases)}
    def save(self,case_id,*,dataset_hash,base_revision,status,relevant,no_match,note):
        cases,tools,digest=dataset(self.root)
        if dataset_hash!=digest:raise AnnotationConflict('查询或工具快照已变化，请重新打开标注页')
        if case_id not in {c['id'] for c in cases}:raise ValueError('查询不存在')
        if status not in ('draft','confirmed'):raise ValueError('标注状态无效')
        if not isinstance(relevant,list) or any(not isinstance(n,str) for n in relevant):raise ValueError('工具标注必须是列表')
        if len(set(relevant))!=len(relevant) or not set(relevant)<={t['name'] for t in tools}:raise ValueError('标注包含未知或重复工具')
        if type(no_match) is not bool or type(base_revision) is not int or base_revision<0:raise ValueError('标注参数无效')
        if no_match and relevant:raise ValueError('无合适工具时不能同时选择工具')
        if status=='confirmed' and not relevant and not no_match:raise ValueError('请选择相关工具，或明确勾选无合适工具')
        if not isinstance(note,str) or len(note)>4000:raise ValueError('备注最多4000字')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT revision FROM judgments WHERE dataset_hash=? AND case_id=? ORDER BY revision DESC LIMIT 1',(digest,case_id)).fetchone()
            if (old['revision'] if old else 0)!=base_revision:raise AnnotationConflict('此条标注已在其他页面更新，当前修改未覆盖；请重新打开后核对')
            cursor=db.execute('INSERT INTO judgments(dataset_hash,case_id,status,relevant_json,no_match,note,created_at) VALUES(?,?,?,?,?,?,?)',
                (digest,case_id,status,json.dumps(sorted(relevant)),int(no_match),note,datetime.now(timezone.utc).isoformat()))
            return self.decode(db.execute('SELECT * FROM judgments WHERE revision=?',(cursor.lastrowid,)).fetchone())
