"""Matched accepted-task accounting. Characters and subscription quota are not tokens."""
from __future__ import annotations
from .common import AutoError,number

def validate_run(r):
    if not isinstance(r,dict):raise AutoError('RUN_OBJECT')
    for k in ('task_id','task_fingerprint','host','model','effort'):
        if not isinstance(r.get(k),str) or not r[k]:raise AutoError('RUN_IDENTITY')
    if not isinstance(r.get('accepted'),bool):raise AutoError('RUN_ACCEPTANCE')
    for k in ('input_tokens','cached_input_tokens','output_tokens'):
        v=r.get(k)
        if v is not None and (not isinstance(v,int) or isinstance(v,bool) or v<0):raise AutoError('RUN_TOKENS')
    if r.get('input_tokens') is not None and r.get('cached_input_tokens') is not None and r['cached_input_tokens']>r['input_tokens']:raise AutoError('CACHE_SUBSET')
    for k in ('elapsed_seconds','total_cost_usd'):
        if r.get(k) is not None and not number(r[k],0,10**12):raise AutoError('RUN_METRIC')
    return r

def compare(a,b):
    validate_run(a);validate_run(b)
    for k in ('task_id','task_fingerprint','host','model','effort'):
        if a[k]!=b[k]:raise AutoError('UNMATCHED_RUNS')
    eligible=a['accepted'] and b['accepted']
    def total(r):
        return r['input_tokens']+r['output_tokens'] if r.get('input_tokens') is not None and r.get('output_tokens') is not None else None
    x,y=total(a),total(b)
    def pct(x,y):return (x-y)/x*100 if eligible and x is not None and y is not None and x>0 else None
    elapsed_a,elapsed_b=a.get('elapsed_seconds'),b.get('elapsed_seconds')
    return {'task_id':a['task_id'],'accepted_both':eligible,'host_token_reduction_percent':pct(x,y),
            'host_tokens_baseline':x,'host_tokens_treatment':y,
            'elapsed_reduction_percent':pct(elapsed_a,elapsed_b),
            'end_to_end_speedup':elapsed_a/elapsed_b if eligible and elapsed_a is not None and elapsed_b and elapsed_b>0 else None,
            'total_cost_reduction_percent':pct(a.get('total_cost_usd'),b.get('total_cost_usd')),
            'subscription_quota_saving_percent':None,
            'note':'Costs must include decision-provider and explicitly estimated local costs. A negative reduction is preserved. No inference from text length.'}

def extract_codex_usage(records,multiple='reject'):
    usages=[r.get('usage') for r in records if isinstance(r,dict) and r.get('type')=='turn.completed' and isinstance(r.get('usage'),dict)]
    if not usages:raise AutoError('NO_HOST_USAGE')
    if len(usages)>1 and multiple not in ('per-turn','last-cumulative'):raise AutoError('DECLARE_USAGE_AGGREGATION')
    if multiple=='last-cumulative':usages=usages[-1:]
    out={}
    for k in ('input_tokens','cached_input_tokens','output_tokens'):
        vals=[u.get(k) for u in usages]
        if any(v is None for v in vals):out[k]=None
        elif any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in vals):raise AutoError('INVALID_HOST_USAGE')
        else:out[k]=sum(vals)
    return out
