"""Exact response usage and explicit provider/model-specific context policies."""
import hashlib
import json
import math
import os
from pathlib import Path


def model_policy(provider, model):
    path=Path(os.getenv('MUSE_MODEL_CONTEXT_CONFIG',str(Path(__file__).resolve().parents[1]/'config/model_contexts.json')))
    configs=json.loads(path.read_text(encoding='utf-8'))
    policy=configs.get(provider+':'+model,{})
    result={'context_window':0,'compression_threshold':.5,'keep_recent_tokens':20000,
            'fallback_utf8_bytes_per_token':2,'image_token_budget':4096,**policy}
    if os.getenv('MUSE_CONTEXT_WINDOW'):result['context_window']=int(os.environ['MUSE_CONTEXT_WINDOW'])
    if result['context_window']<0 or not 0<result['compression_threshold']<1 or result['keep_recent_tokens']<=0 or result['fallback_utf8_bytes_per_token']<=0:
        raise ValueError('模型上下文配置无效')
    return result


def estimate(value, policy):
    if isinstance(value,dict):
        if value.get('type') in ('input_image','image_url'):return policy['image_token_budget']
        return 4+sum(estimate(k,policy)+estimate(v,policy) for k,v in value.items())
    if isinstance(value,list):return sum(estimate(v,policy)+4 for v in value)
    return math.ceil(len(str(value).encode('utf-8'))/policy['fallback_utf8_bytes_per_token'])


def signature(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


class TokenTracker:
    def __init__(self,provider,model,policy,store=None):
        self.provider=provider;self.model=model;self.policy=policy;self.store=store
        self.latest=store.get_usage(provider,model) if store else None

    def observe(self,response,items,instructions,tools):
        usage=getattr(response,'usage',None)
        # Agents SDK normalizes the provider's prompt_tokens into input_tokens.
        actual=getattr(usage,'input_tokens',None)
        requests=getattr(usage,'requests',0)
        if not isinstance(actual,int) or actual<0 or not requests:return
        outgoing=estimate(items,self.policy)
        row={'provider':self.provider,'model':self.model,'input_tokens':actual,
             'output_tokens':getattr(usage,'output_tokens',0),'total_tokens':getattr(usage,'total_tokens',0),
             'input_estimate':outgoing,'input_signature':signature(items),
             'instructions_signature':signature(instructions),'tools_signature':signature(tools),
             'source':'provider_usage'}
        self.latest=row
        if self.store:self.store.save_usage(row)

    def pressure(self,items,instructions,tools):
        current=estimate(items,self.policy);previous=self.latest
        if previous and previous['instructions_signature']==signature(instructions) and previous['tools_signature']==signature(tools):
            if previous['input_signature']==signature(items):return previous['input_tokens'],previous['source']
            calibrated=max(0,previous['input_tokens']+current-previous['input_estimate'])
            # Actual usage exceeding threshold must trigger before the next call.
            source='provider_usage_plus_estimated_delta' if previous['source']=='provider_usage' else 'post_compression_estimate_plus_delta'
            return max(previous['input_tokens'],calibrated),source
        return current+estimate(instructions or '',self.policy)+estimate(tools,self.policy),'fallback_estimate'

    def after_compaction(self,items,instructions,tools):
        message_tokens=estimate(items,self.policy)
        total=message_tokens+estimate(instructions or '',self.policy)+estimate(tools,self.policy)
        row={'provider':self.provider,'model':self.model,'input_tokens':total,
             'output_tokens':None,'total_tokens':None,'input_estimate':message_tokens,
             'input_signature':signature(items),'instructions_signature':signature(instructions),
             'tools_signature':signature(tools),'source':'post_compression_estimate'}
        self.latest=row
        if self.store:self.store.save_usage(row)
        return total

    def reset(self):
        self.latest=None
        if self.store:self.store.clear_usage(self.provider,self.model)
