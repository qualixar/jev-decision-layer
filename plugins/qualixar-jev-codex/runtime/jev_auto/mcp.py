"""Compatibility facade: existing tools stay available; automatic calls return compact data."""
from __future__ import annotations
import json
import sys
import os
import re
import selectors
import signal
import subprocess
import threading
from pathlib import Path
from .common import AutoError,canonical,decode,workspace
from .ipc import ensure,request

VERSIONS=('2025-11-25','2025-06-18','2025-03-26','2024-11-05')
_SETUP_LINE=re.compile(rb'Qualixar setup: (http://127\.0\.0\.1:[0-9]{2,5}/setup)\. Enter keys only in the local browser, never in chat\.\r?\n?\Z')

def _extract_setup_url(line):
    match=_SETUP_LINE.fullmatch(line) if isinstance(line,bytes) else None
    if match is None:raise ValueError('SETUP_URL_INVALID')
    return match.group(1).decode('ascii')

def _open_setup(path):
    """Open only the packaged loopback wizard, never a caller-supplied command."""
    project=workspace(path)
    script=Path(__file__).resolve().parents[2]/'scripts'/'open-setup'
    if not script.is_file() or script.is_symlink():raise AutoError('SETUP_LAUNCHER_MISSING')
    environment={name:os.environ[name] for name in ('PATH','HOME','XDG_STATE_HOME','XDG_CONFIG_HOME') if name in os.environ}
    try:
        process=subprocess.Popen([str(script),str(project)],cwd=project,env=environment,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 start_new_session=True,close_fds=True)
    except OSError:
        raise AutoError('SETUP_START_FAILED') from None
    selector=selectors.DefaultSelector()
    try:
        selector.register(process.stdout,selectors.EVENT_READ)
        if not selector.select(8):raise AutoError('SETUP_START_TIMEOUT')
        line=process.stdout.readline(512)
        if len(line)>=512:raise AutoError('SETUP_START_FAILED')
        url=_extract_setup_url(line)
        def reap():
            process.wait()
            process.stdout.close()
        threading.Thread(target=reap,daemon=True,name='jev-setup-reap').start()
        return {'status':'SETUP_WIZARD_OPEN','url':url,'expires_in_seconds':600,
                'credential_entry':'PRIVATE_BROWSER_ONLY'}
    except (OSError,UnicodeError,ValueError,AutoError):
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL)
        process.wait(timeout=2)
        process.stdout.close()
        raise AutoError('SETUP_START_FAILED') from None
    finally:
        selector.close()

def definitions(legacy):
    tools=legacy.tools(scope='global-hybrid')
    for t in tools:
        if t['name']=='jev_evaluate':
            t['description']='Evaluate one approved case using enrolled workspace authority. No per-request grant is needed in the Jev Decision Layer. Return a compact recommendation and local receipt ID, never execution authority.'
            t['inputSchema'].pop('allOf',None);t['inputSchema']['required']=['case_id','workspace_path','state']
    def tool(name,description,props,required):
        return {'name':name,'description':description,'inputSchema':{'type':'object','properties':props,'required':required,'additionalProperties':False}}
    wp={'type':'string','minLength':1,'maxLength':4096};goal={'type':'string','minLength':1,'maxLength':4000}
    tools += [
      tool('jev_setup','Open the private local two-step setup wizard for this workspace. No API key belongs in chat or tool arguments.',{'workspace_path':wp},['workspace_path']),
      tool('jev_auto_status','Report this workspace\'s Auto status and actual counters; never infer host token savings.',{'workspace_path':wp},['workspace_path']),
      tool('jev_prepare','Prepare a compact file/optional-guidance shortlist for a narrow task. Mandatory instructions and SLM are unchanged.',{'workspace_path':wp,'goal':goal},['workspace_path','goal']),
      tool('jev_reduce','Select relevant blocks from supplied text, keeping omissions exactly recoverable. Never pass secrets.',{'workspace_path':wp,'goal':goal,'text':{'type':'string','maxLength':100000}},['workspace_path','goal','text']),
      tool('jev_recall','Fetch a local receipt or exact omitted line range. No model inference or cloud request.',{'workspace_path':wp,'receipt_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},'start':{'type':'integer','minimum':1},'end':{'type':'integer','minimum':1}},['workspace_path','receipt_id']),
      tool('jev_typed_decide','Ask one bounded, advisory Jev typed question set under explicit workspace consent. Returns answers and a local receipt, never tool execution authority.',
           {'workspace_path':wp,'state':{'type':['string','object','array']},'questions':{'type':'object'},
            'provider':{'type':'string','enum':['typesafe','openrouter','laya-mlx']},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','state','questions','provider','data_classification']),
      tool('jev_route','Recommend one task, tool or skill from a closed candidate list using an enrolled Jev route. Advisory only; it never executes a choice.',
           {'workspace_path':wp,'kind':{'type':'string','enum':['task','tool','skill']},'task':goal,
            'candidates':{'type':'array','minItems':2,'maxItems':12,'items':{'type':'object'}},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','kind','task','candidates','data_classification']),
      tool('jev_recipe_catalog','List the available data-only use-case recipes. They are specifications, not provider accuracy evidence.',{},[]),
      tool('jev_recipe_try','Evaluate one explicit recipe input through the enrolled typed Jev route. The answer is experimental advice, never permission to execute.',
           {'workspace_path':wp,'recipe_id':{'type':'string','minLength':1,'maxLength':128},'input':{'type':'object'},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','recipe_id','input','data_classification']),
      tool('jev_review_diff','Triage caller-supplied diff text to suggest review focus and risk. Never approves code or runs Git.',
           {'workspace_path':wp,'goal':{'type':'string','minLength':1,'maxLength':1000},
            'diff':{'type':'string','minLength':1,'maxLength':16000},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','goal','diff','data_classification']),
    ]
    return tools

def dispatch(name,args,legacy,caller=None,setup_launcher=None):
    if not isinstance(args,dict):raise AutoError('MCP_ARGUMENTS')
    known={t['name']:t for t in definitions(legacy)}
    if name not in known:raise AutoError('MCP_TOOL_NAME')
    schema=known[name]['inputSchema']
    if set(args)-set(schema['properties']) or set(schema.get('required',[]))-set(args):raise AutoError('MCP_ARGUMENTS')
    for k,v in args.items():
        spec=schema['properties'][k];kind=spec.get('type')
        if isinstance(kind,list) and not any((item=='string' and isinstance(v,str)) or (item=='object' and isinstance(v,dict)) or (item=='array' and isinstance(v,list)) for item in kind):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='string' and (not isinstance(v,str) or len(v)<spec.get('minLength',0) or len(v)>spec.get('maxLength',100000)):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='integer' and (not isinstance(v,int) or isinstance(v,bool) or v<spec.get('minimum',0)):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='object' and not isinstance(v,dict):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='array' and not isinstance(v,list):raise AutoError('MCP_ARGUMENT_TYPE')
        if 'enum' in spec and v not in spec['enum']:raise AutoError('MCP_ARGUMENT_ENUM')
    path=args.get('workspace_path')
    if name=='jev_setup':return (setup_launcher or _open_setup)(workspace(path))
    auto_names={'jev_auto_status','jev_prepare','jev_reduce','jev_recall','jev_typed_decide','jev_route','jev_recipe_try','jev_review_diff'}
    if name in auto_names or name=='jev_evaluate':
        if caller is None:
            try:ensure(path)
            except AutoError as error:
                if name=='jev_evaluate' and str(error)=='WORKSPACE_NOT_ENROLLED':return legacy.call(name,args,scope='global-hybrid')
                raise
        call=caller or (lambda obj:request(path,obj))
        if name=='jev_auto_status':return {'health':call({'op':'health'}),'usage':call({'op':'stats'})}
        if name=='jev_prepare':return call({'op':'prepare','goal':args['goal']})
        if name=='jev_reduce':return call({'op':'sieve','goal':args['goal'],'text':args['text'],'tool':'explicit_reduce'})
        if name=='jev_recall':return call({'op':'recall','receipt_id':args['receipt_id'],'start':args.get('start',1),'end':args.get('end',120)})
        if name=='jev_typed_decide':return call({'op':'typed_query','state':args['state'],'questions':args['questions'],
                                                 'provider':args['provider'],'data_classification':args['data_classification']})
        if name=='jev_route':return call({'op':'route','kind':args['kind'],'task':args['task'],
                                          'candidates':args['candidates'],'data_classification':args['data_classification']})
        if name=='jev_recipe_try':return call({'op':'recipe_try','recipe_id':args['recipe_id'],
                                               'input':args['input'],'data_classification':args['data_classification']})
        if name=='jev_review_diff':return call({'op':'review_diff','goal':args['goal'],'diff':args['diff'],
                                                'data_classification':args['data_classification']})
        return call({'op':'evaluate','case_id':args['case_id'],'state':args['state']})
    if name=='jev_recipe_catalog':
        from .recipe_runtime import catalog_preview
        return catalog_preview()
    return legacy.call(name,args,scope='global-hybrid')

def serve():
    from jevkit import mcp_server as legacy
    initialized=False
    while True:
        line=sys.stdin.buffer.readline(512_001)
        if not line:break
        rid=None
        try:
            if not line.endswith(b'\n') and len(line)>512000:raise AutoError('MCP_MESSAGE_SIZE')
            req=decode(line)
            if not isinstance(req,dict) or req.get('jsonrpc')!='2.0':raise AutoError('MCP_REQUEST')
            if 'id' not in req:continue
            rid=req['id'];method=req.get('method');params=req.get('params') or {}
            if not isinstance(params,dict):raise AutoError('MCP_PARAMS')
            if method=='initialize':
                v=params.get('protocolVersion');initialized=True
                result={'protocolVersion':v if v in VERSIONS else VERSIONS[0],
                        'serverInfo':{'name':'qualixar-jev','version':'1.0.0'},'capabilities':{'tools':{'listChanged':False}},
                        'instructions':'Enrolled workspaces use standing Jev Decision Layer authority. Use compact recommendations; detailed receipts are local. Preserve SLM and the existing browser. Never create grants yourself.'}
            elif method=='ping':result={}
            elif not initialized:raise AutoError('MCP_INITIALIZE_FIRST')
            elif method=='tools/list':result={'tools':definitions(legacy)}
            elif method=='tools/call':
                try:
                    output=dispatch(params.get('name'),params.get('arguments',{}),legacy)
                    result={'content':[{'type':'text','text':canonical(output).decode()}],'isError':False}
                except Exception as e:
                    safe=str(e) if isinstance(e,AutoError) else 'JEV_TOOL_UNAVAILABLE'
                    result={'content':[{'type':'text','text':safe}],'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
            out={'jsonrpc':'2.0','id':rid,'result':result}
        except AutoError as e:out={'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':str(e)}}
        except Exception:out={'jsonrpc':'2.0','id':rid,'error':{'code':-32603,'message':'Internal error'}}
        print(canonical(out).decode(),flush=True)
