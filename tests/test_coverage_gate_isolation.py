"""The coverage gate runs the suite against a private state directory and
fails if any test wrote to it.

Before this, one wizard test applied a real grant without isolating
XDG_STATE_HOME, and every run of the suite left a grant in the developer's own
state directory. A test that forgets to isolate state now fails the gate.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _gate():
    spec = importlib.util.spec_from_file_location("coverage_gate_under_test", ROOT / "tools" / "coverage_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateStateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.gate = _gate()
        self.seen = []

    def _fake_run(self, leak: bool):
        document = {"files": {str(self.gate.SOURCE / "jev_auto" / "x.py"):
                              {"summary": {"num_statements": 10, "percent_covered": 100.0}}}}

        def run(command, **kwargs):
            environment = kwargs["env"]
            self.seen.append(environment.get("XDG_STATE_HOME"))
            if "json" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(document), "")
            if leak:
                state = Path(environment["XDG_STATE_HOME"]) / "qualixar-jev-decision-layer" / "abc"
                state.mkdir(parents=True)
                (state / "policy.json").write_text("{}")
            return subprocess.CompletedProcess(command, 0, "", "")
        return run

    def test_the_suite_runs_against_a_private_state_directory(self):
        with patch.object(self.gate.subprocess, "run", self._fake_run(leak=False)):
            measured = self.gate._measure()
        self.assertEqual(measured, {"jev_auto/x.py": 100})
        state = self.seen[0]
        self.assertTrue(state)
        self.assertNotEqual(Path(state), Path.home() / ".local" / "state")
        self.assertFalse(Path(state).exists(), "the private state directory must be removed afterwards")

    def test_a_test_that_writes_state_fails_the_gate_and_names_the_file(self):
        with patch.object(self.gate.subprocess, "run", self._fake_run(leak=True)), \
             self.assertRaises(SystemExit) as ctx:
            self.gate._measure()
        self.assertIn("wrote to the state directory", str(ctx.exception))
        self.assertIn("qualixar-jev-decision-layer/abc/policy.json", str(ctx.exception))

    def test_a_test_that_bypasses_the_variable_and_writes_real_state_fails_the_gate(self):
        """A test can ignore XDG_STATE_HOME and write through Path.home(); the
        gate also compares the real state roots before and after the run."""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real-state"
            (real / "already-there").mkdir(parents=True)
            base = self._fake_run(leak=False)

            def run(command, **kwargs):
                if "json" not in command:
                    (real / "written-by-a-test").mkdir()
                return base(command, **kwargs)

            with patch.object(self.gate, "_real_state_roots", return_value=[real]), \
                 patch.object(self.gate.subprocess, "run", run), \
                 self.assertRaises(SystemExit) as ctx:
                self.gate._measure()
        message = str(ctx.exception)
        self.assertIn("written-by-a-test", message)
        self.assertNotIn("already-there", message)

    def test_the_real_state_roots_include_the_home_fallback(self):
        roots = self.gate._real_state_roots()
        self.assertIn(Path.home() / ".local" / "state" / "qualixar-jev-decision-layer", roots)

    def test_leaked_state_lists_files_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.gate._leaked_state(root), [])
            (root / "empty-dir").mkdir()
            self.assertEqual(self.gate._leaked_state(root), [])
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "b" / "f.json").write_text("{}")
            self.assertEqual(self.gate._leaked_state(root), ["a/b/f.json"])


if __name__ == "__main__":
    unittest.main()
