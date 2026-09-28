"""Resident decision process, shared by hooks, MCP and the existing-browser bridge."""
from __future__ import annotations
import argparse, os, signal, socketserver, threading, time
from pathlib import Path
from .common import AutoError, canonical, private_dir, state_dir
from .engine import Engine
from .ipc import address,authenticate_unix_peer,read_message
from .platform_fs import file_lock

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(20);server=self.server;server.last_activity=time.monotonic()
        try:
            authenticate_unix_peer(self.request)
            req=read_message(self.request)
            if req.get('op')=='shutdown':
                server.stopping.set();result={'stopping':True}
            else:result=server.engine.dispatch(req)
            out={'ok':True,'result':result}
        except AutoError as e:out={'ok':False,'error':str(e)}
        except Exception:out={'ok':False,'error':'BROKER_INTERNAL_ERROR'}
        try:self.request.sendall(canonical(out)+b'\n')
        except (BrokenPipeError,OSError):pass
        server.last_activity=time.monotonic()

if hasattr(socketserver,'UnixStreamServer'):
    _UnixStreamServer=socketserver.UnixStreamServer
else:
    class _UnixStreamServer:
        """Import-only placeholder; Windows uses the named-pipe path below."""

class Server(socketserver.ThreadingMixIn,_UnixStreamServer):
    daemon_threads=True
    def __init__(self,addr,engine):
        if os.name=='nt':raise AutoError('WINDOWS_PIPE_UNVERIFIED')
        self.engine=engine;self.stopping=threading.Event();self.last_activity=time.monotonic();self.slots=threading.BoundedSemaphore(8)
        super().__init__(str(addr),Handler);self.timeout=.25;os.chmod(addr,0o600)
    def process_request(self,request,address):
        if not self.slots.acquire(False):
            request.sendall(canonical({'ok':False,'error':'BROKER_BUSY'})+b'\n');request.close();return
        super().process_request(request,address)
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

def serve(path,base=None,idle_seconds=900):
    if os.name=='nt':raise AutoError('WINDOWS_UNSUPPORTED_IN_1_0_8')
    root=private_dir(state_dir(path,base));lock=root/'broker.lock'
    if lock.is_symlink():raise AutoError('UNSAFE_BROKER_LOCK')
    with file_lock(lock,blocking=False) as acquired:
        if not acquired:return
        if os.name=='nt':
            from .transports.windows_pipe import serve as serve_pipe
            engine=Engine(path,base)
            try:
                serve_pipe(address(path,base),engine,threading.Event(),idle_seconds)
            finally:
                engine.providers.close()
            return
        addr=address(path,base)
        if addr.exists():
            if addr.is_symlink():raise AutoError('UNSAFE_SOCKET')
            addr.unlink()
        engine=Engine(path,base)
        with Server(addr,engine) as server:
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM,lambda *_:server.stopping.set())
                signal.signal(signal.SIGINT,lambda *_:server.stopping.set())
            while not server.stopping.is_set() and time.monotonic()-server.last_activity<idle_seconds:
                server.handle_request()
        engine.providers.close()
        if addr.exists():addr.unlink()

def main():
    p=argparse.ArgumentParser();p.add_argument('--workspace',required=True);p.add_argument('--state-base',type=Path)
    a=p.parse_args();serve(a.workspace,a.state_base)
if __name__=='__main__':main()
