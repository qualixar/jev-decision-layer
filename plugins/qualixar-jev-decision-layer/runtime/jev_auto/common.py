"""Finite JSON, workspace identity and private local storage. No network here."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

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

def _link_is_trusted(link: Path) -> bool:
    """Only this user or the system could have made or replaced `link`.

    The link and the folder holding it belong to this user or root, and
    nobody else can write that folder (a sticky folder such as /tmp keeps
    others from replacing an entry they do not own).
    """
    if os.name == 'nt' or not hasattr(os, 'getuid'):
        return False
    try:
        own = os.lstat(link)
        holder = os.stat(link.parent)
    except OSError:
        return False
    mine = (0, os.getuid())
    if own.st_uid not in mine or holder.st_uid not in mine:
        return False
    return not (holder.st_mode & 0o022) or bool(holder.st_mode & stat.S_ISVTX)

def trusted_path(path: Path, *, allow_link_leaf: bool = False) -> Path:
    """Like `safe_path`, but a symlinked folder on the way is accepted when
    only this user or the system could have made or replaced it.

    The last component must not be a symlink unless `allow_link_leaf` (a
    workspace opened through a link). Returns the absolute, unresolved path.
    """
    p = path.expanduser().absolute()
    for component in (p, *p.parents):
        if component.is_symlink():
            if (component == p and not allow_link_leaf) or not _link_is_trusted(component):
                raise AutoError('SYMLINK_NOT_ALLOWED')
    return p

def private_dir(path: Path) -> Path:
    from .platform_fs import ensure_private_dir
    return ensure_private_dir(path)

def read_private(path: Path, limit=512_000):
    from .platform_fs import open_private_file
    fd = open_private_file(path, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if st.st_size > limit:
            raise AutoError('UNSAFE_PRIVATE_FILE')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            return decode(f.read(limit + 1), limit)
    finally:
        os.close(fd)

def write_private(path: Path, value):
    from .platform_fs import atomic_write_private
    atomic_write_private(path, canonical(value) + b'\n', replace=True)

def workspace(path: str | Path) -> Path:
    # A host can hand us any string. An embedded NUL makes lstat raise a bare
    # ValueError, which is not this module's typed error and escapes callers
    # that catch AutoError. A malformed path is simply not a workspace.
    # (An unpaired surrogate already lands on WORKSPACE_REQUIRED below; this
    # closes the one shape that did not.)
    try:
        p = trusted_path(Path(path), allow_link_leaf=True).resolve()
    except (ValueError, UnicodeError):
        raise AutoError('WORKSPACE_REQUIRED') from None
    if not p.is_dir(): raise AutoError('WORKSPACE_REQUIRED')
    p = on_disk(p)
    root = _repository_root(p)
    return root if root is not None else p

def on_disk(path: Path) -> Path:
    """`path` as the filesystem spells it: its letter case and Unicode form.

    macOS volumes ignore letter case by default, so `client-x` and `Client-X`
    are one folder; identity must not depend on how it was typed. Elsewhere,
    or when the path cannot be opened, it is returned unchanged.
    """
    if sys.platform != 'darwin' or fcntl is None or not hasattr(fcntl, 'F_GETPATH'):
        return path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    except (OSError, TypeError, ValueError):
        return path
    try:
        spelled = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0]
        return Path(os.fsdecode(spelled)) if spelled.startswith(b'/') else path
    except OSError:
        return path
    finally:
        os.close(fd)

_ROOTS: dict[str, tuple[float, Path | None]] = {}
_ROOT_SECONDS = 30.0
_ROOT_ENTRIES = 512

def _repository_root(p: Path) -> Path | None:
    """The git top level holding `p`, or None. Remembered briefly per process.

    Hooks resolve the same folders many times; each lookup used to start a
    `git` process. Git is asked only when a `.git` entry exists at or above
    `p` (or git is pointed elsewhere by the environment), and an answer is
    kept for 30 seconds. The symlink check in `workspace` still runs on
    every call.
    """
    key = str(p)
    now = time.monotonic()
    hit = _ROOTS.get(key)
    if hit is not None and now - hit[0] < _ROOT_SECONDS:
        return hit[1]
    root = None
    steered = any(name in os.environ for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_CEILING_DIRECTORIES'))
    if steered or any(os.path.lexists(folder / '.git') for folder in (p, *p.parents)):
        try:
            r = subprocess.run(['git', '-C', key, 'rev-parse', '--show-toplevel'],
                               capture_output=True, timeout=3, check=False)
            if r.returncode == 0:
                candidate = Path(r.stdout.decode().strip()).resolve()
                p.relative_to(candidate)
                root = candidate
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            root = None
    if len(_ROOTS) >= _ROOT_ENTRIES:
        _ROOTS.clear()
    _ROOTS[key] = (now, root)
    return root

def workspace_id(path: str | Path) -> str:
    p = workspace(path); st = p.stat()
    return digest({'path': str(p), 'device': st.st_dev, 'inode': st.st_ino})[:24]

def home_root() -> Path:
    from .platform_fs import user_state_root
    return user_state_root()

def state_dir(path: str | Path, base: Path | None = None) -> Path:
    return (base or home_root()) / workspace_id(path)

# Contact, workspace-path and private-network patterns. Credential formats
# live in secret_rules, which every outgoing screen shares.
SENSITIVE = [
    ('PRIVATE_URL', re.compile(r'https?://[^\s"<>]{0,2048}?(?:\.internal|\.local|localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])', re.I)),
    ('EMAIL', re.compile(r'\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b')),
    ('PRIVATE_IP', re.compile(r'\b(?:10(?:\.\d{1,3}){3}|127(?:\.\d{1,3}){3}|0\.0\.0\.0|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b')),
    ('HOME_PATH', re.compile(r'(?i)(?:(?:/Users/|/home/)|(?:[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+[\\/])Users[\\/])[^\s"<>]+')),
]

_CONTEXT_FIELDS = frozenset({'EMAIL', 'HOME_PATH'})

def _child_place(where, key):
    """Where a nested value sits: only questions.<name>.criteria holds option labels."""
    if where == 'root' and key == 'questions':
        return 'questions'
    if where == 'questions':
        return 'question'
    if where == 'question' and key == 'criteria':
        return 'labels'
    return 'other'

def screen(value, secrets=(), *, allow_context=False):
    """Scan original strings, not escaped JSON. Return labels only, never matches.

    Contact and workspace paths may be sent only by callers that already
    checked an enrolled non-public data scope. Credential formats always apply.
    """
    from .secret_rules import credential_field, find, variants
    canonical(value)  # reject recursive or nonfinite input before traversing
    findings=set()
    def visit(item,where='root'):
        if isinstance(item,dict):
            for key,nested in item.items():
                # The option labels of a choice question (questions.<name>.criteria)
                # are categories, not credentials.
                if where!='labels' and credential_field(str(key),nested):findings.add('CREDENTIAL')
                visit(str(key));visit(nested,_child_place(where,key))
        elif isinstance(item,list):
            for nested in item:visit(nested)
        elif isinstance(item,str):
            findings.update(find(item))
            for form in variants(item):
                for label,regex in SENSITIVE:
                    if allow_context and label in _CONTEXT_FIELDS:continue
                    if regex.search(form):findings.add(label)
                if any(secret and len(secret)>=4 and secret in form for secret in secrets):findings.add('ACTIVE_KEY')
    visit(value)
    return sorted(findings)

def require_clean(value, secrets=(), *, allow_context=False):
    if screen(value, secrets, allow_context=allow_context): raise AutoError('SENSITIVE_PAYLOAD_NOT_SENT')
