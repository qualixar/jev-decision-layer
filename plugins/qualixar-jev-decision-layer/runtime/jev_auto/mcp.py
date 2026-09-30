"""Compatibility facade: existing tools stay available; automatic calls return compact data."""
from __future__ import annotations
import json
import math
import sys
import os
import re
import selectors
import signal
import subprocess
import threading
import queue
from pathlib import Path
from . import __version__
from .common import AutoError,canonical,decode,workspace
from .ipc import ensure,request

VERSIONS=('2025-11-25','2025-06-18','2025-03-26','2024-11-05')
_IS_WINDOWS=os.name=='nt'
# The wizard's private link goes only to the browser opener. Over a pipe the
# launcher prints one of these fixed lines, so no link or token ever reaches
# this process, the tool result or the model.
_SETUP_OPENED=re.compile(rb'Qualixar setup opened in your browser\. Enter keys only in the local browser, never in chat\.\r?\n?\Z')
_SETUP_NO_BROWSER=re.compile(rb'Qualixar setup could not open a browser\. Run open-setup in a terminal to get a private link\.\r?\n?\Z')

def _setup_outcome(line):
    """'opened' or 'no_browser' for the two fixed launcher lines; anything else is refused."""
    if isinstance(line,bytes) and _SETUP_OPENED.fullmatch(line):return 'opened'
    if isinstance(line,bytes) and _SETUP_NO_BROWSER.fullmatch(line):return 'no_browser'
    raise ValueError('SETUP_LINE_INVALID')

def _open_setup(path):
    """Open only the packaged loopback wizard, never a caller-supplied command."""
    project=workspace(path)
    plugin_root=Path(__file__).resolve().parents[2]
    script=plugin_root/'scripts'/('open-setup.cmd' if _IS_WINDOWS else 'open-setup')
    if not script.is_file() or script.is_symlink():raise AutoError('SETUP_LAUNCHER_MISSING')
    # CLAUDE_CONFIG_DIR locates Claude Code's cached organization policy for
    # the wizard's notice; it is a folder path, never a credential.
    allowed={'PATH','HOME','XDG_STATE_HOME','XDG_CONFIG_HOME','CLAUDE_CONFIG_DIR',
             # A Linux desktop session: the key store is reached over D-Bus and the
             # browser opens through the display. None of these carries a credential.
             'DBUS_SESSION_BUS_ADDRESS','DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','BROWSER'}
    if _IS_WINDOWS:
        allowed.update({'LOCALAPPDATA','USERPROFILE','SYSTEMROOT','WINDIR','TEMP','TMP',
                        'APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','RUNNER_TOOL_CACHE'})
    environment={name:value for name,value in os.environ.items() if name.upper() in allowed}
    command=[str(script),str(project)]
    if _IS_WINDOWS:
        # Batch files parse arguments through cmd.exe even with shell=False.
        # Use the running Python executable so a workspace path cannot become
        # a batch command, while still requiring the packaged launcher.
        command=[sys.executable,'-I','-S','-B','-c',
                 'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                 'sys.argv=["src.adl.api.setup_server","--workspace",sys.argv[2]]; '
                 'runpy.run_module("src.adl.api.setup_server",run_name="__main__")',
                 str(plugin_root/'runtime'),str(project)]
    try:
        process=subprocess.Popen(command,cwd=project,env=environment,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 start_new_session=not _IS_WINDOWS,close_fds=True)
    except OSError:
        raise AutoError('SETUP_START_FAILED') from None
    selector=None
    outcome=None
    try:
        if _IS_WINDOWS:
            # Windows selectors cannot wait on an anonymous subprocess pipe.
            lines=queue.Queue(maxsize=1)
            threading.Thread(target=lambda:lines.put(process.stdout.readline(512)),
                             daemon=True,name='jev-setup-line').start()
            try:line=lines.get(timeout=8)
            except queue.Empty:raise AutoError('SETUP_START_TIMEOUT') from None
        else:
            selector=selectors.DefaultSelector()
            selector.register(process.stdout,selectors.EVENT_READ)
            if not selector.select(8):raise AutoError('SETUP_START_TIMEOUT')
            line=process.stdout.readline(512)
        if len(line)>=512:raise AutoError('SETUP_START_FAILED')
        outcome=_setup_outcome(line)
        if outcome=='no_browser':raise AutoError('SETUP_BROWSER_UNAVAILABLE')
        def reap():
            process.wait()
            process.stdout.close()
        threading.Thread(target=reap,daemon=True,name='jev-setup-reap').start()
        return {'status':'SETUP_WIZARD_OPEN','expires_in_seconds':600,
                'credential_entry':'PRIVATE_BROWSER_ONLY','opened_in':'USER_BROWSER'}
    except (OSError,UnicodeError,ValueError,AutoError):
        if process.poll() is None:
            try:
                if _IS_WINDOWS:process.kill()
                else:os.killpg(process.pid,signal.SIGKILL)
            except OSError:pass
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:pass
        process.stdout.close()
        raise AutoError('SETUP_BROWSER_UNAVAILABLE' if outcome=='no_browser' else 'SETUP_START_FAILED') from None
    finally:
        if selector is not None:selector.close()

# What each tool does to the world, as MCP annotations. A tool that sends text
# to the decision provider or spends the daily budget is not read-only.
_LOCAL_READ={'readOnlyHint':True,'openWorldHint':False}
_PROVIDER={'readOnlyHint':False,'openWorldHint':True}
_TOOL_META={
    'jev_health':('Legacy health',_LOCAL_READ),'jev_policy_status':('Legacy policy status',_LOCAL_READ),
    'jev_policy_check':('Legacy intent check',_LOCAL_READ),'jev_catalog':('Legacy case list',_LOCAL_READ),
    'jev_describe':('Legacy case details',_LOCAL_READ),'jev_run_fixture':('Legacy fixture',_LOCAL_READ),
    'jev_evaluate':('Legacy case decision',_PROVIDER),
    'jev_setup':('Set up Jev',{'readOnlyHint':False,'openWorldHint':False}),
    'jev_auto_status':('Jev status',_LOCAL_READ),'jev_prepare':('Shortlist files',_PROVIDER),
    'jev_reduce':('Reduce text',_PROVIDER),'jev_recall':('Recall receipt',_LOCAL_READ),
    'jev_typed_decide':('Typed decision',_PROVIDER),'jev_route':('Route a choice',_PROVIDER),
    'jev_recipe_catalog':('Recipe catalog',_LOCAL_READ),'jev_recipe_selftest':('Recipe self-test',_LOCAL_READ),
    'jev_recipe_try':('Try a recipe',_PROVIDER),'jev_verify':('Verify extraction',_PROVIDER),
    'jev_rerank':('Rerank passages',_PROVIDER),'jev_review_diff':('Diff review focus',_PROVIDER),
}
# The older tool surface, and the tool to use instead.
_LEGACY={
    'jev_health':'Legacy: report the old tool surface; no provider call. Use jev_auto_status.',
    'jev_policy_status':'Legacy: report the old local Policy Mode, not workspace authority. Use jev_auto_status.',
    'jev_policy_check':'Legacy: locally label an intent SKIP, SUGGEST or BLOCK; calls no provider and keeps no prompt text. Use jev_prepare.',
    'jev_catalog':'Legacy: list the old decision cases. Use jev_recipe_catalog.',
    'jev_describe':'Legacy: read one old case\'s fields, rubric and thresholds. Use jev_recipe_catalog with recipe_id.',
    'jev_run_fixture':'Legacy: run an OFFLINE synthetic fixture; no inference, never proof of accuracy. Use jev_recipe_selftest.',
    'jev_evaluate':'Legacy: evaluate one old case with enrolled workspace authority; returns advice and a local receipt, never execution authority. Use jev_recipe_try or jev_route.',
}
# A short fix for the argument errors a first call most often hits. The code
# itself stays the first, unchanged text of the error result.
_HINTS={
    'ROUTE_CANDIDATES_INVALID':'Send 2-12 candidates, each exactly {"id","description"}: id matches ^[A-Za-z][A-Za-z0-9_-]{0,63}$, is unique and is not "unknown"; description has 1-250 characters.',
    'RERANK_MEMORIES_INVALID':'Send 1-12 memories, each an object with a non-empty "content" string; only its first 1,200 characters are judged.',
    'QUESTION_INVALID':'questions maps 1-60 ids (^[A-Za-z][A-Za-z0-9_-]{0,127}$) to {type, instructions, criteria}: choice needs 2-64 label: description pairs, score a list of 2-10 levels, noul optional "true"/"false" descriptions; 20,000 bytes at most.',
    'VERIFY_THRESHOLD_INVALID':'threshold is a number above 0 and at most 1; the default is 0.7.',
    'RECALL_RANGE':'start is 1 or more, end is at least start, and end - start is under 300.',
    'WORKSPACE_REQUIRED':'workspace_path must be the absolute path of an existing folder.',
    'BROKER_TIMEOUT':'The decision may still be running and may be charged; wait before asking again.',
    'SETUP_BROWSER_UNAVAILABLE':'No browser could open here. Ask the user to run scripts/open-setup with this folder in their own terminal.',
}

def definitions(legacy):
    tools=legacy.tools(scope='global-hybrid')
    for t in tools:
        if t['name'] in _LEGACY:t['description']=_LEGACY[t['name']]
        if t['name']=='jev_evaluate':
            t['inputSchema'].pop('allOf',None);t['inputSchema']['required']=['case_id','workspace_path','state']
    def tool(name,description,props,required):
        return {'name':name,'description':description,'inputSchema':{'type':'object','properties':props,'required':required,'additionalProperties':False}}
    wp={'type':'string','minLength':1,'maxLength':4096};goal={'type':'string','minLength':1,'maxLength':4000}
    text={'type':'string','minLength':1,'maxLength':2000}
    # Nested shapes exactly as routing.py, rerank.py and queries/typed.py accept them.
    candidate={'type':'object','properties':{'id':{'type':'string','pattern':'^[A-Za-z][A-Za-z0-9_-]{0,63}$','not':{'const':'unknown'}},
               'description':{'type':'string','pattern':'^\\s*\\S([\\s\\S]{0,248}\\S)?\\s*$'}},'required':['id','description'],'additionalProperties':False}
    question={'type':'object','properties':{'type':{},'instructions':{'type':'string','pattern':'\\S','maxLength':8000},'criteria':{}},
              'required':['type','instructions'],'additionalProperties':False,'anyOf':[
                {'properties':{'type':{'const':'choice'},'criteria':{'type':'object','minProperties':2,'maxProperties':64,'propertyNames':{'minLength':1,'maxLength':128},'additionalProperties':text}},'required':['criteria']},
                {'properties':{'type':{'const':'score'},'criteria':{'type':'array','minItems':2,'maxItems':10,'items':text}},'required':['criteria']},
                {'properties':{'type':{'const':'noul'},'criteria':{'type':'object','propertyNames':{'enum':['true','false']},'additionalProperties':text}}}]}
    tools += [
      tool('jev_setup','Open the private two-step setup wizard for this workspace in the user\'s browser; no link is returned. No API key belongs in chat or tool arguments.',{'workspace_path':wp},['workspace_path']),
      tool('jev_auto_status','Report this workspace\'s Auto status and actual counters; never infer host token savings.',{'workspace_path':wp},['workspace_path']),
      tool('jev_prepare','Prepare a compact file/optional-guidance shortlist for a narrow task. The goal and candidate names go to this workspace\'s decision provider (hosted Jev, or Laya on this Mac). Mandatory instructions stay; other memory and browser tools are unchanged.',{'workspace_path':wp,'goal':goal},['workspace_path','goal']),
      tool('jev_reduce','Select relevant blocks from supplied text, keeping omissions exactly recoverable. The text goes to this workspace\'s decision provider (hosted Jev, or Laya on this Mac); never pass secrets or content the grant does not cover.',{'workspace_path':wp,'goal':goal,'text':{'type':'string','maxLength':100000}},['workspace_path','goal','text']),
      tool('jev_recall','Fetch a local receipt or exact omitted line range (end - start under 300). No model inference or cloud request.',{'workspace_path':wp,'receipt_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},'start':{'type':'integer','minimum':1,'default':1},'end':{'type':'integer','minimum':1,'default':120}},['workspace_path','receipt_id']),
      tool('jev_typed_decide','Ask one bounded, advisory Jev typed question set under explicit workspace consent. provider is the workspace\'s own (see jev_auto_status). Returns answers and a local receipt, never tool execution authority.',
           {'workspace_path':wp,'state':{'type':['string','object','array']},
            'questions':{'type':'object','minProperties':1,'maxProperties':60,'propertyNames':{'pattern':'^[A-Za-z][A-Za-z0-9_-]{0,127}$'},'additionalProperties':question,
                         'examples':[{'fit':{'type':'choice','instructions':'Which fits?','criteria':{'a':'A','b':'B'}}}]},
            'provider':{'type':'string','enum':['typesafe','openrouter','laya-mlx']},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','state','questions','provider','data_classification']),
      tool('jev_route','Recommend one task, tool or skill from a closed candidate list using an enrolled Jev route. candidate_id is null when none fits. Advisory only; it never executes a choice.',
           {'workspace_path':wp,'kind':{'type':'string','enum':['task','tool','skill']},'task':goal,
            'candidates':{'type':'array','minItems':2,'maxItems':12,'items':candidate,
                          'examples':[[{'id':'quick','description':'Fast'},{'id':'deep','description':'Careful'}]]},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','kind','task','candidates','data_classification']),
      tool('jev_recipe_catalog','List the data-only use-case recipes; recipe_id returns its input_schema and limitations. They are specifications, not provider accuracy evidence.',
           {'recipe_id':{'type':'string','minLength':1,'maxLength':128}},[]),
      tool('jev_recipe_selftest','Replay the shipped synthetic fixtures through the local gate. Offline: no provider call, key or enrollment. Check the gate before spending on a live decision.',
           {'recipe_id':{'type':'string','minLength':1,'maxLength':128},
            'variant':{'type':'string','enum':['nominal','uncertain','adversarial']}},[]),
      tool('jev_recipe_try','Evaluate one explicit recipe input through the enrolled typed Jev route. The answer is experimental advice, never permission to execute.',
           {'workspace_path':wp,'recipe_id':{'type':'string','minLength':1,'maxLength':128},'input':{'type':'object'},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','recipe_id','input','data_classification']),
      tool('jev_verify','Check a structured extraction against the source text it claims to come from. Returns a per-field probability that the field is WRONG, and a `trustworthy` flag. Branch on `trustworthy`, never on an empty suspect list: a field the model could not judge is unknown, not clean.',
           {'workspace_path':wp,'source_text':{'type':'string','minLength':1,'maxLength':20000},
            'extraction':{'type':'object'},'threshold':{'type':'number','exclusiveMinimum':0,'maximum':1,'default':0.7},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','source_text','extraction','data_classification']),
      tool('jev_rerank','Score retrieved passages on an ABSOLUTE scale and decide whether the set answers the question at all. A retrieval score ranks within a set and cannot say "none of these answer it"; this can. If should_abstain is true, say you do not have the answer rather than using the top hit.',
           {'workspace_path':wp,'query':{'type':'string','minLength':1,'maxLength':2000},
            'memories':{'type':'array','minItems':1,'maxItems':12,
                        'items':{'type':'object','properties':{'content':{'type':'string','pattern':'\\S'}},'required':['content']},
                        'examples':[[{'content':'Entries expire after an hour.','fact_id':'f1'}]]},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','query','memories','data_classification']),
      tool('jev_review_diff','Triage caller-supplied diff text to suggest review focus and risk. Never approves code or runs Git.',
           {'workspace_path':wp,'goal':{'type':'string','minLength':1,'maxLength':1000},
            'diff':{'type':'string','minLength':1,'maxLength':16000},
            'data_classification':{'type':'string','enum':['public','internal-minimized','restricted']}},
           ['workspace_path','goal','diff','data_classification']),
    ]
    for t in tools:
        if t['name'] in _TOOL_META:
            t['title'],annotations=_TOOL_META[t['name']];t['annotations']=dict(annotations)
    return tools

def _default_workspace(args,schema):
    """Fill a missing workspace_path from CLAUDE_PROJECT_DIR, which Claude Code
    sets for the stdio servers it spawns. Other hosts do not set it, so for
    them a missing workspace_path is still refused, and the advertised schema
    is identical for every host."""
    if 'workspace_path' in args or 'workspace_path' not in schema['properties']:return args
    project=os.environ.get('CLAUDE_PROJECT_DIR')
    if not isinstance(project,str) or not project or not os.path.isabs(project):return args
    return {**args,'workspace_path':project}

def _claude_policy():
    from .host_policy import notice
    return notice()

def _relative_from_unsafe_folder(path):
    """A relative path resolves against the host's folder. The Claude desktop app
    starts servers in `/`, so `.` there would name the whole filesystem."""
    if not isinstance(path,str) or not path or os.path.isabs(path):return False
    try:
        here=os.path.normpath(os.getcwd());home=os.path.normpath(str(Path.home()))
    except (OSError,RuntimeError,KeyError):return True
    return here in {os.path.normpath(os.sep),home}

def dispatch(name,args,legacy,caller=None,setup_launcher=None,claude_policy=None):
    if not isinstance(args,dict):raise AutoError('MCP_ARGUMENTS')
    known={t['name']:t for t in definitions(legacy)}
    if name not in known:raise AutoError('MCP_TOOL_NAME')
    schema=known[name]['inputSchema']
    args=_default_workspace(args,schema)
    if set(args)-set(schema['properties']) or set(schema.get('required',[]))-set(args):raise AutoError('MCP_ARGUMENTS')
    for k,v in args.items():
        spec=schema['properties'][k];kind=spec.get('type')
        if isinstance(kind,list) and not any((item=='string' and isinstance(v,str)) or (item=='object' and isinstance(v,dict)) or (item=='array' and isinstance(v,list)) for item in kind):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='string' and not isinstance(v,str):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='integer' and (not isinstance(v,int) or isinstance(v,bool) or v<spec.get('minimum',0)):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='object' and not isinstance(v,dict):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='array' and not isinstance(v,list):raise AutoError('MCP_ARGUMENT_TYPE')
        if kind=='number' and (not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v)):raise AutoError('MCP_ARGUMENT_TYPE')
        if isinstance(v,str):
            if len(v)<spec.get('minLength',0) or len(v)>spec.get('maxLength',100000):raise AutoError('MCP_ARGUMENT_TYPE')
            if 'pattern' in spec and re.fullmatch(spec['pattern'],v) is None:raise AutoError('MCP_ARGUMENT_TYPE')
        if isinstance(v,list) and (len(v)<spec.get('minItems',0) or len(v)>spec.get('maxItems',100000)):raise AutoError('MCP_ARGUMENT_TYPE')
        if 'enum' in spec and v not in spec['enum']:raise AutoError('MCP_ARGUMENT_ENUM')
    path=args.get('workspace_path')
    if _relative_from_unsafe_folder(path):raise AutoError('WORKSPACE_PATH_NOT_ABSOLUTE')
    if name=='jev_setup':return (setup_launcher or _open_setup)(workspace(path))
    auto_names={'jev_auto_status','jev_prepare','jev_reduce','jev_recall','jev_typed_decide','jev_route','jev_recipe_try','jev_review_diff','jev_verify','jev_rerank'}
    if name in auto_names or name=='jev_evaluate':
        if caller is None:
            try:ensure(path)
            except AutoError as error:
                if name=='jev_evaluate' and str(error)=='WORKSPACE_NOT_ENROLLED':return legacy.call(name,args,scope='global-hybrid')
                raise
        call=caller or (lambda obj:request(path,obj))
        if name=='jev_auto_status':
            status={'health':call({'op':'health'}),'usage':call({'op':'stats'})}
            # Present only when a Claude Code organization policy restricts the plugin.
            policy=(claude_policy or _claude_policy)()
            return {**status,'claude_code_policy':policy} if policy else status
        if name=='jev_prepare':return call({'op':'prepare','goal':args['goal']})
        if name=='jev_reduce':return call({'op':'sieve','goal':args['goal'],'text':args['text'],'tool':'explicit_reduce'})
        if name=='jev_recall':return call({'op':'recall','receipt_id':args['receipt_id'],'start':args.get('start',1),'end':args.get('end',120)})
        if name=='jev_typed_decide':return call({'op':'typed_query','state':args['state'],'questions':args['questions'],
                                                 'provider':args['provider'],'data_classification':args['data_classification']})
        if name=='jev_route':return call({'op':'route','kind':args['kind'],'task':args['task'],
                                          'candidates':args['candidates'],'data_classification':args['data_classification']})
        if name=='jev_recipe_try':return call({'op':'recipe_try','recipe_id':args['recipe_id'],
                                               'input':args['input'],'data_classification':args['data_classification']})
        if name=='jev_verify':return call({'op':'verify','source_text':args['source_text'],
                                           'extraction':args['extraction'],'threshold':args.get('threshold',0.70),
                                           'data_classification':args['data_classification']})
        if name=='jev_rerank':return call({'op':'rerank','query':args['query'],'memories':args['memories'],
                                           'data_classification':args['data_classification']})
        if name=='jev_review_diff':return call({'op':'review_diff','goal':args['goal'],'diff':args['diff'],
                                                'data_classification':args['data_classification']})
        return call({'op':'evaluate','case_id':args['case_id'],'state':args['state']})
    if name=='jev_recipe_catalog':
        from .recipe_runtime import _catalog,catalog_preview
        if 'recipe_id' not in args:return catalog_preview()
        # Read-only: the fields jev_recipe_try needs, and what the recipe cannot do.
        recipe=next((item for item in _catalog() if item['id']==args['recipe_id']),None)
        if recipe is None:raise AutoError('RECIPE_NOT_FOUND')
        return {'status':recipe['status'],'id':recipe['id'],'title':recipe['title'],'audience':recipe['audience'],
                'input_schema':recipe['input_schema'],'limitations':recipe['limitations']}
    if name=='jev_recipe_selftest':
        # Offline by construction: no workspace, no enrollment, no provider.
        from .recipe_fixtures import run_fixture, selftest
        if 'recipe_id' in args:return run_fixture(args['recipe_id'],args.get('variant','nominal'))
        if 'variant' in args:raise AutoError('MCP_ARGUMENTS')
        return selftest()
    return legacy.call(name,args,scope='global-hybrid')

def _drain_oversized_line(stream):
    """Restore framing after an oversized request, with finite time and memory."""
    remaining=4_194_304
    while remaining>0:
        chunk=stream.readline(min(65_536,remaining))
        if not chunk:return True
        if chunk.endswith(b'\n'):return True
        remaining-=len(chunk)
    return False

def serve():
    from jevkit import mcp_server as legacy
    initialized=False
    while True:
        line=sys.stdin.buffer.readline(512_001)
        if not line:break
        rid=None
        synchronized=True
        try:
            if len(line)>512000:
                if not line.endswith(b'\n'):
                    synchronized=_drain_oversized_line(sys.stdin.buffer)
                raise AutoError('MCP_MESSAGE_SIZE')
            req=decode(line)
            if not isinstance(req,dict) or req.get('jsonrpc')!='2.0':raise AutoError('MCP_REQUEST')
            if 'id' not in req:continue
            rid=req['id'];method=req.get('method');params=req.get('params') or {}
            if not isinstance(params,dict):raise AutoError('MCP_PARAMS')
            if method=='initialize':
                v=params.get('protocolVersion');initialized=True
                result={'protocolVersion':v if v in VERSIONS else VERSIONS[0],
                        'serverInfo':{'name':'qualixar-jev','version':__version__},'capabilities':{'tools':{'listChanged':False}},
                        'instructions':'Enrolled workspaces use standing Jev Decision Layer authority. Use compact recommendations; detailed receipts are local. Other memory and browser tools are unchanged. Never create grants yourself.'}
            elif method=='ping':result={}
            elif not initialized:raise AutoError('MCP_INITIALIZE_FIRST')
            elif method=='tools/list':result={'tools':definitions(legacy)}
            elif method=='tools/call':
                try:
                    output=dispatch(params.get('name'),params.get('arguments',{}),legacy)
                    result={'content':[{'type':'text','text':canonical(output).decode()}],'isError':False}
                except Exception as e:
                    safe=str(e) if isinstance(e,AutoError) else 'JEV_TOOL_UNAVAILABLE'
                    content=[{'type':'text','text':safe}]
                    if safe in _HINTS:content.append({'type':'text','text':'Hint: '+_HINTS[safe]})
                    result={'content':content,'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
            out={'jsonrpc':'2.0','id':rid,'result':result}
        except AutoError as e:out={'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':str(e)}}
        except Exception:out={'jsonrpc':'2.0','id':rid,'error':{'code':-32603,'message':'Internal error'}}
        print(canonical(out).decode(),flush=True)
        if not synchronized:break
