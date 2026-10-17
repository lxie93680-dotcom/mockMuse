"""Evaluation artifact index and versioned human relevance annotation API."""
import hashlib,json,os,sqlite3
from pathlib import Path
from fastapi import APIRouter,HTTPException
from .tool_diagnostics import sanitized
from .evaluation_reviews import RunReviews,ReviewConflict

EVAL_ROOT=Path(__file__).resolve().parents[1]/'evals'
router=APIRouter(prefix='/api/evals')

def read(path):
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(EVAL_ROOT.resolve()):raise ValueError('outside evaluation root')
        if path.stat().st_size>80_000_000:raise ValueError('artifact too large')
        return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()] if path.suffix=='.jsonl' else json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as e:raise HTTPException(422,'评测文件无法读取：'+path.name) from e

def key(path):return hashlib.sha256(str(path.relative_to(EVAL_ROOT)).encode()).hexdigest()[:20]
def suite_type(suite, protocol=None):
    value=(protocol or {}).get('suite_type')
    if value in ('test','evaluation'):return value
    return 'test' if suite in ('tool_lifecycle','compression') else 'evaluation'

def scan():
    suites=[];runs={}
    for directory,dirs,files in os.walk(EVAL_ROOT,followlinks=False):
        dirs[:]=[d for d in dirs if d not in ('node_modules','__pycache__','.git') and not (Path(directory)/d).is_symlink()]
        p=Path(directory)
        if 'cases.jsonl' in files:
            cases=read(p/'cases.jsonl');suite_id=str(p.relative_to(EVAL_ROOT));protocol=read(p/'protocol.json') if 'protocol.json' in files else {}
            suites.append({'id':suite_id,'suite_type':suite_type(suite_id,protocol),'title':protocol.get('title') or {'browser_grounding':'浏览器定位','muse_v1':'Muse Agent 综合','tool_lifecycle':'工具搜索与生命周期','compression/v1':'上下文压缩 v1'}.get(suite_id,suite_id),
                'case_count':len(cases),'status':protocol.get('status','已建立用例'),'path':suite_id+'/cases.jsonl'})
        if 'results' not in p.relative_to(EVAL_ROOT).parts:continue
        selected='results.jsonl' if 'results.jsonl' in files else 'results.json' if 'results.json' in files else None
        if selected:
            path=p/selected;runs[key(path)]=path
        else:
            for name in files:
                if name.endswith('.json') and name not in ('manifest.json','summary.json','mcp-tools.json'):
                    path=p/name;runs[key(path)]=path
    # Earlier compression regression designs remain visible, without claiming executed results.
    p=EVAL_ROOT/'compression'
    if p.exists():suites.append({'id':'compression','suite_type':'test','title':'压缩历史设计与回归','case_count':len(list(p.glob('*.case.json')))+int((p/'case.example.json').exists()),'status':'设计/回归样例，非正式成绩','path':'compression'})
    return suites,runs

def rows(path):
    data=read(path)
    if isinstance(data,list):return data
    if isinstance(data,dict) and isinstance(data.get('results'),list):return data['results']
    return []

def row_status(r):
    if r.get('error') or r.get('compression_error'):return 'error'
    if 'grade' in r:return 'pass' if r['grade'].get('exact') else 'fail'
    status=str(r.get('status','unknown'))
    return status if status in ('pass','fail','error','invalid') else 'unknown'

def run_info(id,path):
    rs=rows(path);counts={s:sum(row_status(r)==s for r in rs) for s in ('pass','fail','error','invalid','unknown')}
    rel=path.relative_to(EVAL_ROOT);parts=rel.parts;index=parts.index('results');suite='/'.join(parts[:index])
    protocol=EVAL_ROOT/suite/'protocol.json'
    classification=suite_type(suite,read(protocol) if protocol.exists() else {})
    summary=path.parent/'summary.json';meta=path.parent/'run.json'
    if not meta.exists():meta=path.parent/'manifest.json'
    grades=review_data(id,path,rs) if rs else []
    return {'initial_grading':{'scored':sum(g['initial_grade']['score'] is not None for g in grades),'pending':sum(g['initial_grade']['score'] is None for g in grades),'reviewed':sum(g['human_review'] is not None for g in grades)},'id':id,'suite':suite,'suite_type':classification,'name':'/'.join(parts[index+1:]),'modified':path.stat().st_mtime,'total':len(rs),'counts':counts,
        'kind':'运行结果' if rs else '验证/基础设施记录','summary':sanitized(read(summary)) if summary.exists() else sanitized(read(path)) if not rs else None,
        'metadata':sanitized(read(meta)) if meta.exists() else None,'artifact':str(rel)}

@router.get('/catalog')
def catalog():
    suites,runs=scan();infos=[];errors=[]
    for id,path in runs.items():
        try:infos.append(run_info(id,path))
        except HTTPException as e:errors.append({'artifact':str(path.relative_to(EVAL_ROOT)),'error':e.detail})
    return {'suites':suites,'runs':sorted(infos,key=lambda r:r['modified'],reverse=True),'errors':errors,'read_only':False,'annotation_suites':['tool_search']}

@router.get('/cases')
def cases(suite:str):
    suites,_=scan()
    if suite not in {s['id'] for s in suites}:raise HTTPException(404,'评测集不存在')
    p=EVAL_ROOT/suite/'cases.jsonl'
    data=read(p) if p.exists() else [read(p) for p in sorted((EVAL_ROOT/suite).glob('*.json')) if p.name.endswith('.case.json') or p.name=='case.example.json']
    return {'cases':sanitized(data),'display_limits':'文本与列表有展示长度限制；不改变源文件'}

def lookup(id):
    _,runs=scan()
    if id not in runs:raise HTTPException(404,'运行不存在')
    return runs[id]

@router.get('/runs/{id}')
def run_rows(id:str,offset:int=0,limit:int=100,status:str='all',q:str='',review:str='all'):
    if offset<0 or not 1<=limit<=200:raise HTTPException(400,'分页参数错误')
    if review not in ('all','unreviewed','reviewed','adjusted','pending'):raise HTTPException(400,'复核筛选无效')
    path=lookup(id);rs=rows(path);grades=review_data(id,path,rs);result=[]
    for index,r in enumerate(rs):
        state=row_status(r);grading=grades[index];human=grading['human_review']
        if review=='unreviewed' and human is not None:continue
        if review=='reviewed' and human is None:continue
        if review=='adjusted' and (human is None or human['verdict']!='adjust'):continue
        if review=='pending' and grading['initial_grade']['score'] is not None:continue
        if status!='all' and state!=status:continue
        if q and q.lower() not in json.dumps({k:v for k,v in r.items() if k not in ('trace','summary')},ensure_ascii=False).lower():continue
        result.append({**grading,'index':index,'case':r.get('case_id',r.get('variant_id','未命名')),'title':r.get('title',r.get('category','')),'status':state,
            'backend':r.get('backend',r.get('mode','')),'seed':r.get('seed',r.get('repeat')),'round':r.get('round'),
            'duration_ms':r.get('duration_ms',r.get('answer_ms')),'tool_calls':r.get('tool_calls'),'tool_errors':r.get('tool_errors')})
    return {'rows':sanitized(result[offset:offset+limit]),'total':len(result),'offset':offset,'limit':limit}

@router.get('/runs/{id}/cases/{index}')
def detail(id:str,index:int):
    path=lookup(id);rs=rows(path)
    if not 0<=index<len(rs):raise HTTPException(404,'case不存在')
    r=rs[index];trace=r.get('trace',[])
    if not trace and ('error' in r or 'compression_error' in r):
        trace=[{'operation':'compression','error':r.get('compression_error'),'usage':r.get('compression_usage')},
               {'operation':'reader','error':r.get('error'),'answer':r.get('answer'),'grade':r.get('grade'),'usage':r.get('answer_usage')}]
    grading=review_data(id,path,rs)[index]
    return {**sanitized(grading),'case':sanitized(r),'trace':sanitized(trace),'status':row_status(r),
        'trace_note':'展示已有记录，最多150个步骤、单文本截断；未记录的内部阶段无法回补。' if trace else '该记录没有逐步执行链路，不能据此推断根因。',
        'artifact':str(path.relative_to(EVAL_ROOT))}

# Human labels are stored separately; original cases and historical scores remain immutable.
from pydantic import BaseModel,Field,StrictBool,StrictInt,ConfigDict
from typing import Literal
from .search_annotations import Annotations,AnnotationConflict

class JudgmentBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    dataset_hash:str=Field(min_length=64,max_length=64)
    base_revision:StrictInt=Field(ge=0)
    status:Literal['draft','confirmed']
    relevant:list[str]=Field(max_length=1000)
    no_match:StrictBool=False
    note:str=Field(default='',max_length=4000)

def annotation_store():
    root=EVAL_ROOT/'tool_search'
    if root.is_symlink() or not root.resolve().is_relative_to(EVAL_ROOT.resolve()):raise HTTPException(422,'标注目录无效')
    if not (root/'cases.jsonl').exists():raise HTTPException(404,'搜索评测集不存在')
    return Annotations(root)

@router.get('/tool-search/annotations')
def search_annotations():
    try:return annotation_store().load()
    except (OSError,ValueError,sqlite3.Error) as error:raise HTTPException(422,'无法读取标注数据') from error

@router.post('/tool-search/annotations/{case_id}')
def save_search_annotation(case_id:str,body:JudgmentBody):
    try:return {'annotation':annotation_store().save(case_id,**body.model_dump())}
    except AnnotationConflict as error:raise HTTPException(409,str(error)) from error
    except ValueError as error:raise HTTPException(422,str(error)) from error
    except (OSError,sqlite3.Error) as error:raise HTTPException(422,'标注保存失败，原标注保留') from error


def review_data(id,path,rs):
    parts=path.relative_to(EVAL_ROOT).parts;suite='/'.join(parts[:parts.index('results')])
    try:return RunReviews(EVAL_ROOT).load(id,path,rs,suite)
    except ReviewConflict as error:raise HTTPException(409,str(error)) from error
    except (OSError,ValueError,sqlite3.Error) as error:raise HTTPException(422,'无法读取任务评分') from error

class RunReviewBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    artifact_hash:str=Field(min_length=64,max_length=64)
    grade_id:StrictInt=Field(gt=0)
    base_revision:StrictInt=Field(ge=0)
    verdict:Literal['agree','adjust','unscorable']
    score:float|None=Field(default=None,ge=0,le=100,allow_inf_nan=False)
    note:str=Field(default='',max_length=4000)

@router.post('/runs/{id}/cases/{index}/review')
def save_run_review(id:str,index:int,body:RunReviewBody):
    path=lookup(id);rs=rows(path)
    if not 0<=index<len(rs):raise HTTPException(404,'任务不存在')
    current=review_data(id,path,rs)[index]
    if body.artifact_hash!=current['artifact_hash'] or body.grade_id!=current['initial_grade']['grade_id']:
        raise HTTPException(409,'运行结果或评分规则已变化，请重新打开；旧复核未覆盖')
    try:return {'human_review':RunReviews(EVAL_ROOT).save(body.grade_id,base_revision=body.base_revision,
        verdict=body.verdict,score=body.score,note=body.note)}
    except ReviewConflict as error:raise HTTPException(409,str(error)) from error
    except ValueError as error:raise HTTPException(422,str(error)) from error
    except (OSError,sqlite3.Error) as error:raise HTTPException(422,'复核保存失败，原记录保留') from error
