"""Archive original tool returns; produce a bounded model-context projection."""
import json
import re
import uuid
from html.parser import HTMLParser
from contextlib import closing

MAX_CHARS=15000
MARKER='\n[…工具结果已截断，完整原文通过read_history读取…]\n'


def head_tail(text,limit=MAX_CHARS):
    if len(text)<=limit:return text
    room=max(0,limit-len(MARKER));head=(room+1)//2
    return text[:head]+MARKER+text[-(room-head):]


class PageText(HTMLParser):
    """Strip markup and common boilerplate without executing page code."""
    SKIP={'script','style','noscript','nav','header','footer','aside','iframe','svg','template'}
    BLOCK={'p','div','section','article','main','br','li','h1','h2','h3','h4','tr'}
    def __init__(self):super().__init__(convert_charrefs=True);self.stack=[];self.parts=[];self.title=[];self.in_title=False
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);labels=' '.join(attrs.get(k,'') for k in ('class','id','role'))
        hidden='hidden' in attrs or attrs.get('aria-hidden')=='true'
        boilerplate=bool(re.search(r'(^|[\s_\-])(ads?|adsbygoogle|advertisement|advertising|sponsor(?:ed)?|navigation|navbar|sidebar|cookie-banner)([\s_\-]|$)',labels,re.I))
        skip=tag in self.SKIP or hidden or boilerplate or bool(self.stack and self.stack[-1][1])
        if tag not in {'br','img','input','meta','link','hr','source','wbr','area','embed'}:self.stack.append((tag,skip))
        if tag=='title':self.in_title=True
        if tag in self.BLOCK and not skip:self.parts.append('\n')
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:del self.stack[i:];break
        if tag=='title':self.in_title=False
        if tag in self.BLOCK:self.parts.append('\n')
    def handle_data(self,data):
        if self.in_title:self.title.append(data)
        elif not (self.stack and self.stack[-1][1]):self.parts.append(data)
    def text(self):
        return '\n'.join(line for line in (re.sub(r'\s+',' ',p).strip() for p in ''.join(self.parts).splitlines()) if line)


def page_projection(value,focus=''):
    parser=PageText();html=value.get('html','')
    if html:parser.feed(html);text=parser.text()
    else:text=str(value.get('text') or value.get('content') or '')
    paragraphs=[p.strip() for p in text.splitlines() if p.strip()]
    # Ranking is deterministic keyword overlap, not semantic retrieval.
    terms=re.findall(r'[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,8}',focus)[:30]
    ranked=sorted(enumerate(paragraphs),key=lambda row:(-sum(t.lower() in row[1].lower() for t in terms),row[0]))
    indices=sorted(i for i,_ in ranked[:6]);snippets=[]
    for i in indices:snippets.append(head_tail(paragraphs[i],1000))
    result={'title':str(value.get('title') or ''.join(parser.title))[:500],'url':str(value.get('url',''))[:2000],
        'snippets':snippets,'selection':'keyword_overlap_then_document_order','html_cleaned':bool(html),
        'truncated':bool(html) or len(paragraphs)>6 or any(len(p)>1000 for p in paragraphs)}
    for key in ('status','action_performed','requested_action','message','recovery','snapshot','backend'):
        if key in value:result[key]=value[key]
    # Interactive browser controls are operational metadata, not search snippets.
    if 'elements' in value:
        result['elements']=value['elements']
        for key in ('width','height'):result[key]=value.get(key)
    return result


def serialize(value):
    return value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,default=lambda x:x.model_dump(mode='json') if hasattr(x,'model_dump') else str(x))


class ToolResultProcessor:
    def __init__(self,store=None,focus=''):
        self.store=store;self.focus=focus
        if store:
            with closing(store.connect()) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS tool_originals (
                    message_id TEXT PRIMARY KEY,session_id TEXT,task_id TEXT,turn_id TEXT,tool_call_id TEXT,
                    tool_name TEXT,raw_content TEXT, UNIQUE(session_id,task_id,turn_id,tool_call_id))''');db.commit()

    def process(self,name,arguments,result,call_id):
        raw=serialize(result);message_id='message_'+uuid.uuid4().hex
        if self.store:
            task=getattr(self.store,'task_id',None)
            with closing(self.store.connect()) as db:
                owner=db.execute('SELECT current_turn_id FROM task_runs WHERE task_id=?',(task,)).fetchone()
                turn=owner[0] if owner else None
                previous=db.execute('SELECT message_id FROM tool_originals WHERE session_id=? AND task_id IS ? AND turn_id IS ? AND tool_call_id=?',(self.store.session_id,task,turn,call_id)).fetchone()
                if previous:message_id=previous[0]
                else:db.execute('INSERT INTO tool_originals VALUES (?,?,?,?,?,?,?)',(message_id,self.store.session_id,task,turn,call_id,name,raw))
                db.commit()
        # Images stay typed; accompanying text shares the same character budget.
        if isinstance(result,list) and any(getattr(x,'type',None)=='image' or type(x).__name__=='ToolOutputImage' for x in result):
            from agents import ToolOutputText
            texts=[x.text for x in result if isinstance(x,ToolOutputText)]
            joined='\n'.join(texts)
            if len(joined)>MAX_CHARS:
                prefix=f'完整原文ID：{message_id}\n' if self.store else ''
                return [x for x in result if not isinstance(x,ToolOutputText)]+[ToolOutputText(text=prefix+head_tail(joined,MAX_CHARS-len(prefix)))]
            if self.store:
                return result+[ToolOutputText(text=f'图片原文记录ID：{message_id}；原图保留，模型上下文可能省略历史图片。')]
            return result
        try:value=json.loads(raw)
        except (ValueError,TypeError):value=None
        web=isinstance(value,dict) and ('html' in value or name in ('browser_action','read_browser') and ('text' in value or 'elements' in value))
        context_text=raw
        if web:
            value=page_projection(value,self.focus)
            value['source_message_id']=message_id if self.store else None
            context_text=json.dumps(value,ensure_ascii=False)
        # One final head/tail rule applies to every text result, including pages.
        if len(context_text)<=MAX_CHARS:return context_text if web else result
        # A valid envelope lets consumers distinguish omission from tool failure.
        envelope={'truncated':True,'original_chars':len(raw),'context_chars_before_truncation':len(context_text),'source_message_id':message_id if self.store else None,'content':''}
        if isinstance(value,dict):
            for key in ('_muse_tool_error','exit_code','timed_out','is_error','isError','status','error_type','message','found','message_id','next_offset','offset','total_chars','tool_call_id','action_performed','requested_action','recovery'):
                if key in value and isinstance(value[key],(str,int,float,bool,type(None))):
                    envelope[key]=head_tail(value[key],500) if isinstance(value[key],str) else value[key]
        # JSON escaping can expand the text; bound the entire model-visible return.
        budget=MAX_CHARS-len(json.dumps(envelope,ensure_ascii=False))-100
        while True:
            envelope['content']=head_tail(context_text,max(100,budget))
            rendered=json.dumps(envelope,ensure_ascii=False)
            if len(rendered)<=MAX_CHARS:return rendered
            budget-=max(100,len(rendered)-MAX_CHARS)
