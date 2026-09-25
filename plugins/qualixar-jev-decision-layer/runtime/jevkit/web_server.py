"""Read-only loopback evidence viewer. Explicit resource allowlist, no credential access."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
from .engine import ROOT,catalog
from .security import load_json,screen,SafeError,canonical

def records(root=ROOT):
    runs=[]
    folder=root/'artifacts'/'runs'
    if folder.is_symlink():return []
    for file in sorted(folder.glob('*.json'),reverse=True)[:300]:
        try:
            r=load_json(file)
            if not isinstance(r,dict) or r.get('schema_version')!=1:continue
            # Custom-source evidence is intentionally never served by this viewer.
            if r.get('data_classification')!='synthetic' or r.get('mode') not in ('fixture','live'):continue
            clean,findings=screen(r)
            if not findings:runs.append(clean)
        except SafeError:continue
    return runs

def make_server(port=8765,root=ROOT):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,data,kind='application/json'):
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(data)
        def safe_origin(self):
            hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            return self.headers.get('Host') in hosts and (not self.headers.get('Origin') or self.headers['Origin'] in {'http://'+h for h in hosts})
        def do_GET(self):
            if not self.safe_origin():return self.send(403,b'{"error":"Origin or Host rejected"}')
            path=urlsplit(self.path).path
            if path=='/api/data':return self.send(200,canonical({'catalog':catalog(root),'runs':records(root),'read_only':True,'custom_data_hidden':True}))
            files={'/':('index.html','text/html'),'/index.html':('index.html','text/html'),'/app.js':('app.js','application/javascript'),'/styles.css':('styles.css','text/css')}
            if path not in files:return self.send(404,b'{"error":"Not found"}')
            file,kind=files[path];p=root/'web'/file
            if p.is_symlink():return self.send(404,b'{"error":"Not found"}')
            return self.send(200,p.read_bytes(),kind)
        do_HEAD=do_GET
        def do_POST(self):self.send(405,b'{"error":"Read-only viewer"}')
        do_PUT=do_POST;do_DELETE=do_POST;do_PATCH=do_POST
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)

def serve(port=8765):
    if not 1024<=port<=65535:raise SafeError('INVALID_PORT')
    server=make_server(port)
    print(f'Local read-only viewer: http://127.0.0.1:{port} — Ctrl-C to stop.',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
