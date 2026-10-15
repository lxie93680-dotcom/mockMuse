"""Exact structured QA grading; no substring, no self-grading, no LLM judge."""
import json

def grade(answer,gold,critical):
    try:
        value=json.loads(answer) if isinstance(answer,str) else answer
        if not isinstance(value,dict):raise ValueError()
    except (ValueError,TypeError):return {'exact':False,'field_hits':0,'field_total':len(gold),'critical_failures':list(critical),'invalid_json':True}
    hits={k:k in value and type(value[k]) is type(v) and json.dumps(value[k],sort_keys=True,ensure_ascii=False)==json.dumps(v,sort_keys=True,ensure_ascii=False) for k,v in gold.items()}
    return {'exact':all(hits.values()) and set(value)==set(gold),'field_hits':sum(hits.values()),'field_total':len(gold),
            'critical_failures':[k for k in critical if not hits[k]],'invalid_json':False}

def metrics(rows):
    out={}
    for mode in ('full','compressed','tail'):
        rs=[r for r in rows if r['mode']==mode];valid=[r for r in rs if 'grade' in r]
        out[mode]={'attempts':len(rs),'valid':len(valid),'errors':len(rs)-len(valid),
            'strict_accuracy':sum(r['grade']['exact'] for r in valid)/len(rs) if rs else None,
            'field_accuracy':sum(r['grade']['field_hits'] for r in valid)/sum(r['grade']['field_total'] for r in valid) if valid else None,
            'critical_failure_runs':sum(bool(r['grade']['critical_failures']) for r in valid)}
    # Only complete matched triples. Also report missing pairs; never silently drop errors.
    paired={}
    for r in rows:paired.setdefault((r['variant_id'],r['round'],r['repeat']),{})[r['mode']]=r
    pairs=[v for v in paired.values() if all(m in v and 'grade' in v[m] for m in ('full','compressed','tail'))]
    out['paired']={'complete':len(pairs),'incomplete':len(paired)-len(pairs),
        'accuracy_drop_pp':100*sum(int(p['full']['grade']['exact'])-int(p['compressed']['grade']['exact']) for p in pairs)/len(pairs) if pairs else None,
        'lost_after_full_correct':sum(p['full']['grade']['exact'] and not p['compressed']['grade']['exact'] for p in pairs),
        'full_correct':sum(p['full']['grade']['exact'] for p in pairs)}
    return out

def efficiency(rows):
    def total(usages,key):
        if not usages or any(u is None or u.get(key) is None for u in usages):return None
        return sum(u[key] for u in usages)
    def percentile(values,q):
        if not values:return None
        return sorted(values)[round((len(values)-1)*q)]
    groups={}
    for row in rows:groups.setdefault((row['variant_id'],row['round'],row['repeat']),{})[row['mode']]=row
    ratios=[];net=[];elapsed=[];applied=0;failed=0;unknown=0
    for g in groups.values():
        if not all(m in g for m in ('full','compressed')):continue
        f,c=g['full'],g['compressed'];applied+=int(c['compression_applied']);failed+=int(bool(c.get('compression_error')))
        before=total(f.get('answer_usage',[]),'input_tokens');after=total(c.get('answer_usage',[]),'input_tokens')
        if before and after is not None:ratios.append(1-after/before)
        else:unknown+=1
        a=total(f.get('answer_usage',[]),'total_tokens');b=total(c.get('answer_usage',[]),'total_tokens')
        overhead=total(c.get('compression_usage',[]),'total_tokens') if c['compression_applied'] or c.get('compression_error') else 0
        if a and b is not None and overhead is not None:net.append(1-(b+overhead)/a)
        if c['compression_applied']:elapsed.append(c['compression_ms'])
    return {'compression_applied':applied,'compression_failed_fallback':failed,
        'request_input_reduction_mean_actual':sum(ratios)/len(ratios) if ratios else None,
        'one_probe_net_total_token_reduction_mean_actual':sum(net)/len(net) if net else None,
        'actual_usage_missing_pairs':unknown,'compression_ms_p50':percentile(elapsed,.5),'compression_ms_p95':percentile(elapsed,.95),
        'monetary_saving':None,'note':'Actual normalized provider usage only; input+output token savings are not money savings; one-probe horizon excludes extra future task calls.'}

def report(rows):
    import random
    quality=metrics(rows)
    compressed=[r for r in rows if r['mode']=='compressed']
    applied=[r for r in compressed if r.get('compression_applied')]
    quality['compression_effect_scope']={'attempts':len(compressed),'applied':len(applied),'skipped_or_fallback':len(compressed)-len(applied),'applied_strict_accuracy':sum(r.get('grade',{}).get('exact',False) for r in applied)/len(applied) if applied else None}
    quality['by_category']={c:metrics([r for r in rows if r['category']==c]) for c in sorted({r['category'] for r in rows})}
    quality['macro_category_accuracy']={m:sum(x[m]['strict_accuracy'] for x in quality['by_category'].values())/len(quality['by_category']) if quality['by_category'] else None for m in ('full','compressed','tail')}
    pairs={}
    for r in rows:pairs.setdefault((r['variant_id'],r['round'],r['repeat']),{})[r['mode']]=r
    family={}
    for p in pairs.values():
        if all(m in p and 'grade' in p[m] for m in ('full','compressed')):
            family.setdefault(p['full']['family'],[]).append(int(p['full']['grade']['exact'])-int(p['compressed']['grade']['exact']))
    values=[sum(v)/len(v)*100 for v in family.values()]
    rng=random.Random(20261015);boot=sorted(sum(rng.choices(values,k=len(values)))/len(values) for _ in range(2000)) if len(values)>1 else []
    quality['family_clustered_drop']={'families':len(values),'mean_pp':sum(values)/len(values) if values else None,'bootstrap_95ci_pp':[boot[49],boot[1949]] if boot else None,'note':'Synthetic related families remain limited; no generalization claim.'}
    return {'quality':quality,'efficiency':efficiency(rows)}
