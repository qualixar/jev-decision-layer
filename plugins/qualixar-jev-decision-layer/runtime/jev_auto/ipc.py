"""Private same-user Unix socket; no HTTP listener or credential-bearing browser calls."""
from __future__ import annotations
import fcntl, os, socket, stat, subprocess, sys, tempfile, time
from pathlib import Path
from .common import AutoError, canonical, decode, digest, private_dir, state_dir, workspace, workspace_id
from .settings import load_policy

MAX=512_000

def address(path,base=None):
    uid=os.getuid() if hasattr(os,'getuid') else 0
    # macOS's per-user TMPDIR can exceed the Unix socket path limit by itself.
    temp_root=Path('/private/tmp') if sys.platform=='darwin' else Path(tempfile.gettempdir()).resolve()
    root=private_dir(temp_root/f'qualixar-jev-decision-layer-{uid}')
    identity=digest({'workspace':workspace_id(path),'state':str(state_dir(path,base))})[:24]
    addr=root/(identity+'.sock')
    if len(str(addr).encode())>100:raise AutoError('SOCKET_PATH_TOO_LONG')
    return addr

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
    addr=address(path,base)
    try:
        st=addr.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_mode & 0o077 or st.st_uid!=os.getuid():raise AutoError('UNSAFE_SOCKET')
        data=canonical(obj)+b'\n'
        if len(data)>MAX:raise AutoError('IPC_MESSAGE_SIZE')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(timeout);s.connect(str(addr));s.sendall(data);result=read_message(s)
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
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        try:request(path,{'op':'health'},base,timeout=1);return
        except AutoError:pass
        env=dict(os.environ);env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]);env['PYTHONDONTWRITEBYTECODE']='1'
        args=[sys.executable,'-m','jev_auto.server','--workspace',str(workspace(path))]
        if base is not None:args+=['--state-base',str(base)]
        subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         env=env,start_new_session=True)
        for _ in range(30):
            time.sleep(.1)
            try:request(path,{'op':'health'},base,timeout=.3);return
            except AutoError:pass
        raise AutoError('BROKER_START_FAILED')
    finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
