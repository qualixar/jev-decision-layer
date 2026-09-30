"""Coverage-floor tests for ``jev_auto/doctor.py``.

These target the guard branches the existing ``test_doctor.py`` suite does not
reach: malformed runtime manifests, an old interpreter, an unexpected policy
error code, a broken or malformed offline self-test, an unsafe receipt
database, an impossible evidence count, and the "FAILED-without-ACTION"
branch of ``diagnose()``'s ``next_action`` selection.

Every test asserts on the exact returned dict (status + code), not just "no
exception" -- and each guard is verified live with a revert-check (see the
bottom of this file / the final report) by temporarily neutralizing the
guard, confirming the test fails, then restoring the source exactly.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

import jev_auto.doctor as doctor  # noqa: E402
from jev_auto.common import AutoError, state_dir  # noqa: E402
from jev_auto.settings import make_policy, save_policy  # noqa: E402
from jev_auto.store import Store  # noqa: E402


def _workspace(root: Path) -> Path:
    workspace = root / "project"
    workspace.mkdir()
    return workspace


class RuntimeManifestGuardTests(unittest.TestCase):
    """``_runtime_manifest`` must fail closed for every malformed shape, not just a hash mismatch."""

    def test_manifest_without_a_files_table_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"schema": 1}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_with_an_empty_files_table_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_entry_with_an_absolute_path_is_rejected_before_any_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {"/etc/passwd": "a" * 64}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_entry_with_a_malformed_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            # Right length, but not hex -- must not be treated as a valid digest.
            manifest.write_text(json.dumps({"files": {"jev_auto/doctor.py": "z" * 64}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_entry_naming_a_directory_instead_of_a_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            # "jev_auto" is a real package directory under RUNTIME, not a file.
            manifest.write_text(json.dumps({"files": {"jev_auto": "0" * 64}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})

    def test_manifest_entry_naming_a_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {"jev_auto/does_not_exist.py": "0" * 64}}))
            with patch.object(doctor, "MANIFEST", manifest):
                check = doctor._runtime_manifest()
        self.assertEqual(check, {"id": "runtime_manifest", "status": "FAILED", "code": "RUNTIME_MANIFEST_INVALID"})


class PythonVersionGuardTests(unittest.TestCase):
    def test_an_interpreter_older_than_3_11_is_reported_as_failed(self):
        with patch.object(doctor.sys, "version_info", (3, 10, 9)):
            check = doctor._python()
        self.assertEqual(check, {"id": "python", "status": "FAILED", "code": "PYTHON_3_11_REQUIRED"})

    def test_the_running_interpreter_passes(self):
        # Sanity companion to the guard test above -- proves the guard is not
        # simply always FAILED under patching artifacts.
        check = doctor._python()
        self.assertEqual(check["status"], "PASS")


class PolicyCheckGuardTests(unittest.TestCase):
    def test_an_unrecognized_policy_error_code_is_reported_failed_verbatim_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(Path(directory))
            with patch.object(doctor, "load_policy", side_effect=AutoError("SOME_OTHER_POLICY_ERROR")):
                check, policy_document = doctor._policy(workspace)
        self.assertEqual(check, {"id": "workspace_policy", "status": "FAILED", "code": "SOME_OTHER_POLICY_ERROR"})
        self.assertIsNone(policy_document)

    def test_an_enrolled_workspace_reports_pass_with_provider_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _workspace(root)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                policy = make_policy(workspace, "typesafe", generic_query_enabled=True, case_ids=[])
                save_policy(workspace, policy)
                check, policy_document = doctor._policy(workspace)
        self.assertEqual(check["id"], "workspace_policy")
        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["provider"], "typesafe")
        self.assertEqual(check["enrollment_scope"], "exact")
        self.assertIsNotNone(policy_document)
        self.assertEqual(policy_document["provider"], "typesafe")


class OfflineGateGuardTests(unittest.TestCase):
    def test_a_raising_selftest_is_reported_failed_without_a_cases_count(self):
        with patch.object(doctor, "selftest", side_effect=RuntimeError("fixture engine exploded")):
            check = doctor._offline_gate()
        self.assertEqual(check, {"id": "offline_gate", "status": "FAILED", "code": "FIXTURE_INTEGRITY_FAILURE"})
        self.assertNotIn("cases", check)

    def test_a_selftest_reporting_failures_is_reported_failed_with_the_cases_count(self):
        with patch.object(doctor, "selftest", return_value={"all_passed": False, "cases": 42, "failures": ["x"]}):
            check = doctor._offline_gate()
        self.assertEqual(check, {"id": "offline_gate", "status": "FAILED", "code": "FIXTURE_INTEGRITY_FAILURE", "cases": 42})

    def test_a_selftest_with_a_non_integer_cases_field_falls_back_to_zero(self):
        with patch.object(doctor, "selftest", return_value={"all_passed": True, "cases": "not-a-number"}):
            check = doctor._offline_gate()
        self.assertEqual(check, {"id": "offline_gate", "status": "FAILED", "code": "FIXTURE_INTEGRITY_FAILURE", "cases": 0})


class ReceiptIndexGuardTests(unittest.TestCase):
    def test_group_or_world_readable_receipt_database_is_reported_unavailable_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _workspace(root)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                database = state_dir(workspace) / "auto.sqlite3"
                Store(state_dir(workspace)).put({"kind": "decision"})
                self.assertTrue(database.exists())
                database.chmod(0o644)  # group/other-readable -- must fail closed, not read the file
                check = doctor._receipt_index(workspace)
        self.assertEqual(check, {"id": "receipt_index", "status": "FAILED", "code": "RECEIPT_INDEX_UNAVAILABLE"})

    def test_an_impossible_evidence_count_from_the_database_is_reported_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _workspace(root)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                Store(state_dir(workspace)).put({"kind": "decision"})

                class _FakeConnection:
                    def execute(self, _query):
                        return self

                    def fetchone(self):
                        return (-1,)

                    def close(self):
                        return None

                with patch.object(doctor.sqlite3, "connect", return_value=_FakeConnection()):
                    check = doctor._receipt_index(workspace)
        self.assertEqual(check, {"id": "receipt_index", "status": "FAILED", "code": "RECEIPT_INDEX_UNAVAILABLE"})


class DiagnoseOverallStatusTests(unittest.TestCase):
    def test_a_broken_runtime_manifest_on_an_otherwise_enrolled_workspace_asks_for_repair_not_setup(self):
        """overall == FAILED (not ACTION_REQUIRED) must select the REPAIR_INSTALLATION branch."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = _workspace(root)
            bad_manifest = root / "manifest.json"
            bad_manifest.write_text(json.dumps({"files": {}}))
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}), \
                    patch.object(doctor, "MANIFEST", bad_manifest):
                policy = make_policy(workspace, "typesafe", generic_query_enabled=True, case_ids=[])
                save_policy(workspace, policy)
                result = doctor.diagnose(workspace)
        self.assertEqual(result["overall"], "FAILED")
        manifest_check = next(check for check in result["checks"] if check["id"] == "runtime_manifest")
        self.assertEqual(manifest_check["status"], "FAILED")
        policy_check = next(check for check in result["checks"] if check["id"] == "workspace_policy")
        self.assertEqual(policy_check["status"], "PASS")
        self.assertEqual(
            result["next_action"],
            {"command": "jev doctor --workspace " + str(workspace), "reason": "REPAIR_INSTALLATION"},
        )
        self.assertEqual(doctor.exit_code(result), 2)


if __name__ == "__main__":
    unittest.main()
