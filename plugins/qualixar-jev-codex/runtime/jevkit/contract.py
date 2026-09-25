"""Strict adapter for the documented TypeSafe v1 subset used by this kit."""
from __future__ import annotations
import math
from typing import Any
from .security import SafeError, canonical
from .score import score_matches_rounded_probabilities

def num(x: Any, low: float, high: float) -> bool:
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and low<=x<=high

def validate_questions(qs: dict) -> None:
    if not isinstance(qs,dict) or not 1<=len(qs)<=60: raise SafeError('INVALID_QUESTIONS')
    for key,q in qs.items():
        if not isinstance(key,str) or not isinstance(q,dict): raise SafeError('INVALID_QUESTION')
        if q.get('type') not in ('choice','score','noul'): raise SafeError('INVALID_QUESTION_TYPE')
        if not isinstance(q.get('instructions'),str) or not q['instructions'].strip(): raise SafeError('MISSING_INSTRUCTIONS')
        if q['type']=='choice':
            if not isinstance(q.get('criteria'),dict) or not 2<=len(q['criteria'])<=255: raise SafeError('INVALID_CHOICE_CRITERIA')
        if q['type']=='score':
            if not isinstance(q.get('criteria'),list) or not 2<=len(q['criteria'])<=10: raise SafeError('INVALID_SCORE_CRITERIA')
    if len(canonical(qs))>16_000: raise SafeError('QUESTION_BUDGET_EXCEEDED')

def validate_response_model(raw: Any, requested_model: str) -> dict:
    """Reject aliases, suffixes, and provider fallbacks before parsing answers."""
    if not isinstance(raw,dict) or raw.get('model') != requested_model:
        raise SafeError('MODEL_ID_MISMATCH')
    return raw

def validate_response(raw: Any, questions: dict) -> dict:
    if not isinstance(raw,dict) or not isinstance(raw.get('model'),str): raise SafeError('API_SCHEMA_MISMATCH: model')
    answers=raw.get('answers')
    if not isinstance(answers,dict) or set(answers)!=set(questions): raise SafeError('API_SCHEMA_MISMATCH: answer IDs')
    normalized={}
    for key,q in questions.items():
        a=answers[key]
        if not isinstance(a,dict) or a.get('type')!=q['type']: raise SafeError('API_SCHEMA_MISMATCH: type')
        if q['type']=='noul':
            if not num(a.get('noul'),0,1): raise SafeError('API_SCHEMA_MISMATCH: noul')
            # No fabricated confidence field. Noul is P(yes).
            normalized[key]={'type':'noul','noul':a['noul']};continue
        probs=a.get('probabilities')
        keys=set(q['criteria']) if q['type']=='choice' else {str(i) for i in range(len(q['criteria']))}
        if not isinstance(probs,dict) or set(probs)!=keys: raise SafeError('API_SCHEMA_MISMATCH: probabilities')
        if not all(num(v,0,1) for v in probs.values()) or abs(sum(probs.values())-1)>0.001:
            raise SafeError('API_SCHEMA_MISMATCH: probability normalization')
        if not num(a.get('confidence'),0,1): raise SafeError('API_SCHEMA_MISMATCH: confidence')
        if q['type']=='choice':
            choice=a.get('choice')
            if choice not in keys or probs[choice]<max(probs.values())-0.000001: raise SafeError('API_SCHEMA_MISMATCH: choice')
            normalized[key]={'type':'choice','choice':choice,'confidence':a['confidence'],'probabilities':probs}
        else:
            if not num(a.get('score'),0,len(keys)-1): raise SafeError('API_SCHEMA_MISMATCH: score')
            if not score_matches_rounded_probabilities(a['score'],probs): raise SafeError('API_SCHEMA_MISMATCH: weighted score')
            legend=a.get('legend')
            if not isinstance(legend,dict) or set(legend)!=keys: raise SafeError('API_SCHEMA_MISMATCH: legend')
            # Do not expose untrusted vendor prose; the locally reviewed rubric is canonical.
            normalized[key]={'type':'score','score':a['score'],'confidence':a['confidence'],
                'probabilities':probs,'legend':{str(i):v for i,v in enumerate(q['criteria'])}}
    usage=raw.get('usage')
    if not isinstance(usage,dict): raise SafeError('API_SCHEMA_MISMATCH: usage')
    out_usage={}
    for k in ('input_tokens','output_tokens'):
        x=usage.get(k)
        if not isinstance(x,int) or isinstance(x,bool) or x<0: raise SafeError('API_SCHEMA_MISMATCH: token usage')
        out_usage[k]=x
    return {'model':raw['model'],'answers':normalized,'usage':out_usage}
