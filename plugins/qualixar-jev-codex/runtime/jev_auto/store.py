"""Transactional budgets and content-addressed cache. Same-user local state only."""
from __future__ import annotations
import contextlib, datetime, os, re, sqlite3, threading, time
from pathlib import Path
from .common import AutoError, canonical, decode, digest, private_dir, trusted_path
from .platform_fs import open_private_file, verify_private_dir

def _damaged(error):
    """SQLite's own verdict that a file is not a readable database."""
    code=getattr(error,'sqlite_errorcode',None)
    if code is not None:return code in (11,26)  # SQLITE_CORRUPT, SQLITE_NOTADB
    return not isinstance(error,sqlite3.OperationalError) and any(
        text in str(error) for text in ('file is not a database','malformed'))

class Store:
    def __init__(self,root):
        self.root=private_dir(Path(root));self.path=self.root/'auto.sqlite3';trusted_path(self.path)
        if os.name == 'nt':
            self._prepare_windows_database()
        elif self.path.exists():
            s=self.path.stat()
            if s.st_mode & 0o077 or s.st_nlink!=1 or (hasattr(os,'getuid') and s.st_uid!=os.getuid()): raise AutoError('UNSAFE_DATABASE')
        try:
            self._create_schema()
        except sqlite3.DatabaseError as error:
            if os.name=='nt' or not _damaged(error):raise
            # A crash or a full disk mid-write can leave a file SQLite cannot
            # read. Keep it aside, still private, and start a fresh one.
            self._set_aside()
            self._create_schema()
            self.event('store_reset',{'reason':'damaged_database'})

    def _set_aside(self):
        suffix='.corrupt-'+str(int(time.time()))
        for path in (self.path,Path(str(self.path)+'-journal')):
            if path.exists() and not path.is_symlink():os.replace(path,str(path)+suffix)

    def _create_schema(self):
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS budgets(day TEXT,policy TEXT,calls INTEGER,bytes INTEGER,PRIMARY KEY(day,policy));
            CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY,expires REAL,body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(k TEXT PRIMARY KEY,created REAL,body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS goals(session TEXT PRIMARY KEY,created REAL,text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created REAL,kind TEXT,body BLOB NOT NULL);
            ''')

    def _verify_windows_database(self):
        """Reject unsafe SQLite files before recovery or any query can read them.

        WAL/SHM are rejected even when private: this store uses rollback journaling,
        and silently ignoring an old WAL could lose uncheckpointed transactions.
        """
        verify_private_dir(self.root)
        for suffix in ('-wal', '-shm'):
            if os.path.lexists(str(self.path) + suffix):
                raise AutoError('UNSAFE_DATABASE')
        for path in (self.path, Path(str(self.path) + '-journal')):
            if not os.path.lexists(path):
                raise AutoError('UNSAFE_DATABASE')
            fd = open_private_file(path, os.O_RDONLY)
            os.close(fd)

    def _prepare_windows_database(self):
        verify_private_dir(self.root)
        for suffix in ('-wal', '-shm'):
            if os.path.lexists(str(self.path) + suffix):
                raise AutoError('UNSAFE_DATABASE')
        # SQLite opens paths itself, so pre-create both files with native
        # protected DACLs. PERSIST mode keeps the rollback journal in place.
        for path in (self.path, Path(str(self.path) + '-journal')):
            if not os.path.lexists(path):
                try:
                    fd = open_private_file(path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                except AutoError:
                    # Another host process may have created it since lexists.
                    # Reopen through the native verifier; any other failure
                    # still fails closed without replacing existing state.
                    if not os.path.lexists(path):
                        raise
                    fd = open_private_file(path, os.O_RDONLY)
                os.close(fd)
        self._verify_windows_database()

    @contextlib.contextmanager
    def connection(self):
        trusted_path(self.path)
        if os.name == 'nt':
            self._verify_windows_database()
        old=os.umask(0o077)
        try:c=sqlite3.connect(self.path,timeout=5)
        finally:os.umask(old)
        if os.name == 'nt':
            try:
                mode=c.execute('PRAGMA journal_mode=PERSIST').fetchone()[0]
                if mode.lower() != 'persist': raise AutoError('UNSAFE_DATABASE')
                c.execute('PRAGMA temp_store=MEMORY')
                self._verify_windows_database()
            except BaseException:
                c.close()
                raise
        else:
            self.path.chmod(0o600)
        try:
            c.execute('PRAGMA busy_timeout=5000')
            yield c;c.commit()
            if os.name == 'nt':
                self._verify_windows_database()
        except Exception:c.rollback();raise
        finally:c.close()
    def reserve(self,p,n,now=None):
        now=time.time() if now is None else now
        if not isinstance(n,int) or isinstance(n,bool) or n<0 or n>p['max_request_bytes']:raise AutoError('REQUEST_BYTE_BUDGET')
        if not p.get('enabled') or p['expires_at']<=now:raise AutoError('AUTO_DISABLED_OR_EXPIRED')
        day=datetime.datetime.fromtimestamp(now,datetime.timezone.utc).date().isoformat()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT calls,bytes FROM budgets WHERE day=? AND policy=?',(day,p['policy_id'])).fetchone() or (0,0)
            if row[0]>=p['max_calls_per_day'] or row[1]+n>p['max_bytes_per_day']:raise AutoError('AUTO_DAILY_BUDGET')
            c.execute('INSERT OR REPLACE INTO budgets VALUES(?,?,?,?)',(day,p['policy_id'],row[0]+1,row[1]+n))
    def cached(self,k):
        with self.connection() as c:
            r=c.execute('SELECT expires,body FROM cache WHERE k=?',(k,)).fetchone()
        return decode(r[1]) if r and r[0]>time.time() else None
    def cache(self,k,value,ttl):
        with self.connection() as c:c.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(k,time.time()+ttl,canonical(value)))
    def put(self,value):
        k=digest(value)
        with self.connection() as c:c.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?)',(k,time.time(),canonical(value)))
        return k
    def get(self,k):
        if not isinstance(k,str) or len(k)!=64 or any(x not in '0123456789abcdef' for x in k):raise AutoError('EVIDENCE_ID')
        with self.connection() as c:r=c.execute('SELECT body FROM evidence WHERE k=?',(k,)).fetchone()
        if not r:raise AutoError('EVIDENCE_NOT_FOUND')
        obj=decode(r[0]);
        if digest(obj)!=k:raise AutoError('EVIDENCE_INTEGRITY')
        return obj
    def set_goal(self,session,text):
        with self.connection() as c:c.execute('INSERT OR REPLACE INTO goals VALUES(?,?,?)',(session,time.time(),text[:4000]))
    def goal(self,session):
        with self.connection() as c:r=c.execute('SELECT created,text FROM goals WHERE session=?',(session,)).fetchone()
        return r[1] if r and r[0]>time.time()-86400 else ''
    def event(self,kind,body):
        with self.connection() as c:c.execute('INSERT INTO events(created,kind,body) VALUES(?,?,?)',(time.time(),kind,canonical(body)))
    def stats(self):
        with self.connection() as c:
            budgets=c.execute('SELECT day,calls,bytes FROM budgets ORDER BY day DESC LIMIT 30').fetchall()
            count=c.execute('SELECT count(*) FROM evidence').fetchone()[0]
            events=[decode(r[0]) for r in c.execute("SELECT body FROM events WHERE kind='sieve' ORDER BY id DESC LIMIT 10000")]
        return {'budget_rows':[{'utc_day':d,'reserved_attempts':n,'reserved_payload_bytes':b} for d,n,b in budgets],
                'evidence_records':count,'proposed_tool_characters_withheld':sum(x.get('withheld_chars',0) for x in events),
                'host_tokens_saved':None,'host_cost_saved':None,'measurement_note':'Characters are not tokens; import matched host runs to measure savings.'}
    def purge_content(self):
        """Delete the text stored for this folder: receipts, cached answers, goals.

        Budget counters and event codes carry no content and stay.
        """
        with self.connection() as c:
            for table in ('evidence','cache','goals'):c.execute(f'DELETE FROM {table}')
    def prune(self,days):
        now=time.time()
        with self.connection() as c:
            c.execute('DELETE FROM cache WHERE expires<=?',(now,))
            for table in ('evidence','events','goals'):c.execute(f'DELETE FROM {table} WHERE created<?',(now-days*86400,))

_FOLDER_ID=re.compile(r'[0-9a-f]{24}\Z')

def sweep_inactive(state_root,exclude=None,now=None,limit=256):
    """Age out the text of folders whose grant is no longer active.

    No local service starts for a revoked or expired folder, so its own
    retention never runs. A running service does it for them: a withdrawn
    folder's text is deleted, an expired folder's is pruned by its own
    retention setting. Active folders are left to their own service.
    """
    from .common import read_private
    now=time.time() if now is None else now
    try:entries=sorted(Path(state_root).iterdir())[:limit]
    except OSError:return
    for folder in entries:
        if folder==exclude or folder.is_symlink() or not _FOLDER_ID.match(folder.name):continue
        if not (folder/'auto.sqlite3').is_file():continue
        try:policy=read_private(folder/'policy.json',16_000)
        except (AutoError,OSError):continue
        if not isinstance(policy,dict):continue
        enabled=policy.get('enabled') is True and policy.get('consent')!='refused'
        expires=policy.get('expires_at')
        if enabled and isinstance(expires,(int,float)) and not isinstance(expires,bool) and expires>now:continue
        days=policy.get('retention_days')
        days=days if isinstance(days,int) and not isinstance(days,bool) and 1<=days<=30 else 30
        try:
            store=Store(folder)
            if enabled:store.prune(days)
            else:store.purge_content()
        except (AutoError,OSError,sqlite3.Error):continue

class SingleFlight:
    """One in-process concurrent computation per key; failures are never cached."""
    def __init__(self): self._lock=threading.Lock();self._work={}
    def run(self,key,fn,on_join=None):
        from concurrent.futures import Future
        with self._lock:
            f=self._work.get(key);owner=f is None
            if owner:f=Future();self._work[key]=f
        if not owner:
            value=f.result(timeout=35)
            return on_join(value) if on_join else value
        try:
            v=fn();f.set_result(v);return v
        except BaseException as e:f.set_exception(e);raise
        finally:
            with self._lock:self._work.pop(key,None)
