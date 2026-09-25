"""Provider-neutral validation without inventing a common confidence meaning."""
from .common import AutoError, canonical, number
from jevkit.score import score_matches_rounded_probabilities

def validate_questions(qs):
    if not isinstance(qs, dict) or not 1 <= len(qs) <= 60: raise AutoError('QUESTION_COUNT')
    for key, q in qs.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not isinstance(q, dict): raise AutoError('QUESTION_SHAPE')
        if q.get('type') not in ('noul', 'choice', 'score'): raise AutoError('QUESTION_TYPE')
        if not isinstance(q.get('instructions'), str) or not q['instructions'].strip(): raise AutoError('QUESTION_INSTRUCTIONS')
        c = q.get('criteria')
        if q['type'] == 'choice' and (not isinstance(c, dict) or not 2 <= len(c) <= 64 or not all(isinstance(k,str) and k for k in c)): raise AutoError('CHOICE_OPTIONS')
        if q['type'] == 'score' and (not isinstance(c, list) or not 2 <= len(c) <= 10): raise AutoError('SCORE_LEVELS')
    if len(canonical(qs)) > 20_000: raise AutoError('QUESTION_BYTES')

def validate_response(raw, questions, expected_model=None):
    validate_questions(questions)
    if not isinstance(raw,dict) or not isinstance(raw.get('model'),str): raise AutoError('RESPONSE_MODEL')
    if expected_model and raw['model'] != expected_model: raise AutoError('MODEL_MISMATCH')
    answers = raw.get('answers')
    if not isinstance(answers,dict) or set(answers) != set(questions): raise AutoError('ANSWER_IDS')
    out={}
    for key,q in questions.items():
        a=answers[key]
        if not isinstance(a,dict) or a.get('type') != q['type']: raise AutoError('ANSWER_TYPE')
        if q['type']=='noul':
            if not number(a.get('noul')): raise AutoError('NOUL_RANGE')
            out[key]={'type':'noul','noul':a['noul']}
            # Laya supplies confidence/action; TypeSafe Noul does not. Keep extras only locally.
            continue
        p=a.get('probabilities'); labels=set(q['criteria']) if q['type']=='choice' else {str(i) for i in range(len(q['criteria']))}
        if not isinstance(p,dict) or set(p)!=labels or not all(number(v) for v in p.values()): raise AutoError('PROBABILITIES')
        # Four-decimal Laya rounding: tolerance grows with option count, bounded here.
        if abs(sum(p.values())-1) > max(.001, len(labels)*.000051): raise AutoError('PROBABILITY_SUM')
        if not number(a.get('confidence')): raise AutoError('CONFIDENCE_RANGE')
        if q['type']=='choice':
            v=a.get('choice')
            if v not in p or p[v] < max(p.values())-.00011: raise AutoError('CHOICE_ARGMAX')
            out[key]={'type':'choice','choice':v,'confidence':a['confidence'],'probabilities':p}
        else:
            v=a.get('score')
            if not number(v,0,len(labels)-1):raise AutoError('SCORE_EXPECTATION')
            if raw['model'] in ('jev-1.13.0','typesafe/jev-1.13'):
                consistent=score_matches_rounded_probabilities(v,p)
            else:
                consistent=abs(v-sum(int(k)*x for k,x in p.items()))<=.01+1e-9
            if not consistent:raise AutoError('SCORE_EXPECTATION')
            out[key]={'type':'score','score':v,'confidence':a['confidence'],'probabilities':p,
                      'legend':{str(i):x for i,x in enumerate(q['criteria'])}}
    usage=raw.get('usage') or {}
    if not isinstance(usage,dict): raise AutoError('USAGE_SHAPE')
    u={}
    for name in ('input_tokens','output_tokens'):
        v=usage.get(name)
        if v is not None and (not isinstance(v,int) or isinstance(v,bool) or v<0): raise AutoError('USAGE_VALUE')
        u[name]=v
    return {'model':raw['model'],'answers':out,'usage':u}

def compact_receipt(record):
    """Project only an allowlist. Detailed source/question text remains local."""
    policy = record.get('policy',{})
    out={'version':'1.0.0','case_id':record.get('case_id'),
         'status':policy.get('status','REVIEW'),'recommendation':policy.get('recommendation'),
         'execution_authorized':False,'receipt_id':record.get('record_sha256'),
         'mode':record.get('mode'),'cache_hit':record.get('cache_hit',False)}
    if 'items' in policy: out['items']=policy['items']
    if policy.get('reasons'): out['reason_codes']=policy['reasons'][:3]
    if len(canonical(out)) > 5000:
        out.pop('items',None); out['detail_available']=True
        if len(canonical(out))>5000:
            out.pop('reason_codes',None);out['recommendation']=None
    return out
