"""The background service must never import code from the folder it starts in.

Hosts start the broker from wherever they are: VS Code from the workspace
folder, a terminal from any project. `python -m` puts that folder first on the
import path, so a project file named like a standard module ran inside the
broker. The broker now starts isolated, from its own private state folder.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import ipc  # noqa: E402
from jev_auto.common import AutoError, private_dir, state_dir  # noqa: E402
from jev_auto.settings import make_policy, save_policy  # noqa: E402

_PLANTED = ("argparse", "json", "socketserver", "jev_auto")


def _plant(folder: Path) -> None:
    for name in _PLANTED:
        body = ("import pathlib\n"
                f"pathlib.Path({str(folder)!r}, 'MARKER-{name}').write_text('ran')\n")
        if name == "jev_auto":
            (folder / name).mkdir()
            (folder / name / "__init__.py").write_text(body)
        else:
            (folder / f"{name}.py").write_text(body)


class BrokerSpawnTests(unittest.TestCase):
    def test_the_broker_starts_isolated_in_its_private_state_folder(self):
        captured = {}
        real_popen = subprocess.Popen

        def popen(args, **kwargs):
            if "--workspace" not in args:
                return real_popen(args, **kwargs)  # the git call that resolves the workspace
            captured.update(args=args, kwargs=kwargs)
            return object()

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(directory) / "state")}):
            workspace = Path(directory) / "project"
            workspace.mkdir()
            save_policy(workspace, make_policy(workspace, "typesafe", days=1))
            with patch.object(ipc, "request", side_effect=AutoError("BROKER_UNAVAILABLE")), \
                    patch.object(ipc.subprocess, "Popen", side_effect=popen), \
                    patch.object(ipc.time, "sleep"):
                with self.assertRaisesRegex(AutoError, "BROKER_START_FAILED"):
                    ipc.ensure(workspace)
            expected_cwd = private_dir(state_dir(workspace))
        args, kwargs = captured["args"], captured["kwargs"]
        self.assertEqual(args[:4], [sys.executable, "-I", "-S", "-B"])
        self.assertNotIn("-m", args)
        self.assertEqual(Path(kwargs["cwd"]), expected_cwd)
        self.assertEqual(args[args.index("-c") + 2], str(RUNTIME))


class RealBrokerTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "the broker refuses to start on Windows")
    def test_a_planted_module_in_the_working_folder_never_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planted, workspace, state = root / "planted", root / "project", root / "state"
            for folder in (planted, workspace):
                folder.mkdir()
            _plant(planted)
            environment = {"HOME": str(root), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                           "XDG_STATE_HOME": str(state)}
            script = ("import sys; sys.path.insert(0, sys.argv[1])\n"
                      "from pathlib import Path\n"
                      "from jev_auto.settings import make_policy, save_policy\n"
                      "from jev_auto.ipc import ensure, request\n"
                      "ws = Path(sys.argv[2])\n"
                      "save_policy(ws, make_policy(ws, 'typesafe', days=1))\n"
                      "ensure(ws)\n"
                      "print(request(ws, {'op': 'health'})['active'])\n"
                      "request(ws, {'op': 'shutdown'})\n")
            result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script, str(RUNTIME), str(workspace)],
                                    cwd=planted, env=environment, capture_output=True, text=True,
                                    timeout=120, check=False)
            markers = sorted(path.name for path in planted.glob("MARKER-*"))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "True")
        self.assertEqual(markers, [])


if __name__ == "__main__":
    unittest.main()
