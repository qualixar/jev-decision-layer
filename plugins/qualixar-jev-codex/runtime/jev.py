#!/usr/bin/env python3
"""Qualixar Jev Decision Layer command line. Python 3.11+. No core third-party dependencies."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from jevkit.engine import ROOT
from jevkit.security import SafeError,load_json,canonical

def runtime_options(parser):
    parser.add_argument('--scope',choices=['global-offline','global-hybrid','project-live'],default='global-offline')
    parser.add_argument('--state-root','--runtime-state',dest='runtime_state',type=Path)
    parser.add_argument('--workspace',type=Path)

def runtime(args):
    from jevkit.mcp_server import context
    return context(ROOT,args.scope,args.runtime_state,args.workspace)

def main():
    p=argparse.ArgumentParser(description='Qualixar Jev Decision Layer. Offline until workspace setup.')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('catalog','health','mcp'):
        runtime_options(sub.add_parser(name))
    d=sub.add_parser('describe');d.add_argument('case_id');runtime_options(d)
    r=sub.add_parser('run');r.add_argument('case_id');r.add_argument('--variant',choices=['nominal','adversarial','uncertain'],default='nominal')
    r.add_argument('--mode',choices=['fixture','live'],default='fixture');r.add_argument('--state',type=Path)
    r.add_argument('--request-id')
    r.add_argument('--data-classification',choices=['public','internal-minimized'])
    runtime_options(r)
    s=sub.add_parser('suite');s.add_argument('--mode',choices=['fixture','live'],default='fixture');s.add_argument('--variant',choices=['all','nominal','adversarial','uncertain'],default='all');runtime_options(s)
    w=sub.add_parser('serve');w.add_argument('--port',type=int,default=8765)
    args=p.parse_args()
    if args.command=='mcp':
        from jevkit.mcp_server import serve
        serve(ROOT,args.scope,args.runtime_state,args.workspace);return 0
    if args.command=='serve':
        from jevkit.web_server import serve
        serve(args.port);return 0
    active=runtime(args)
    if args.command=='catalog':result=active.catalog()
    elif args.command=='health':
        from jevkit.mcp_server import health
        result=health(ctx=active)
    elif args.command=='describe':result=active.describe(args.case_id)
    elif args.command=='run':
        state=load_json(args.state) if args.state else None
        if args.mode=='live':
            result=active.evaluate(args.case_id,state,request_id=args.request_id,
                                   data_classification=args.data_classification,
                                   workspace_root=args.workspace if args.scope=='global-hybrid' else None)
        else:result=active.run_fixture(args.case_id,args.variant)
    else:
        rows=[];variants=['nominal','adversarial','uncertain'] if args.variant=='all' else [args.variant]
        for c in active.catalog():
            for v in variants:
                try:
                    result=active.evaluate(c['id'],workspace_root=args.workspace if args.scope=='global-hybrid' else None) if args.mode=='live' else active.run_fixture(c['id'],v)
                    rows.append({'case_id':c['id'],'variant':v,'run_id':result['run_id'],'status':result['policy']['status'],
                      'fixture_contract_passed':result['fixture_contract_passed']})
                except SafeError as e:
                    rows.append({'case_id':c['id'],'variant':v,'error':str(e)})
                    # Live service failures stop the suite; never silently fall back to fixtures.
                    if args.mode=='live':break
            if rows and 'error' in rows[-1] and args.mode=='live':break
        result={'mode':args.mode,'runs':rows,'all_fixture_contracts_passed':all(r.get('fixture_contract_passed') is True for r in rows) if args.mode=='fixture' else None,
                'live_accuracy_evaluated':False}
    print(json.dumps(result,indent=2,ensure_ascii=True,allow_nan=False))
    if args.command=='suite' and (any('error' in r for r in result['runs']) or result.get('all_fixture_contracts_passed') is False):return 1
    return 0
if __name__=='__main__':
    try:sys.exit(main())
    except SafeError as e:print(str(e),file=sys.stderr);sys.exit(2)
    except KeyboardInterrupt:print('Stopped.',file=sys.stderr);sys.exit(130)
    except Exception:print('INTERNAL_ERROR: details suppressed; run the offline tests.',file=sys.stderr);sys.exit(3)
