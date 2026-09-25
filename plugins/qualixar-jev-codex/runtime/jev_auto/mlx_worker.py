"""Actual laya_mlx integration, intended to run only in the dedicated Mac venv."""
from __future__ import annotations
import contextlib, hashlib, importlib.metadata, json, os, platform, stat, sys
from pathlib import Path
from .common import AutoError, canonical, decode, read_private, safe_path
from .mlx_preflight import preflight

def _hash_file(path):
    path=safe_path(Path(path))
    try:descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    except OSError:raise AutoError('MLX_ARTIFACT_UNSAFE') from None
    try:
        before=os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise AutoError('MLX_ARTIFACT_UNSAFE')
        digest=hashlib.sha256()
        while part:=os.read(descriptor,1<<20):digest.update(part)
        after=os.fstat(descriptor);named=path.lstat()
        identity=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(named):raise AutoError('MLX_ARTIFACT_CHANGED')
        return digest.hexdigest()
    finally:os.close(descriptor)

def main():
    agent=None;cfg=None
    for raw in sys.stdin.buffer:
        try:
            req=decode(raw,150_000)
            if req.get('kind')=='load':
                cfg=req['config']
                if platform.system()!='Darwin' or platform.machine()!='arm64':raise AutoError('MLX_APPLE_SILICON_REQUIRED')
                folder=Path(cfg['model_dir'])
                if not folder.is_dir():raise AutoError('MLX_MODEL_DIRECTORY')
                # Pin non-weight files too: enrollment supplies the installer-generated manifest.
                expected_manifest=cfg.get('manifest_sha256')
                if not isinstance(expected_manifest,str) or len(expected_manifest)!=64:raise AutoError('MLX_MANIFEST_UNBOUND')
                manifest_path=Path(cfg['artifact_manifest'])
                if _hash_file(manifest_path)!=expected_manifest:raise AutoError('MLX_MANIFEST_CHANGED')
                manifest=read_private(Path(cfg['artifact_manifest']),100_000)
                if _hash_file(manifest_path)!=expected_manifest:raise AutoError('MLX_MANIFEST_CHANGED')
                if manifest.get('files',{}).get('model.safetensors')!=cfg['weight_sha256']:raise AutoError('MLX_WEIGHT_HASH')
                for name,sha in manifest['files'].items():
                    rel=Path(name)
                    if rel.is_absolute() or '..' in rel.parts:raise AutoError('MLX_ARTIFACT_PATH')
                    artifact=safe_path(folder/rel)
                    if _hash_file(artifact)!=sha:raise AutoError('MLX_ARTIFACT_HASH')
                import laya_mlx
                import mlx.core as mlx
                with contextlib.redirect_stdout(sys.stderr):
                    agent=laya_mlx.load(str(folder),dtype='float16',batch_size=16,compile=False,cache_prompts=True)
                for name,sha in manifest['files'].items():
                    if _hash_file(folder/name)!=sha:raise AutoError('MLX_ARTIFACT_CHANGED_DURING_LOAD')
                if _hash_file(manifest_path)!=expected_manifest:raise AutoError('MLX_MANIFEST_CHANGED_DURING_LOAD')
                runtime_device = str(mlx.default_device()).lower()
                if 'gpu' in runtime_device and 'cpu' not in runtime_device:
                    device = 'gpu'
                elif 'cpu' in runtime_device and 'gpu' not in runtime_device:
                    device = 'cpu'
                else:
                    raise AutoError('MLX_DEVICE_UNKNOWN')
                result={'ready':True,'runtime_version':importlib.metadata.version('laya-mlx'),'checkpoint':cfg['repository'],
                        'default_device':device,'device_source':'mlx.core.default_device_after_load'}
            elif req.get('kind')=='predict' and agent is not None:
                preflight(agent.tok,agent.cfg,req['state'],req['questions'])
                with contextlib.redirect_stdout(sys.stderr):raw_result=agent.predict(req['state'],req['questions'])
                # The artifact is verified above. Do not leak its absolute local path as model name.
                result={**raw_result,'model':cfg['repository']}
            else:raise AutoError('MLX_LOAD_FIRST')
            message={'ok':True,'result':result}
        except AutoError as e:message={'ok':False,'error':str(e)}
        except Exception:message={'ok':False,'error':'MLX_RUNTIME_FAILURE'}
        print(canonical(message).decode(),flush=True)
if __name__=='__main__':main()
