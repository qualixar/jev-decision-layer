"""Minimal synchronous MCP stdio server. No HTTP listener, shell tool, or arbitrary API schema."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from . import __version__
from .build_mode import OFFLINE_ONLY
from .engine import ROOT,catalog as case_catalog
from .policy_mode import classify_intent,policy_status
from .runtime import GLOBAL_HYBRID,GLOBAL_OFFLINE,PROJECT_LIVE,RuntimeContext
from .security import SafeError,canonical
VERSIONS=('2025-11-25','2025-06-18','2025-03-26','2024-11-05')

def context(root=ROOT,scope=GLOBAL_OFFLINE,state_root:Path|None=None,workspace_root:Path|None=None):
    """Build a scope-explicit runtime without making global registration live."""
    package_root=Path(root)
    runtime_root=Path(state_root) if state_root is not None else None
    return RuntimeContext(package_root,runtime_root,scope,workspace_root)

def health(root=ROOT,scope=GLOBAL_OFFLINE,state_root:Path|None=None,workspace_root:Path|None=None,ctx=None):
    active=ctx or context(root,scope,state_root,workspace_root)
    return {'version':__version__,'service':'qualixar-jev-decision-layer','scope':active.scope,
            'legacy_tool_surface':True,'provider_checked':False,
            'current_workspace_status_tool':'jev_auto_status'}

def tools(root=ROOT,scope=GLOBAL_OFFLINE,state_root:Path|None=None,workspace_root:Path|None=None,ctx=None):
    # Listing tool schemas must not create a state directory. MCP clients ask
    # for tools before selecting a workspace or granting filesystem access.
    active_scope=ctx.scope if ctx is not None else scope
    if active_scope not in (GLOBAL_OFFLINE,GLOBAL_HYBRID,PROJECT_LIVE):raise SafeError('INVALID_RUNTIME_SCOPE')
    if active_scope in (GLOBAL_HYBRID,PROJECT_LIVE) and OFFLINE_ONLY:raise SafeError('LIVE_RUNTIME_NOT_AVAILABLE')
    if active_scope==PROJECT_LIVE and ctx is None and workspace_root is None:raise SafeError('WORKSPACE_ID_REQUIRED')
    ids=[x['id'] for x in (ctx.catalog() if ctx is not None else case_catalog(Path(root)))]
    def obj(properties,required=(),**extra):
        return {'type':'object','properties':properties,'required':list(required),
                'additionalProperties':False,**extra}
    case={'type':'string','enum':ids}
    variant={'type':'string','enum':['nominal','adversarial','uncertain'],'default':'nominal'}
    def tool(name,description,schema,read=True,external=False):
        return {'name':name,'description':description,'inputSchema':schema,'annotations':{
            'readOnlyHint':read,'destructiveHint':False,'idempotentHint':read,'openWorldHint':external}}
    offline=[
      tool('jev_health','Report the legacy tool surface only. Use jev_auto_status for current workspace/provider readiness; no provider call.',obj({})),
      tool('jev_policy_status','Report legacy local Policy Mode, not current Jev workspace authority. No provider call.',obj({})),
      tool('jev_policy_check','Legacy advisory classifier: locally label an intent SKIP, SUGGEST, or sensitive-input BLOCK. It never denies a host tool, calls a provider, or persists prompt text.',obj({'intent':{'type':'string','minLength':1,'maxLength':8000}},['intent'])),
      tool('jev_catalog','List the approved decision workflows; choose a case ID before judging.',obj({})),
      tool('jev_describe','Read required input fields, rubric and demonstration thresholds for one case.',obj({'case_id':case},['case_id'])),
      tool('jev_run_fixture','Run a clearly labeled OFFLINE synthetic fixture. No model inference; never use as proof of Jev accuracy.',obj({'case_id':case,'variant':variant},['case_id']),False)]
    if active_scope==GLOBAL_OFFLINE:return offline
    live_properties={'case_id':case,
             'state':{'type':'object','description':'Reviewed minimized object data only.'},
             'request_id':{'type':'string','minLength':1,'maxLength':128},
             'data_classification':{'type':'string','enum':['public','internal-minimized']}}
    live_required=['case_id']
    if active_scope==GLOBAL_HYBRID:
        live_properties['workspace_path']={'type':'string','minLength':1,'maxLength':4096,
            'description':'Absolute path to the trusted Git workspace with a human-created live grant.'}
        live_required.append('workspace_path')
    return offline+[
      tool('jev_evaluate','Send reviewed, minimized data to the human-selected Jev provider. Requires a workspace-bound human grant. Custom state also requires a request ID and allowed classification. Returns advice, never permission to execute.',
        obj(live_properties,live_required,
            allOf=[{'if':{'required':['state']},'then':{'required':['request_id','data_classification']}}]),False,True)
    ]

def _validate_arguments(schema: dict, args: dict) -> None:
    if set(args)-set(schema['properties']) or any(k not in args for k in schema['required']):
        raise SafeError('INVALID_TOOL_ARGUMENTS')
    for condition in schema.get('allOf',[]):
        trigger=condition.get('if',{}).get('required',[])
        required=condition.get('then',{}).get('required',[])
        if all(key in args for key in trigger) and any(key not in args for key in required):
            raise SafeError('INVALID_TOOL_ARGUMENTS')
    for key,value in args.items():
        definition=schema['properties'][key]
        expected=definition.get('type')
        if expected=='object' and (not isinstance(value,dict)):
            raise SafeError('INVALID_TOOL_ARGUMENTS')
        if expected=='string' and (not isinstance(value,str)):
            raise SafeError('INVALID_TOOL_ARGUMENTS')
        if isinstance(value,str) and (len(value)<definition.get('minLength',0) or len(value)>definition.get('maxLength',float('inf'))):
            raise SafeError('INVALID_TOOL_ARGUMENTS')
        if 'enum' in definition and value not in definition['enum']:
            raise SafeError('INVALID_TOOL_ARGUMENTS')


def call(name,args,root=ROOT,scope=GLOBAL_OFFLINE,state_root:Path|None=None,workspace_root:Path|None=None,ctx=None):
    active=ctx or context(root,scope,state_root,workspace_root)
    definitions={t['name']:t for t in tools(ctx=active)}
    if name not in definitions:raise SafeError('UNKNOWN_TOOL')
    if not isinstance(args,dict):raise SafeError('INVALID_TOOL_ARGUMENTS')
    schema=definitions[name]['inputSchema']
    _validate_arguments(schema,args)
    if name=='jev_health':return health(ctx=active)
    if name=='jev_policy_status':return policy_status()
    if name=='jev_policy_check':return classify_intent(args['intent'])
    if name=='jev_catalog':return active.catalog()
    if name=='jev_describe':return active.describe(args['case_id'])
    if name=='jev_run_fixture':return active.run_fixture(args['case_id'],args.get('variant','nominal'))
    if name=='jev_evaluate':
        return active.evaluate(args['case_id'],args.get('state'),
                               request_id=args.get('request_id'),
                               data_classification=args.get('data_classification'),
                               workspace_root=Path(args['workspace_path']) if 'workspace_path' in args else None)
    raise SafeError('UNKNOWN_TOOL')

def serve(root=ROOT,scope=GLOBAL_OFFLINE,state_root:Path|None=None,workspace_root:Path|None=None):
    active=context(root,scope,state_root,workspace_root)
    initialized=False
    for line in sys.stdin:
        if len(line)>512_000:
            print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Message too large'}}),flush=True);continue
        try:req=json.loads(line)
        except (ValueError,RecursionError):
            print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}}),flush=True);continue
        if not isinstance(req,dict) or req.get('jsonrpc')!='2.0':
            print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}}),flush=True);continue
        method=req.get('method');rid=req.get('id');params=req.get('params') or {}
        if 'id' not in req:
            # initialized and cancellation notifications have no response.
            continue
        try:
            if method=='initialize':
                version=params.get('protocolVersion')
                instructions=(
                    'Global offline mode exposes reviewed catalogs and simulated fixtures only. '
                    'It never reads credentials, grants live access, or sends text to TypeSafe. '
                    'Fixture outputs are not Jev inference and never authorize an action.'
                    if active.scope==GLOBAL_OFFLINE else
                    'Legacy jev_evaluate requires its own exact workspace/revision/request-bound grant. '
                    'The Jev Decision Layer tools use separately reviewed standing workspace policy. '
                    'No judgment authorizes a shell command, deployment, or access change.'
                )
                result={'protocolVersion':version if version in VERSIONS else VERSIONS[0],
                    'serverInfo':{'name':'qualixar-jev-decision-layer','version':__version__},'capabilities':{'tools':{'listChanged':False}},
                    'instructions':instructions}
                initialized=True
            elif method=='ping':result={}
            elif not initialized:raise SafeError('INITIALIZE_REQUIRED')
            elif method=='tools/list':result={'tools':tools(ctx=active)}
            elif method=='tools/call':
                try:
                    output=call(params.get('name'),params.get('arguments',{}),ctx=active)
                    result={'content':[{'type':'text','text':canonical(output).decode()}],'isError':False}
                except SafeError as e:result={'content':[{'type':'text','text':str(e)}],'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
            message={'jsonrpc':'2.0','id':rid,'result':result}
        except SafeError as e:message={'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':str(e)}}
        except Exception:message={'jsonrpc':'2.0','id':rid,'error':{'code':-32603,'message':'Internal error; sensitive details suppressed'}}
        print(json.dumps(message,ensure_ascii=True,allow_nan=False),flush=True)
