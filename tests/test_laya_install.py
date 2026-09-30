"""Installing local Laya: build exactly what the attestor verifies, and nothing looser.

No network: the runner that would call venv, pip and the model download is
faked. The final step runs the real attestor over the files the installer
wrote, with a synthetic pin whose weight hash is that of a synthetic model.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import laya_install as li  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402
from src.adl.api.local_attestor import PRODUCTION_PINS, ApprovedPin, attest_local_config  # noqa: E402

WEIGHTS = b"synthetic laya weights for tests only"
PIN = ApprovedPin("aac6fef/laya-mlx", "a" * 40, hashlib.sha256(WEIGHTS).hexdigest(), "c" * 40)
MODEL_FILES = {"model.safetensors": WEIGHTS, "mlx_config.json": b"{}", "tokenizer/tokenizer.json": b"{}"}


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class FakeRunner:
    """Answers the installer's commands the way a working machine would."""

    def __init__(self, version="3.12", fail=None):
        self.calls, self.version, self.fail = [], version, fail

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        joined = " ".join(map(str, command))
        if self.fail and self.fail in joined:
            return subprocess.CompletedProcess(command, 1, "", "failed")
        if "sys.version_info" in joined:
            return ok(self.version)
        if " -m venv " in f" {joined} ":
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\necho 3.12\n")
            python.chmod(0o700)
            return ok()
        if "snapshot_download" in joined:
            target = Path(command[-1])
            for name, body in MODEL_FILES.items():
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(body)
            (target / ".cache" / "huggingface").mkdir(parents=True)
            (target / ".cache" / "huggingface" / "download.lock").write_text("")
            return ok()
        return ok()


class _Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        state = self.base / "state"
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = state / "qualixar-jev-decision-layer"


class PinTests(unittest.TestCase):
    def test_both_published_models_have_an_approved_pin_and_others_are_refused(self):
        self.assertEqual(li.pin_for("english").repository, "aac6fef/laya-mlx")
        self.assertEqual(li.pin_for("multilingual").repository, "aac6fef/laya-multilingual-mlx")
        self.assertTrue(all(li.pin_for(name) in PRODUCTION_PINS for name in li.MODELS))
        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_UNKNOWN"):
            li.pin_for("typed-decisions")


class PreflightTests(unittest.TestCase):
    def check(self, system="Darwin", machine="arm64", version="15.3", git="/usr/bin/git"):
        li.preflight(system=lambda: system, machine=lambda: machine, mac_version=lambda: version,
                     which=lambda name: git)

    def test_an_apple_silicon_mac_with_macos_14_and_git_passes(self):
        self.check()
        self.check(version="14.0")

    def test_everything_else_is_refused_with_a_reason(self):
        for kwargs, code in (({"system": "Linux"}, "LAYA_REQUIRES_APPLE_SILICON"),
                             ({"machine": "x86_64"}, "LAYA_REQUIRES_APPLE_SILICON"),
                             ({"version": "13.6"}, "LAYA_REQUIRES_MACOS_14"),
                             ({"version": ""}, "LAYA_REQUIRES_MACOS_14"),
                             ({"git": None}, "LAYA_REQUIRES_GIT")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(AutoError, code):
                self.check(**kwargs)


class PythonTests(unittest.TestCase):
    def test_the_given_interpreter_must_be_3_11_or_later(self):
        executable = sys.executable
        self.assertEqual(li.find_python(executable, runner=FakeRunner("3.11")), executable)
        with self.assertRaisesRegex(AutoError, "PYTHON_3_11_REQUIRED"):
            li.find_python(executable, runner=FakeRunner("3.10"))
        with self.assertRaisesRegex(AutoError, "PYTHON_3_11_REQUIRED"):
            li.find_python("relative/python", runner=FakeRunner())

    def test_the_system_python_is_never_chosen(self):
        runner = FakeRunner()
        with patch.object(li, "PYTHON_CANDIDATES", ()), patch.object(li.sys, "executable", ""):
            with self.assertRaisesRegex(AutoError, "PYTHON_3_11_REQUIRED"):
                li.find_python(runner=runner, which=lambda name: "/usr/bin/python3")
        self.assertEqual(runner.calls, [])


class StepTests(_Home):
    def where(self):
        return li.paths("english", self.root)

    def test_the_environment_is_built_with_copies_once(self):
        runner = FakeRunner()
        li.create_environment("/opt/python3", self.where(), runner)
        commands = [command for command, _kwargs in runner.calls]
        self.assertEqual(commands[-1], ["/opt/python3", "-m", "venv", "--copies", str(self.where()["env"])])
        runner.calls.clear()
        li.create_environment("/opt/python3", self.where(), runner)
        self.assertFalse(any("venv" in command for command, _kwargs in runner.calls))

    def test_an_old_environment_is_refused(self):
        li.create_environment("/opt/python3", self.where(), FakeRunner())
        with self.assertRaisesRegex(AutoError, "LAYA_ENVIRONMENT_UNUSABLE"):
            li.create_environment("/opt/python3", self.where(), FakeRunner("3.9"))

    def test_the_runtime_comes_from_git_at_the_pinned_commit(self):
        runner = FakeRunner()
        li.install_runtime(PIN, self.where(), runner)
        command, kwargs = runner.calls[0]
        self.assertEqual(command[-1], f"laya-mlx @ git+{li.RUNTIME_REPOSITORY}@{PIN.runtime_commit}")
        self.assertNotIn("PYTHONPATH", kwargs["env"])

    def test_the_download_asks_for_the_pinned_revision_into_the_private_folder(self):
        runner = FakeRunner()
        target = li.download_model(PIN, self.where(), runner)
        command, _kwargs = runner.calls[0]
        self.assertEqual(command[1], "-I")
        self.assertEqual(command[-3:], [PIN.repository, PIN.revision, str(target)])
        self.assertEqual(target, self.where()["model_dir"])

    def test_a_failed_or_missing_step_reports_a_fixed_code(self):
        with self.assertRaisesRegex(AutoError, "LAYA_RUNTIME_INSTALL_FAILED"):
            li.install_runtime(PIN, self.where(), FakeRunner(fail="pip"))

        def missing(command, **kwargs):
            raise FileNotFoundError("no python")

        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_DOWNLOAD_FAILED"):
            li.download_model(PIN, self.where(), missing)
        with patch.dict(os.environ, {"PYTHONPATH": "/evil"}):
            self.assertNotIn("PYTHONPATH", li._environment())


class ManifestTests(_Home):
    def model(self, files=MODEL_FILES):
        folder = self.base / "model"
        for name, body in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(body)
        return folder

    def test_every_visible_file_is_hashed_and_the_weights_must_match_the_pin(self):
        folder = self.model()
        (folder / ".gitattributes").write_text("hidden")
        manifest = li.build_manifest(PIN, folder)
        self.assertEqual(sorted(manifest["files"]), ["mlx_config.json", "model.safetensors", "tokenizer/tokenizer.json"])
        self.assertEqual(manifest["files"]["model.safetensors"], PIN.weight_sha256)
        wrong = ApprovedPin(PIN.repository, PIN.revision, "0" * 64, PIN.runtime_commit)
        with self.assertRaisesRegex(AutoError, "LAYA_WEIGHTS_MISMATCH"):
            li.build_manifest(wrong, folder)

    def test_links_oversize_and_relative_folders_are_refused(self):
        folder = self.model()
        (folder / "link.json").symlink_to(folder / "mlx_config.json")
        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_INVALID"):
            li.build_manifest(PIN, folder)
        (folder / "link.json").unlink()
        with patch.object(li, "MAX_FILES", 2), self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_TOO_LARGE"):
            li.build_manifest(PIN, folder)
        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_INVALID"):
            li.build_manifest(PIN, Path("model"))


class AdoptTests(_Home):
    def snapshot(self, revision):
        blobs = self.base / "hub" / "blobs"
        snap = self.base / "hub" / "snapshots" / revision
        (snap / "tokenizer").mkdir(parents=True)
        blobs.mkdir(parents=True)
        listed = {}
        for name, body in MODEL_FILES.items():
            blob = blobs / hashlib.sha256(body).hexdigest()
            blob.write_bytes(body)
            (snap / name).symlink_to(blob)
            listed[name] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        (snap / "manifest.json").write_text(json.dumps({"files": listed}))
        return snap

    def test_a_cache_snapshot_is_copied_out_of_its_links(self):
        target = li.adopt_folder(PIN, self.snapshot(PIN.revision), li.paths("english", self.root))
        self.assertTrue((target / "model.safetensors").is_file())
        self.assertFalse((target / "model.safetensors").is_symlink())
        self.assertEqual((target / "model.safetensors").read_bytes(), WEIGHTS)
        self.assertFalse(target.with_name(target.name + ".partial").exists())
        li.build_manifest(PIN, target)

    def test_a_snapshot_of_another_revision_is_refused(self):
        with self.assertRaisesRegex(AutoError, "LAYA_REVISION_MISMATCH"):
            li.adopt_folder(PIN, self.snapshot("b" * 40), li.paths("english", self.root))

    def test_a_file_that_disagrees_with_the_models_own_manifest_is_refused(self):
        snap = self.snapshot(PIN.revision)
        (snap / "mlx_config.json").unlink()
        (snap / "mlx_config.json").write_text('{"changed": true}')
        with self.assertRaisesRegex(AutoError, "LAYA_WEIGHTS_MISMATCH"):
            li.adopt_folder(PIN, snap, li.paths("english", self.root))

    def test_an_existing_target_or_relative_source_is_refused(self):
        where = li.paths("english", self.root)
        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_INVALID"):
            li.adopt_folder(PIN, Path("relative"), where)
        li.adopt_folder(PIN, self.snapshot(PIN.revision), where)
        with self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_EXISTS"):
            li.adopt_folder(PIN, self.base / "hub" / "snapshots" / PIN.revision, where)


class EndToEndTests(_Home):
    def attest(self, config):
        return attest_local_config(config, approved_pins=(PIN,), runtime_probe=lambda python: PIN.runtime_commit)

    def install(self, **kwargs):
        with patch.object(li, "pin_for", return_value=PIN):
            return li.install("english", runner=FakeRunner(), attest=self.attest,
                              check_platform=lambda: None, python=sys.executable, **kwargs)

    def test_a_download_install_passes_the_real_attestor_and_writes_a_private_record(self):
        verified = self.install()
        record_path = self.root / "mlx-installation.json"
        record = json.loads(record_path.read_text())
        self.assertEqual(verified, record)
        self.assertEqual(set(record), {"repository", "revision", "weight_sha256", "runtime_commit", "python",
                                       "model_dir", "artifact_manifest"})
        self.assertEqual(record["python"], str(self.root / "mlx-env" / "bin" / "python"))
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        manifest = json.loads(Path(record["artifact_manifest"]).read_text())
        self.assertNotIn(".cache/huggingface/download.lock", manifest["files"])

    def test_the_wizard_offers_laya_once_the_record_verifies(self):
        from src.adl.api.setup_controller import SetupController

        self.install()
        workspace = self.base / "project"
        workspace.mkdir()
        ready = SetupController(workspace, local_attestor=self.attest)
        self.assertTrue(ready.local_ready())
        self.assertFalse(SetupController(workspace, local_attestor=None).local_ready())

    def test_a_record_is_never_written_when_verification_fails(self):
        def refuse(config):
            from src.adl.api.local_attestor import AttestationError

            raise AttestationError("LOCAL_RUNTIME_NOT_APPROVED")

        with patch.object(li, "pin_for", return_value=PIN), \
                self.assertRaisesRegex(AutoError, "LOCAL_RUNTIME_NOT_APPROVED"):
            li.install("english", runner=FakeRunner(), attest=refuse, check_platform=lambda: None,
                       python=sys.executable)
        self.assertFalse((self.root / "mlx-installation.json").exists())

    def test_a_supplied_relative_folder_is_refused_before_anything_runs(self):
        runner = FakeRunner()
        with patch.object(li, "pin_for", return_value=PIN), self.assertRaisesRegex(AutoError, "LAYA_MODEL_FOLDER_INVALID"):
            li.install("english", runner=runner, attest=self.attest, check_platform=lambda: None,
                       python=sys.executable, model_dir="relative")
        self.assertEqual(runner.calls, [])


class CommandTests(unittest.TestCase):
    def run_cli(self, argv, *, tty=True, answer="INSTALL", result=None):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from jev_auto import cli

        out, err, seen = io.StringIO(), io.StringIO(), {}

        def fake_install(model, **kwargs):
            seen.update(model=model, **kwargs)
            return result or {"repository": PIN.repository, "revision": PIN.revision}

        with patch.object(li, "install", side_effect=fake_install), patch.object(cli.sys.stdin, "isatty", return_value=tty), \
                patch("builtins.input", return_value=answer), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue(), seen

    def test_the_command_installs_after_confirmation_and_names_what_it_downloads(self):
        code, out, err, seen = self.run_cli(["laya-install", "--model", "multilingual", "--python", "/opt/py"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"laya_installed": True, "repository": PIN.repository, "revision": PIN.revision})
        self.assertEqual((seen["model"], seen["python"], seen["model_dir"]), ("multilingual", "/opt/py", None))
        self.assertIn("github.com", err)
        self.assertIn("huggingface.co", err)
        self.assertIn("choose Laya only or Jev + Laya", err)

    def test_a_supplied_model_folder_downloads_nothing_from_hugging_face(self):
        _code, _out, err, seen = self.run_cli(["laya-install", "--model-dir", "/models/laya", "--yes"], tty=False)
        self.assertEqual(seen["model_dir"], "/models/laya")
        self.assertNotIn("huggingface.co", err)

    def test_nothing_is_installed_without_a_confirmation(self):
        code, _out, err, seen = self.run_cli(["laya-install"], tty=False)
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("CONFIRMATION_REQUIRED", err)
        code, _out, err, seen = self.run_cli(["laya-install"], answer="yes")
        self.assertEqual((code, seen), (2, {}))
        self.assertIn("SETUP_CANCELLED", err)


class WizardHintTests(unittest.TestCase):
    def hint(self, system, machine):
        from src.adl.api import setup_server

        with patch("platform.system", return_value=system), patch("platform.machine", return_value=machine):
            return setup_server._laya_unavailable_hint()

    def test_a_mac_that_can_run_laya_is_told_the_exact_command(self):
        text = self.hint("Darwin", "arm64")
        self.assertIn("Laya is not installed yet", text)
        self.assertIn("scripts/jev laya-install</code>", text)
        self.assertIn("ask Claude to run it", text)

    def test_a_computer_that_cannot_run_laya_is_told_so_plainly(self):
        for system, machine in (("Linux", "x86_64"), ("Darwin", "x86_64"), ("Windows", "AMD64")):
            with self.subTest(system=system, machine=machine):
                text = self.hint(system, machine)
                self.assertIn("only with an Apple-Silicon Mac", text)
                self.assertNotIn("laya-install", text)


if __name__ == "__main__":
    unittest.main()
