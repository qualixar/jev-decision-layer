"""Reject any input that the pinned MLX formatter would truncate; no model import needed."""
from __future__ import annotations
import json
from .common import AutoError

def options(q):
    def render(x):return x if isinstance(x,str) else json.dumps(x,ensure_ascii=False,separators=(', ', ': '))
    if q['type']=='choice':return [k if v in (None,'') else k+': '+render(v) for k,v in q['criteria'].items()]
    if q['type']=='score':return ['level %d: %s'%(i,render(v)) for i,v in enumerate(q['criteria'])]
    c=q.get('criteria') or {}
    return ['false: '+render(c.get('false') or 'no, the statement does not hold'),
            'true: '+render(c.get('true') or 'yes, the statement holds')]

def preflight(tok,cfg,state,qs):
    state=state if isinstance(state,str) else json.dumps(state,ensure_ascii=False)
    def enc(s):return tok(s.replace(tok.mask_token,' '),add_special_tokens=False)['input_ids']
    st=enc(state);max_len=cfg.get('max_len',512);head_max=cfg.get('head_max_len',192);counts={}
    for key,q in qs.items():
        head=enc(q['type']+' question: '+q['instructions'])
        opts=[enc(' '+v) for v in options(q)]
        if any(len(v)>48 for v in opts):raise AutoError('MLX_OPTION_WOULD_TRUNCATE')
        lengths=[1+len(v) for v in opts];budget=head_max-sum(lengths)
        if budget<16:
            per=max(4,(head_max-16)//max(1,len(opts)))
            if any(x>per for x in lengths):raise AutoError('MLX_RUBRIC_WOULD_TRUNCATE')
            budget=head_max-sum(lengths)
        if len(head)>max(8,budget):raise AutoError('MLX_INSTRUCTIONS_WOULD_TRUNCATE')
        total=1+len(head)+1+sum(lengths)+1+len(st)+1
        if total>max_len:raise AutoError('MLX_STATE_WOULD_TRUNCATE')
        counts[key]=total
    return counts
