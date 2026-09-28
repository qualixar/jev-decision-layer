"""Exercise the production Windows IPC entrypoints with a synthetic engine."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


@unittest.skipUnless(os.name == "nt", "requires native Windows named pipes and ACLs")
class WindowsProductionIPCTests(unittest.TestCase):
    def test_ensure_and_request_round_trip_through_server_with_private_state(self):
        from jev_auto import ipc, platform_fs, server, settings

        with tempfile.TemporaryDirectory(prefix="jev-ipc-native-") as temporary:
            root = Path(temporary)
            workspace = root / "Workspace With Space Ω"
            workspace.mkdir()
            ascii_workspace = root / "workspace-ascii"
            ascii_workspace.mkdir()
            state_base = root / "state"
            self.assertNotEqual(
                ipc.address(workspace, state_base), ipc.address(ascii_workspace, state_base)
            )
            policy = settings.make_policy(workspace, "typesafe", credential_store="os")
            settings.save_policy_new(workspace, policy, state_base)
            failures: list[str] = []

            class SyntheticEngine:
                def __init__(self, path: Path, base: Path):
                    self.path = path
                    self.base = base
                    self.providers = SimpleNamespace(close=lambda: None)

                def dispatch(self, request: dict[str, object]) -> dict[str, object]:
                    if request.get("op") == "health":
                        return {"version": "1.0.0"}
                    if request.get("op") != "synthetic-policy-check":
                        return {"status": "UNEXPECTED_OPERATION"}
                    loaded = settings.load_policy(self.path, self.base)
                    state_root = settings.state_dir(self.path, self.base)
                    platform_fs.verify_private_dir(state_root)
                    descriptor = platform_fs.open_private_file(state_root / "broker.lock", os.O_RDONLY)
                    try:
                        platform_fs.verify_private_file_fd(descriptor)
                    finally:
                        os.close(descriptor)
                    return {
                        "status": "PASS",
                        "provider": loaded["provider"],
                        "private_state_verified": True,
                        "broker_lock_verified": True,
                    }

            def run_server() -> None:
                try:
                    server.serve(workspace, state_base, idle_seconds=8)
                except Exception as error:  # keep diagnostics non-sensitive
                    failures.append(type(error).__name__)

            thread = threading.Thread(target=run_server, name="jev-windows-ipc-ci", daemon=True)
            with patch.object(server, "Engine", side_effect=SyntheticEngine):
                thread.start()
                deadline = time.monotonic() + 20
                last_error = "BROKER_UNAVAILABLE"
                ready = False
                while time.monotonic() < deadline and thread.is_alive():
                    try:
                        self.assertEqual(ipc.request(workspace, {"op": "health"}, state_base, timeout=0.5),
                                         {"version": "1.0.0"})
                        ready = True
                        break
                    except Exception as error:
                        last_error = str(error)
                        time.sleep(0.1)

                try:
                    self.assertTrue(ready, f"native production broker did not become ready: {last_error}; {failures}")
                    # ensure() must reuse the real running endpoint and enforce
                    # the persisted policy before the synthetic request.
                    ipc.ensure(workspace, state_base)
                    result = ipc.request(
                        workspace, {"op": "synthetic-policy-check"}, state_base, timeout=3
                    )
                    self.assertEqual(result, {
                        "status": "PASS",
                        "provider": "typesafe",
                        "private_state_verified": True,
                        "broker_lock_verified": True,
                    })
                    self.assertEqual(ipc.request(workspace, {"op": "shutdown"}, state_base, timeout=3),
                                     {"stopping": True})
                finally:
                    thread.join(timeout=10)

            self.assertFalse(thread.is_alive(), "production broker should stop after shutdown")
            self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
