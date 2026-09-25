"""Standing permission belongs to a workspace, not to its current Git status."""
from __future__ import annotations
import ctypes
import errno
import os
import platform
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit
from .common import AutoError, canonical, number, private_dir, read_private, state_dir, workspace_id, write_private

PROVIDERS=('typesafe','openrouter','laya-mlx')
DEFAULTS={
 'schema_version':1,'version':'1.0.0','enabled':True,
 'max_calls_per_day':1000,'max_bytes_per_day':20_000_000,'max_request_bytes':48_000,
 'timeout_seconds':12,'cache_seconds':600,'retention_days':7,
 'drop_probability':0.10,'min_reduction':0.20,'min_chars':2000,
 'max_blocks':48,'block_lines':25,'native_output_rewrite':True,
 'prepare_context':True,'browser_origins':[],'browser_max_steps':10,
 'data_classification':'public','case_ids':[],
 'generic_query_enabled':False,
 'auto_prepare_jev':False,
 'local_laya_enabled':False,
 'decision_mode':None,
 'credential_store':'legacy',
 'local_recipe_ids':['sieve','prepare','browser','probe'],
}

def make_policy(path, provider, days=30, **overrides):
    if provider not in PROVIDERS or not isinstance(days,int) or not 1<=days<=365: raise AutoError('POLICY_CONFIG')
    p={**DEFAULTS,**overrides,'workspace_id':workspace_id(path),'provider':provider,
       'policy_id':uuid.uuid4().hex,'expires_at':time.time()+days*86400}
    validate_policy(p,path); return p

def validate_policy(p,path,now=None):
    now=time.time() if now is None else now
    if not isinstance(p,dict) or p.get('schema_version')!=1 or p.get('provider') not in PROVIDERS: raise AutoError('POLICY_CONFIG')
    if p.get('workspace_id') != workspace_id(path): raise AutoError('POLICY_WORKSPACE_MISMATCH')
    if p.get('enabled') is not True or not number(p.get('expires_at'),0,10**12) or p['expires_at']<=now: raise AutoError('AUTO_DISABLED_OR_EXPIRED')
    for name,lo,hi in [('max_calls_per_day',1,100_000),('max_bytes_per_day',1000,10**10),('max_request_bytes',1000,100_000),('max_blocks',1,48),('block_lines',1,100),('min_chars',500,100_000),('browser_max_steps',1,30),('retention_days',1,30)]:
        if not isinstance(p.get(name),int) or isinstance(p[name],bool) or not lo<=p[name]<=hi: raise AutoError('POLICY_RANGE')
    for name,lo,hi in [('timeout_seconds',1,30),('cache_seconds',0,3600),('drop_probability',0,.2),('min_reduction',0,1)]:
        if not number(p.get(name),lo,hi): raise AutoError('POLICY_RANGE')
    if p.get('data_classification') not in ('public','internal-minimized','restricted'): raise AutoError('DATA_CLASSIFICATION')
    mode=p.get('decision_mode')
    if mode is not None:
        shapes={'jev-public':('public',False),'jev-internal':('internal-minimized',False),
                'jev-maximum':('restricted',False),'hybrid':('internal-minimized',True),
                'laya-only':('restricted',False)}
        if mode not in shapes or (p['data_classification'],p['local_laya_enabled'])!=shapes[mode] or (p['provider']=='laya-mlx')!=(mode=='laya-only'):
            raise AutoError('DECISION_MODE_INVALID')
    if p.get('credential_store','legacy') not in ('legacy','keychain'): raise AutoError('CREDENTIAL_STORE')
    for name in ('native_output_rewrite','prepare_context','auto_prepare_jev'):
        if not isinstance(p.get(name),bool): raise AutoError('POLICY_BOOLEAN')
    if not isinstance(p.get('local_laya_enabled',False),bool):raise AutoError('POLICY_BOOLEAN')
    if not isinstance(p.get('generic_query_enabled',False),bool): raise AutoError('POLICY_BOOLEAN')
    if p['auto_prepare_jev'] and not p['generic_query_enabled']:raise AutoError('AUTO_PREPARE_REQUIRES_GENERIC_CONSENT')
    if p['auto_prepare_jev'] and p['provider']=='laya-mlx':raise AutoError('AUTO_PREPARE_HOSTED_ONLY')
    if p.get('local_laya_enabled',False) and (p['provider']=='laya-mlx' or not isinstance(p.get('mlx'),dict)):
        raise AutoError('LOCAL_ROUTE_NOT_ATTESTED')
    for name in ('case_ids','local_recipe_ids','browser_origins'):
        if not isinstance(p.get(name),list) or len(p[name])>100 or not all(isinstance(x,str) for x in p[name]): raise AutoError('POLICY_LIST')
    for value in p['browser_origins']:
        u=urlsplit(value)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment: raise AutoError('BROWSER_ORIGIN')
    routes=p.get('routes',{})
    if not isinstance(routes,dict) or any(not isinstance(k,str) or v not in PROVIDERS for k,v in routes.items()):raise AutoError('POLICY_ROUTES')
    if mode is not None:
        allowed={p['provider'],'laya-mlx'} if mode=='hybrid' else {p['provider']}
        if any(value not in allowed for value in routes.values()):raise AutoError('DECISION_MODE_ROUTE_CONFLICT')
    if len(canonical(p))>16_000: raise AutoError('POLICY_SIZE')
    return p

def load_policy(path,base=None):
    f=state_dir(path,base)/'policy.json'
    try: p=read_private(f,16_000)
    except FileNotFoundError: raise AutoError('WORKSPACE_NOT_ENROLLED') from None
    return validate_policy(p,path)

def save_policy(path,p,base=None):
    validate_policy(p,path)
    with _policy_lock(path,base):write_private(state_dir(path,base)/'policy.json',p)

def replace_reviewed_policy(path,expected,replacement,base=None):
    """Atomically apply a reviewed wizard change without reviving a revoked grant."""
    validate_policy(replacement,path)
    with _policy_lock(path,base):
        destination=state_dir(path,base)/'policy.json'
        try:current=read_private(destination,16_000)
        except (AutoError,OSError):raise AutoError('SETUP_RECOVERY_CONFLICT') from None
        if current!=expected or current.get('setup_origin')!='local_wizard' or current.get('setup_state')!='ready' or current.get('enabled') is not True:
            raise AutoError('SETUP_RECOVERY_CONFLICT')
        write_private(destination,replacement)

@contextmanager
def _policy_lock(path,base=None):
    root=private_dir(state_dir(path,base));lock_path=root/'.policy.lock'
    descriptor=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or metadata.st_mode&0o077 or (hasattr(os,'getuid') and metadata.st_uid!=os.getuid()):raise AutoError('POLICY_LOCK_UNSAFE')
        if os.name=='nt':
            import msvcrt
            if metadata.st_size==0:os.write(descriptor,b'0')
            os.lseek(descriptor,0,os.SEEK_SET);msvcrt.locking(descriptor,msvcrt.LK_LOCK,1)
            try:yield
            finally:os.lseek(descriptor,0,os.SEEK_SET);msvcrt.locking(descriptor,msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(descriptor,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally:os.close(descriptor)

def _rename_exclusive(source,destination):
    if platform.system()=='Darwin':
        libc=ctypes.CDLL(None,use_errno=True)
        operation=libc.renamex_np
        operation.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        operation.restype=ctypes.c_int
        if operation(os.fsencode(source),os.fsencode(destination),0x00000004)!=0:
            if ctypes.get_errno()==errno.EEXIST:raise AutoError('WORKSPACE_ALREADY_ENROLLED')
            raise AutoError('POLICY_WRITE_FAILED')
        return
    try:
        os.link(source,destination,follow_symlinks=False)
    except FileExistsError:raise AutoError('WORKSPACE_ALREADY_ENROLLED') from None
    except OSError:raise AutoError('POLICY_WRITE_FAILED') from None

def save_policy_new(path,p,base=None):
    """Create a first-run policy without ever replacing a pre-existing grant."""
    validate_policy(p,path)
    with _policy_lock(path,base):
        root=state_dir(path,base);destination=root/'policy.json'
        descriptor,temporary=tempfile.mkstemp(prefix='.policy-new-',dir=root)
        try:
            os.fchmod(descriptor,0o600)
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(canonical(p)+b'\n')
                stream.flush();os.fsync(stream.fileno())
            _rename_exclusive(temporary,destination)
            directory_fd=os.open(root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        except AutoError:raise
        except OSError:raise AutoError('POLICY_WRITE_FAILED') from None
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

def transition_setup_policy_ready(path,expected_pending,base=None):
    """Only promote the exact pending wizard policy; never undo a revoke."""
    with _policy_lock(path,base):
        destination=state_dir(path,base)/'policy.json'
        try:current=read_private(destination,16_000)
        except (AutoError,OSError):raise AutoError('SETUP_RECOVERY_CONFLICT') from None
        if (current!=expected_pending or current.get('setup_origin')!='local_wizard'
            or current.get('setup_state')!='pending' or not current.get('setup_choice_digest')):
            raise AutoError('SETUP_RECOVERY_CONFLICT')
        ready={**current,'setup_state':'ready'}
        validate_policy(ready,path)
        write_private(destination,ready)

def revoke(path,base=None):
    with _policy_lock(path,base):
        f=state_dir(path,base)/'policy.json'
        p=read_private(f,16_000);p['enabled']=False;write_private(f,p)
