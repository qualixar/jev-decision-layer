"""Standing permission belongs to a workspace, not to its current Git status."""
from __future__ import annotations
import ctypes
import errno
import os
import platform
import tempfile
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlsplit
from .common import AutoError, canonical, number, private_dir, read_private, safe_path, state_dir, workspace, workspace_id, write_private
from .platform_fs import atomic_write_private, file_lock

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

DESCENDANT_COVERAGE_APPROVED = "DESCENDANT_COVERAGE_APPROVED"
_BLOCKED_DESCENDANT_ROOTS = frozenset({
    "/", "/Users", "/home", "/private", "/private/tmp", "/private/var",
    "/tmp", "/var", "/opt", "/Volumes", "/System", "/Applications",
})


def descendant_root_allowed(root: Path) -> bool:
    """A descendant grant may not attach to a home directory or a filesystem top."""
    try:
        resolved = Path(root).resolve()
    except OSError:
        return False
    if resolved == Path.home().resolve() or resolved == Path(resolved.anchor):
        return False
    # Any user's home is a home: /Users/<name> and /home/<name> are homes too,
    # not just the current user's. The "home" segment can sit deeper after
    # symlink resolution (macOS: /home -> /System/Volumes/Data/home), so match
    # a home segment with exactly one child anywhere in the resolved path.
    # Children of temp tops (/tmp/<child>) stay allowed by design: they are
    # scoped scratch dirs, not filesystem tops.
    parts = resolved.parts
    if any(
        segment in ("Users", "home") and len(parts) == index + 2
        for index, segment in enumerate(parts)
    ):
        return False
    if str(resolved) in _BLOCKED_DESCENDANT_ROOTS or len(resolved.parts) < 3:
        return False
    return True


def make_policy(path, provider, days=30, **overrides):
    if provider not in PROVIDERS or not isinstance(days,int) or not 1<=days<=365: raise AutoError('POLICY_CONFIG')
    covers = overrides.get('covers_descendants', False)
    if covers is True:
        if overrides.get('descendant_approval') != DESCENDANT_COVERAGE_APPROVED:
            raise AutoError('DESCENDANT_APPROVAL_REQUIRED')
        approved = workspace(path)
        if not descendant_root_allowed(approved):
            raise AutoError('DESCENDANT_ROOT_NOT_ALLOWED')
        overrides = {**overrides, 'workspace_path': str(approved)}
    elif covers is not False:
        raise AutoError('POLICY_CONFIG')
    elif 'descendant_approval' in overrides or 'workspace_path' in overrides:
        raise AutoError('POLICY_CONFIG')
    else:
        overrides = {key: value for key, value in overrides.items() if key != 'covers_descendants'}
    p={**DEFAULTS,**overrides,'workspace_id':workspace_id(path),'provider':provider,
       'policy_id':uuid.uuid4().hex,'expires_at':time.time()+days*86400}
    validate_policy(p,path); return p

def validate_policy(p,path,now=None):
    now=time.time() if now is None else now
    if not isinstance(p,dict) or p.get('schema_version')!=1 or p.get('provider') not in PROVIDERS: raise AutoError('POLICY_CONFIG')
    if p.get('workspace_id') != workspace_id(path): raise AutoError('POLICY_WORKSPACE_MISMATCH')
    if 'covers_descendants' in p or 'descendant_approval' in p or 'workspace_path' in p:
        if p.get('covers_descendants') is not True or p.get('descendant_approval') != DESCENDANT_COVERAGE_APPROVED:
            raise AutoError('DESCENDANT_APPROVAL_REQUIRED' if p.get('covers_descendants') is True else 'POLICY_CONFIG')
        raw_root = p.get('workspace_path')
        if not isinstance(raw_root, str):
            raise AutoError('POLICY_CONFIG')
        try:
            approved = workspace(path)
            if safe_path(Path(raw_root)).resolve() != approved:
                raise AutoError('POLICY_WORKSPACE_MISMATCH')
        except AutoError:
            raise
        except (OSError, ValueError):
            raise AutoError('POLICY_CONFIG') from None
        if not descendant_root_allowed(approved):
            raise AutoError('DESCENDANT_ROOT_NOT_ALLOWED')
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
    if p.get('credential_store','legacy') not in ('legacy','keychain','os'): raise AutoError('CREDENTIAL_STORE')
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

def _existing_file(path) -> bool:
    """True when `path` resolves to an existing file (symlinks already rejected)."""
    try:
        return safe_path(Path(path)).resolve().is_file()
    except (AutoError, OSError, ValueError):
        return False


def _read_exact_file(path, base=None):
    return read_private(state_dir(path, base) / 'policy.json', 16_000)


def _load_exact(path, base=None):
    try:
        policy = _read_exact_file(path, base)
    except FileNotFoundError:
        raise AutoError('WORKSPACE_NOT_ENROLLED') from None
    return validate_policy(policy, path)


def _ancestor_may_cover(parent: Path) -> bool:
    try:
        resolved = parent.resolve()
    except OSError:
        return False
    if resolved == Path.home().resolve() or resolved == Path(resolved.anchor):
        return False
    return str(resolved) not in _BLOCKED_DESCENDANT_ROOTS


def _strictly_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return child.resolve() != parent.resolve()


def _inherited_root(path, base=None):
    """Nearest explicit descendant grant. Exact child consent is handled earlier."""
    try:
        requested = safe_path(Path(path)).resolve()
    except (AutoError, OSError, ValueError):
        return None
    # Files (and not-yet-created child paths) resolve through the nearest
    # existing directory so coverage does not depend on path existence shape.
    anchor = requested if requested.is_dir() else None
    if anchor is None:
        for candidate in requested.parents:
            try:
                if candidate.is_dir():
                    anchor = candidate
                    break
            except OSError:
                return None
        if anchor is None:
            return None
    try:
        start = workspace(anchor)
    except (AutoError, OSError, ValueError):
        return None
    seen: set[str] = set()
    for parent in start.parents:
        if not _ancestor_may_cover(parent):
            break
        try:
            identity = workspace(parent)
        except AutoError:
            continue
        key = str(identity)
        if key in seen or identity == start:
            continue
        seen.add(key)
        try:
            policy = _load_exact(identity, base)
        except AutoError:
            continue
        if policy.get('covers_descendants') is not True:
            continue
        raw_root = policy.get('workspace_path')
        if not isinstance(raw_root, str):
            continue
        try:
            grant = safe_path(Path(raw_root)).resolve()
        except (AutoError, OSError, ValueError):
            continue
        if grant != identity or not descendant_root_allowed(grant):
            continue
        if _strictly_inside(requested, grant):
            return identity
    return None


def _expired_not_withdrawn(path, base=None) -> bool:
    """True only for an exact grant that is still enabled and has passed its expiry.

    A revoked grant (`enabled` false) and a recorded refusal are withdrawals and
    must keep blocking an ancestor. A malformed expiry is not a plain expiry.
    """
    try:
        policy = _read_exact_file(path, base)
    except (AutoError, OSError, ValueError):
        return False
    if not isinstance(policy, dict) or policy.get('enabled') is not True or policy.get('consent') == 'refused':
        return False
    expires = policy.get('expires_at')
    return number(expires, 0, 10**12) and expires <= time.time()


def enrollment_binding(path, base=None):
    """Exact consent wins. A disabled or refused child is never reopened by a parent.

    An exact grant that merely expired falls through to an approved ancestor:
    the person approved that ancestor's coverage, and letting a stale child
    grant switch the folder off contradicts it.
    """
    try:
        policy = _load_exact(path, base)
    except AutoError as error:
        code = str(error)
        if code == 'WORKSPACE_NOT_ENROLLED':
            pass
        elif code == 'WORKSPACE_REQUIRED' and _existing_file(path):
            # A file inside a workspace is not an exact grant; resolve it as
            # a descendant candidate instead of rejecting it outright.
            pass
        elif code == 'AUTO_DISABLED_OR_EXPIRED' and _expired_not_withdrawn(path, base):
            root = _inherited_root(path, base)
            if root is None:
                raise
            return {'scope': 'descendant', 'workspace': root, 'policy': _load_exact(root, base)}
        else:
            raise
    else:
        return {'scope': 'exact', 'workspace': workspace(path), 'policy': policy}
    root = _inherited_root(path, base)
    if root is None:
        raise AutoError('WORKSPACE_NOT_ENROLLED')
    return {'scope': 'descendant', 'workspace': root, 'policy': _load_exact(root, base)}


def enrollment_state(path, base=None):
    """Classify consent for advisory surfaces without changing any decision.

    Returns `enrolled` (with the binding), `not_enrolled`, `expired`, or
    `withdrawn`. Any other failure is raised unchanged.
    """
    try:
        binding = enrollment_binding(path, base)
    except AutoError as error:
        code = str(error)
        if code == 'WORKSPACE_NOT_ENROLLED':
            return {'state': 'not_enrolled'}
        if code == 'AUTO_DISABLED_OR_EXPIRED':
            return {'state': 'expired' if _expired_not_withdrawn(path, base) else 'withdrawn'}
        raise
    return {'state': 'enrolled', **binding}


def load_policy(path,base=None):
    return enrollment_binding(path, base)['policy']


def governing_workspace(path, base=None) -> Path:
    """Workspace whose grant, budget, and broker actually apply to `path`."""
    return enrollment_binding(path, base)['workspace']

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
    try:
        with file_lock(lock_path):
            yield
    except AutoError as error:
        if str(error)=='UNSAFE_PRIVATE_FILE':raise AutoError('POLICY_LOCK_UNSAFE') from None
        raise

def _rename_exclusive(source,destination):
    if os.name=='nt':raise AutoError('WINDOWS_PRIVATE_STATE_UNVERIFIED')
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
        if os.name=='nt':
            atomic_write_private(destination,canonical(p)+b'\n',replace=False)
            return
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

def _write_descendant_refusal(path, base=None):
    """Record local non-consent without changing the ancestor grant."""
    policy = make_policy(path, 'typesafe', days=1)
    policy['enabled'] = False
    policy['consent'] = 'refused'
    with _policy_lock(path, base):
        destination = state_dir(path, base) / 'policy.json'
        try:
            _read_exact_file(path, base)
        except FileNotFoundError:
            write_private(destination, policy)
            return
        raise AutoError('WORKSPACE_ALREADY_ENROLLED')


def revoke(path,base=None):
    try:
        _read_exact_file(path, base)
    except FileNotFoundError:
        if _inherited_root(path, base) is None:
            raise AutoError('WORKSPACE_NOT_ENROLLED')
        _write_descendant_refusal(path, base)
        return
    with _policy_lock(path,base):
        f=state_dir(path,base)/'policy.json'
        p=read_private(f,16_000);p['enabled']=False;write_private(f,p)
