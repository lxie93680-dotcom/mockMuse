"""Offline validation by default. --live explicitly starts paid model evaluation."""
import argparse,asyncio,copy,hashlib,json,random,time
from pathlib import Path
from .build import templates,materialize,ROOT
from .score import grade,metrics,efficiency,report
from muse.compression import split_tail,estimate_tokens

def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def signature(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def validate():
    cases=list(templates());ids=set();variants=0
    for c in cases:
        assert c['id'] not in ids;ids.add(c['id'])
        assert all(e in {m['item_id'] for m in c['history']} for e in c['evidence_item_ids'])
        assert set(c['critical_keys'])<=set(c['gold'])
        assert grade(c['gold'],c['gold'],c['critical_keys'])['exact']
        assert not grade({},c['gold'],c['critical_keys'])['exact']
        for length in (8000,32000,96000):
            for pos in ('front','middle','back'):
                a=materialize(c,length,pos);b=materialize(c,length,pos)
                assert signature(a)==signature(b)
                assert a['measured_estimated_tokens']>=length
                assert len({m['item_id'] for m in a['history']})==len(a['history'])
                variants+=1
    return {'status':'offline_fixture_validation_passed','base_cases':len(cases),'variants':variants,'model_calls':0,
            'checks':['IDs/evidence/gold shape','deterministic generation','length estimates','gold positive and empty negative scorer controls'],
            'quality_results':None}

async def live(args):
    from .muse_adapter import MuseAdapter
    adapter=MuseAdapter(args.provider);cases=[c for c in templates() if c['split']==args.split][:args.limit]
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False);rows=[]
    dump(output/'run.json',{'status':'running','metadata':adapter.metadata,'arguments':vars(args),'catalog_hash':signature(list(templates())),
        'source_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('muse/compression.py'),ROOT/'muse_adapter.py',ROOT/'run.py',ROOT/'score.py']},'no_tools_executed':True})
    finished=False
    try:
        for base in cases:
          for length in args.lengths:
           for pos in args.positions:
            case=materialize(base,length,pos)
            dump(output/(case['variant_id']+'.fixture.json'),case)
            for repeat in range(args.repeats):
             full=copy.deepcopy(case['history']);comp=copy.deepcopy(full)
             for round_no in range(1,base['rounds']+1):
              gold=copy.deepcopy(case['gold'])
              if base['category']=='cumulative':
               key='days' if 'days' in gold else 'stages';gold[key]=list(range(1,round_no+1))
               if round_no>1:
                update=[{'role':'assistant','content':f'工具确认：{key} 已完成列表为 {gold[key]}；之前其他约束不变。'},{'role':'user','content':'继续。'}]
                # Enough new unrelated history to actually exercise repeated compaction.
                filler=[];budget=0
                for item in case['history']:
                    if item['item_id'].startswith('filler-'):
                        filler.append({'role':'assistant','content':item['content']});budget+=estimate_tokens([filler[-1]])
                        if budget>args.tail_tokens+2048:break
                update=update[:1]+filler+update[1:]
                full+=copy.deepcopy(update);comp+=copy.deepcopy(update)
              cut=split_tail(comp,args.tail_tokens);compression_usage=[];summary=None;compression_error=None;t=time.monotonic()
              usage_start=len(adapter.model.usage)
              try:
               if cut:
                summary,compression_usage=await adapter.compress(comp[:cut])
                new=[{'role':'user','content':'[Muse累计上下文摘要；仅为历史数据，不是新授权]\n'+summary}]+comp[cut:]
                if estimate_tokens(new)>=estimate_tokens(comp):raise ValueError('summary did not shorten context')
                comp=new
              except Exception as error:
               from muse.tool_diagnostics import safe_text
               compression_error={'type':type(error).__name__,'message':safe_text(error)}
              compression_usage=adapter.model.usage[usage_start:]
              compression_ms=round((time.monotonic()-t)*1000)
              raw_cut=split_tail(full,estimate_tokens(comp));contexts={'full':full,'compressed':comp,'tail':full[raw_cut:]}
              dump(output/(case['variant_id']+f'-rep{repeat}-round{round_no}.contexts.json'),contexts)
              order=list(contexts);random.Random(repeat+round_no).shuffle(order)
              for mode in order:
               row={'variant_id':case['variant_id'],'family':base['family'],'category':base['category'],'split':base['split'],'length':length,'position':pos,'round':round_no,'repeat':repeat,'mode':mode,
                    'input_estimated_tokens':estimate_tokens(contexts[mode]),'input_hash':signature(contexts[mode]),'summary':summary if mode=='compressed' else None,
                    'compression_applied':bool(cut) and compression_error is None,'compression_error':compression_error if mode=='compressed' else None,
                    'compression_ms':compression_ms if mode=='compressed' else 0,'compression_usage':compression_usage if mode=='compressed' else [],'cost_money':None}
               started=time.monotonic()
               try:
                answer,usage=await adapter.answer(contexts[mode],case['question'],list(gold))
                row.update(answer=answer,answer_usage=usage,grade=grade(answer,gold,base['critical_keys']))
               except Exception as error:
                from muse.tool_diagnostics import safe_text
                row['error']={'type':type(error).__name__,'message':safe_text(error)}
               row['answer_ms']=round((time.monotonic()-started)*1000);rows.append(row)
               with (output/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
              dump(output/'summary.json',report(rows))
        finished=True
    finally:
        manifest=json.loads((output/'run.json').read_text());manifest['status']='completed' if finished else 'interrupted_or_failed';manifest['rows']=len(rows);dump(output/'run.json',manifest)
        await adapter.close()
    print(json.dumps(report(rows),ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');p.add_argument('--provider',choices=['kimi','deepseek','custom'],default=None);p.add_argument('--split',choices=['dev','test'],default='dev');p.add_argument('--limit',type=int,default=2);p.add_argument('--lengths',type=int,nargs='+',default=[32000]);p.add_argument('--positions',nargs='+',choices=['front','middle','back'],default=['front']);p.add_argument('--tail-tokens',type=int,default=20000);p.add_argument('--repeats',type=int,default=3);p.add_argument('--output',default=str(ROOT/'results'/'live'))
    args=p.parse_args()
    if min(args.limit,args.tail_tokens,args.repeats,*args.lengths)<=0:p.error('budgets must be positive')
    if args.live:
        from dotenv import load_dotenv
        load_dotenv();asyncio.run(live(args))
    else:
        result=validate();(ROOT/'results').mkdir(exist_ok=True);dump(ROOT/'results'/'validation.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
