"""Private peer-authenticated local broker IPC; never a TCP listener."""
from __future__ import annotations
import ctypes, os, socket, stat, struct, subprocess, sys, tempfile, time
from pathlib import Path
from .common import AutoError, canonical, decode, digest, private_dir, state_dir, workspace, workspace_id
from .platform_fs import file_lock
from .settings import load_policy

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
    root=private_dir(temp_root/leaf)
    identity=digest({'workspace':workspace_id(path),'state':str(state_dir(path,base))})[:24]
    addr=root/(identity+'.sock')
    if len(str(addr).encode())>100:raise AutoError('SOCKET_PATH_TOO_LONG')
    return addr

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

def request(path,obj,base=None,timeout=16):
    if not isinstance(obj,dict):raise AutoError('IPC_REQUEST_INVALID')
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
    try:
        st=addr.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_mode & 0o077 or st.st_uid!=os.getuid():raise AutoError('UNSAFE_SOCKET')
        data=canonical(obj)+b'\n'
        if len(data)>MAX:raise AutoError('IPC_MESSAGE_SIZE')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(timeout);s.connect(str(addr));authenticate_unix_peer(s);s.sendall(data);result=read_message(s)
    except (FileNotFoundError,ConnectionRefusedError,TimeoutError,OSError):
        raise AutoError('BROKER_UNAVAILABLE') from None
    if not isinstance(result,dict) or not result.get('ok'):raise AutoError(result.get('error','BROKER_ERROR') if isinstance(result,dict) else 'BROKER_ERROR')
    return result['result']

def ensure(path,base=None):
    load_policy(path,base)
    try:
        ready=request(path,{'op':'health'},base,timeout=1)
        if ready.get('version')!='1.0.0':raise AutoError('BROKER_VERSION_MISMATCH')
        return
    except AutoError as e:
        if str(e) not in ('BROKER_UNAVAILABLE',):raise
    root=private_dir(state_dir(path,base));lock=root/'start.lock'
    if lock.is_symlink():raise AutoError('UNSAFE_START_LOCK')
    with file_lock(lock):
        try:request(path,{'op':'health'},base,timeout=1);return
        except AutoError:pass
        env=dict(os.environ);env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]);env['PYTHONDONTWRITEBYTECODE']='1'
        args=[sys.executable,'-m','jev_auto.server','--workspace',str(workspace(path))]
        if base is not None:args+=['--state-base',str(base)]
        spawn={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,
               'stderr':subprocess.DEVNULL,'env':env}
        if os.name=='nt':
            spawn['creationflags']=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS)
        else:
            spawn['start_new_session']=True
        subprocess.Popen(args,**spawn)
        for _ in range(60 if os.name=='nt' else 30):
            time.sleep(.1)
            try:request(path,{'op':'health'},base,timeout=.3);return
            except AutoError:pass
        raise AutoError('BROKER_START_FAILED')
