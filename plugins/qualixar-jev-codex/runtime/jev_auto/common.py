"""Finite JSON, workspace identity and private local storage. No network here."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

class AutoError(Exception):
    """Only fixed safe codes are exposed; provider exception bodies are not."""

def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise AutoError('INVALID_FINITE_JSON') from None

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def decode(data: bytes | str, limit: int = 512_000) -> Any:
    if len(data) > limit:
        raise AutoError('MESSAGE_TOO_LARGE')
    def reject(_): raise AutoError('NONFINITE_JSON')
    try:
        return json.loads(data, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError):
        raise AutoError('INVALID_JSON') from None

def number(v, low=0, high=1):
    return isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and low <= v <= high

def safe_path(path: Path) -> Path:
    p = path.expanduser().absolute()
    for component in (p, *p.parents):
        if component.is_symlink():
            if component == Path('/var') and component.resolve() == Path('/private/var'):
                continue
            raise AutoError('SYMLINK_NOT_ALLOWED')
    return p

def private_dir(path: Path) -> Path:
    p = safe_path(path)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = p.stat()
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, 'getuid') and st.st_uid != os.getuid()):
        raise AutoError('PRIVATE_DIRECTORY_OWNER')
    p.chmod(0o700)
    return p

def read_private(path: Path, limit=512_000):
    p = safe_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(p, flags)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077
            or (hasattr(os, 'getuid') and st.st_uid != os.getuid()) or st.st_size > limit):
            raise AutoError('UNSAFE_PRIVATE_FILE')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            return decode(f.read(limit + 1), limit)
    finally:
        os.close(fd)

def write_private(path: Path, value):
    p = safe_path(path); private_dir(p.parent)
    fd, name = tempfile.mkstemp(prefix='.auto-', dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(canonical(value) + b'\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, p)
    finally:
        if os.path.exists(name): os.unlink(name)

def workspace(path: str | Path) -> Path:
    # A host can hand us any string. An embedded NUL makes lstat raise a bare
    # ValueError, which is not this module's typed error and escapes callers
    # that catch AutoError. A malformed path is simply not a workspace.
    # (An unpaired surrogate already lands on WORKSPACE_REQUIRED below; this
    # closes the one shape that did not.)
    try:
        p = safe_path(Path(path)).resolve()
    except (ValueError, UnicodeError):
        raise AutoError('WORKSPACE_REQUIRED') from None
    if not p.is_dir(): raise AutoError('WORKSPACE_REQUIRED')
    try:
        r = subprocess.run(['git', '-C', str(p), 'rev-parse', '--show-toplevel'],
                           capture_output=True, timeout=3, check=False)
        if r.returncode == 0:
            root = Path(r.stdout.decode().strip()).resolve()
            p.relative_to(root); return root
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        pass
    return p

def workspace_id(path: str | Path) -> str:
    p = workspace(path); st = p.stat()
    return digest({'path': str(p), 'device': st.st_dev, 'inode': st.st_ino})[:24]

def home_root() -> Path:
    # Operational storage; this is not an OS isolation boundary against the same UID.
    base = os.environ.get('XDG_STATE_HOME')
    return (Path(base).expanduser() if base else Path.home()/'.local'/'state')/'qualixar-jev-decision-layer'

def state_dir(path: str | Path, base: Path | None = None) -> Path:
    return (base or home_root()) / workspace_id(path)

SENSITIVE = [
    ('PRIVATE_KEY', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('CREDENTIAL', re.compile(r'(?i)\b(?:api[_-]?key|password|secret|access[_-]?token|authorization)\s*[:=]\s*["\']?[^\s,;"\']{6,}')),
    ('TOKEN', re.compile(r'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b')),
    ('BEARER', re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}')),
    ('PRIVATE_URL', re.compile(r'https?://[^\s"<>]*(?:\.internal|\.local|localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])[^\s"<>]*', re.I)),
    ('EMAIL', re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')),
    ('PRIVATE_IP', re.compile(r'\b(?:10(?:\.\d{1,3}){3}|127(?:\.\d{1,3}){3}|0\.0\.0\.0|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b')),
    ('HOME_PATH', re.compile(r'(?:/Users/|/home/)[^\s"<>]+')),
]

def screen(value, secrets=()):
    """Scan original strings, not escaped JSON. Return labels only, never matches."""
    canonical(value)  # reject recursive or nonfinite input before traversing
    findings=set()
    sensitive_key=re.compile(r"(?i)^(?:[a-z0-9]+[_-])*(?:api[_-]?key|password|secret|access[_-]?token|authorization|credential|private[_-]?key)$")
    def visit(item):
        if isinstance(item,dict):
            for key,nested in item.items():
                if sensitive_key.match(str(key)) and nested:findings.add('CREDENTIAL')
                visit(str(key));visit(nested)
        elif isinstance(item,list):
            for nested in item:visit(nested)
        elif isinstance(item,str):
            for label,regex in SENSITIVE:
                if regex.search(item):findings.add(label)
            if any(secret and len(secret)>=4 and secret in item for secret in secrets):findings.add('ACTIVE_KEY')
    visit(value)
    return sorted(findings)

def require_clean(value, secrets=()):
    if screen(value, secrets): raise AutoError('SENSITIVE_PAYLOAD_NOT_SENT')
