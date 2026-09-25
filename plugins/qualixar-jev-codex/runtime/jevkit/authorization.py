"""Local, expiring, workspace-bound call grants. Every HTTP attempt reserves a slot."""
from __future__ import annotations
from contextlib import contextmanager
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from .security import SafeError, private_dir

class CallBudget:
    _COLUMNS = (
        'id', 'expires', 'max_calls', 'used', 'allow_custom', 'workspace_id',
        'revision', 'case_id', 'data_classification', 'request_id',
        'request_sha256', 'provider_id', 'provider_profile_sha256',
    )
    def __init__(self, root: Path, storage_root: Path | None = None,
                 workspace_id: str | None = None, revision: str | None = None,
                 provider_id: str | None = None,
                 provider_profile_sha256: str | None = None):
        self.path = (storage_root if storage_root is not None else root / '.local') / 'live-grants.sqlite3'
        self.workspace_id = workspace_id
        self.revision = revision
        self.provider_id = provider_id
        self.provider_profile_sha256 = provider_profile_sha256

    def _validate_ledger(self) -> None:
        if not self.path.exists():
            return
        try:
            metadata = self.path.lstat()
        except OSError as error:
            raise SafeError('UNSAFE_BUDGET_PATH') from error
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or (hasattr(os, 'getuid') and metadata.st_uid != os.getuid())):
            raise SafeError('UNSAFE_BUDGET_PATH')

    def connect(self):
        private_dir(self.path.parent)
        self._validate_ledger()
        if self.path.is_symlink():
            raise SafeError('UNSAFE_BUDGET_PATH')
        previous_umask = os.umask(0o077)
        try:
            con = sqlite3.connect(self.path, timeout=5)
        finally:
            os.umask(previous_umask)
        self.path.chmod(0o600)
        try:
            self._validate_ledger()
            existing = tuple(
                row[1] for row in con.execute("PRAGMA table_info(grants)").fetchall()
            )
            if existing and existing != self._COLUMNS:
                # Legacy grants were not workspace/request bound. Invalidate them
                # instead of attempting to preserve ambient authority.
                con.execute('DROP TABLE grants')
            con.execute('''CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, expires REAL NOT NULL, max_calls INTEGER NOT NULL,
                used INTEGER NOT NULL, allow_custom INTEGER NOT NULL,
                workspace_id TEXT, revision TEXT, case_id TEXT,
                data_classification TEXT, request_id TEXT, request_sha256 TEXT,
                provider_id TEXT, provider_profile_sha256 TEXT
            )''')
            con.commit()
        except Exception:
            con.close()
            raise
        return con

    @contextmanager
    def connection(self):
        con = self.connect()
        try:
            yield con
        finally:
            con.close()

    def grant(self, calls: int, minutes: int, allow_custom: bool = False,
              *, case_id: str | None = None, data_classification: str | None = None,
              request_id: str | None = None, request_sha256: str | None = None) -> dict:
        if not 1 <= calls <= 100 or not 1 <= minutes <= 120:
            raise SafeError('INVALID_GRANT_LIMIT')
        if allow_custom and not all((self.workspace_id, self.revision, case_id,
                                     data_classification, request_id, request_sha256)):
            raise SafeError('REQUEST_GRANT_REQUIRED')
        grant_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute('DELETE FROM grants')
            c.execute(
                'INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (grant_id, time.time() + minutes * 60, calls, 0, int(allow_custom),
                 self.workspace_id, self.revision, case_id, data_classification,
                 request_id, request_sha256, self.provider_id,
                 self.provider_profile_sha256),
            )
            c.commit()
        return self.status()

    def revoke(self) -> None:
        with self.connection() as c:
            c.execute('DELETE FROM grants')
            c.commit()

    def status(self) -> dict:
        with self.connection() as c:
            row = c.execute(
                'SELECT id,expires,max_calls,used,allow_custom,workspace_id,revision,'
                'case_id,data_classification,request_id,request_sha256,provider_id,'
                'provider_profile_sha256 FROM grants'
            ).fetchone()
        if not row:
            return {'enabled': False, 'remaining_calls': 0, 'custom_data_allowed': False}
        if self.workspace_id and row[5] != self.workspace_id:
            return {'enabled': False, 'remaining_calls': 0, 'custom_data_allowed': False}
        if self.revision and row[6] != self.revision:
            return {'enabled': False, 'remaining_calls': 0, 'custom_data_allowed': False}
        if self.provider_id and row[11] != self.provider_id:
            return {'enabled': False, 'remaining_calls': 0, 'custom_data_allowed': False}
        if self.provider_profile_sha256 and row[12] != self.provider_profile_sha256:
            return {'enabled': False, 'remaining_calls': 0, 'custom_data_allowed': False}
        return {
            'enabled': row[1] > time.time() and row[3] < row[2],
            'remaining_calls': max(0, row[2] - row[3]),
            'custom_data_allowed': bool(row[4]), 'expires_utc_epoch': row[1],
            'grant_id': row[0],
        }

    def reserve(self, custom: bool, *, case_id: str | None = None,
                data_classification: str | None = None, request_id: str | None = None,
                request_sha256: str | None = None) -> str:
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(
                'SELECT id,expires,max_calls,used,allow_custom,workspace_id,revision,'
                'case_id,data_classification,request_id,request_sha256,provider_id,'
                'provider_profile_sha256 FROM grants'
            ).fetchone()
            if custom and not row:
                raise SafeError('REQUEST_GRANT_REQUIRED')
            if not row or row[1] <= time.time():
                raise SafeError('LIVE_NOT_AUTHORIZED: use the interactive local grant helper')
            if self.workspace_id and row[5] != self.workspace_id:
                raise SafeError('LIVE_NOT_AUTHORIZED: grant belongs to another workspace')
            if self.revision and row[6] != self.revision:
                raise SafeError('LIVE_NOT_AUTHORIZED: grant revision does not match')
            if self.provider_id and row[11] != self.provider_id:
                raise SafeError('LIVE_NOT_AUTHORIZED: grant belongs to another provider')
            if self.provider_profile_sha256 and row[12] != self.provider_profile_sha256:
                raise SafeError('LIVE_NOT_AUTHORIZED: provider profile changed')
            if custom:
                if not row[4]:
                    raise SafeError('CUSTOM_DATA_NOT_AUTHORIZED')
                if (row[7], row[8], row[9], row[10]) != (
                    case_id, data_classification, request_id, request_sha256
                ):
                    raise SafeError('REQUEST_GRANT_REQUIRED')
            if row[3] >= row[2]:
                raise SafeError('CALL_BUDGET_EXHAUSTED')
            c.execute('UPDATE grants SET used=used+1 WHERE id=?', (row[0],))
            c.commit()
            return row[0]
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
