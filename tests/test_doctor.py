"""Offline-first contract tests for ``jev doctor``.

The diagnostic is intentionally not a lighter version of ``probe``.  Its
default path must remain a local read-only check, even for an unenrolled
workspace, so a developer can safely run it before deciding to enroll.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


def _run(argv, *, ensure=None, request=None):
    import jev_auto.cli as cli
    import jev_auto.doctor as doctor

    out, err = io.StringIO(), io.StringIO()
    with ExitStack() as stack:
        # Package resealing is owned by the release lane. These command tests
        # isolate doctor behavior from that mechanically verified artifact.
        stack.enter_context(patch.object(
            doctor, "_runtime_manifest",
            return_value={"id": "runtime_manifest", "status": "PASS", "detail": "runtime files verified"},
        ))
        if ensure is not None:
            stack.enter_context(patch.object(cli, "ensure", ensure))
        if request is not None:
            stack.enter_context(patch.object(cli, "request", request))
        stack.enter_context(redirect_stdout(out))
        stack.enter_context(redirect_stderr(err))
        code = cli.main(argv)
    return code, json.loads(out.getvalue()), err.getvalue()


class DoctorOfflineTests(unittest.TestCase):
    def test_unenrolled_workspace_is_actionable_without_creating_state_or_calling_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                code, payload, stderr = _run(
                    ["doctor", "--workspace", str(workspace)],
                    ensure=lambda _path: self.fail("offline doctor must not start a broker"),
                    request=lambda _path, _request: self.fail("offline doctor must not request a broker"),
                )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["mode"], "offline")
        self.assertEqual(payload["overall"], "ACTION_REQUIRED")
        policy = next(check for check in payload["checks"] if check["id"] == "workspace_policy")
        self.assertEqual(policy["status"], "ACTION_REQUIRED")
        self.assertEqual(policy["code"], "NOT_ENROLLED")
        self.assertEqual(payload["next_action"]["action"], "OPEN_PRIVATE_SETUP_WIZARD")
        self.assertIn("jev_setup", payload["next_action"]["instruction"])
        self.assertNotIn("jev enroll", json.dumps(payload["next_action"]).lower())
        self.assertFalse((root / "state").exists())

    def test_default_result_has_only_safe_receipt_summary_and_stable_check_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                _code, payload, _stderr = _run(["doctor", "--workspace", str(workspace)])
        self.assertEqual(
            {check["id"] for check in payload["checks"]},
            {"runtime_manifest", "python", "workspace_policy", "offline_gate", "host_surface", "receipt_index"},
        )
        receipt = next(check for check in payload["checks"] if check["id"] == "receipt_index")
        self.assertEqual(receipt, {"id": "receipt_index", "status": "NONE", "count": 0})
        host = next(check for check in payload["checks"] if check["id"] == "host_surface")
        self.assertEqual(host["status"], "PORTABLE_RUNTIME")
        self.assertIn("not checked", host["detail"])
        self.assertNotIn("receipt", json.dumps(payload).lower().replace("receipt_index", ""))

    def test_manifest_failure_is_explicit_and_does_not_hide_the_other_offline_checks(self):
        import jev_auto.doctor as doctor

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {"jev_auto/doctor.py": "0" * 64}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_accepts_a_matching_sealed_runtime_file(self):
        import jev_auto.doctor as doctor

        target = RUNTIME / "jev_auto" / "doctor.py"
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {"jev_auto/doctor.py": hashlib.sha256(target.read_bytes()).hexdigest()}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "PASS", "detail": "runtime files verified"})

    def test_existing_evidence_is_counted_without_exposing_receipt_content(self):
        from jev_auto.store import Store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                from jev_auto.common import state_dir
                store = Store(state_dir(workspace))
                store.put({"kind": "decision", "secret_like_receipt_body": "do not show this"})
                _code, payload, _stderr = _run(["doctor", "--workspace", str(workspace)])
        receipt = next(check for check in payload["checks"] if check["id"] == "receipt_index")
        self.assertEqual(receipt, {"id": "receipt_index", "status": "PRESENT", "count": 1})
        self.assertNotIn("secret_like_receipt_body", json.dumps(payload))

    def test_relative_xdg_state_home_can_read_an_existing_receipt_index(self):
        from jev_auto.common import state_dir
        from jev_auto.store import Store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"XDG_STATE_HOME": "relative-state"}):
                    Store(state_dir(workspace)).put({"kind": "decision"})
                    _code, payload, _stderr = _run(["doctor", "--workspace", str(workspace)])
            finally:
                os.chdir(previous)
        receipt = next(check for check in payload["checks"] if check["id"] == "receipt_index")
        self.assertEqual(receipt, {"id": "receipt_index", "status": "PRESENT", "count": 1})

    def test_dangling_receipt_database_symlink_is_failed_not_reported_as_empty(self):
        from jev_auto.common import state_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                database = state_dir(workspace) / "auto.sqlite3"
                database.parent.mkdir(parents=True)
                database.symlink_to(root / "missing.sqlite3")
                _code, payload, _stderr = _run(["doctor", "--workspace", str(workspace)])
        receipt = next(check for check in payload["checks"] if check["id"] == "receipt_index")
        self.assertEqual(receipt, {
            "id": "receipt_index", "status": "FAILED", "code": "RECEIPT_INDEX_UNAVAILABLE",
        })

    def test_live_flag_is_not_a_doctor_option(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            with self.assertRaises(SystemExit) as raised:
                _run(["doctor", "--workspace", str(workspace), "--live"])
        self.assertEqual(raised.exception.code, 2)


class PackagedDoctorSmokeTest(unittest.TestCase):
    """Runs against the sealed launcher once the package lane has resealed it."""

    LAUNCHER = ROOT / "plugins" / "qualixar-jev-decision-layer" / "scripts" / "jev"

    def test_packaged_launcher_runs_offline_from_an_unrelated_directory(self):
        manifest = json.loads((RUNTIME / "RUNTIME_MANIFEST.json").read_text())
        if "jev_auto/doctor.py" not in manifest.get("files", {}):
            self.skipTest("runtime manifest resealing is owned by the package lane")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            result = subprocess.run(
                [str(self.LAUNCHER), "doctor", "--workspace", str(workspace)], cwd=root,
                env={**os.environ, "XDG_STATE_HOME": str(root / "state")},
                capture_output=True, text=True, timeout=120, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "offline")
        self.assertEqual(payload["overall"], "ACTION_REQUIRED")
        self.assertEqual(payload["next_action"]["action"], "OPEN_PRIVATE_SETUP_WIZARD")


if __name__ == "__main__":
    unittest.main()
