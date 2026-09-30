"""Resident decision process, shared by hooks, MCP and the existing-browser bridge."""
from __future__ import annotations
import argparse, os, signal, socketserver, sqlite3, threading, time
from pathlib import Path
from .common import AutoError, canonical, private_dir, state_dir
from .engine import Engine
from .ipc import address,authenticate_unix_peer,read_message
from .platform_fs import file_lock
from .store import _damaged

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
        except sqlite3.DatabaseError as e:
            # A receipts file damaged while running: stop, so the next start
            # sets it aside and replaces it.
            if _damaged(e):server.stopping.set();out={'ok':False,'error':'BROKER_STORE_DAMAGED'}
            else:out={'ok':False,'error':'BROKER_INTERNAL_ERROR'}
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
        self.refusals=threading.BoundedSemaphore(16)
        super().__init__(str(addr),Handler);self.timeout=.25;os.chmod(addr,0o600)
    def process_request(self,request,address):
        if not self.slots.acquire(False):
            # Read the request before refusing it: a refusal written to a
            # client that is still sending is lost, and the client would
            # report the broker as unavailable. A refused request never ran.
            if self.refusals.acquire(False):
                threading.Thread(target=self._refuse,args=(request,),daemon=True).start()
            else:
                self.shutdown_request(request)
            return
        super().process_request(request,address)
    def _refuse(self,request):
        try:
            request.settimeout(2)
            try:authenticate_unix_peer(request);read_message(request)
            except (AutoError,OSError):pass
            try:request.sendall(canonical({'ok':False,'error':'BROKER_BUSY'})+b'\n')
            except OSError:pass
        finally:
            self.shutdown_request(request);self.refusals.release()
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

PRUNE_EVERY_SECONDS=3600

def prune_if_due(engine,last,now,every=PRUNE_EVERY_SECONDS):
    """Retention holds while a broker stays busy, not only when one starts."""
    if now-last<every:return last
    try:engine.prune()
    except Exception:pass  # a revoked or expired grant has nothing to prune by
    return now

def serve(path,base=None,idle_seconds=900):
    if os.name=='nt':raise AutoError('WINDOWS_UNSUPPORTED_IN_1_0_8')
    root=private_dir(state_dir(path,base));lock=root/'broker.lock'
    if lock.is_symlink():raise AutoError('UNSAFE_BROKER_LOCK')
    with file_lock(lock,blocking=False) as acquired:
        if not acquired:return
        # The Windows named-pipe broker (transports.windows_pipe) is closed
        # since 1.0.8 by the refusal above. Its dispatch lived here, dormant
        # and unreachable, until 1.0.11; restore it from history when the
        # Windows private-state contract is revalidated.
        addr=address(path,base)
        if addr.exists():
            if addr.is_symlink():raise AutoError('UNSAFE_SOCKET')
            addr.unlink()
        engine=Engine(path,base)
        with Server(addr,engine) as server:
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM,lambda *_:server.stopping.set())
                signal.signal(signal.SIGINT,lambda *_:server.stopping.set())
            last_prune=time.monotonic()
            while not server.stopping.is_set() and time.monotonic()-server.last_activity<idle_seconds:
                server.handle_request()
                last_prune=prune_if_due(engine,last_prune,time.monotonic())
        engine.providers.close()
        if addr.exists():addr.unlink()

def main():
    p=argparse.ArgumentParser();p.add_argument('--workspace',required=True);p.add_argument('--state-base',type=Path)
    a=p.parse_args();serve(a.workspace,a.state_base)
if __name__=='__main__':main()
