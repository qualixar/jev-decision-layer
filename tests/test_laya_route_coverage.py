"""Coverage for jev_auto's Laya route: the artifact-verification chain that stands between an
enrolled local-model config and ever executing laya_mlx, plus the read-only attestation used by
the setup UI. Every check exercised here exists to catch a swapped, corrupted, or mismatched
model artifact before it is trusted -- a corrupted download or an attacker does not announce
itself, it just changes bytes on disk and hopes nothing notices.

None of the four modules under test require laya_mlx or mlx to be installed: mlx_preflight.py
takes a tokenizer CALLABLE (never a model); mlx_worker.py runs its whole artifact-verification
chain before it ever imports laya_mlx, and the two lines that do import it are exercised here by
injecting fake modules into sys.modules for the duration of one test, never by importing the
real packages at module scope; mlx_process.py imports its worker class lazily and is tested
against the real (stdlib-only) src.adl.providers.laya_worker module with that one class faked
out; local_attestor.py never touches laya_mlx/mlx at all.

House style: stdlib only (unittest, unittest.mock); no pytest fixtures. See
tests/test_core_contracts.py and tests/test_hermes_parity.py for the conventions this mirrors.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# --------------------------------------------------------------------------------------
# Shared, module-level test infrastructure. Nothing here imports laya_mlx or mlx.
# --------------------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _ScriptedTokenizer:
    """Fakes the laya_mlx Tokenizer interface (callable + .mask_token) with exactly one
    synthetic token per character, so a test can hit an exact length threshold by construction.

    The real interface (laya_mlx.tokenizer.Tokenizer, laya-mlx==0.2.0, inspected live from the
    dedicated venv at /Users/v.pratap.bhardwaj/.local/share/laya-venv) is:
        __call__(self, text, add_special_tokens=False) -> {"input_ids": [...]}
        .mask_token: str  -- the real constructor RAISES if it cannot resolve a mask token id,
        so mlx_preflight.py may assume .mask_token always exists as a plain string.
    This fake matches that contract exactly; it just replaces the real BPE tokenizer's
    unpredictable subword counts with a fully deterministic 1-char-to-1-token mapping.
    """

    mask_token = "MASK"

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [1] * len(text)}


# ---- jev_auto.mlx_worker helpers -------------------------------------------------------

def _encode_stdin_lines(requests):
    return b"".join(json.dumps(item).encode() + b"\n" for item in requests)


def _run_worker_raw(mlx_worker, raw_stdin: bytes):
    """Feed raw bytes to jev_auto.mlx_worker.main()'s stdin and return the parsed JSON replies."""
    captured = io.StringIO()
    fake_stdin = SimpleNamespace(buffer=io.BytesIO(raw_stdin))
    with patch.object(mlx_worker.sys, "stdin", fake_stdin), redirect_stdout(captured):
        mlx_worker.main()
    return [json.loads(line) for line in captured.getvalue().splitlines() if line]


def _run_worker(mlx_worker, *requests):
    return _run_worker_raw(mlx_worker, _encode_stdin_lines(requests))


@contextmanager
def _apple_silicon(mlx_worker, is_apple=True):
    """The real host is Apple Silicon macOS, so every load-path test patches platform.system
    and platform.machine AS SEEN BY THE MODULE -- both to reach MLX_APPLE_SILICON_REQUIRED on
    purpose, and to make every OTHER branch reachable deterministically regardless of what
    machine actually runs the suite."""
    system, machine = ("Darwin", "arm64") if is_apple else ("Linux", "x86_64")
    with patch.object(mlx_worker.platform, "system", return_value=system), \
         patch.object(mlx_worker.platform, "machine", return_value=machine):
        yield


def _build_mlx_worker_fixture(root, *, weight_content=b"synthetic-weight-bytes",
                               extra_files=None, extra_manifest_entries=None):
    """A real, on-disk model_dir + manifest + config that passes every artifact check in
    jev_auto.mlx_worker.main()'s 'load' branch, for a fictitious repository (never a
    PRODUCTION_PINS value). Creates a fresh subdirectory under `root` on every call so it is
    safe to call repeatedly against the same TemporaryDirectory."""
    base = Path(tempfile.mkdtemp(dir=root))
    model_dir = base / "model"
    model_dir.mkdir()
    manifest_files = {}

    def _add_real(name, content):
        path = model_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest_files[name] = _sha256_bytes(content)

    _add_real("model.safetensors", weight_content)
    for name, content in (extra_files or {}).items():
        _add_real(name, content)
    manifest_files.update(extra_manifest_entries or {})

    manifest = {"schema_version": 1, "repository": "test/laya", "revision": "a" * 40,
                "files": manifest_files}
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path = base / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o600)
    config = {
        "model_dir": str(model_dir),
        "artifact_manifest": str(manifest_path),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "weight_sha256": manifest_files["model.safetensors"],
        "repository": "test/laya",
    }
    return config, manifest_path, model_dir


def _fake_laya_modules(*, load, device_string="Device(gpu, 0)"):
    """Build fake laya_mlx / mlx / mlx.core module objects for sys.modules injection.

    `import mlx.core as mlx` needs BOTH sys.modules['mlx'] and sys.modules['mlx.core']
    present, AND the parent's .core attribute set -- Python's import system only performs
    that attribute-wiring on a genuine cache MISS, so a bare sys.modules injection with no
    attribute set can fail with AttributeError. Verified live against this exact pattern
    before relying on it.
    """
    fake_core = types.ModuleType("mlx.core")
    fake_core.default_device = lambda: device_string
    fake_pkg = types.ModuleType("mlx")
    fake_pkg.core = fake_core
    fake_laya = types.ModuleType("laya_mlx")
    fake_laya.load = load
    return {"laya_mlx": fake_laya, "mlx": fake_pkg, "mlx.core": fake_core}


@contextmanager
def _loaded_laya(mlx_worker, *, agent, device_string="Device(gpu, 0)", on_load=None):
    """Make jev_auto.mlx_worker.main()'s `import laya_mlx` / `import mlx.core as mlx` resolve
    to fakes for the duration of one call, and pin importlib.metadata.version so the result
    does not depend on whether laya-mlx happens to be pip-registered on the host running the
    suite. laya_mlx/mlx are never imported at this file's module scope -- only injected here,
    inside a test, exactly mirroring how mlx_worker.main() itself imports them lazily."""
    def fake_load(model_dir, **kwargs):
        if on_load is not None:
            on_load(model_dir, kwargs)
        return agent

    modules = _fake_laya_modules(load=fake_load, device_string=device_string)
    with patch.dict(sys.modules, modules), \
         patch.object(mlx_worker.importlib.metadata, "version", return_value="0.0.0-test"):
        yield


# ========================================================================================
# jev_auto.mlx_preflight
# ========================================================================================

class MlxPreflightOptionsRenderingTests(unittest.TestCase):
    """options() renders the exact label text preflight() measures the token length of. A
    mismatch here does not crash -- it silently mis-predicts whether the real laya_mlx call
    would have had to truncate, which is the one thing this module exists to prevent. Expected
    shapes below are cross-checked against the installed laya-mlx==0.2.0
    laya_mlx.common.render_options (see /Users/v.pratap.bhardwaj/.local/share/laya-venv)."""

    def setUp(self):
        from jev_auto.mlx_preflight import options
        self.options = options

    def test_choice_uses_bare_key_only_for_none_or_empty_value(self):
        q = {"type": "choice", "criteria": {
            "bare_none": None, "bare_empty": "", "labelled": "Some text", "structured": {"n": 1},
        }}
        self.assertEqual(self.options(q), [
            "bare_none", "bare_empty", "labelled: Some text", 'structured: {"n": 1}',
        ])

    def test_score_enumerates_levels_in_list_order(self):
        q = {"type": "score", "criteria": ["No fit", "Some fit", 3]}
        self.assertEqual(self.options(q), ["level 0: No fit", "level 1: Some fit", "level 2: 3"])

    def test_noul_defaults_when_criteria_absent_or_empty(self):
        for q in ({"type": "noul"}, {"type": "noul", "criteria": {}}, {"type": "noul", "criteria": None}):
            with self.subTest(criteria=q.get("criteria", "<absent>")):
                self.assertEqual(self.options(q), [
                    "false: no, the statement does not hold",
                    "true: yes, the statement holds",
                ])

    def test_noul_renders_explicit_custom_text(self):
        q = {"type": "noul", "criteria": {"false": "no way", "true": "yes indeed"}}
        self.assertEqual(self.options(q), ["false: no way", "true: yes indeed"])

    def test_noul_renders_falsy_json_values_literally_not_as_missing(self):
        """GUARD (was a confirmed defect, fixed in jev_auto/mlx_preflight.py options(),
        noul/else branch): the branch used to read `c.get('false') or default` /
        `c.get('true') or default`. `x or default` treats 0, 0.0, False, [], {} as falsy, so
        an explicitly-supplied falsy criterion silently reverted to the long default text
        instead of being rendered -- unlike this SAME file's own `choice` branch, which
        correctly guarded with `v in (None, '')`, and unlike the real
        laya_mlx.common.render_options (laya-mlx==0.2.0, installed at
        /Users/v.pratap.bhardwaj/.local/share/laya-venv), which guards with
        `false_crit not in (None, "")`:
            >>> import laya_mlx.common as c
            >>> c.render_options({"t": "noul", "ins": "x", "crit": {"false": 0, "true": 1}})
            ['false: 0', 'true: 1']
        The bug mattered because preflight() measures the token length of whatever options()
        returns: rendering the wrong (much longer) default label could make preflight refuse
        a request as MLX_RUBRIC_WOULD_TRUNCATE / MLX_INSTRUCTIONS_WOULD_TRUNCATE /
        MLX_STATE_WOULD_TRUNCATE that the real laya_mlx call would have accepted. Now fixed:
        only None/'' fall back to the default text; 0/False/0.0 render literally.
        """
        q = {"type": "noul", "instructions": "Is the count zero?", "criteria": {"false": 0, "true": 1}}
        self.assertEqual(self.options(q), ["false: 0", "true: 1"])
        q_bool = {"type": "noul", "instructions": "On?", "criteria": {"false": False, "true": True}}
        self.assertEqual(self.options(q_bool), ["false: false", "true: true"])


class MlxPreflightTruncationTests(unittest.TestCase):
    """preflight(tok, cfg, state, qs) re-implements, in refuse-instead-of-truncate form, the
    exact prefix-building arithmetic of laya_mlx.common.build_prefix/build_sequence -- verified
    line-by-line against the installed laya-mlx==0.2.0 source. It takes a tokenizer CALLABLE,
    not a model, so every refusal branch is reachable with no weights and no laya_mlx
    installed. Numeric expectations below were captured by RUNNING preflight() with this exact
    ScriptedTokenizer, not hand-derived -- hand arithmetic on this function is error-prone (an
    earlier draft of this file got two of them wrong before switching to this approach)."""

    def setUp(self):
        from jev_auto.mlx_preflight import preflight
        self.preflight = preflight
        self.tok = _ScriptedTokenizer()

    def test_small_request_computes_the_documented_total_for_every_question(self):
        qs = {
            "decision": {"type": "choice", "instructions": "Pick one", "criteria": {"a": "", "b": ""}},
            "second": {"type": "noul", "instructions": "Ready?"},
        }
        counts = self.preflight(self.tok, {}, "state", qs)
        self.assertEqual(counts, {"decision": 40, "second": 102})

    def test_non_string_state_is_json_encoded_before_measuring(self):
        counts = self.preflight(self.tok, {}, {"k": "v"}, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(counts, {"q": 102})

    def test_option_would_truncate_when_a_single_rendered_option_exceeds_forty_eight_tokens(self):
        from jev_auto.common import AutoError
        qs = {"q": {"type": "score", "instructions": "Rate", "criteria": ["x" * 50]}}
        with self.assertRaisesRegex(AutoError, "MLX_OPTION_WOULD_TRUNCATE"):
            self.preflight(self.tok, {}, "s", qs)

    def test_rubric_would_truncate_when_many_options_cannot_share_the_head_budget(self):
        from jev_auto.common import AutoError
        # Each option is 44 tokens (<=48, so it individually would not truncate), but five of
        # them together blow the head budget, and each exceeds its fair per-option share.
        qs = {"q": {"type": "score", "instructions": "Rate", "criteria": ["x" * 35] * 5}}
        with self.assertRaisesRegex(AutoError, "MLX_RUBRIC_WOULD_TRUNCATE"):
            self.preflight(self.tok, {}, "s", qs)

    def test_instructions_would_truncate_when_head_text_exceeds_the_remaining_budget(self):
        from jev_auto.common import AutoError
        qs = {"q": {"type": "choice", "instructions": "y" * 300, "criteria": {"only": ""}}}
        with self.assertRaisesRegex(AutoError, "MLX_INSTRUCTIONS_WOULD_TRUNCATE"):
            self.preflight(self.tok, {}, "s", qs)

    def test_state_would_truncate_when_total_exceeds_a_small_configured_max_len(self):
        from jev_auto.common import AutoError
        qs = {"q": {"type": "choice", "instructions": "hi", "criteria": {"only": ""}}}
        with self.assertRaisesRegex(AutoError, "MLX_STATE_WOULD_TRUNCATE"):
            self.preflight(self.tok, {"max_len": 20}, "s" * 100, qs)

    def test_every_question_in_the_batch_is_measured_independently(self):
        qs = {
            "a": {"type": "choice", "instructions": "A", "criteria": {"x": ""}},
            "b": {"type": "score", "instructions": "B", "criteria": ["lo", "hi"]},
            "c": {"type": "noul", "instructions": "C"},
        }
        counts = self.preflight(self.tok, {}, "s", qs)
        self.assertEqual(set(counts), {"a", "b", "c"})
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in counts.values()))


# ========================================================================================
# jev_auto.mlx_worker
# ========================================================================================

class MlxWorkerVerificationChainTests(unittest.TestCase):
    """jev_auto.mlx_worker.main() runs its entire artifact-verification chain BEFORE ever
    importing laya_mlx. These are the checks that stop a swapped, corrupted, symlinked, or
    hardlinked model file from ever reaching the loader -- they are the whole point of the
    module. Every case here is driven through main()'s public stdin/stdout JSON protocol, not
    by calling a private helper directly."""

    def setUp(self):
        from jev_auto import mlx_worker
        self.mlx_worker = mlx_worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, *requests):
        return _run_worker(self.mlx_worker, *requests)

    def test_predict_or_any_unrecognized_kind_before_a_load_is_refused(self):
        for request in ({"kind": "predict", "state": "s", "questions": {}}, {"kind": "not-a-real-kind"}):
            with self.subTest(kind=request["kind"]):
                [reply] = self._run(request)
                self.assertEqual(reply, {"ok": False, "error": "MLX_LOAD_FIRST"})

    def test_oversized_or_malformed_stdin_line_is_rejected_without_crashing(self):
        [reply] = _run_worker_raw(self.mlx_worker, b"x" * 200_000 + b"\n")
        self.assertEqual(reply, {"ok": False, "error": "MESSAGE_TOO_LARGE"})
        [reply2] = _run_worker_raw(self.mlx_worker, b"{not json\n")
        self.assertEqual(reply2, {"ok": False, "error": "INVALID_JSON"})

    def test_a_nonexistent_manifest_path_is_refused_as_unsafe_not_a_crash(self):
        """_hash_file's `except OSError: raise AutoError('MLX_ARTIFACT_UNSAFE')` (its own
        os.open call) is distinct from safe_path's earlier symlink-ancestor scan -- a missing
        file (ENOENT) never involves a symlink, so it reaches _hash_file's own os.open and is
        refused there."""
        model_dir = self.root / "model"
        model_dir.mkdir()
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": {
                "model_dir": str(model_dir), "manifest_sha256": "0" * 64,
                "artifact_manifest": str(self.root / "does-not-exist.json")}})
        self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_UNSAFE"})

    def test_an_unexpected_non_autoerror_exception_is_normalized_to_runtime_failure(self):
        """A malformed 'load' request missing the 'config' key raises a plain KeyError, not
        an AutoError -- proving the broad `except Exception` fallback exists and never leaks
        the raw exception text or a traceback back over the wire."""
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load"})
        self.assertEqual(reply, {"ok": False, "error": "MLX_RUNTIME_FAILURE"})

    def test_load_requires_apple_silicon_darwin_arm64(self):
        for system, machine in (("Linux", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64")):
            with self.subTest(system=system, machine=machine):
                with patch.object(self.mlx_worker.platform, "system", return_value=system), \
                     patch.object(self.mlx_worker.platform, "machine", return_value=machine):
                    [reply] = self._run({"kind": "load", "config": {}})
                self.assertEqual(reply, {"ok": False, "error": "MLX_APPLE_SILICON_REQUIRED"})

    def test_load_requires_an_existing_model_directory(self):
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": {"model_dir": str(self.root / "missing")}})
        self.assertEqual(reply, {"ok": False, "error": "MLX_MODEL_DIRECTORY"})

    def test_manifest_sha_must_be_a_bound_sixty_four_character_string(self):
        model_dir = self.root / "model"
        model_dir.mkdir()
        for bad in (None, "short", 12345, "a" * 63, "a" * 65):
            with self.subTest(bad=bad):
                with _apple_silicon(self.mlx_worker):
                    [reply] = self._run({"kind": "load", "config": {
                        "model_dir": str(model_dir), "manifest_sha256": bad}})
                self.assertEqual(reply, {"ok": False, "error": "MLX_MANIFEST_UNBOUND"})

    def test_manifest_hash_mismatch_is_refused_by_the_first_check(self):
        model_dir = self.root / "model"
        model_dir.mkdir()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_bytes(b'{"anything": true}')  # deliberately NOT chmod 0600
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": {
                "model_dir": str(model_dir), "manifest_sha256": "0" * 64,
                "artifact_manifest": str(manifest_path)}})
        self.assertEqual(reply, {"ok": False, "error": "MLX_MANIFEST_CHANGED"})

    def test_manifest_that_is_a_directory_hardlink_or_symlink_is_refused_as_unsafe(self):
        model_dir = self.root / "model"
        model_dir.mkdir()

        directory_as_manifest = self.root / "manifest_dir"
        directory_as_manifest.mkdir()

        real_manifest = self.root / "real_manifest.json"
        real_manifest.write_bytes(b"{}")
        real_manifest.chmod(0o600)
        hardlinked_manifest = self.root / "hardlinked_manifest.json"
        os.link(real_manifest, hardlinked_manifest)

        symlink_target = self.root / "symlink_target.json"
        symlink_target.write_bytes(b"{}")
        symlink_target.chmod(0o600)
        symlinked_manifest = self.root / "symlinked_manifest.json"
        symlinked_manifest.symlink_to(symlink_target)

        # A symlinked path is caught even earlier, by safe_path()'s own ancestor-symlink
        # scan (before _hash_file ever reaches os.open/O_NOFOLLOW) -- a stronger, earlier
        # refusal than MLX_ARTIFACT_UNSAFE, not a weaker one.
        cases = {"directory": (directory_as_manifest, "MLX_ARTIFACT_UNSAFE"),
                 "hardlink": (hardlinked_manifest, "MLX_ARTIFACT_UNSAFE"),
                 "symlink": (symlinked_manifest, "SYMLINK_NOT_ALLOWED")}
        for name, (manifest_path, expected_error) in cases.items():
            with self.subTest(kind=name):
                with _apple_silicon(self.mlx_worker):
                    [reply] = self._run({"kind": "load", "config": {
                        "model_dir": str(model_dir), "manifest_sha256": "0" * 64,
                        "artifact_manifest": str(manifest_path)}})
                self.assertEqual(reply, {"ok": False, "error": expected_error})

    def test_manifest_toctou_between_the_two_hash_checks_is_caught_by_the_second(self):
        """MUTATION TARGET 1: main() calls _hash_file(manifest_path) once BEFORE
        read_private() and once again immediately AFTER -- specifically to catch a manifest
        swapped in that gap. Simulate the swap by mutating the file from inside a patched
        read_private, between the two checks."""
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        original_read_private = self.mlx_worker.read_private

        def mutate_then_read(path, limit):
            Path(path).write_bytes(b"{}")  # same inode, different content, mid-verification
            return original_read_private(path, limit)

        with _apple_silicon(self.mlx_worker), \
             patch.object(self.mlx_worker, "read_private", side_effect=mutate_then_read):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_MANIFEST_CHANGED"})

    def test_hash_file_own_before_after_identity_check_catches_a_mutation_during_the_read(self):
        """MUTATION TARGET 2: _hash_file's own before/after fstat + named-lstat identity
        comparison. Mutate the manifest file between _hash_file's own two internal fstat
        calls (not between two separate _hash_file calls) by trapping os.fstat for exactly
        this file's descriptor."""
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        target_path = os.fspath(manifest_path)
        real_open, real_fstat = os.open, os.fstat
        state = {"target_fd": None, "hits": 0}

        def trapped_open(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == target_path:
                state["target_fd"] = fd
            return fd

        def trapped_fstat(fd, *a, **kw):
            if fd == state["target_fd"]:
                state["hits"] += 1
                if state["hits"] == 2:
                    manifest_path.write_bytes(b'{"mutated": true, "padding": "xxxxxxxxxxxx"}')
            return real_fstat(fd, *a, **kw)

        with _apple_silicon(self.mlx_worker), \
             patch.object(self.mlx_worker.os, "open", side_effect=trapped_open), \
             patch.object(self.mlx_worker.os, "fstat", side_effect=trapped_fstat):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_CHANGED"})
        self.assertGreaterEqual(state["hits"], 2)

    def test_weight_hash_mismatch_between_manifest_and_config_is_refused(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        config["weight_sha256"] = "f" * 64  # does not match the manifest's declared hash
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_WEIGHT_HASH"})

    def test_absolute_or_traversal_manifest_file_names_are_refused_before_any_open(self):
        for bad_name in ("/etc/passwd", "../escape.bin", "sub/../../escape.bin"):
            with self.subTest(bad_name=bad_name):
                config, manifest_path, model_dir = _build_mlx_worker_fixture(
                    self.root, extra_manifest_entries={bad_name: "0" * 64})
                with _apple_silicon(self.mlx_worker):
                    [reply] = self._run({"kind": "load", "config": config})
                self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_PATH"})

    def test_artifact_hash_mismatch_between_manifest_and_real_file_is_refused(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        (model_dir / "model.safetensors").write_bytes(b"the-real-file-was-swapped-after-enrollment")
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_HASH"})

    def test_hardlinked_weight_file_is_refused_as_unsafe(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        real_weight = model_dir / "model.safetensors"
        hardlink = self.root / "hardlink.bin"
        os.link(real_weight, hardlink)
        with _apple_silicon(self.mlx_worker):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_UNSAFE"})


class MlxWorkerLoadedModelTests(unittest.TestCase):
    """Everything after `import laya_mlx` -- device detection, the re-verification performed
    immediately after load, and the predict hand-off -- faked via sys.modules injection, never
    real weights. laya_mlx and mlx are never imported at this file's module scope; they are
    injected only for the duration of one test, exactly mirroring how main() itself imports
    them: lazily, inside the function body, only after the artifact chain above has already
    verified everything on disk."""

    def setUp(self):
        from jev_auto import mlx_worker
        self.mlx_worker = mlx_worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, *requests):
        return _run_worker(self.mlx_worker, *requests)

    def test_device_detection_covers_gpu_cpu_and_the_unknown_refusal(self):
        cases = {
            "Device(gpu, 0)": ("ready", "gpu"),
            "Device(cpu, 0)": ("ready", "cpu"),
            "Device(unrecognized, 0)": ("error", "MLX_DEVICE_UNKNOWN"),
        }
        for device_string, (kind, expected) in cases.items():
            with self.subTest(device_string=device_string):
                config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
                with _apple_silicon(self.mlx_worker), \
                     _loaded_laya(self.mlx_worker, agent=SimpleNamespace(), device_string=device_string):
                    [reply] = self._run({"kind": "load", "config": config})
                if kind == "ready":
                    self.assertTrue(reply["ok"], reply)
                    self.assertEqual(reply["result"]["default_device"], expected)
                    self.assertEqual(reply["result"]["checkpoint"], config["repository"])
                else:
                    self.assertEqual(reply, {"ok": False, "error": expected})

    def test_artifact_changed_during_load_is_caught_by_the_post_load_reverify(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)

        def swap_weights_during_load(_model_dir, _kwargs):
            (model_dir / "model.safetensors").write_bytes(b"swapped-in-during-the-fake-load-call")

        with _apple_silicon(self.mlx_worker), \
             _loaded_laya(self.mlx_worker, agent=SimpleNamespace(), on_load=swap_weights_during_load):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_ARTIFACT_CHANGED_DURING_LOAD"})

    def test_manifest_changed_during_load_is_caught_after_the_per_file_reverify_passes(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)

        def swap_manifest_during_load(_model_dir, _kwargs):
            manifest_path.write_bytes(json.dumps({
                "schema_version": 1, "repository": "test/laya", "revision": "a" * 40, "files": {},
            }).encode())

        with _apple_silicon(self.mlx_worker), \
             _loaded_laya(self.mlx_worker, agent=SimpleNamespace(), on_load=swap_manifest_during_load):
            [reply] = self._run({"kind": "load", "config": config})
        self.assertEqual(reply, {"ok": False, "error": "MLX_MANIFEST_CHANGED_DURING_LOAD"})

    def test_successful_load_then_predict_reaches_the_real_preflight_and_agent_predict_calls(self):
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        agent = SimpleNamespace(
            tok=_ScriptedTokenizer(), cfg={},
            predict=lambda state, questions: {"answers": {"q": {"type": "noul", "noul": 0.5}}},
        )
        with _apple_silicon(self.mlx_worker), _loaded_laya(self.mlx_worker, agent=agent):
            load_reply, predict_reply = self._run(
                {"kind": "load", "config": config},
                {"kind": "predict", "state": "s", "questions": {
                    "q": {"type": "noul", "instructions": "Ready?"}}},
            )
        self.assertTrue(load_reply["ok"], load_reply)
        self.assertTrue(predict_reply["ok"], predict_reply)
        self.assertEqual(predict_reply["result"]["answers"], {"q": {"type": "noul", "noul": 0.5}})
        self.assertEqual(predict_reply["result"]["model"], config["repository"])

    def test_predict_still_enforces_preflight_even_after_a_successful_load(self):
        """MUTATION TARGET (bonus): also proves call order -- preflight() runs before
        agent.predict(). If it did not, agent.predict below would run and raise
        AssertionError, which the broad except turns into MLX_RUNTIME_FAILURE instead of the
        MLX_STATE_WOULD_TRUNCATE asserted here."""
        config, manifest_path, model_dir = _build_mlx_worker_fixture(self.root)
        agent = SimpleNamespace(
            tok=_ScriptedTokenizer(), cfg={"max_len": 20},
            predict=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("agent.predict must not run when preflight refuses")),
        )
        with _apple_silicon(self.mlx_worker), _loaded_laya(self.mlx_worker, agent=agent):
            _, predict_reply = self._run(
                {"kind": "load", "config": config},
                {"kind": "predict", "state": "s" * 200, "questions": {
                    "q": {"type": "noul", "instructions": "Ready?"}}},
            )
        self.assertEqual(predict_reply, {"ok": False, "error": "MLX_STATE_WOULD_TRUNCATE"})


# ========================================================================================
# jev_auto.mlx_process
# ========================================================================================

class MlxProcessFacadeTests(unittest.TestCase):
    """MLXProcess is a thin, stateful facade in front of SandboxedLayaProcess: it must not
    call predict() before a successful warmup(), must discard a dead worker on failure, and
    must keep a live worker alive across a recoverable 'would truncate' refusal so the caller
    can just retry with shorter input instead of paying a full cold-start. It imports
    src.adl.providers.laya_worker LAZILY inside _create_worker -- patched here on the real
    module (stdlib-only, no mlx dependency), never sys.modules-faked."""

    def setUp(self):
        from jev_auto.mlx_process import MLXProcess
        self.MLXProcess = MLXProcess
        self.cfg = {"revision": "a" * 40, "weight_sha256": "b" * 64,
                    "model_dir": "/synthetic/model", "python": "/synthetic/python"}

    @staticmethod
    def _patched_worker_class(fake_factory):
        import src.adl.providers.laya_worker as laya_worker
        return patch.object(laya_worker, "SandboxedLayaProcess", fake_factory)

    def test_create_worker_normalizes_any_whitelisted_construction_failure(self):
        from jev_auto.common import AutoError
        from src.adl.providers.laya_worker import WorkerError

        for error in (KeyError("weight_sha256"), TypeError("bad"), ValueError("bad"),
                       WorkerError("ARTIFACT_CONFIG_MISMATCH")):
            with self.subTest(error=type(error).__name__):
                def boom(*_a, **_kw):
                    raise error

                process = self.MLXProcess(self.cfg)
                with self._patched_worker_class(boom):
                    with self.assertRaisesRegex(AutoError, "MLX_ARTIFACT_NOT_READY"):
                        process._create_worker()

    def test_warmup_success_records_process_and_telemetry(self):
        fake_process = object()
        fake_worker = SimpleNamespace(
            warmup=lambda timeout: {"ready": True, "extra": "diagnostic"},
            _process=fake_process, close=lambda: None,
        )
        process = self.MLXProcess(self.cfg)
        with self._patched_worker_class(lambda *a, **kw: fake_worker):
            result = process.warmup()
        self.assertEqual(result, {"ready": True, "provider": "laya-mlx"})
        self.assertIs(process.proc, fake_process)
        self.assertEqual(process.last_telemetry, {"extra": "diagnostic"})

    def test_warmup_failure_discards_the_worker_and_raises_a_fixed_code(self):
        from jev_auto.common import AutoError
        from src.adl.providers.laya_worker import WorkerError

        closed = []
        fake_worker = SimpleNamespace(
            warmup=lambda timeout: (_ for _ in ()).throw(WorkerError("OFFLINE_SANDBOX_UNAVAILABLE")),
            close=lambda: closed.append(True),
        )
        process = self.MLXProcess(self.cfg)
        with self._patched_worker_class(lambda *a, **kw: fake_worker):
            with self.assertRaisesRegex(AutoError, "MLX_WARMUP_FAILED"):
                process.warmup()
        self.assertIsNone(process._worker)
        self.assertEqual(closed, [True])

    def test_predict_requires_a_prior_successful_warmup(self):
        from jev_auto.common import AutoError
        process = self.MLXProcess(self.cfg)
        with self.assertRaisesRegex(AutoError, "MLX_WARMUP_REQUIRED"):
            process.predict("state", {}, timeout=5)

    def test_predict_success_updates_process_and_telemetry(self):
        fake_process = object()
        fake_worker = SimpleNamespace(_process=fake_process)
        fake_worker.predict = lambda state, questions, timeout: {
            "output": {"answers": {}}, "telemetry": {"cold": False}}
        process = self.MLXProcess(self.cfg)
        process._worker = fake_worker
        process.proc = fake_process
        result = process.predict("state", {}, timeout=5)
        self.assertEqual(result, {"answers": {}})
        self.assertEqual(process.last_telemetry, {"cold": False})

    def test_predict_keeps_a_live_worker_for_a_recoverable_would_truncate_refusal(self):
        from jev_auto.common import AutoError
        from src.adl.providers.laya_worker import WorkerError

        fake_process = object()
        fake_worker = SimpleNamespace(_process=fake_process)
        fake_worker.predict = lambda *a, **kw: (_ for _ in ()).throw(WorkerError("MLX_STATE_WOULD_TRUNCATE"))
        process = self.MLXProcess(self.cfg)
        process._worker = fake_worker
        process.proc = fake_process
        with self.assertRaisesRegex(AutoError, "MLX_STATE_WOULD_TRUNCATE"):
            process.predict("state", {}, timeout=5)
        self.assertIsNotNone(process._worker, "a recoverable refusal must not discard the live worker")

    def test_predict_discards_the_worker_on_a_non_recoverable_failure(self):
        from jev_auto.common import AutoError
        from src.adl.providers.laya_worker import WorkerError

        fake_process = object()
        closed = []
        fake_worker = SimpleNamespace(_process=fake_process, close=lambda: closed.append(True))
        fake_worker.predict = lambda *a, **kw: (_ for _ in ()).throw(WorkerError("MLX_WORKER_STOPPED"))
        process = self.MLXProcess(self.cfg)
        process._worker = fake_worker
        process.proc = fake_process
        with self.assertRaisesRegex(AutoError, "MLX_WORKER_FAILURE"):
            process.predict("state", {}, timeout=5)
        self.assertIsNone(process._worker)
        self.assertEqual(closed, [True])

    def test_close_cancels_the_worker_before_taking_its_own_lock(self):
        closed = []
        fake_worker = SimpleNamespace(close=lambda: closed.append(True))
        process = self.MLXProcess(self.cfg)
        process._worker = fake_worker
        process.proc = object()
        process.last_telemetry = {"x": 1}
        process.close()
        self.assertEqual(closed, [True])
        self.assertIsNone(process._worker)
        self.assertIsNone(process.proc)
        self.assertIsNone(process.last_telemetry)


# ========================================================================================
# src.adl.api.local_attestor
# ========================================================================================

class LocalAttestorTests(unittest.TestCase):
    """attest_local_config proves an ALREADY-INSTALLED local artifact still matches an
    approved (repository, revision, weight hash, runtime commit) pin -- read-only, and
    explicitly NOT a sandbox-isolation guarantee (see the module docstring). approved_pins,
    expected_python and runtime_probe are all injectable seams, exactly so a test never needs
    the two hardcoded PRODUCTION_PINS or a real laya-mlx runtime install."""

    def setUp(self):
        from src.adl.api.local_attestor import ApprovedPin, AttestationError, attest_local_config
        self.ApprovedPin = ApprovedPin
        self.AttestationError = AttestationError
        self.attest = attest_local_config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.python_path = self.root / "python-stub"
        self.python_path.write_text("#!/bin/sh\nexit 0\n")
        self.python_path.chmod(0o700)

    def _install(self, *, weight_content=b"weights", manifest_overrides=None, config_overrides=None):
        model_dir = Path(tempfile.mkdtemp(dir=self.root))
        weight_path = model_dir / "model.safetensors"
        weight_path.write_bytes(weight_content)
        weight_hash = _sha256_bytes(weight_content)
        manifest = {"schema_version": 1, "repository": "test/laya", "revision": "a" * 40,
                    "files": {"model.safetensors": weight_hash}}
        if manifest_overrides:
            manifest.update(manifest_overrides)
        manifest_path = self.root / (model_dir.name + ".manifest.json")
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o600)
        config = {"repository": "test/laya", "revision": "a" * 40, "weight_sha256": weight_hash,
                  "model_dir": str(model_dir), "artifact_manifest": str(manifest_path),
                  "runtime_commit": "c" * 40, "python": str(self.python_path)}
        if config_overrides:
            config.update(config_overrides)
        pin = self.ApprovedPin(config["repository"], config["revision"], weight_hash, config["runtime_commit"])
        return config, pin, model_dir, manifest_path

    # ---- happy path -----------------------------------------------------------------

    def test_valid_installation_matches_an_explicit_approved_pin(self):
        config, pin, model_dir, manifest_path = self._install()
        result = self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                              runtime_probe=lambda python: pin.runtime_commit)
        self.assertEqual(result, config)
        self.assertIsNot(result, config)

    def test_nested_file_entries_are_resolved_through_the_directory_walk(self):
        config, pin, model_dir, manifest_path = self._install()
        nested_dir = model_dir / "weights"
        nested_dir.mkdir()
        nested_content = b"nested-weight-shard"
        (nested_dir / "shard0.bin").write_bytes(nested_content)
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["weights/shard0.bin"] = hashlib.sha256(nested_content).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        result = self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                              runtime_probe=lambda python: pin.runtime_commit)
        self.assertEqual(result["model_dir"], config["model_dir"])

    # ---- config shape / platform / pin -----------------------------------------------

    def test_config_shape_is_rejected_before_anything_else_is_touched(self):
        with self.subTest(bad="not_a_dict"):
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                self.attest(["not", "a", "dict"])
        with self.subTest(bad="unexpected_key"):
            config, pin, model_dir, manifest_path = self._install()
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                self.attest({**config, "unexpected_extra_key": "x"}, approved_pins=(pin,))

    def test_platform_without_directory_scoped_open_support_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        with patch("src.adl.api.local_attestor._supports_secure_file_open", return_value=False):
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_PLATFORM_UNVERIFIED"):
                self.attest(config, approved_pins=(pin,))

    def test_repository_revision_and_weight_hash_shape_are_validated(self):
        config, pin, model_dir, manifest_path = self._install()
        bad_variants = {
            "repository_not_string": {**config, "repository": 123},
            "revision_wrong_length": {**config, "revision": "a" * 39},
            "revision_not_hex": {**config, "revision": "g" * 40},
            "weight_not_string": {**config, "weight_sha256": 123},
            "weight_wrong_length": {**config, "weight_sha256": "b" * 63},
            "model_dir_not_string": {**config, "model_dir": 123},
            "artifact_manifest_not_string": {**config, "artifact_manifest": 123},
        }
        for name, bad_config in bad_variants.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                    self.attest(bad_config, approved_pins=(pin,))

    def test_pin_not_in_the_approved_set_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        other_pin = self.ApprovedPin("other/repo", "d" * 40, "e" * 64, "f" * 40)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_PIN_NOT_APPROVED"):
            self.attest(config, approved_pins=(other_pin,))

    def test_model_dir_and_manifest_paths_must_be_absolute(self):
        config, pin, model_dir, manifest_path = self._install(
            config_overrides={"model_dir": "relative/model"})
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    # ---- python interpreter attestation ------------------------------------------------

    def test_python_field_shape_and_executability_are_all_verified(self):
        config, pin, model_dir, manifest_path = self._install()
        not_a_file = self.root / "no-such-interpreter"
        not_executable = self.root / "not-executable"
        not_executable.write_text("#!/bin/sh\n")
        not_executable.chmod(0o600)
        cases = {
            "python_field_not_a_string": ({**config, "python": 12345}, self.python_path),
            "python_field_does_not_match_expected_python": ({**config, "python": str(not_a_file)}, self.python_path),
            "expected_python_missing_on_disk": ({**config, "python": str(not_a_file)}, not_a_file),
            "expected_python_exists_but_is_not_executable": (
                {**config, "python": str(not_executable)}, not_executable),
        }
        for name, (bad_config, expected_python) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                    self.attest(bad_config, approved_pins=(pin,), expected_python=expected_python,
                                runtime_probe=lambda python: pin.runtime_commit)

    def test_default_expected_python_resolves_legacy_vs_current_install_path(self):
        from src.adl.api import local_attestor as attestor_module
        config, pin, model_dir, manifest_path = self._install()
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            legacy_python = home / ".local" / "state" / "qualixar-jev-auto" / "mlx-env" / "bin" / "python"
            legacy_python.parent.mkdir(parents=True)
            legacy_python.write_text("#!/bin/sh\n")
            legacy_python.chmod(0o700)
            current_state = home / "new-state" / "qualixar-jev-decision-layer"
            current_python = current_state / "mlx-env" / "bin" / "python"
            current_python.parent.mkdir(parents=True)
            current_python.write_text("#!/bin/sh\n")
            current_python.chmod(0o700)
            with patch.object(Path, "home", return_value=home), \
                 patch.object(attestor_module, "home_root", return_value=current_state):
                legacy_result = self.attest({**config, "python": str(legacy_python)}, approved_pins=(pin,),
                                             runtime_probe=lambda python: pin.runtime_commit)
                self.assertEqual(legacy_result["python"], str(legacy_python))

                current_result = self.attest({**config, "python": str(current_python)}, approved_pins=(pin,),
                                              runtime_probe=lambda python: pin.runtime_commit)
                self.assertEqual(current_result["python"], str(current_python))

    def test_expected_python_itself_behind_a_symlinked_parent_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        real_dir = self.root / "real-bin-dir"
        real_dir.mkdir()
        real_python = real_dir / "python"
        real_python.write_text("#!/bin/sh\n")
        real_python.chmod(0o700)
        symlinked_dir = self.root / "symlinked-bin-dir"
        symlinked_dir.symlink_to(real_dir)
        expected_python = symlinked_dir / "python"
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.attest({**config, "python": str(expected_python)}, approved_pins=(pin,),
                        expected_python=expected_python, runtime_probe=lambda python: pin.runtime_commit)

    def test_runtime_probe_exception_and_wrong_commit_are_both_refused(self):
        config, pin, model_dir, manifest_path = self._install()

        def boom(python):
            raise RuntimeError("synthetic probe failure")

        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path, runtime_probe=boom)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: "wrong-commit")

    # ---- manifest / model_dir content shape --------------------------------------------

    def test_manifest_content_shape_is_validated(self):
        base_config, base_pin, model_dir, manifest_path = self._install()

        def with_manifest(name, overrides):
            manifest = json.loads(manifest_path.read_text())
            manifest.update(overrides)
            path = self.root / f"manifest-{name}.json"
            path.write_text(json.dumps(manifest))
            path.chmod(0o600)
            return {**base_config, "artifact_manifest": str(path)}

        variants = {
            "wrong_schema_version": with_manifest("schema", {"schema_version": 2}),
            "repository_mismatch": with_manifest("repo", {"repository": "other/repo"}),
            "revision_mismatch": with_manifest("revision", {"revision": "d" * 40}),
            "files_not_a_dict": with_manifest("not-dict", {"files": ["not", "a", "dict"]}),
            "files_empty": with_manifest("empty", {"files": {}}),
            "model_safetensors_hash_missing": with_manifest("no-weight", {"files": {"other.bin": "0" * 64}}),
        }
        for name, config in variants.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                    self.attest(config, approved_pins=(base_pin,), expected_python=self.python_path,
                                runtime_probe=lambda python: base_pin.runtime_commit)

    def test_manifest_with_too_many_file_entries_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest = json.loads(manifest_path.read_text())
        for i in range(64):
            manifest["files"][f"extra-{i}.bin"] = "0" * 64
        self.assertGreater(len(manifest["files"]), 64)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_manifest_json_that_is_not_an_object_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest_path.write_text("[1, 2, 3]")  # valid JSON, not a dict
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_world_readable_manifest_is_refused_not_silently_trusted(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest_path.chmod(0o644)  # group/other readable
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_symlinked_manifest_path_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        real_target = self.root / "real-manifest-target.json"
        real_target.write_bytes(manifest_path.read_bytes())
        real_target.chmod(0o600)
        manifest_path.unlink()
        manifest_path.symlink_to(real_target)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_model_dir_must_exist_and_be_a_real_directory(self):
        config, pin, model_dir, manifest_path = self._install()
        shutil.rmtree(model_dir)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    # ---- per-file artifact verification -------------------------------------------------

    def test_malicious_manifest_file_names_are_refused_by_path_safety(self):
        config, pin, model_dir, manifest_path = self._install()
        base_manifest = json.loads(manifest_path.read_text())
        bad_names = ("/etc/passwd", "../escape.bin", "a/../../escape.bin", "a\\b", "a//b", "", ".", "..")
        for bad_name in bad_names:
            with self.subTest(bad_name=bad_name):
                variant = {**base_manifest, "files": {**base_manifest["files"], bad_name: "0" * 64}}
                path = self.root / f"manifest-{hash(bad_name)}.json"
                path.write_text(json.dumps(variant))
                path.chmod(0o600)
                bad_config = {**config, "artifact_manifest": str(path)}
                with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                    self.attest(bad_config, approved_pins=(pin,), expected_python=self.python_path,
                                runtime_probe=lambda python: pin.runtime_commit)

    def test_file_hash_mismatch_and_non_hex_expected_hash_are_both_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest = json.loads(manifest_path.read_text())
        (model_dir / "extra.bin").write_bytes(b"real-extra-content")

        manifest["files"]["extra.bin"] = hashlib.sha256(b"different-content").hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.subTest("hash_mismatch"):
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                            runtime_probe=lambda python: pin.runtime_commit)

        manifest["files"]["extra.bin"] = "not-valid-hex"
        manifest_path.write_text(json.dumps(manifest))
        with self.subTest("not_hex"):
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                            runtime_probe=lambda python: pin.runtime_commit)

    def test_missing_referenced_file_fails_closed(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["never-written.bin"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_zero_byte_artifact_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest = json.loads(manifest_path.read_text())
        (model_dir / "empty.bin").write_bytes(b"")
        manifest["files"]["empty.bin"] = hashlib.sha256(b"").hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_directory_in_place_of_a_manifest_listed_file_is_refused(self):
        config, pin, model_dir, manifest_path = self._install()
        manifest = json.loads(manifest_path.read_text())
        (model_dir / "adir").mkdir()
        manifest["files"]["adir"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
            self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                        runtime_probe=lambda python: pin.runtime_commit)

    def test_hash_artifact_enforces_the_cumulative_byte_budget_directly(self):
        """LOCAL_ARTIFACT_TOO_LARGE is only reachable when a file's real size exceeds the
        REMAINING cumulative budget (_MAX_TOTAL_BYTES = 2_000_000_000). attest_local_config
        always starts that budget at the full 2GB and exposes no seam to shrink it, so
        proving this branch through the public function would need multi-gigabyte fixtures.
        This is the one branch in this module where calling the private helper directly is
        the only proportionate option -- it still asserts the observable error code for a
        malformed (oversized-relative-to-budget) input, not internal state."""
        from src.adl.api.local_attestor import _hash_artifact
        config, pin, model_dir, manifest_path = self._install(weight_content=b"x" * 100)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_ARTIFACT_TOO_LARGE"):
            _hash_artifact(model_dir, "model.safetensors", remaining_bytes=5)

    def test_hash_artifact_rejects_a_file_grown_after_its_size_check(self):
        """GUARD (was a confirmed defect, fixed in src/adl/api/local_attestor.py::
        _hash_artifact): the function used to fstat the artifact file ONCE, check
        `0 < st_size <= _MAX_ARTIFACT_BYTES` and `st_size <= remaining_bytes` against THAT
        single stat, then read to EOF via a plain `while chunk := stream.read(...)` loop --
        with no post-read re-fstat/identity check. That meant a file grown in place (same
        inode) immediately after the size check was hashed and accepted in full, while the
        `size` returned to the caller was still the stale, pre-growth figure -- silently
        undercounting attest_local_config's cumulative remaining_bytes budget by everything
        actually read.

        Contrast with the two OTHER hash-checking implementations in this same codebase that
        verify the same kind of installed artifact -- jev_auto/mlx_worker.py's _hash_file and
        src/adl/providers/laya_worker.py's _sha256 -- both of which already re-fstat AFTER
        the read loop and reject if identity changed; _hash_artifact now does the same
        (re-fstats, and also cross-checks the actual bytes read against the original size).

        This test simulates the growth (10 bytes -> 5000 bytes, same inode) mid-verification
        by mutating on the os.fstat call that reads the stale 10-byte size, and now expects
        the correct, hardened outcome: refusal, not silent acceptance.
        """
        config, pin, model_dir, manifest_path = self._install()
        weight_path = model_dir / "model.safetensors"
        small_content = b"0123456789"  # 10 bytes -- passes every size gate as-is
        grown_content = b"X" * 5000  # swapped in mid-verification; still small enough to be fast

        weight_path.write_bytes(small_content)
        grown_hash = hashlib.sha256(grown_content).hexdigest()
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["model.safetensors"] = grown_hash
        manifest_path.write_text(json.dumps(manifest))
        config = {**config, "weight_sha256": grown_hash}
        pin = self.ApprovedPin(pin.repository, pin.revision, grown_hash, pin.runtime_commit)

        real_fstat = os.fstat
        hits = {"n": 0}

        def trapped_fstat(fd, *a, **kw):
            result = real_fstat(fd, *a, **kw)
            if result.st_size == len(small_content):
                hits["n"] += 1
                if hits["n"] == 1:
                    weight_path.write_bytes(grown_content)  # grow the SAME inode in place
            return result

        with patch("src.adl.api.local_attestor.os.fstat", side_effect=trapped_fstat):
            with self.assertRaises(self.AttestationError):
                self.attest(config, approved_pins=(pin,), expected_python=self.python_path,
                            runtime_probe=lambda python: pin.runtime_commit)


class RuntimeCommitProbeTests(unittest.TestCase):
    """_runtime_commit_from_python spawns `<python> -I -c <code>` and parses its stdout as the
    sole channel of truth. Tested against small fake 'python' executables -- plain scripts,
    shebanged to the REAL interpreter (sys.executable), that ignore their argv and print a
    scripted response -- not the real laya-mlx package, since there is no laya-mlx
    distribution metadata to discover here without writing outside a TemporaryDirectory, and
    -I (isolated mode) intentionally ignores PYTHONPATH tricks anyway. (A shebang script's
    argv, here [-I, -c, <code>], is simply never referenced by these scripts, the same way a
    real interpreter given a script path ignores them as ITS OWN flags.)"""

    def setUp(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python
        self.probe = _runtime_commit_from_python
        self.AttestationError = AttestationError
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _fake_python(self, name, *, stdout_text="", exit_code=0):
        path = self.root / name
        body = (
            "import sys\n"
            f"sys.stdout.write({stdout_text!r})\n"
            "sys.stdout.flush()\n"
            f"sys.exit({exit_code})\n"
        )
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o700)
        return path

    def test_valid_commit_id_is_parsed_from_stdout(self):
        commit = "d" * 40
        fake = self._fake_python("good", stdout_text=json.dumps({"vcs_info": {"commit_id": commit}}))
        self.assertEqual(self.probe(fake), commit)

    def test_nonzero_exit_is_refused(self):
        fake = self._fake_python("fails", exit_code=7)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(fake)

    def test_non_json_stdout_is_refused(self):
        fake = self._fake_python("garbage", stdout_text="not json")
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(fake)

    def test_json_missing_vcs_info_is_refused(self):
        fake = self._fake_python("novcs", stdout_text=json.dumps({"other": 1}))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(fake)

    def test_commit_id_failing_the_hex_shape_check_is_refused(self):
        fake = self._fake_python("badcommit", stdout_text=json.dumps({"vcs_info": {"commit_id": "not-hex"}}))
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(fake)

    def test_output_over_four_kilobytes_is_refused(self):
        fake = self._fake_python("huge", stdout_text="x" * 5000)
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(fake)

    def test_missing_interpreter_is_refused_without_crashing(self):
        with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
            self.probe(self.root / "does-not-exist")

    def test_a_hung_process_hits_the_deadline_and_a_killpg_race_is_absorbed(self):
        """Slow by construction (~5s): the read-loop deadline is hardcoded to 5 seconds from
        entry, with no injectable override, so proving MLX -- this module's -- own hang
        protection needs a process that genuinely writes nothing and outlives it. The same
        call also exercises the cleanup race in the `finally` block: `os.killpg` is patched
        to raise ProcessLookupError (simulating the process exiting in the gap between the
        `poll() is None` check and the kill), proving that race is absorbed, not left to
        propagate out of a `finally` and mask the real (deadline) error."""
        fake = self.root / "hangs"
        fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        fake.chmod(0o700)
        real_killpg = os.killpg

        def killpg_then_simulate_race(pgid, sig):
            real_killpg(pgid, sig)  # actually stop the hung process so nothing leaks
            raise ProcessLookupError()  # then surface the race the code is built to absorb

        with patch("src.adl.api.local_attestor.os.killpg", side_effect=killpg_then_simulate_race):
            with self.assertRaisesRegex(self.AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                self.probe(fake)


if __name__ == "__main__":
    unittest.main()
