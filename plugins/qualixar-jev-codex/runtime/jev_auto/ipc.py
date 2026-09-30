"""Private peer-authenticated local broker IPC; never a TCP listener."""
from __future__ import annotations
import ctypes, os, re, secrets, socket, stat, struct, subprocess, sys, tempfile, time
from pathlib import Path
from .common import AutoError, canonical, decode, digest, private_dir, state_dir, workspace, workspace_id, write_private
from .platform_fs import file_lock
from .settings import governing_workspace, load_policy

MAX=512_000

def address(path,base=None):
    if os.name=='nt':
        from .transports.windows_pipe import current_identity
        _,logon_sid=current_identity()
        identity=digest({'workspace':workspace_id(path),'state':str(state_dir(path,base))})[:24]
        logon=digest({'logon_sid':logon_sid})[:16]
        return r'\\.\pipe\qualixar-jev-'+logon+'-'+identity+'-v1'
    uid=os.getuid() if hasattr(os,'getuid') else 0
    # macOS's per-user TMPDIR can exceed the Unix socket path limit by itself.
    temp_root=Path('/private/tmp') if sys.platform=='darwin' else Path(tempfile.gettempdir()).resolve()
    leaf=f'qualixar-jev-decision-layer-{uid}'
    if sys.platform.startswith('linux'):
        runtime=os.environ.get('XDG_RUNTIME_DIR')
        if runtime:
            candidate=Path(runtime)
            try:
                st=candidate.lstat()
                if stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid() and not st.st_mode & 0o077:
                    temp_root=candidate
                    leaf='qualixar-jev-decision-layer'
            except OSError:
                pass
    try:
        root=private_dir(temp_root/leaf)
    except AutoError as error:
        if str(error)!='PRIVATE_DIRECTORY_OWNER':raise
        # Another account created the shared name first.
        root=_private_socket_folder(temp_root,uid)
    identity=digest({'workspace':workspace_id(path),'state':str(state_dir(path,base))})[:24]
    addr=root/(identity+'.sock')
    if len(str(addr).encode())>100:raise AutoError('SOCKET_PATH_TOO_LONG')
    return addr

def _darwin_user_temp():
    """This user's own temporary folder on macOS, which other accounts cannot list."""
    try:
        libc=ctypes.CDLL(None,use_errno=True);buffer=ctypes.create_string_buffer(1024)
        if libc.confstr(65537,buffer,1024)<=0:return None  # _CS_DARWIN_USER_TEMP_DIR
        folder=Path(os.fsdecode(buffer.value)).resolve()
        st=folder.stat()
        return folder if stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid() and not st.st_mode&0o077 else None
    except (OSError,AttributeError,ValueError):
        return None

def _private_socket_folder(temp_root,uid):
    if sys.platform=='darwin':
        folder=_darwin_user_temp()
        if folder is not None:return private_dir(folder/'qj')
    try:
        return private_dir(temp_root/_private_leaf(uid))
    except AutoError as error:
        if str(error)!='PRIVATE_DIRECTORY_OWNER':raise
        # The shared folder is listable, so a recorded name can be taken after
        # a cleanup. Choose and record a new one.
        return private_dir(temp_root/_private_leaf(uid,renew=True))

def _private_leaf(uid,renew=False):
    from .common import home_root, read_private
    from .platform_fs import atomic_write_private
    record=private_dir(home_root())/'socket-folder.json'
    if renew:
        leaf=f'qualixar-jev-{uid}-{secrets.token_hex(8)}'
        atomic_write_private(record,canonical({'leaf':leaf})+b'\n',replace=True)
        return leaf
    for _ in range(2):
        try:
            leaf=read_private(record,1000).get('leaf')
            if isinstance(leaf,str) and re.fullmatch(rf'qualixar-jev-{uid}-[0-9a-f]{{16}}',leaf):return leaf
            raise AutoError('SOCKET_FOLDER_RECORD_INVALID')
        except FileNotFoundError:
            try:atomic_write_private(record,canonical({'leaf':f'qualixar-jev-{uid}-{secrets.token_hex(8)}'})+b'\n',replace=False)
            except AutoError as error:
                if str(error)!='WORKSPACE_ALREADY_ENROLLED':raise  # a parallel process wrote it first
    raise AutoError('SOCKET_FOLDER_RECORD_INVALID')

def authenticate_unix_peer(sock):
    """Ask the kernel for the connected peer's effective user ID on both sides."""
    if sys.platform.startswith('linux') and hasattr(socket,'SO_PEERCRED'):
        try:
            _,uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        except (OSError,struct.error):
            raise AutoError('IPC_PEER_UNVERIFIED') from None
    elif sys.platform=='darwin':
        libc=ctypes.CDLL(None,use_errno=True)
        getpeereid=getattr(libc,'getpeereid',None)
        if getpeereid is None:raise AutoError('IPC_PEER_UNVERIFIED')
        getpeereid.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype=ctypes.c_int
        uid=ctypes.c_uint();gid=ctypes.c_uint()
        if getpeereid(sock.fileno(),ctypes.byref(uid),ctypes.byref(gid))!=0:
            raise AutoError('IPC_PEER_UNVERIFIED')
        uid=uid.value
    else:
        raise AutoError('IPC_PEER_UNVERIFIED')
    if uid!=os.geteuid():raise AutoError('IPC_PEER_UID')

def read_message(sock):
    data=bytearray()
    while b'\n' not in data:
        chunk=sock.recv(min(65536,MAX+1-len(data)))
        if not chunk:raise AutoError('IPC_EOF')
        data.extend(chunk)
        if len(data)>MAX:raise AutoError('IPC_MESSAGE_SIZE')
    line,extra=bytes(data).split(b'\n',1)
    if extra.strip():raise AutoError('IPC_ONE_REQUEST_PER_CONNECTION')
    return decode(line,MAX)

def _ipc_workspace(path, base=None):
    """Follow an ancestor grant, but do not invent one for an unenrolled path."""
    try:
        return governing_workspace(path, base)
    except AutoError as error:
        if str(error) == 'WORKSPACE_NOT_ENROLLED':
            return workspace(path)
        raise


def call_timeout(path, base=None):
    """How long a client waits: two provider attempts plus local readiness.

    A decision may be retried once when the provider's answer is malformed,
    and a local model may first wait to become ready.
    """
    try:
        seconds = load_policy(path, base)['timeout_seconds']
    except (AutoError, OSError, KeyError, TypeError):
        return 16
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 1 <= seconds <= 30:
        return 16
    return min(2 * seconds + 12, 75)


def request(path,obj,base=None,timeout=None):
    # Absolute, as the caller meant it: the broker runs in another folder.
    requested = os.path.abspath(os.path.expanduser(os.fspath(path)))
    path = _ipc_workspace(path, base)
    if timeout is None:
        timeout = call_timeout(path, base)
    if not isinstance(obj,dict):raise AutoError('IPC_REQUEST_INVALID')
    # Name the folder the request is for: the broker serves the grant root and
    # re-checks that folder's consent right before any provider call.
    obj={**obj,'requested_path':str(requested)}
    if os.name=='nt':
        from .transports.windows_pipe import request as pipe_request
        endpoint=address(path,base)
        def unpack(envelope):
            if not isinstance(envelope,dict) or not envelope.get('ok'):
                raise AutoError(envelope.get('error','BROKER_ERROR') if isinstance(envelope,dict) else 'BROKER_ERROR')
            return envelope['result']
        if obj.get('op')!='health':
            ready=unpack(pipe_request(endpoint,{'op':'health'},min(timeout,1)))
            if not isinstance(ready,dict) or ready.get('version')!='1.0.0':
                raise AutoError('BROKER_VERSION_MISMATCH')
        return unpack(pipe_request(endpoint,obj,timeout))
    addr=address(path,base)
    data=canonical(obj)+b'\n'
    if len(data)>MAX:raise AutoError('IPC_MESSAGE_SIZE')
    deadline=time.monotonic()+timeout
    pause=.1
    while True:
        result=_exchange(addr,data,max(deadline-time.monotonic(),.05))
        busy=isinstance(result,dict) and result.get('ok') is False and result.get('error')=='BROKER_BUSY'
        # A busy broker refused the request before running it, so waiting and
        # asking again cannot charge twice.
        if not busy or time.monotonic()+pause>=deadline:break
        time.sleep(pause);pause=min(pause*2,1.0)
    if not isinstance(result,dict) or not result.get('ok'):raise AutoError(result.get('error','BROKER_ERROR') if isinstance(result,dict) else 'BROKER_ERROR')
    return result['result']

def _exchange(addr,data,timeout):
    """One request. Unavailable before it was sent; a timeout after it was sent.

    A timeout after sending means the broker may still run the request, and a
    provider may charge for it: retrying at once could charge twice.
    """
    try:
        st=addr.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_mode & 0o077 or st.st_uid!=os.getuid():raise AutoError('UNSAFE_SOCKET')
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    except (FileNotFoundError,ConnectionRefusedError,TimeoutError,OSError):
        raise AutoError('BROKER_UNAVAILABLE') from None
    with s:
        try:
            s.settimeout(timeout);s.connect(str(addr));authenticate_unix_peer(s);s.sendall(data)
        except (FileNotFoundError,ConnectionRefusedError,TimeoutError,OSError):
            raise AutoError('BROKER_UNAVAILABLE') from None
        try:
            return read_message(s)
        except TimeoutError:
            raise AutoError('BROKER_TIMEOUT') from None
        except OSError:
            raise AutoError('BROKER_UNAVAILABLE') from None

def ensure(path,base=None):
    policy = load_policy(path,base)
    # Descendant coverage stores the approved root on the policy. Retarget
    # from that field so a covered child shares the root broker. An exact
    # policy, and a test double that does not return one, keeps `path`.
    if isinstance(policy, dict) and policy.get('covers_descendants') is True:
        grant = policy.get('workspace_path')
        if isinstance(grant, str) and grant:
            path = grant
    try:
        ready=request(path,{'op':'health'},base,timeout=1)
        if ready.get('version')!='1.0.0':raise AutoError('BROKER_VERSION_MISMATCH')
        return
    except AutoError as e:
        # A busy service is running: the request itself waits for a free slot.
        if str(e)=='BROKER_BUSY':return
        if str(e) not in ('BROKER_UNAVAILABLE','BROKER_TIMEOUT'):raise
    root=private_dir(state_dir(path,base));lock=root/'start.lock';failed=root/'start-failed'
    if lock.is_symlink():raise AutoError('UNSAFE_START_LOCK')
    with file_lock(lock):
        try:request(path,{'op':'health'},base,timeout=1);return
        except AutoError:pass
        # A start that just failed will fail again: say so at once instead of
        # making every hook wait out the start timeout.
        # Seconds after a failure, fail at once; later in the minute, try again
        # unless a service still holds the lock.
        age=_failure_age(failed)
        if age is not None and (age<START_FAST_FAIL_SECONDS or _broker_running(root)):raise AutoError('BROKER_START_FAILED')
        env=dict(os.environ);env['PYTHONDONTWRITEBYTECODE']='1'
        # Isolated (-I -S -B) and started from its own private folder: the
        # folder the host runs in, PYTHONPATH and site-packages never reach the
        # broker's imports. `-m` would put the working folder first on the path.
        runtime=str(Path(__file__).resolve().parents[1])
        args=[sys.executable,'-I','-S','-B','-c',
              'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
              'sys.argv=["jev_auto.server",*sys.argv[2:]]; runpy.run_module("jev_auto.server",run_name="__main__")',
              runtime,'--workspace',str(workspace(path))]
        if base is not None:args+=['--state-base',str(base)]
        spawn={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,
               'stderr':subprocess.DEVNULL,'env':env,'cwd':str(root)}
        if os.name=='nt':
            spawn['creationflags']=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS)
        else:
            spawn['start_new_session']=True
        subprocess.Popen(args,**spawn)
        for _ in range(60 if os.name=='nt' else 30):
            time.sleep(.1)
            try:request(path,{'op':'health'},base,timeout=.3)
            except AutoError:continue
            if failed.exists() and not failed.is_symlink():failed.unlink()
            return
        write_private(failed,{'failed_at':time.time()})
        raise AutoError('BROKER_START_FAILED')

START_RETRY_SECONDS=60
START_FAST_FAIL_SECONDS=10

def _failure_age(marker):
    """Seconds since a start failed within the last minute, or None."""
    try:
        st=marker.lstat()
    except FileNotFoundError:
        return None
    age=time.time()-st.st_mtime
    return age if stat.S_ISREG(st.st_mode) and 0<=age<START_RETRY_SECONDS else None

def _failed_recently(marker):
    return _failure_age(marker) is not None

def _broker_running(root):
    """A process holds this folder's broker lock: a start now would only wait.

    If nobody holds it, the earlier failure has passed, and a start is tried.
    Starts happen only under the start lock, so this probe cannot race one.
    """
    lock=root/'broker.lock'
    if lock.is_symlink() or not lock.exists():return False
    with file_lock(lock,blocking=False) as acquired:
        return not acquired
