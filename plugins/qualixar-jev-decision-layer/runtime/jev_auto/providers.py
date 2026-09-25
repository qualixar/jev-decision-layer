"""Fixed destination Jev transport, or explicit resident Laya-MLX. No silent fallback."""
from __future__ import annotations
import http.client
import ssl
import threading
import time
from .common import AutoError, canonical, decode, require_clean
from .protocol import validate_response

ENDPOINTS={
 'typesafe':('api.typesafe.ai','/v1/systemone','jev-1.13.0'),
 'openrouter':('openrouter.ai','/api/alpha/decisions','typesafe/jev-1.13'),
}
class Providers:
    def __init__(self):self._threads=threading.local();self._connections=set();self._connections_lock=threading.Lock();self._mlx=None;self._mlx_identity=None;self._mlx_lock=threading.Lock();self._ready=threading.Event();self._warming=False;self._warm_error=None
    def remote(self,p,state,qs):
        from jevkit.providers import get_provider_credential, provider_profile
        name=p['provider'];host,path,model=ENDPOINTS[name]
        profile=provider_profile(name)
        if profile.endpoint != 'https://'+host+path or profile.model != model:raise AutoError('PROVIDER_PROFILE_CHANGED')
        store=p.get('credential_store','legacy')
        if store not in ('legacy','keychain'):raise AutoError('CREDENTIAL_STORE')
        key=get_provider_credential(profile,credential_store='keychain') if store=='keychain' else get_provider_credential(profile)
        payload={'model':model,'state':state,'questions':qs};require_clean(payload,(key,))
        data=canonical(payload);deadline=time.monotonic()+p['timeout_seconds']
        connection=getattr(self._threads,name,None)
        if connection is None:
            connection=http.client.HTTPSConnection(host,timeout=p['timeout_seconds'],context=ssl.create_default_context())
            setattr(self._threads,name,connection)
            with self._connections_lock:self._connections.add(connection)
        try:
            connection.timeout=p['timeout_seconds']
            if connection.sock:connection.sock.settimeout(p['timeout_seconds'])
            connection.request('POST',path,body=data,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','User-Agent':'Qualixar-Jev-Decision-Layer/1.0.0'})
            response=connection.getresponse()
            if response.status!=200:
                code=response.status;response.close();connection.close();setattr(self._threads,name,None)
                raise AutoError('PROVIDER_HTTP_'+str(code))
            chunks=[];total=0
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise AutoError('PROVIDER_TIMEOUT')
                if connection.sock:connection.sock.settimeout(remaining)
                part=response.read1(min(65536,1_000_001-total))
                if not part:break
                chunks.append(part);total+=len(part)
                if total>1_000_000:raise AutoError('PROVIDER_RESPONSE_SIZE')
            raw=decode(b''.join(chunks),1_000_000)
            require_clean(raw,(key,))
            result=validate_response(raw,qs,model)
            result['provenance']={'provider':name,'model_requested':model,'confidence_kind':'provider-reported'}
            return result
        except AutoError:
            connection.close();setattr(self._threads,name,None)
            with self._connections_lock:self._connections.discard(connection)
            raise
        except Exception:
            connection.close();setattr(self._threads,name,None)
            with self._connections_lock:self._connections.discard(connection)
            # An attempted request may have been billed. Never retry an ambiguous failure.
            raise AutoError('PROVIDER_TRANSPORT_FAILURE_NO_RETRY') from None
    def warmup(self,p,wait=False):
        from .mlx_process import MLXProcess
        from .common import digest
        cfg=p.get('mlx')
        if not isinstance(cfg,dict):raise AutoError('MLX_NOT_CONFIGURED')
        identity=digest(cfg)
        with self._mlx_lock:
            if self._mlx is None or self._mlx_identity!=identity:
                if self._warming:raise AutoError('MLX_CONFIGURATION_CHANGE_DURING_WARMUP')
                if self._mlx:self._mlx.close()
                self._mlx=MLXProcess(cfg);self._mlx_identity=identity
                self._ready=threading.Event();self._warm_error=None;self._warming=False
            if not self._ready.is_set() and not self._warming:
                self._warming=True;worker=self._mlx;ready=self._ready
                def load():
                    error=None
                    try:worker.warmup()
                    except Exception:error='MLX_WARMUP_FAILED'
                    with self._mlx_lock:
                        if worker is self._mlx:
                            self._warm_error=error;self._warming=False
                        ready.set()
                threading.Thread(target=load,daemon=True).start()
            ready=self._ready
        if wait and not ready.wait(125):raise AutoError('MLX_WARMUP_TIMEOUT')
        if self._warm_error:raise AutoError(self._warm_error)
        return {'ready':ready.is_set(),'provider':'laya-mlx'}
    def ready_for_request(self,p):
        if p['provider']=='laya-mlx':
            self.warmup(p)
            # After an idle broker restart, the first local call should wait a
            # bounded time for the resident worker rather than require a manual
            # warmup command. Leave room inside the default 16s IPC timeout.
            if not self._ready.wait(min(p['timeout_seconds'],10)):
                raise AutoError('MLX_WARMING_UP_USE_NORMAL_CODEX')
            if self._warm_error:raise AutoError(self._warm_error)
    def local(self,p,state,qs):
        self.warmup(p)
        if not self._ready.is_set():raise AutoError('MLX_WARMING_UP_USE_NORMAL_CODEX')
        cfg=p['mlx']
        with self._mlx_lock:
            try:raw=self._mlx.predict(state,qs,p['timeout_seconds'])
            except AutoError:
                if self._mlx.proc is None:
                    self._ready.clear();self._warm_error=None
                raise
            telemetry=getattr(self._mlx,'last_telemetry',None)
        result=validate_response(raw,qs,cfg['repository'])
        # The worker reports its repository name. Its pinned artifact was
        # verified before launch; expose that revision-bound identity to the
        # typed broker instead of accepting an unpinned model alias.
        result['model']='laya-mlx@'+cfg['revision']
        result['provenance']={'provider':'laya-mlx','model_requested':cfg['repository'],'model_revision':cfg['revision'],
          'weight_sha256':cfg['weight_sha256'],'confidence_kind':'pinned MLX entropy/temperature; not Jev calibration'}
        if isinstance(telemetry,dict):
            keys=('cold','cold_load_ms','inference_ms','runtime_default_device','actual_device','device_source',
                  'device_status','offline_guard','checkpoint_revision','checkpoint_sha256','resident_calls')
            result['provenance']['telemetry']={name:telemetry[name] for name in keys if name in telemetry}
        return result
    def evaluate(self,p,state,qs):
        return self.local(p,state,qs) if p['provider']=='laya-mlx' else self.remote(p,state,qs)
    def close(self):
        if self._mlx:self._mlx.close()
        with self._connections_lock:
            connections=set(self._connections);self._connections.clear()
        for name in ENDPOINTS:
            connection=getattr(self._threads,name,None)
            if connection is not None:connections.add(connection)
        for connection in connections:connection.close()
        for name in ENDPOINTS:setattr(self._threads,name,None)
