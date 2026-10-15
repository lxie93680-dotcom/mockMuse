"""Canonical role/content message storage, projected from SDK replay items."""
import json
import uuid
from contextlib import closing


def uid(prefix):return prefix+'_'+uuid.uuid4().hex


class MessageStore:
    def __init__(self,store):
        self.store=store
        with closing(store.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS messages (
              message_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT,
              turn_id TEXT, api_request_id TEXT, role TEXT NOT NULL, content TEXT,
              tool_calls TEXT, tool_call_id TEXT, created_at REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'message', PRIMARY KEY(session_id,message_id));
              CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,created_at);
              CREATE TABLE IF NOT EXISTS message_items (
                session_id TEXT, item_id TEXT, message_id TEXT, PRIMARY KEY(session_id,item_id));
              CREATE TABLE IF NOT EXISTS context_item_identity (
                session_id TEXT, item_id TEXT, task_id TEXT, turn_id TEXT, api_request_id TEXT,
                PRIMARY KEY(session_id,item_id));
              CREATE TABLE IF NOT EXISTS task_runs (
                task_id TEXT PRIMARY KEY, session_id TEXT, parent_task_id TEXT, status TEXT,
                api_sequence INTEGER DEFAULT 0, current_turn_id TEXT, current_api_request_id TEXT);
              CREATE TABLE IF NOT EXISTS model_requests (
                api_request_id TEXT PRIMARY KEY, task_id TEXT, turn_id TEXT, retry_index INTEGER DEFAULT 0);
              CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                session_id UNINDEXED,message_id UNINDEXED,content,tokenize='trigram');
              CREATE TRIGGER IF NOT EXISTS messages_search_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(session_id,message_id,content) VALUES(new.session_id,new.message_id,coalesce(new.content,'')||' '||coalesce(new.tool_calls,'')); END;
              CREATE TRIGGER IF NOT EXISTS messages_search_update AFTER UPDATE ON messages BEGIN
                DELETE FROM messages_fts WHERE session_id=old.session_id AND message_id=old.message_id;
                INSERT INTO messages_fts(session_id,message_id,content) VALUES(new.session_id,new.message_id,coalesce(new.content,'')||' '||coalesce(new.tool_calls,'')); END;
              CREATE TRIGGER IF NOT EXISTS messages_search_delete AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE session_id=old.session_id AND message_id=old.message_id; END;
            ''');db.commit()

    def begin_task(self,task_id=None,parent_task_id=None):
        task_id=task_id or uid('task');self.store.task_id=task_id
        with closing(self.store.connect()) as db:
            db.execute('INSERT OR IGNORE INTO task_runs(task_id,session_id,parent_task_id,status) VALUES (?,?,?,?)',(task_id,self.store.session_id,parent_task_id,'running'));db.commit()
        return task_id

    def request_started(self):
        task_id=self.store.task_id
        with closing(self.store.connect()) as db:
            db.execute('UPDATE task_runs SET api_sequence=api_sequence+1 WHERE task_id=?',(task_id,))
            seq=db.execute('SELECT api_sequence FROM task_runs WHERE task_id=?',(task_id,)).fetchone()[0]
            request_id=f'{task_id}:api:{seq}'
            db.execute('INSERT INTO model_requests(api_request_id,task_id) VALUES (?,?)',(request_id,task_id))
            db.execute('UPDATE task_runs SET current_api_request_id=?,current_turn_id=NULL WHERE task_id=?',(request_id,task_id));db.commit()
        return request_id

    def retry(self,request_id,index):
        with closing(self.store.connect()) as db:
            db.execute('UPDATE model_requests SET retry_index=? WHERE api_request_id=?',(index,request_id));db.commit()

    def response_received(self,request_id):
        turn=uid('turn')
        with closing(self.store.connect()) as db:
            db.execute('UPDATE model_requests SET turn_id=? WHERE api_request_id=?',(turn,request_id))
            db.execute('UPDATE task_runs SET current_turn_id=? WHERE task_id=?',(turn,self.store.task_id));db.commit()
        return turn

    def sync(self):
        from .history_tools import searchable_text
        with closing(self.store.connect()) as db:
            rows=db.execute('SELECT item_id,content,created_at FROM context_items WHERE session_id=? ORDER BY rowid',(self.store.session_id,)).fetchall()
            identities={r[0]:r[1:] for r in db.execute('SELECT item_id,task_id,turn_id,api_request_id FROM context_item_identity WHERE session_id=?',(self.store.session_id,))}
            groups=[]
            for item_id,raw,created in rows:
                item=json.loads(raw);typ=item.get('type');role=item.get('role')
                if typ=='reasoning':continue
                identity=identities.get(item_id,(None,None,None))
                if typ=='function_call':
                    role='assistant'
                    call={'id':item.get('call_id'),'type':'function','function':{'name':item.get('name'),'arguments':item.get('arguments','{}')}}
                    response=item.get('provider_data',{}).get('response_id')
                    if groups and groups[-1]['role']=='assistant' and groups[-1]['identity']==identity and (identity[1] or response and response==groups[-1]['response'] or not response and not groups[-1]['response'] and groups[-1]['last_type']=='function_call'):
                        groups[-1]['ids'].append(item_id);groups[-1]['calls'].append(call);groups[-1]['last_type']=typ;continue
                    groups.append(dict(ids=[item_id],role=role,content='',calls=[call],call_id=None,created=created,identity=identity,response=response,last_type=typ));continue
                if typ=='function_call_output':role='tool';content=searchable_text(item.get('output',''))
                elif role in ('user','assistant','system','developer'):content=searchable_text(item.get('content',''))
                else:continue
                response=item.get('provider_data',{}).get('response_id')
                if role=='assistant' and groups and groups[-1]['role']=='assistant' and groups[-1]['identity']==identity and (identity[1] or response and response==groups[-1]['response']):
                    groups[-1]['ids'].append(item_id);groups[-1]['content']+='\n'+content;continue
                groups.append(dict(ids=[item_id],role=role,content=content,calls=[],call_id=item.get('call_id') if role=='tool' else None,created=created,identity=identity,response=response,last_type=typ))
            for group in groups:
                existing=db.execute('SELECT message_id FROM message_items WHERE session_id=? AND item_id=?',(self.store.session_id,group['ids'][0])).fetchone()
                message_id=existing[0] if existing else (group['ids'][0] if group['ids'][0].startswith('summary_') else uid('message'))
                # Existing source message IDs remain stable; legacy item IDs are accepted aliases.
                task,turn,request=group['identity']
                if group['role']=='tool' and db.execute("SELECT 1 FROM sqlite_master WHERE name='tool_originals'").fetchone():
                    original=db.execute('SELECT message_id,raw_content FROM tool_originals WHERE session_id=? AND task_id IS ? AND turn_id IS ? AND tool_call_id=?',(self.store.session_id,task,turn,group['call_id'])).fetchone()
                    if original:
                        message_id=original[0];group['content']=original[1]
                kind='summary' if message_id.startswith('summary_') else 'message'
                fields=(message_id,self.store.session_id,task,turn,request,group['role'],group['content'],json.dumps(group['calls'],ensure_ascii=False) if group['calls'] else None,group['call_id'],group['created'],kind)
                old=db.execute('SELECT message_id,session_id,task_id,turn_id,api_request_id,role,content,tool_calls,tool_call_id,created_at,kind FROM messages WHERE session_id=? AND message_id=?',(self.store.session_id,message_id)).fetchone()
                if old!=fields:
                    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO UPDATE SET content=excluded.content,tool_calls=excluded.tool_calls,task_id=excluded.task_id,turn_id=excluded.turn_id,api_request_id=excluded.api_request_id',fields)
                for item_id in group['ids']:
                    db.execute('INSERT OR REPLACE INTO message_items VALUES (?,?,?)',(self.store.session_id,item_id,message_id))
            # Map old checkpoint boundaries to the canonical message identity.
            for checkpoint_id,content in db.execute('SELECT item_id,content FROM context_checkpoints WHERE session_id=?',(self.store.session_id,)).fetchall():
                cp=json.loads(content);boundary=cp.get('covered_through_item_id')
                mapped=db.execute('SELECT message_id FROM message_items WHERE session_id=? AND item_id=?',(self.store.session_id,boundary)).fetchone()
                cp.update(checkpoint_id=checkpoint_id,covered_through_message_id=mapped[0] if mapped else None)
                db.execute('UPDATE context_checkpoints SET content=? WHERE session_id=? AND item_id=?',(json.dumps(cp,ensure_ascii=False),self.store.session_id,checkpoint_id))
            db.commit()

    def tag_items(self,db,rows):
        task_id=getattr(self.store,'task_id',None)
        if not task_id:return
        current=db.execute('SELECT current_turn_id,current_api_request_id FROM task_runs WHERE task_id=?',(task_id,)).fetchone()
        for row in rows:
            db.execute('INSERT OR IGNORE INTO context_item_identity VALUES (?,?,?,?,?)',(self.store.session_id,row['item_id'],task_id,*(current or (None,None))))
