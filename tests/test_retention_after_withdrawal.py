"""Text stored for a folder does not outlive its grant.

Only a folder's own local service pruned its receipts, and no service starts
for a revoked or expired folder, so reduction text and prompt goals stayed on
disk indefinitely. Revoking a folder now deletes the text Jev stored for it,
and any running service ages out an expired folder's text by that folder's
own retention setting.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core_engine_coverage import FakeProviders  # noqa: E402

from jev_auto import common  # noqa: E402
from jev_auto.common import state_dir, write_private  # noqa: E402
from jev_auto.engine import Engine  # noqa: E402
from jev_auto.settings import make_policy, revoke, save_policy_new  # noqa: E402
from jev_auto.store import Store  # noqa: E402

DAY = 86_400


def _counts(root: Path) -> dict:
    with Store(root).connection() as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("evidence", "cache", "goals")}


class _Folders(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def folder(self, name, **fields):
        path = self.root / name
        path.mkdir()
        save_policy_new(path, make_policy(path, "typesafe", **fields))
        return path

    def fill(self, path, age_days):
        store = Store(state_dir(path))
        created = time.time() - age_days * DAY
        with store.connection() as connection:
            connection.execute("INSERT INTO evidence VALUES(?,?,?)", (f"{age_days:064d}", created, b"{}"))
            connection.execute("INSERT INTO goals VALUES(?,?,?)", (f"s{age_days}", created, "a goal"))
        store.cache(f"k{age_days}", {"v": 1}, 600)


class RevokeTests(_Folders):
    def test_revoking_a_folder_deletes_the_text_stored_for_it(self):
        folder = self.folder("client")
        self.fill(folder, 1)
        self.assertEqual(_counts(state_dir(folder)), {"evidence": 1, "cache": 1, "goals": 1})
        revoke(folder)
        self.assertEqual(_counts(state_dir(folder)), {"evidence": 0, "cache": 0, "goals": 0})

    def test_revoking_a_folder_without_stored_text_creates_nothing(self):
        folder = self.folder("empty")
        revoke(folder)
        self.assertFalse((state_dir(folder) / "auto.sqlite3").exists())


class ExpiryTests(_Folders):
    def test_a_running_service_ages_out_an_expired_folders_text(self):
        expired = self.folder("old-client", retention_days=7)
        self.fill(expired, 30)
        self.fill(expired, 1)
        policy_file = state_dir(expired) / "policy.json"
        write_private(policy_file, {**json.loads(policy_file.read_text()), "expires_at": time.time() - 60})
        active = self.folder("current")
        Engine(active, provider=FakeProviders())
        self.assertEqual(_counts(state_dir(expired))["evidence"], 1)
        self.assertEqual(_counts(state_dir(expired))["goals"], 1)

    def test_an_active_folder_is_left_to_its_own_service(self):
        other = self.folder("other", retention_days=1)
        self.fill(other, 5)
        Engine(self.folder("current"), provider=FakeProviders())
        self.assertEqual(_counts(state_dir(other))["evidence"], 1)


class SweepEdgeTests(_Folders):
    def test_a_missing_state_folder_is_nothing_to_sweep(self):
        from jev_auto.store import sweep_inactive
        sweep_inactive(self.root / "no-such-folder")  # must not raise

    def test_a_withdrawn_folder_found_by_the_sweep_has_its_text_deleted(self):
        folder = self.folder("withdrawn")
        self.fill(folder, 1)
        policy_file = state_dir(folder) / "policy.json"
        write_private(policy_file, {**json.loads(policy_file.read_text()), "enabled": False})
        Engine(self.folder("current"), provider=FakeProviders())
        self.assertEqual(_counts(state_dir(folder)), {"evidence": 0, "cache": 0, "goals": 0})

    def test_a_folder_whose_grant_cannot_be_read_is_left_alone(self):
        folder = self.folder("unreadable")
        self.fill(folder, 1)
        (state_dir(folder) / "policy.json").chmod(0o644)
        self.addCleanup((state_dir(folder) / "policy.json").chmod, 0o600)
        Engine(self.folder("current"), provider=FakeProviders())
        (state_dir(folder) / "policy.json").chmod(0o600)
        self.assertEqual(_counts(state_dir(folder))["evidence"], 1)

    def test_a_store_that_cannot_be_opened_does_not_stop_the_sweep(self):
        from jev_auto import store as store_module
        from jev_auto.common import AutoError
        expired = self.folder("expired")
        self.fill(expired, 1)
        policy_file = state_dir(expired) / "policy.json"
        write_private(policy_file, {**json.loads(policy_file.read_text()), "expires_at": time.time() - 60})
        with patch.object(store_module, "Store", side_effect=AutoError("UNSAFE_DATABASE")):
            store_module.sweep_inactive(common.home_root())  # must not raise


if __name__ == "__main__":
    unittest.main()
