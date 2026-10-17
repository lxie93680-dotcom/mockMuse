"""Offline relevance benchmark for the actual tool search path, without query rewriting."""
import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from muse.tool_access import ToolAccess
from muse.tool_registry import ToolRegistry
from muse.search_annotations import Annotations

ROOT=Path(__file__).parent
REPO=Path(__file__).resolve().parents[2]

def grade(retrieved,relevant,k=5):
    expected=set(relevant);top=retrieved[:k]
    if not expected:
        return {'no_match_correct':not top}
    hits=[i+1 for i,name in enumerate(top) if name in expected]
    return {'hit_at_1':bool(hits and hits[0]<=1),'hit_at_3':bool(hits and hits[0]<=3),
            'hit_at_5':bool(hits),'recall_at_5':len(set(top)&expected)/len(expected),
            'precision_at_5':len(set(top)&expected)/len(top) if top else 0,
            'mrr_at_5':1/hits[0] if hits else 0}

def aggregate(rows):
    positive=[r for r in rows if r['relevant']];negative=[r for r in rows if not r['relevant']]
    metrics={k:sum(r['metrics'][k] for r in positive)/len(positive) if positive else None
             for k in ('hit_at_1','hit_at_3','hit_at_5','recall_at_5','precision_at_5','mrr_at_5')}
    metrics.update(positive_queries=len(positive),negative_queries=len(negative),
                   no_match_correct_rate=sum(r['metrics']['no_match_correct'] for r in negative)/len(negative) if negative else None)
    return metrics

async def evaluate(output,labels="generated",allow_partial=False):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    cases=[json.loads(s) for s in (ROOT/'cases.jsonl').read_text().splitlines() if s.strip()]
    catalog=json.loads((ROOT/'catalog.json').read_text());names=[r['name'] for r in catalog]
    if len(set(names))!=len(names):raise ValueError('Benchmark requires unambiguous unique fixture names')
    for case in cases:
        if not set(case['relevant'])<=set(names):raise ValueError('Unknown relevance label')
    label_snapshot=None
    if labels not in ('generated','human'):raise ValueError('Unknown label source')
    if labels=='human':
        state=Annotations(ROOT).load()
        cases=state['cases'];catalog=state['tools']
        confirmed={k:v for k,v in state['annotations'].items() if v['status']=='confirmed'}
        if len(confirmed)!=len(cases) and not allow_partial:raise ValueError('人工标注尚未全部确认；可以使用--allow-partial仅评测已确认部分')
        cases=[{**c,'relevant':confirmed[c['id']]['relevant']} for c in cases if c['id'] in confirmed]
        if not cases:raise ValueError('没有已确认的人工标注')
        label_snapshot={'dataset_hash':state['dataset_hash'],'annotations':confirmed,'total_dataset_cases':state['total']}
    output.mkdir(parents=True,exist_ok=False)
    rows=[]
    with tempfile.TemporaryDirectory(prefix='muse-search-benchmark-') as temp:
        registry=ToolRegistry(Path(temp)/'muse.db')
        try:
            registry.store.upsert_many([dict(source_id=r['source_id'],source_kind=r['source_id'].split(':')[0],
                tool_key=r['name'],name=r['name'],description=r['description'],parameters=r['parameters']) for r in catalog])
            access=ToolAccess(registry)
            for case in cases:
                start=time.perf_counter();result=access.search(case['query'],5)
                retrieved=[r['name'] for r in result];metrics=grade(retrieved,case['relevant'])
                row={**case,'case_id':case['id'],'duration_ms':round((time.perf_counter()-start)*1000,3),
                     'retrieved':retrieved,'metrics':metrics,'status':'pass' if metrics.get('hit_at_5',metrics.get('no_match_correct',False)) else 'fail',
                     'trace':[{'operation':'tool_search','query':case['query'],'limit':5,'result':result}]}
                rows.append(row)
        finally:await registry.close()
    summary={'metrics':aggregate(rows),'by_category':{cat:aggregate([r for r in rows if r['category']==cat]) for cat in sorted({r['category'] for r in rows})},
        'total':len(rows),'scoring':'pass=相关查询Top5至少命中一个标注工具；无匹配查询返回空。主要比较排名/召回指标，不与系统测试通过率合并。',
        'precision_denominator':'实际返回候选数，最多5；不是固定除以5',
        'scope':'固定快照及人工二元标注；直接原始查询，无模型改写；非Agent端到端，浏览器不执行。',
        'temporary_namespace_removed':not Path(temp).exists(),'product_state_modified':False}
    (output/'results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    summary.update(label_source=labels,partial=bool(label_snapshot and len(rows)<label_snapshot['total_dataset_cases']))
    if label_snapshot:(output/'labels.json').write_text(json.dumps(label_snapshot,ensure_ascii=False,indent=2)+'\n')
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    sources={f'evals/tool_search/{name}':ROOT/name for name in ('cases.jsonl','catalog.json','protocol.json')}
    sources.update({str(p.relative_to(REPO)):p for p in [Path(__file__).resolve(),REPO/'muse/tool_access.py',REPO/'muse/tool_store.py',REPO/'muse/tool_enable_cache.py',REPO/'muse/search_annotations.py']})
    (output/'run.json').write_text(json.dumps({'suite_type':'evaluation','status':'completed','mode':'raw_query_retrieval','model_requests':0,'catalog_tools':len(catalog),'k':5,'label_source':labels,
        'label_sha256':hashlib.sha256(json.dumps(label_snapshot,sort_keys=True).encode()).hexdigest() if label_snapshot else None,
        'source_sha256':{name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in sources.items()}},ensure_ascii=False,indent=2)+'\n')
    return summary

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'results'/datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    parser.add_argument('--labels',choices=['generated','human'],default='generated');parser.add_argument('--allow-partial',action='store_true')
    args=parser.parse_args();print(json.dumps(asyncio.run(evaluate(args.output,args.labels,args.allow_partial)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
