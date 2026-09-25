"""Human setup once; runtime operations do not ask for a grant on every request."""
from __future__ import annotations
import argparse, json, os, stat, sys
from contextlib import contextmanager
from pathlib import Path
from .common import AutoError, canonical, home_root, private_dir, read_private, state_dir, workspace, write_private
from .settings import make_policy,save_policy,load_policy,revoke
from .ipc import address,ensure,request

@contextmanager
def _bridge_lock(root):
    if os.name=='nt':
        raise AutoError('BRIDGE_LOCK_PLATFORM_UNVERIFIED')
    lock_path=private_dir(root)/'.auto-bridge.lock'
    descriptor=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        metadata=os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or metadata.st_mode&0o077
            or (hasattr(os,'getuid') and metadata.st_uid!=os.getuid())):
            raise AutoError('BRIDGE_LOCK_UNSAFE')
        import fcntl
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally:os.close(descriptor)

def bridge_record(path,p):
    root=Path(os.environ.get('XDG_CONFIG_HOME',Path.home()/'.config'))/'qualixar-jev-decision-layer'
    f=root/'auto-bridge.json'
    with _bridge_lock(root):
        try:data=read_private(f,100_000)
        except FileNotFoundError:data={'schema_version':1,'workspaces':{}}
        if not isinstance(data,dict) or data.get('schema_version')!=1 or not isinstance(data.get('workspaces'),dict):
            raise AutoError('BRIDGE_SCHEMA')
        data['workspaces'][str(workspace(path))]={'socketPath':str(address(path)),'allowedOrigins':p['browser_origins'],
                                              'maxSteps':p['browser_max_steps']}
        if len(canonical(data))+1>100_000:raise AutoError('BRIDGE_SIZE')
        write_private(f,data)

def main(argv=None):
    parser=argparse.ArgumentParser(description='Qualixar Jev Decision Layer 1.0.0')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('mcp')
    en=sub.add_parser('enroll');en.add_argument('--workspace',required=True);en.add_argument('--provider',choices=['existing','typesafe','openrouter','laya-mlx'],default='existing')
    en.add_argument('--days',type=int,default=30);en.add_argument('--daily-calls',type=int,default=1000);en.add_argument('--daily-bytes',type=int,default=20_000_000)
    en.add_argument('--classification',choices=['public','internal-minimized'],default='public');en.add_argument('--browser-origin',action='append',default=[])
    en.add_argument('--generic-query',action='store_true',help='Include explicit advisory typed queries in this workspace consent')
    for name in ('status','start','stop','revoke','warmup','stats','bridge-config'):
        p=sub.add_parser(name);p.add_argument('--workspace',required=True)
    route=sub.add_parser('route-local');route.add_argument('--workspace',required=True);route.add_argument('--recipe',action='append',choices=['sieve','prepare','browser','probe'],default=[])
    decide=sub.add_parser('route',help='Ask for one advisory task/tool/skill choice from a closed list')
    decide.add_argument('--workspace',required=True);decide.add_argument('--kind',required=True,choices=['task','tool','skill'])
    decide.add_argument('--task',required=True);decide.add_argument('--candidate',action='append',required=True)
    decide.add_argument('--classification',required=True,choices=['public','internal-minimized','restricted'])
    recall=sub.add_parser('recall');recall.add_argument('--workspace',required=True);recall.add_argument('--receipt-id',required=True);recall.add_argument('--start',type=int,default=1);recall.add_argument('--end',type=int,default=120)
    probe=sub.add_parser('probe');probe.add_argument('--workspace',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='mcp':
            from .mcp import serve
            serve();return 0
        path=workspace(args.workspace)
        if args.command in ('enroll','route-local'):
            if not sys.stdin.isatty():raise AutoError('PRIVATE_TERMINAL_SETUP_REQUIRED')
        if args.command=='enroll':
            provider=args.provider
            if provider=='existing':
                from jevkit.providers import resolve_provider
                provider=resolve_provider().provider_id
            from jevkit.engine import catalog
            p=make_policy(path,provider,args.days,max_calls_per_day=args.daily_calls,
                max_bytes_per_day=args.daily_bytes,data_classification=args.classification,
                browser_origins=args.browser_origin,case_ids=[c['id'] for c in catalog()],
                generic_query_enabled=args.generic_query)
            if provider=='laya-mlx':p['mlx']=read_private(home_root()/'mlx-installation.json',100_000)
            print('One-time workspace enrollment. Applies to reviewed workspace data, not the entire computer.')
            print('Provider:',provider,'Days:',args.days,'Maximum attempts/day:',args.daily_calls)
            print('Explicit generic typed queries:', 'ENABLED' if args.generic_query else 'DISABLED')
            print('No per-turn grants. Native Codex/browser permissions stay unchanged. Same-user local controls are not tamper-proof.')
            if input('Type ENABLE to activate: ').strip()!='ENABLE':raise AutoError('SETUP_CANCELLED')
            save_policy(path,p);bridge_record(path,p);ensure(path)
            print('Jev Decision Layer enabled. Restart Codex after plugin update and review changed hooks once.');return 0
        if args.command=='route-local':
            p=load_policy(path);p['mlx']=read_private(home_root()/'mlx-installation.json',100_000)
            if not args.recipe:raise AutoError('SELECT_LOCAL_RECIPE')
            print('Local routes:',','.join(args.recipe),'No automatic cloud fallback for these recipes.')
            if input('Type LOCAL to activate: ').strip()!='LOCAL':raise AutoError('SETUP_CANCELLED')
            p.setdefault('routes',{}).update({r:'laya-mlx' for r in args.recipe});save_policy(path,p)
            ensure(path);print(canonical(request(path,{'op':'warmup'},timeout=130)).decode());return 0
        if args.command=='revoke':
            revoke(path)
            try:request(path,{'op':'shutdown'})
            except AutoError:pass
            print('Further Auto requests disabled. Existing native Codex use is unchanged.');return 0
        if args.command=='stop':print(canonical(request(path,{'op':'shutdown'})).decode());return 0
        if args.command=='status':
            try:
                p=load_policy(path);print(canonical({'enrolled':True,'provider':p['provider'],'routes':p.get('routes',{}),'expires_at':p['expires_at']}).decode())
            except AutoError:print('{"enrolled":false}')
            return 0
        if args.command=='route':
            from .routing import compile_route
            candidates=[]
            for candidate in args.candidate:
                if '=' not in candidate:raise AutoError('ROUTE_CANDIDATES_INVALID')
                identifier,description=candidate.split('=',1)
                candidates.append({'id':identifier,'description':description})
            compile_route(args.kind,args.task,candidates)
            try:
                ensure(path)
                result=request(path,{'op':'route','kind':args.kind,'task':args.task,
                                     'candidates':candidates,'data_classification':args.classification})
            except AutoError as error:
                code=str(error);print(code,file=sys.stderr)
                return 4 if code.startswith(('PROVIDER_','DECISION_','MODEL_','KEYCHAIN_','NO_CREDENTIAL')) else 2
            print(canonical(result).decode())
            return 3 if result.get('status')=='ABSTAIN_UNKNOWN' else 0
        ensure(path)
        if args.command=='bridge-config':print('Browser connection is registered; bridge loadConfig() reads it without API keys.');return 0
        if args.command=='start':print(canonical(request(path,{'op':'health'})).decode());return 0
        if args.command=='warmup':print(canonical(request(path,{'op':'warmup'},timeout=130)).decode());return 0
        if args.command=='stats':print(canonical(request(path,{'op':'stats'})).decode());return 0
        if args.command=='recall':print(canonical(request(path,{'op':'recall','receipt_id':args.receipt_id,'start':args.start,'end':args.end})).decode());return 0
        if args.command=='probe':
            # A live/synthetic request; never a substituted fixture response.
            result=request(path,{'op':'probe'})
            print(canonical(result).decode());return 0
    except AutoError as e:print(str(e),file=sys.stderr);return 2
    except Exception:print('SETUP_OR_RUNTIME_FAILURE: details suppressed; run the supplied local tests.',file=sys.stderr);return 2
    return 0
