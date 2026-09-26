"""Hermetic coverage tests for the paid-provider transport, credential
storage, and local-attestation boundaries.

No test in this file makes a real network call, touches the real macOS
Keychain, or writes outside a TemporaryDirectory. HTTP is replaced at
http.client.HTTPSConnection; the Keychain is replaced either through
MacKeychain's own `backend=` seam or, for _SecurityFrameworkBackend itself,
through mocked ctypes function objects; the local-attestation subprocess is
replaced with a real OS pipe carrying synthetic bytes.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


def _resolved_root(directory: str, *parts: str) -> Path:
    """Build a config-root path with every symlink ancestor already resolved.

    macOS mounts /var (and /tmp) as a symlink to /private/var (/private/tmp),
    and tempfile.TemporaryDirectory() lands under /var/folders/... by
    default. _safe_regular_file() (jevkit/providers.py) walks a path's
    ancestors and refuses any symlink it finds, with no carve-out for this
    OS-level mount -- so an un-resolved temp path trips it. Resolving here
    keeps that (otherwise legitimate) check from firing on the test
    harness's own directory layout. See JevkitProvidersKnownDefectTests for
    why this is a real product defect and not just test plumbing.
    """
    return Path(directory).resolve().joinpath(*parts)


class _FakePipeProcess:
    """Stand-in for subprocess.Popen with a real, in-process pipe as stdout.

    _runtime_commit_from_python() drives selectors + os.read() straight off
    process.stdout's file descriptor, so the double must be a real pipe --
    selectors cannot select() a Mock. No subprocess is ever spawned.
    """

    def __init__(self, payload: bytes = b"", *, exit_code: int = 0, stays_alive: bool = False):
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.pid = 999_999
        self._exit_code = exit_code
        self._alive = stays_alive

    def poll(self):
        return None if self._alive else self._exit_code

    def wait(self, timeout=None):
        self._alive = False
        return self._exit_code


class JevAutoProvidersRemoteTests(unittest.TestCase):
    """Providers.remote() (jev_auto/providers.py) is the paid-provider HTTP
    boundary: a provider outage, a malformed body, or a missing field must
    never be silently reinterpreted as a clean answer. Every test replaces
    http.client.HTTPSConnection with an in-memory double; nothing here ever
    reaches a socket."""

    POLICY = {"provider": "typesafe", "timeout_seconds": 30}
    QUESTIONS = {"decision": {"type": "noul", "instructions": "Is this synthetic?"}}

    @staticmethod
    def _fake_connection(status, chunks, *, sock=None):
        connection = MagicMock(name="HTTPSConnection")
        connection.sock = sock
        response = MagicMock(name="HTTPResponse")
        response.status = status
        response.read1.side_effect = list(chunks) + [b""]
        connection.getresponse.return_value = response
        return connection, response

    def test_happy_path_returns_validated_answer_with_provenance(self):
        from jev_auto.providers import Providers

        body = json.dumps({
            "model": "jev-1.13.0",
            "answers": {"decision": {"type": "noul", "noul": 0.7}},
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode()
        connection, _response = self._fake_connection(200, [body], sock=MagicMock())
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection) as ctor, \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            result = provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
        self.assertEqual(result["answers"], {"decision": {"type": "noul", "noul": 0.7}})
        self.assertEqual(result["provenance"], {
            "provider": "typesafe", "model_requested": "jev-1.13.0", "confidence_kind": "provider-reported",
        })
        ctor.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/v1/systemone"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "A" * 32)
        connection.sock.settimeout.assert_called()

    def test_connection_is_reused_across_calls_on_the_same_thread(self):
        from jev_auto.providers import Providers

        body = json.dumps({"model": "jev-1.13.0",
                           "answers": {"decision": {"type": "noul", "noul": 0.2}},
                           "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        connection, _response = self._fake_connection(200, [body])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection) as ctor, \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            provider._threads.typesafe = connection
            provider._connections.add(connection)
            provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
        ctor.assert_not_called()

    def test_non_200_status_raises_and_evicts_the_connection(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        for status in (400, 429, 500, 503):
            with self.subTest(status=status):
                connection, _response = self._fake_connection(status, [b""])
                with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
                     patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
                    provider = Providers()
                    with self.assertRaisesRegex(AutoError, f"PROVIDER_HTTP_{status}"):
                        provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
                self.assertTrue(connection.close.called)
                self.assertIsNone(getattr(provider._threads, "typesafe", None))
                self.assertNotIn(connection, provider._connections)

    def test_transport_exception_is_never_retried_and_never_looks_like_success(self):
        """HIGH-SEVERITY class: an ambiguous transport failure (the request
        may have already been billed) must become a fixed refusal -- not a
        retry, and not a decoded answer."""
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        connection = MagicMock(name="HTTPSConnection")
        connection.sock = None
        connection.request.side_effect = OSError("synthetic connection reset")
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "PROVIDER_TRANSPORT_FAILURE_NO_RETRY"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
        self.assertEqual(connection.request.call_count, 1)
        self.assertTrue(connection.close.called)

    def test_timeout_budget_expiring_before_the_body_is_read_raises_timeout(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        connection, _response = self._fake_connection(200, [b"{}"])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "PROVIDER_TIMEOUT"):
                provider.remote({**self.POLICY, "timeout_seconds": 0}, {"k": "v"}, self.QUESTIONS)

    def test_oversized_response_body_is_rejected_before_full_buffering(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        connection = MagicMock(name="HTTPSConnection")
        connection.sock = None
        response = MagicMock(name="HTTPResponse")
        response.status = 200
        response.read1.side_effect = lambda n: b"x" * n
        connection.getresponse.return_value = response
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "PROVIDER_RESPONSE_SIZE"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)

    def test_malformed_json_body_is_never_accepted_as_an_answer(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        connection, _response = self._fake_connection(200, [b"{not-valid-json"])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "INVALID_JSON"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)

    def test_response_missing_required_fields_is_rejected_not_laundered(self):
        """A body that is valid JSON but fails the response contract (no
        `answers` key at all) must raise, never degrade into a blank or
        default-valued 'success'."""
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        body = json.dumps({"model": "jev-1.13.0", "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        connection, _response = self._fake_connection(200, [body])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "ANSWER_IDS"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)

    def test_model_mismatch_is_rejected(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        body = json.dumps({"model": "some-other-model",
                           "answers": {"decision": {"type": "noul", "noul": 0.5}},
                           "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        connection, _response = self._fake_connection(200, [body])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "MODEL_MISMATCH"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)

    def test_invalid_credential_store_value_is_rejected_before_any_connection(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        with patch("jev_auto.providers.http.client.HTTPSConnection") as ctor:
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "CREDENTIAL_STORE"):
                provider.remote({**self.POLICY, "credential_store": "bogus"}, {"k": "v"}, self.QUESTIONS)
        ctor.assert_not_called()

    def test_changed_provider_profile_is_detected_before_sending_anything(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        with patch.dict("jev_auto.providers.ENDPOINTS",
                        {"typesafe": ("wrong-host.example", "/wrong-path", "wrong-model")}), \
             patch("jev_auto.providers.http.client.HTTPSConnection") as ctor:
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "PROVIDER_PROFILE_CHANGED"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
        ctor.assert_not_called()

    def test_refuses_cleanly_when_no_credential_is_available(self):
        """No credential must be a refusal, distinguishable from a transport
        failure -- never a silent 'proceed anyway'."""
        from jev_auto.providers import Providers
        from jevkit.security import SafeError

        with patch("jevkit.providers.get_provider_credential",
                  side_effect=SafeError("NO_CREDENTIAL: configure TYPESAFE_API_KEY using the private terminal helper")), \
             patch("jev_auto.providers.http.client.HTTPSConnection") as ctor:
            provider = Providers()
            with self.assertRaisesRegex(SafeError, "NO_CREDENTIAL"):
                provider.remote(self.POLICY, {"k": "v"}, self.QUESTIONS)
        ctor.assert_not_called()

    def test_keychain_credential_store_is_requested_explicitly(self):
        from jev_auto.providers import Providers

        body = json.dumps({"model": "jev-1.13.0",
                           "answers": {"decision": {"type": "noul", "noul": 0.5}},
                           "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        connection, _response = self._fake_connection(200, [body])
        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=connection), \
             patch("jevkit.providers.get_provider_credential", return_value="B" * 32) as getter:
            provider = Providers()
            provider.remote({**self.POLICY, "credential_store": "keychain"}, {"k": "v"}, self.QUESTIONS)
        self.assertEqual(getter.call_args.kwargs.get("credential_store"), "keychain")


class JevAutoProvidersLifecycleTests(unittest.TestCase):
    """Local-worker lifecycle (warmup/readiness/shutdown) for Providers.
    jev_auto.mlx_process.MLXProcess is always replaced by a fast, synchronous
    fake, so no real local model process is ever spawned by this file."""

    @staticmethod
    def _fake_mlx_factory(*, on_warmup=None):
        class _FakeMLX:
            def __init__(self, cfg):
                self.cfg = cfg
                self.proc = "fake-pid"
                self.last_telemetry = None
                self.closed = False

            def warmup(self):
                if on_warmup is not None:
                    on_warmup()

            def close(self):
                self.closed = True

            def predict(self, state, questions, timeout):
                raise AssertionError("predict() should not be reached from a warmup test")

        return _FakeMLX

    def test_warmup_requires_a_dict_mlx_configuration(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        provider = Providers()
        with self.assertRaisesRegex(AutoError, "MLX_NOT_CONFIGURED"):
            provider.warmup({"mlx": None, "timeout_seconds": 5})

    def test_warmup_waits_for_the_worker_and_reports_ready(self):
        from jev_auto.providers import Providers

        cfg = {"revision": "a" * 40}
        with patch("jev_auto.mlx_process.MLXProcess", self._fake_mlx_factory()):
            provider = Providers()
            result = provider.warmup({"mlx": cfg, "timeout_seconds": 5}, wait=True)
        self.assertEqual(result, {"ready": True, "provider": "laya-mlx"})
        self.assertTrue(provider._ready.is_set())
        self.assertIsNone(provider._warm_error)

    def test_warmup_failure_is_reported_once_and_does_not_hang(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        def blow_up():
            raise RuntimeError("synthetic worker crash")

        cfg = {"revision": "b" * 40}
        with patch("jev_auto.mlx_process.MLXProcess", self._fake_mlx_factory(on_warmup=blow_up)):
            provider = Providers()
            with self.assertRaisesRegex(AutoError, "MLX_WARMUP_FAILED"):
                provider.warmup({"mlx": cfg, "timeout_seconds": 5}, wait=True)

    def test_configuration_change_while_warming_is_rejected(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError, digest

        provider = Providers()
        provider._mlx = object()
        provider._mlx_identity = digest({"revision": "old"})
        provider._warming = True
        with self.assertRaisesRegex(AutoError, "MLX_CONFIGURATION_CHANGE_DURING_WARMUP"):
            provider.warmup({"mlx": {"revision": "new"}, "timeout_seconds": 5})

    def test_ready_for_request_ignores_non_local_providers(self):
        from jev_auto.providers import Providers

        provider = Providers()
        with patch.object(provider, "warmup") as warmup:
            provider.ready_for_request({"provider": "typesafe", "timeout_seconds": 5})
        warmup.assert_not_called()

    def test_ready_for_request_refuses_fast_while_still_warming(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError, digest

        provider = Providers()
        cfg = {"revision": "c" * 40}
        provider._mlx = object()
        provider._mlx_identity = digest(cfg)
        provider._warming = True
        provider._ready = threading.Event()
        provider._warm_error = None
        with self.assertRaisesRegex(AutoError, "MLX_WARMING_UP_USE_NORMAL_CODEX"):
            provider.ready_for_request({"provider": "laya-mlx", "timeout_seconds": 0, "mlx": cfg})

    def test_ready_for_request_surfaces_a_recorded_warm_error(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError, digest

        provider = Providers()
        cfg = {"revision": "d" * 40}
        provider._mlx = object()
        provider._mlx_identity = digest(cfg)
        provider._warming = False
        provider._ready.set()
        provider._warm_error = "MLX_WARMUP_FAILED"
        with self.assertRaisesRegex(AutoError, "MLX_WARMUP_FAILED"):
            provider.ready_for_request({"provider": "laya-mlx", "timeout_seconds": 5, "mlx": cfg})

    def test_evaluate_dispatches_by_provider_name(self):
        from jev_auto.providers import Providers

        provider = Providers()
        with patch.object(provider, "local", return_value={"local": True}) as local, \
             patch.object(provider, "remote", return_value={"remote": True}) as remote:
            self.assertEqual(provider.evaluate({"provider": "laya-mlx"}, "s", {}), {"local": True})
            self.assertEqual(provider.evaluate({"provider": "typesafe"}, "s", {}), {"remote": True})
        local.assert_called_once()
        remote.assert_called_once()

    def test_close_shuts_down_the_worker_and_every_tracked_connection(self):
        from jev_auto.providers import Providers

        provider = Providers()
        fake_mlx = MagicMock()
        provider._mlx = fake_mlx
        stray = MagicMock(name="stray-connection")
        tracked = MagicMock(name="tracked-connection")
        provider._connections.add(tracked)
        provider._threads.typesafe = stray
        provider.close()
        fake_mlx.close.assert_called_once()
        tracked.close.assert_called_once()
        stray.close.assert_called_once()
        self.assertIsNone(getattr(provider._threads, "typesafe", None))
        self.assertIsNone(getattr(provider._threads, "openrouter", None))

    def test_close_tolerates_no_worker_ever_having_started(self):
        from jev_auto.providers import Providers

        provider = Providers()
        provider.close()  # must not raise

    def test_ready_for_request_raises_its_own_warm_error_check(self):
        """warmup() already raises on a recorded _warm_error before it
        returns, so ready_for_request()'s own `if self._warm_error: raise`
        line is normally unreachable through warmup() itself -- it only
        guards the narrow window where _warm_error is set by the
        background thread after warmup() has already returned. Stubbing
        self.warmup() as a no-op isolates ready_for_request()'s own check
        instead of relying on a timing-dependent race."""
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError

        provider = Providers()
        provider._ready.set()
        provider._warm_error = "MLX_WARMUP_FAILED"
        with patch.object(provider, "warmup", return_value={"ready": True, "provider": "laya-mlx"}) as warmup:
            with self.assertRaisesRegex(AutoError, "MLX_WARMUP_FAILED"):
                provider.ready_for_request({"provider": "laya-mlx", "timeout_seconds": 5})
        warmup.assert_called_once()

    def test_local_clears_readiness_only_when_the_worker_process_has_exited(self):
        from jev_auto.providers import Providers
        from jev_auto.common import AutoError
        from types import SimpleNamespace

        questions = {"decision": {"type": "noul", "instructions": "?"}}
        policy = {"mlx": {"repository": "r", "revision": "a" * 40, "weight_sha256": "b" * 64},
                 "timeout_seconds": 5}

        def blow_up(*_args):
            raise AutoError("MLX_PREDICT_FAILED")

        for case, proc_value in (("worker_exited", None), ("worker_still_running", "pid-123")):
            with self.subTest(case=case):
                provider = Providers()
                provider._ready.set()
                provider._warm_error = "stale-should-be-untouched-unless-exited"
                provider._mlx = SimpleNamespace(predict=blow_up, proc=proc_value, last_telemetry=None)
                with patch.object(provider, "warmup", return_value={"ready": True, "provider": "laya-mlx"}):
                    with self.assertRaisesRegex(AutoError, "MLX_PREDICT_FAILED"):
                        provider.local(policy, "state", questions)
                if proc_value is None:
                    self.assertFalse(provider._ready.is_set())
                    self.assertIsNone(provider._warm_error)
                else:
                    self.assertTrue(provider._ready.is_set())
                    self.assertEqual(provider._warm_error, "stale-should-be-untouched-unless-exited")

    def test_local_success_path_filters_telemetry_to_the_known_keys(self):
        from jev_auto.providers import Providers
        from types import SimpleNamespace

        questions = {"decision": {"type": "noul", "instructions": "?"}}
        cfg = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40, "weight_sha256": "b" * 64}
        policy = {"mlx": cfg, "timeout_seconds": 5}
        telemetry = {"cold": True, "inference_ms": 12.5, "actual_device": "cpu",
                    "an_unlisted_field_that_must_be_dropped": "secret-ish"}

        def predict(_state, _qs, _timeout):
            return {"model": cfg["repository"], "answers": {"decision": {"type": "noul", "noul": 0.6}}}

        provider = Providers()
        provider._ready.set()
        provider._mlx = SimpleNamespace(predict=predict, proc="pid-123", last_telemetry=telemetry)
        with patch.object(provider, "warmup", return_value={"ready": True, "provider": "laya-mlx"}):
            result = provider.local(policy, "state", questions)
        self.assertEqual(result["provenance"]["telemetry"], {
            "cold": True, "inference_ms": 12.5, "actual_device": "cpu",
        })
        self.assertNotIn("an_unlisted_field_that_must_be_dropped", result["provenance"]["telemetry"])


class JevkitProviderProfileTests(unittest.TestCase):
    """Provider profile lookup and identity hashing (jevkit/providers.py)
    are pure and fixed -- they must not silently drift or accept an unknown
    provider id."""

    def test_known_providers_resolve_to_fixed_profiles(self):
        from jevkit.providers import provider_profile

        typesafe = provider_profile("typesafe")
        openrouter = provider_profile("openrouter")
        self.assertEqual(typesafe.endpoint, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(typesafe.model, "jev-1.13.0")
        self.assertEqual(openrouter.endpoint, "https://openrouter.ai/api/alpha/decisions")
        self.assertNotEqual(typesafe.profile_sha256, openrouter.profile_sha256)

    def test_unknown_provider_id_is_rejected(self):
        from jevkit.providers import provider_profile
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "INVALID_PROVIDER"):
            provider_profile("not-a-real-provider")

    def test_profile_hash_is_deterministic_and_field_sensitive(self):
        from dataclasses import replace

        from jevkit.providers import provider_profile

        base = provider_profile("typesafe")
        self.assertEqual(base.profile_sha256, provider_profile("typesafe").profile_sha256)
        changed = replace(base, model="a-different-model")
        self.assertNotEqual(base.profile_sha256, changed.profile_sha256)

    def test_default_config_root_prefers_xdg_then_falls_back_to_home(self):
        from jevkit.providers import default_config_root

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/synthetic/xdg"}):
            self.assertEqual(default_config_root(), Path("/synthetic/xdg/qualixar-jev-decision-layer"))
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), \
             patch.object(Path, "home", return_value=Path("/synthetic/home")):
            self.assertEqual(default_config_root(), Path("/synthetic/home/.config/qualixar-jev-decision-layer"))


class JevkitSafeRegularFileAndPrivateEnvTests(unittest.TestCase):
    """_safe_regular_file and _private_env are the local filesystem trust
    boundary for provider credentials: a symlink, a loose permission bit, or
    an oversized file must all be refused, never silently accepted."""

    def test_rejects_a_symlinked_ancestor(self):
        from jevkit.providers import _safe_regular_file
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory)
            real_dir = root / "real"
            real_dir.mkdir(parents=True)
            target = real_dir / "secret"
            target.write_text("x" * 10)
            target.chmod(0o600)
            link_dir = root / "link"
            link_dir.symlink_to(real_dir)
            with self.assertRaisesRegex(SafeError, "TEST_ERROR"):
                _safe_regular_file(link_dir / "secret", "TEST_ERROR")

    def test_rejects_group_or_world_permission_bits(self):
        from jevkit.providers import _safe_regular_file
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            path = _resolved_root(directory, "secret")
            path.write_text("x" * 10)
            path.chmod(0o644)
            with self.assertRaisesRegex(SafeError, "TEST_ERROR"):
                _safe_regular_file(path, "TEST_ERROR")

    def test_rejects_a_file_with_more_than_one_hard_link(self):
        from jevkit.providers import _safe_regular_file
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            path = _resolved_root(directory, "secret")
            path.write_text("x" * 10)
            path.chmod(0o600)
            os.link(path, path.with_name("secret-2"))
            with self.assertRaisesRegex(SafeError, "TEST_ERROR"):
                _safe_regular_file(path, "TEST_ERROR")

    def test_rejects_a_file_not_owned_by_the_caller(self):
        from jevkit.providers import _safe_regular_file
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            path = _resolved_root(directory, "secret")
            path.write_text("x" * 10)
            path.chmod(0o600)
            with patch("jevkit.providers.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(SafeError, "TEST_ERROR"):
                    _safe_regular_file(path, "TEST_ERROR")

    def test_accepts_a_correctly_permissioned_regular_file(self):
        from jevkit.providers import _safe_regular_file

        with tempfile.TemporaryDirectory() as directory:
            path = _resolved_root(directory, "secret")
            path.write_text("x" * 10)
            path.chmod(0o600)
            self.assertIsNone(_safe_regular_file(path, "UNREACHABLE"))

    def test_private_env_missing_file_returns_empty(self):
        from jevkit.providers import _private_env

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            self.assertEqual(_private_env(root), {})

    def test_private_env_rejects_a_loosely_permissioned_config_directory(self):
        from jevkit.providers import _private_env
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            (root / ".env").write_text("TYPESAFE_API_KEY=x\n")
            root.chmod(0o755)
            with self.assertRaisesRegex(SafeError, "UNSAFE_PROVIDER_ENV_DIRECTORY"):
                _private_env(root)

    def test_private_env_parses_quoted_export_and_comment_lines(self):
        from jevkit.providers import _private_env

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            env_path = root / ".env"
            env_path.write_text(
                "# a comment\n\nexport JEV_PROVIDER=openrouter\n"
                "TYPESAFE_API_KEY=\"abc12345\"\nOPENROUTER_API_KEY='def67890'\n"
            )
            env_path.chmod(0o600)
            self.assertEqual(_private_env(root), {
                "JEV_PROVIDER": "openrouter", "TYPESAFE_API_KEY": "abc12345",
                "OPENROUTER_API_KEY": "def67890",
            })

    def test_private_env_rejects_oversized_file(self):
        from jevkit.providers import _private_env
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            env_path = root / ".env"
            env_path.write_text("TYPESAFE_API_KEY=" + "x" * 13_000 + "\n")
            env_path.chmod(0o600)
            with self.assertRaisesRegex(SafeError, "^INVALID_PROVIDER_ENV$"):
                _private_env(root)

    def test_private_env_rejects_a_line_without_an_equals_sign(self):
        from jevkit.providers import _private_env
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            env_path = root / ".env"
            env_path.write_text("this-line-has-no-equals-sign\n")
            env_path.chmod(0o600)
            with self.assertRaisesRegex(SafeError, "^INVALID_PROVIDER_ENV$"):
                _private_env(root)

    def test_private_env_rejects_a_key_outside_the_private_allowlist(self):
        from jevkit.providers import _private_env
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            env_path = root / ".env"
            env_path.write_text("SOME_RANDOM_SECRET=value\n")
            env_path.chmod(0o600)
            with self.assertRaisesRegex(SafeError, "INVALID_PROVIDER_ENV_KEY"):
                _private_env(root)


class JevkitResolveProviderTests(unittest.TestCase):
    """resolve_provider()'s priority order: explicit arg > JEV_PROVIDER env >
    selection file > private .env > exactly-one-configured-credential >
    default typesafe > ambiguous (raise)."""

    @staticmethod
    def _clean_env():
        return patch.dict(os.environ, {"JEV_PROVIDER": "", "TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": ""})

    def test_explicit_provider_id_wins_over_everything_else(self):
        from jevkit.providers import resolve_provider

        with self._clean_env():
            profile = resolve_provider("openrouter", config_root=Path("/nonexistent-cfg-root"))
        self.assertEqual(profile.provider_id, "openrouter")

    def test_env_var_selects_when_no_explicit_id_given(self):
        from jevkit.providers import resolve_provider

        with patch.dict(os.environ, {"JEV_PROVIDER": "OpenRouter", "TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": ""}):
            profile = resolve_provider(config_root=Path("/nonexistent-cfg-root"))
        self.assertEqual(profile.provider_id, "openrouter")

    def test_selection_file_is_used_when_no_id_or_env_var(self):
        from jevkit.providers import resolve_provider

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            selection = root / "provider"
            selection.write_text("openrouter\n")
            selection.chmod(0o600)
            with self._clean_env():
                profile = resolve_provider(config_root=root)
        self.assertEqual(profile.provider_id, "openrouter")

    def test_oversized_selection_file_is_rejected(self):
        from jevkit.providers import resolve_provider
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            selection = root / "provider"
            selection.write_text("x" * 65)
            selection.chmod(0o600)
            with self._clean_env(), self.assertRaisesRegex(SafeError, "INVALID_PROVIDER_CONFIG"):
                resolve_provider(config_root=root)

    def test_private_env_provider_selection_is_used_last_before_autodetect(self):
        from jevkit.providers import resolve_provider

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            env_path = root / ".env"
            env_path.write_text("JEV_PROVIDER=openrouter\n")
            env_path.chmod(0o600)
            with self._clean_env():
                profile = resolve_provider(config_root=root)
        self.assertEqual(profile.provider_id, "openrouter")

    def test_autodetect_defaults_to_typesafe_when_nothing_is_configured(self):
        from jevkit.providers import resolve_provider

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            with self._clean_env():
                profile = resolve_provider(config_root=root)
        self.assertEqual(profile.provider_id, "typesafe")

    def test_autodetect_picks_the_single_configured_provider(self):
        from jevkit.providers import resolve_provider

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            with patch.dict(os.environ, {"JEV_PROVIDER": "", "TYPESAFE_API_KEY": "",
                                         "OPENROUTER_API_KEY": "sk-fake-000000000000"}):
                profile = resolve_provider(config_root=root)
        self.assertEqual(profile.provider_id, "openrouter")

    def test_autodetect_refuses_to_guess_between_two_configured_providers(self):
        from jevkit.providers import resolve_provider
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(mode=0o700)
            with patch.dict(os.environ, {"JEV_PROVIDER": "", "TYPESAFE_API_KEY": "sk-fake-1",
                                         "OPENROUTER_API_KEY": "sk-fake-2"}), \
                 self.assertRaisesRegex(SafeError, "PROVIDER_SELECTION_REQUIRED"):
                resolve_provider(config_root=root)


class JevkitCredentialStorageTests(unittest.TestCase):
    """validate_key / credential_path / get_provider_credential /
    store_provider_credential / credential_available: the legacy
    (non-keychain) local credential path. All file I/O stays inside a
    resolved TemporaryDirectory; the keychain store is exercised only
    through a stub, never src.adl.api.keychain's real ctypes backend."""

    def test_validate_key_enforces_length_ascii_and_no_whitespace(self):
        from jevkit.providers import validate_key
        from jevkit.security import SafeError

        cases = {
            "too_short": "short",
            "too_long": "x" * 4097,
            "non_ascii": "café-café-café-1234",
            "embedded_space": "abcd efgh ijkl",
            "embedded_tab": "abcd\tefgh\tijkl",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(SafeError, "INVALID_CREDENTIAL"):
                    validate_key(value)
        self.assertEqual(validate_key("a" * 32), "a" * 32)

    def test_get_provider_credential_prefers_env_var_over_file(self):
        from jevkit.providers import get_provider_credential, provider_profile

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "E" * 20}):
                key = get_provider_credential(profile, config_root=root)
        self.assertEqual(key, "E" * 20)

    def test_get_provider_credential_falls_back_to_private_env_then_file(self):
        from jevkit.providers import get_provider_credential, provider_profile

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(parents=True, mode=0o700)
            env_path = root / ".env"
            env_path.write_text("TYPESAFE_API_KEY=" + "F" * 20 + "\n")
            env_path.chmod(0o600)
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                key = get_provider_credential(profile, config_root=root)
        self.assertEqual(key, "F" * 20)

    def test_get_provider_credential_reads_the_key_file_as_last_resort(self):
        from jevkit.providers import credential_path, get_provider_credential, provider_profile

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(parents=True, mode=0o700)
            key_path = credential_path(profile, config_root=root)
            key_path.write_text("G" * 20)
            key_path.chmod(0o600)
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                key = get_provider_credential(profile, config_root=root)
        self.assertEqual(key, "G" * 20)

    def test_get_provider_credential_rejects_an_oversized_key_file(self):
        from jevkit.providers import credential_path, get_provider_credential, provider_profile
        from jevkit.security import SafeError

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(parents=True, mode=0o700)
            key_path = credential_path(profile, config_root=root)
            key_path.write_text("H" * 5000)
            key_path.chmod(0o600)
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                with self.assertRaisesRegex(SafeError, "^INVALID_CREDENTIAL$"):
                    get_provider_credential(profile, config_root=root)

    def test_get_provider_credential_refuses_when_nothing_is_configured(self):
        from jevkit.providers import get_provider_credential, provider_profile
        from jevkit.security import SafeError

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                with self.assertRaisesRegex(SafeError, "NO_CREDENTIAL"):
                    get_provider_credential(profile, config_root=root)

    def test_store_then_get_round_trip_via_the_atomic_file_writer(self):
        from jevkit.providers import (credential_path, get_provider_credential, provider_profile,
                                      store_provider_credential)

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            returned_profile = store_provider_credential("openrouter", "K" * 40, config_root=root)
            self.assertEqual(returned_profile.provider_id, "openrouter")
            key_path = credential_path("openrouter", config_root=root)
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertEqual((root / "provider").read_text().strip(), "openrouter")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
                self.assertEqual(get_provider_credential(provider_profile("openrouter"), config_root=root), "K" * 40)

    def test_get_provider_credential_uses_the_keychain_store_when_asked(self):
        from jevkit.providers import get_provider_credential, provider_profile

        profile = provider_profile("typesafe")
        with patch("src.adl.api.keychain.MacKeychain") as keychain_cls:
            keychain_cls.return_value.get.return_value = "L" * 32
            key = get_provider_credential(profile, credential_store="keychain")
        self.assertEqual(key, "L" * 32)
        keychain_cls.return_value.get.assert_called_once_with("typesafe")

    def test_get_provider_credential_maps_a_keychain_failure_to_a_safe_refusal(self):
        from jevkit.providers import get_provider_credential, provider_profile
        from jevkit.security import SafeError
        from src.adl.api.keychain import KeychainError

        profile = provider_profile("typesafe")
        with patch("src.adl.api.keychain.MacKeychain") as keychain_cls:
            keychain_cls.return_value.get.side_effect = KeychainError("KEYCHAIN_ITEM_MISSING")
            with self.assertRaisesRegex(SafeError, "KEYCHAIN_CREDENTIAL_UNAVAILABLE"):
                get_provider_credential(profile, credential_store="keychain")

    def test_get_provider_credential_rejects_an_unknown_credential_store(self):
        from jevkit.providers import get_provider_credential, provider_profile
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "INVALID_CREDENTIAL_STORE"):
            get_provider_credential(provider_profile("typesafe"), credential_store="bogus")

    def test_credential_available_reflects_success_and_failure(self):
        from jevkit.providers import credential_available, provider_profile

        profile = provider_profile("typesafe")
        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                self.assertFalse(credential_available(profile, config_root=root))
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "M" * 20}):
                self.assertTrue(credential_available(profile, config_root=root))

    def test_store_provider_credential_refuses_a_symlinked_config_root(self):
        from jevkit.providers import store_provider_credential
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            real = _resolved_root(directory, "real-root")
            real.mkdir(parents=True, mode=0o700)
            link = _resolved_root(directory, "linked-root")
            link.symlink_to(real)
            with self.assertRaises(SafeError):
                store_provider_credential("typesafe", "N" * 20, config_root=link)

    def test_store_provider_credential_refuses_a_preexisting_symlink_at_the_key_path(self):
        """The config_root itself is a normal directory here -- only the
        exact destination the writer is about to replace is a symlink, so
        this exercises the loop's own `if path.is_symlink()` guard rather
        than private_dir()'s root-level check."""
        from jevkit.providers import credential_path, provider_profile, store_provider_credential
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            root.mkdir(parents=True, mode=0o700)
            elsewhere = _resolved_root(directory, "elsewhere")
            elsewhere.write_text("not a credential")
            key_path = credential_path(provider_profile("typesafe"), config_root=root)
            key_path.symlink_to(elsewhere)
            with self.assertRaisesRegex(SafeError, "UNSAFE_CREDENTIAL_PATH"):
                store_provider_credential("typesafe", "Q" * 20, config_root=root)

    def test_store_provider_credential_cleans_up_its_temp_file_if_chmod_fails(self):
        """A failure between mkstemp() and fdopen() must not leak the
        just-created temp file, and must not leave the fd open. The
        function has no except clause around this section, so the
        synthetic OSError propagates as-is -- the finally block's own
        cleanup is what this test is really checking."""
        from jevkit.providers import store_provider_credential

        real_chmod = os.chmod

        def selective_failure(path, mode, *args, **kwargs):
            if ".new-" in str(path):
                raise OSError("synthetic chmod failure")
            return real_chmod(path, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = _resolved_root(directory, "cfgroot")
            with patch("jevkit.providers.os.chmod", side_effect=selective_failure):
                with self.assertRaisesRegex(OSError, "synthetic chmod failure"):
                    store_provider_credential("typesafe", "R" * 20, config_root=root)
            leftover = [p for p in root.iterdir() if ".new-" in p.name]
            self.assertEqual(leftover, [])


class JevkitProvidersKnownDefectTests(unittest.TestCase):
    """Defect discovered incidentally while writing this coverage sweep.
    Reported here, not patched -- see the task's pre-existing-bugs policy.
    Marked @unittest.expectedFailure so the rest of the suite stays green;
    guarded with skipUnless so a non-macOS host (where /var is not a
    symlink) reports SKIP rather than an unexpected pass/fail flip."""

    # Fixed in 1.0.7; kept as a regression guard.
    @unittest.skipUnless(platform.system() == "Darwin" and Path("/var").is_symlink(),
                         "reproduces a macOS /var-symlink-specific defect")
    def test_defect_file_credential_written_under_a_var_symlink_cannot_be_read_back(self):
        """FILE: plugins/qualixar-jev-decision-layer/runtime/jevkit/providers.py
        _safe_regular_file, lines 65-76 (used by get_provider_credential's
        file fallback and by _private_env / resolve_provider's selection
        file check).

        Current behaviour: rejects ANY path with a symlinked ancestor,
        including macOS's own /var -> /private/var (and /tmp ->
        /private/tmp) mount, which every stock macOS process sees
        regardless of what the user configured. jev_auto/common.py's
        safe_path() solves the identical problem two files away with an
        explicit carve-out:

            if component == Path('/var') and component.resolve() == Path('/private/var'):
                continue

        _safe_regular_file has no equivalent carve-out.

        Concrete failing input -> wrong output: store_provider_credential()
        writes a key file under a config_root that resolves under /var
        (macOS's default tempfile.gettempdir(), e.g. /var/folders/.../T/),
        with correct 0600 permissions, a single hard link, and the correct
        owner -- and the very next get_provider_credential() call against
        that same file raises SafeError('UNSAFE_CREDENTIAL_PERMISSIONS'),
        even though nothing about the file itself is unsafe. The only
        reason it fails is an ancestor directory the OS itself put there.

        Impact: the file-based credential store is unusable whenever
        config_root resolves under /var or /tmp -- which happens for anyone
        who points XDG_CONFIG_HOME at a temp directory (an ordinary
        choice), and for any hermetic test of this exact path that
        (correctly) roots config_root in tempfile.TemporaryDirectory()
        without manually pre-resolving it, as every other test in this file
        must do (see _resolved_root()) specifically to route around this.
        """
        with tempfile.TemporaryDirectory() as directory:
            from jevkit.providers import get_provider_credential, provider_profile, store_provider_credential

            # Deliberately UNRESOLVED -- the real macOS temp layout, e.g.
            # /var/folders/xx/.../T/tmpXXXXXX, where /var itself is a
            # symlink to /private/var. Contrast with _resolved_root(),
            # which every other test in this file uses to avoid this.
            root = Path(directory) / "cfgroot"
            profile = provider_profile("typesafe")
            store_provider_credential("typesafe", "P" * 32, config_root=root)
            self.assertEqual(get_provider_credential(profile, config_root=root), "P" * 32)


class JevkitEngineFixtureFileTests(unittest.TestCase):
    """catalog/spec/fixture: the read-only file-loading layer under
    jevkit/engine.py. These read the real, shipped fixtures/use_cases --
    exactly what production code loads -- rather than synthesized files."""

    def test_catalog_lists_every_shipped_use_case(self):
        from jevkit.engine import catalog

        entries = catalog()
        self.assertEqual(len(entries), 20)
        self.assertIn("01-skill-routing", {item["id"] for item in entries})

    def test_spec_loads_a_known_case_and_rejects_an_unknown_one(self):
        from jevkit.engine import spec
        from jevkit.security import SafeError

        loaded = spec("01-skill-routing")
        self.assertEqual(loaded["policy"]["kind"], "route")
        with self.assertRaisesRegex(SafeError, "UNKNOWN_CASE"):
            spec("99-does-not-exist")

    def test_fixture_loads_known_variants_and_rejects_an_unknown_one(self):
        from jevkit.engine import fixture
        from jevkit.security import SafeError

        nominal = fixture("08-completion-gate", "nominal")
        self.assertEqual(nominal["expected_status"], "CHECKS_PASSED")
        adversarial = fixture("08-completion-gate", "adversarial")
        self.assertEqual(adversarial["expected_status"], "BLOCK")
        with self.assertRaisesRegex(SafeError, "UNKNOWN_VARIANT"):
            fixture("08-completion-gate", "not-a-real-variant")


class JevkitEngineQuestionsForTests(unittest.TestCase):
    """questions_for() builds the per-item rubric for rank/context/test_select
    cases and enforces the state contract and byte budget for every kind."""

    ROUTE_SPEC = {"required_fields": ["request"], "questions": {
        "decision": {"type": "choice", "instructions": "Pick one",
                    "criteria": {"a": "Option A", "b": "Option B"}}},
        "policy": {"kind": "route"}}

    def test_missing_required_field_is_rejected(self):
        from jevkit.engine import questions_for
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "STATE_CONTRACT_MISMATCH"):
            questions_for(self.ROUTE_SPEC, {})

    def test_non_dict_state_is_rejected(self):
        from jevkit.engine import questions_for
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "STATE_CONTRACT_MISMATCH"):
            questions_for(self.ROUTE_SPEC, ["not", "a", "dict"])

    def test_route_kind_returns_the_spec_questions_unchanged(self):
        from jevkit.engine import questions_for

        qs = questions_for(self.ROUTE_SPEC, {"request": "hello"})
        self.assertEqual(set(qs), {"decision"})

    def test_rank_kind_builds_one_score_question_per_item(self):
        from jevkit.engine import questions_for

        rank_spec = {"required_fields": ["query", "items"], "questions": {}, "policy": {"kind": "rank"}}
        state = {"query": "q", "items": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]}
        qs = questions_for(rank_spec, state)
        self.assertEqual(set(qs), {"item_0", "item_1"})
        self.assertEqual(qs["item_0"]["type"], "score")

    def test_test_select_kind_builds_one_noul_question_per_item(self):
        from jevkit.engine import questions_for

        select_spec = {"required_fields": ["query", "items"], "questions": {}, "policy": {"kind": "test_select"}}
        state = {"query": "q", "items": [{"id": "a", "text": "A"}]}
        qs = questions_for(select_spec, state)
        self.assertEqual(qs["item_0"]["type"], "noul")

    def test_context_kind_builds_three_questions_per_item(self):
        from jevkit.engine import questions_for

        context_spec = {"required_fields": ["query", "items"], "questions": {}, "policy": {"kind": "context"}}
        state = {"query": "q", "items": [{"id": "a", "text": "A"}]}
        qs = questions_for(context_spec, state)
        self.assertEqual(set(qs), {"relevant_0", "injection_0", "conflict_0"})
        self.assertTrue(all(q["type"] == "noul" for q in qs.values()))

    def test_items_shape_is_validated(self):
        from jevkit.engine import questions_for
        from jevkit.security import SafeError

        rank_spec = {"required_fields": ["query", "items"], "questions": {}, "policy": {"kind": "rank"}}
        bad_cases = {
            "not_a_list": {"query": "q", "items": "nope"},
            "too_many": {"query": "q", "items": [{"id": str(i), "text": "x"} for i in range(16)]},
            "empty": {"query": "q", "items": []},
            "item_not_dict": {"query": "q", "items": ["not-a-dict"]},
            "item_missing_text": {"query": "q", "items": [{"id": "a"}]},
            "duplicate_id": {"query": "q", "items": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]},
        }
        for name, state in bad_cases.items():
            with self.subTest(case=name):
                with self.assertRaises(SafeError):
                    questions_for(rank_spec, state)

    def test_oversized_state_trips_the_request_byte_budget(self):
        from jevkit.engine import questions_for
        from jevkit.security import SafeError

        rank_spec = {"required_fields": ["query", "items"], "questions": {}, "policy": {"kind": "rank"}}
        state = {"query": "q", "items": [{"id": "a", "text": "x" * 30_000}]}
        with self.assertRaisesRegex(SafeError, "REQUEST_BYTE_BUDGET_EXCEEDED"):
            questions_for(rank_spec, state)


class JevkitEngineSimulatedResponseTests(unittest.TestCase):
    """simulated_response() must produce internally-consistent probability
    distributions for every question type, both confident and in the
    deliberately-uncertain ('uncertain' fixture variant) mode."""

    def test_noul_question(self):
        from jevkit.engine import simulated_response

        qs = {"q": {"type": "noul", "instructions": "?"}}
        self.assertEqual(simulated_response(qs, {"q": 0.9})["answers"]["q"]["noul"], 0.9)
        self.assertEqual(simulated_response(qs, {}, uncertain=True)["answers"]["q"]["noul"], 0.5)

    def test_choice_question(self):
        from jevkit.engine import simulated_response

        qs = {"q": {"type": "choice", "instructions": "?", "criteria": {"a": "A", "b": "B", "c": "C"}}}
        confident = simulated_response(qs, {"q": "b"})["answers"]["q"]
        self.assertEqual(confident["choice"], "b")
        self.assertAlmostEqual(confident["probabilities"]["b"], 0.98)
        self.assertEqual(confident["confidence"], 0.95)
        uncertain = simulated_response(qs, {}, uncertain=True)["answers"]["q"]
        self.assertEqual(uncertain["confidence"], 0.1)
        self.assertAlmostEqual(sum(uncertain["probabilities"].values()), 1.0)

    def test_score_question(self):
        from jevkit.engine import simulated_response

        qs = {"q": {"type": "score", "instructions": "?", "criteria": ["low", "mid", "high"]}}
        confident = simulated_response(qs, {"q": 1.5})["answers"]["q"]
        self.assertEqual(confident["score"], 1.5)
        self.assertAlmostEqual(confident["probabilities"]["1"] + confident["probabilities"]["2"], 1.0)
        self.assertEqual(confident["legend"]["0"], "low")
        uncertain = simulated_response(qs, {}, uncertain=True)["answers"]["q"]
        self.assertEqual(uncertain["score"], 1.0)  # midpoint of a 3-level scale


class JevkitEnginePolicyTests(unittest.TestCase):
    """policy() turns judged answers into a bounded advisory decision. Every
    kind (route/gate/completion/rank/test_select/context) and its BLOCK vs
    REVIEW vs RECOMMEND boundaries are exercised directly, without a
    fixture file."""

    THRESHOLDS = {"choice_confidence": 0.8, "choice_probability": 0.8, "yes": 0.9, "no": 0.1, "rank_score": 1.4}
    GOOD_EVIDENCE = {"test_exit_code": 0, "lint_exit_code": 0, "tests_executed": 2,
                     "source_hash": "rev-a", "tested_source_hash": "rev-a", "age_seconds": 10}

    def _route_spec(self):
        return {"thresholds": self.THRESHOLDS,
               "policy": {"kind": "route", "block_labels": ["forbidden"], "review_labels": ["unclear"]}}

    def _completion_spec(self):
        return {"thresholds": self.THRESHOLDS,
               "policy": {"kind": "completion", "require_true": ["satisfies_requirement"],
                         "forbid_true": ["missing_required_test"]}}

    def test_route_kind_boundaries(self):
        from jevkit.engine import policy

        cases = {
            "uncertain_confidence": ({"confidence": 0.5, "choice": "ok",
                                     "probabilities": {"ok": 0.95, "other": 0.05}}, "REVIEW"),
            "uncertain_probability": ({"confidence": 0.95, "choice": "ok",
                                      "probabilities": {"ok": 0.5, "other": 0.5}}, "REVIEW"),
            "blocked_label": ({"confidence": 0.95, "choice": "forbidden",
                              "probabilities": {"forbidden": 0.95, "ok": 0.05}}, "BLOCK"),
            "review_label": ({"confidence": 0.95, "choice": "unclear",
                             "probabilities": {"unclear": 0.95, "ok": 0.05}}, "REVIEW"),
            "recommended": ({"confidence": 0.95, "choice": "ok",
                            "probabilities": {"ok": 0.95, "other": 0.05}}, "RECOMMEND"),
        }
        for name, (decision_answer, expected_status) in cases.items():
            with self.subTest(case=name):
                result = policy(self._route_spec(), {}, {"decision": decision_answer})
                self.assertEqual(result["status"], expected_status)
                self.assertFalse(result["execution_authorized"])

    def test_completion_kind_blocks_on_any_failed_deterministic_check(self):
        from jevkit.engine import policy

        broken_variants = {
            "test_failed": {**self.GOOD_EVIDENCE, "test_exit_code": 1},
            "lint_failed": {**self.GOOD_EVIDENCE, "lint_exit_code": 1},
            "no_tests_executed": {**self.GOOD_EVIDENCE, "tests_executed": 0},
            "revision_not_bound": {**self.GOOD_EVIDENCE, "tested_source_hash": "rev-b"},
            "stale_evidence": {**self.GOOD_EVIDENCE, "age_seconds": 10_000},
            "non_integer_exit_code": {**self.GOOD_EVIDENCE, "test_exit_code": True},
        }
        good_answers = {"satisfies_requirement": {"noul": 0.98}, "missing_required_test": {"noul": 0.01}}
        for name, evidence in broken_variants.items():
            with self.subTest(case=name):
                result = policy(self._completion_spec(), {"evidence": evidence}, good_answers)
                self.assertEqual(result["status"], "BLOCK")

    def test_completion_kind_then_checks_the_judged_answers(self):
        from jevkit.engine import policy

        state = {"evidence": self.GOOD_EVIDENCE}
        passed = policy(self._completion_spec(), state,
                        {"satisfies_requirement": {"noul": 0.98}, "missing_required_test": {"noul": 0.01}})
        self.assertEqual(passed["status"], "CHECKS_PASSED")
        blocked = policy(self._completion_spec(), state,
                         {"satisfies_requirement": {"noul": 0.02}, "missing_required_test": {"noul": 0.01}})
        self.assertEqual(blocked["status"], "BLOCK")
        blocked_forbidden = policy(self._completion_spec(), state,
                                   {"satisfies_requirement": {"noul": 0.98}, "missing_required_test": {"noul": 0.95}})
        self.assertEqual(blocked_forbidden["status"], "BLOCK")
        reviewed = policy(self._completion_spec(), state,
                          {"satisfies_requirement": {"noul": 0.5}, "missing_required_test": {"noul": 0.01}})
        self.assertEqual(reviewed["status"], "REVIEW")

    def test_plain_gate_kind_skips_the_deterministic_evidence_block(self):
        from jevkit.engine import policy

        gate_spec = {"thresholds": self.THRESHOLDS,
                    "policy": {"kind": "gate", "require_true": ["a"], "forbid_true": ["b"]}}
        # No 'evidence' key in state: if the completion-only block ran
        # anyway, state.get('evidence', {}) would BLOCK. A plain 'gate'
        # must not run it.
        result = policy(gate_spec, {}, {"a": {"noul": 0.95}, "b": {"noul": 0.02}})
        self.assertEqual(result["status"], "CHECKS_PASSED")

    def test_rank_kind_sorts_selects_and_reviews(self):
        from jevkit.engine import policy

        rank_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "rank"}}
        state = {"items": [{"id": "low"}, {"id": "high"}, {"id": "unsure"}]}
        answers = {
            "item_0": {"score": 0.2, "confidence": 0.95, "probabilities": {}},
            "item_1": {"score": 1.9, "confidence": 0.95, "probabilities": {}},
            "item_2": {"score": 1.9, "confidence": 0.5, "probabilities": {}},
        }
        result = policy(rank_spec, state, answers)
        self.assertEqual(result["items"]["selected"], ["high"])
        self.assertEqual(result["items"]["review"], ["unsure"])
        self.assertEqual([item["id"] for item in result["items"]["ranked"]], ["high", "unsure", "low"])
        self.assertEqual(result["status"], "REVIEW")

    def test_rank_kind_recommends_when_nothing_needs_review(self):
        from jevkit.engine import policy

        rank_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "rank"}}
        state = {"items": [{"id": "high"}]}
        answers = {"item_0": {"score": 1.9, "confidence": 0.95, "probabilities": {}}}
        result = policy(rank_spec, state, answers)
        self.assertEqual(result["status"], "RECOMMEND")

    def test_test_select_kind_partitions_by_the_noul_thresholds(self):
        from jevkit.engine import policy

        select_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "test_select"}}
        state = {"items": [{"id": "relevant"}, {"id": "maybe"}, {"id": "irrelevant"}]}
        answers = {"item_0": {"noul": 0.95}, "item_1": {"noul": 0.5}, "item_2": {"noul": 0.02}}
        result = policy(select_spec, state, answers)
        self.assertEqual(result["items"]["selected"], ["relevant"])
        self.assertEqual(result["items"]["review"], ["maybe"])

    def test_context_kind_action_matrix(self):
        from jevkit.engine import policy

        context_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "context"}}
        table = {
            "quarantine": ((0.0, 0.95, 0.0), "QUARANTINE"),
            "injection_review": ((0.0, 0.5, 0.0), "REVIEW"),
            "drop": ((0.02, 0.0, 0.0), "DROP_CANDIDATE"),
            "relevance_review": ((0.5, 0.0, 0.0), "REVIEW"),
            "keep_with_conflict": ((0.95, 0.0, 0.95), "KEEP_WITH_CONFLICT"),
            "conflict_review": ((0.95, 0.0, 0.5), "REVIEW"),
            "keep": ((0.95, 0.0, 0.02), "KEEP"),
        }
        for name, ((rel, inj, conf), expected_action) in table.items():
            with self.subTest(case=name):
                state = {"items": [{"id": "x"}]}
                answers = {"relevant_0": {"noul": rel}, "injection_0": {"noul": inj}, "conflict_0": {"noul": conf}}
                result = policy(context_spec, state, answers)
                self.assertEqual(result["items"][0]["action"], expected_action)

    def test_context_kind_status_aggregation(self):
        from jevkit.engine import policy

        context_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "context"}}
        state = {"items": [{"id": "a"}, {"id": "b"}]}
        all_quarantined = policy(context_spec, state, {
            "relevant_0": {"noul": 0.0}, "injection_0": {"noul": 0.95}, "conflict_0": {"noul": 0.0},
            "relevant_1": {"noul": 0.0}, "injection_1": {"noul": 0.95}, "conflict_1": {"noul": 0.0}})
        self.assertEqual(all_quarantined["status"], "BLOCK")
        one_keep_one_drop = policy(context_spec, state, {
            "relevant_0": {"noul": 0.95}, "injection_0": {"noul": 0.0}, "conflict_0": {"noul": 0.0},
            "relevant_1": {"noul": 0.0}, "injection_1": {"noul": 0.0}, "conflict_1": {"noul": 0.0}})
        self.assertEqual(one_keep_one_drop["status"], "RECOMMEND")

    def test_unknown_policy_kind_is_rejected(self):
        from jevkit.engine import policy
        from jevkit.security import SafeError

        bogus_spec = {"thresholds": self.THRESHOLDS, "policy": {"kind": "not-a-real-kind"}}
        with self.assertRaisesRegex(SafeError, "UNKNOWN_POLICY"):
            policy(bogus_spec, {}, {})


class JevkitEngineRecordHashTests(unittest.TestCase):
    """_record_sha256 must hash the receipt's substantive content only --
    changing a storage-derived field (record_sha256, receipt_path) must
    never change the hash, and changing anything else always must."""

    def test_storage_derived_fields_are_excluded_from_the_hash(self):
        from jevkit.engine import _record_sha256

        record = {"case_id": "01-skill-routing", "status": "RECOMMEND",
                 "record_sha256": "placeholder", "receipt_path": "/some/path.json"}
        base = _record_sha256(record)
        same = _record_sha256({**record, "record_sha256": "different-placeholder",
                               "receipt_path": "/a/totally/different/path.json"})
        self.assertEqual(base, same)

    def test_any_other_field_change_changes_the_hash(self):
        from jevkit.engine import _record_sha256

        record = {"case_id": "01-skill-routing", "status": "RECOMMEND"}
        base = _record_sha256(record)
        self.assertNotEqual(_record_sha256({**record, "status": "REVIEW"}), base)


class JevkitEngineRunTests(unittest.TestCase):
    """run() wires spec/fixture/questions_for/policy/persistence together.
    Fixture mode exercises the real shipped fixtures; live mode replaces
    jevkit.client.evaluate and jevkit.providers.resolve_provider so no
    network call is ever made."""

    @staticmethod
    def _fake_live_answers():
        return {"decision": {"type": "choice", "choice": "testing", "confidence": 0.95,
                             "probabilities": {"testing": 0.95, "docs": 0.03, "browser": 0.01, "none": 0.01}}}

    def test_fixture_mode_nominal_run_recommends_and_persists_a_receipt(self):
        from jevkit import engine

        with tempfile.TemporaryDirectory() as directory:
            record = engine.run("01-skill-routing", "nominal", mode="fixture",
                                root=RUNTIME, persist=True, state_root=Path(directory))
        self.assertEqual(record["policy"]["status"], "RECOMMEND")
        self.assertTrue(record["fixture_contract_passed"])
        self.assertEqual(record["provenance_label"], "FIXTURE — SIMULATED RESPONSE — NOT LIVE JEV")
        self.assertIsNone(record["model_requested"])

    def test_fixture_mode_persists_a_receipt_file_that_round_trips(self):
        from jevkit import engine

        with tempfile.TemporaryDirectory() as directory:
            record = engine.run("08-completion-gate", "adversarial", mode="fixture",
                                root=RUNTIME, persist=True, state_root=Path(directory))
            self.assertEqual(record["policy"]["status"], "BLOCK")
            receipt = Path(record["receipt_path"])
            self.assertTrue(receipt.is_file())
            reloaded = json.loads(receipt.read_text())
        self.assertEqual(reloaded["record_sha256"], record["record_sha256"])

    def test_fixture_mode_with_persist_false_writes_nothing(self):
        from jevkit import engine

        with tempfile.TemporaryDirectory() as directory:
            record = engine.run("01-skill-routing", "nominal", mode="fixture",
                                root=RUNTIME, persist=False, state_root=Path(directory))
            self.assertNotIn("receipt_path", record)
            self.assertEqual(list(Path(directory).rglob("*")), [])

    def test_custom_state_in_fixture_mode_is_forbidden(self):
        from jevkit import engine
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "FIXTURE_CUSTOM_STATE_FORBIDDEN"):
            engine.run("01-skill-routing", state={"request": "x", "skills": {}}, mode="fixture",
                      root=RUNTIME, persist=False)

    def test_invalid_mode_is_rejected(self):
        from jevkit import engine
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "INVALID_MODE"):
            engine.run("01-skill-routing", "nominal", mode="not-a-real-mode", root=RUNTIME, persist=False)

    def test_screened_custom_state_is_blocked_before_touching_any_provider(self):
        from jevkit import engine
        from jevkit.security import SafeError

        state = {"request": "Escalate to someone@example.com please", "skills": {}}
        with patch("jevkit.providers.resolve_provider", side_effect=AssertionError("PROVIDER_TOUCHED")), \
             patch("jevkit.client.evaluate", side_effect=AssertionError("CLIENT_TOUCHED")):
            with self.assertRaisesRegex(SafeError, "INPUT_DATA_BLOCKED"):
                engine.run("01-skill-routing", state=state, mode="live", root=RUNTIME, persist=False)

    def test_live_mode_success_records_provider_metadata(self):
        from jevkit import engine
        from jevkit.providers import provider_profile

        profile = provider_profile("typesafe")
        response = {"model": profile.model, "answers": self._fake_live_answers(),
                   "usage": {"input_tokens": 10, "output_tokens": 3},
                   "_transport": {"attempts": 1, "observed_latency_ms": 5.0},
                   "_grant_id": "grant-1", "_provider_id": "typesafe", "_provider_profile_sha256": "x" * 64}
        state = {"request": "Run and diagnose the checkout regression tests.",
                 "skills": {"testing": "unit tests", "docs": "edit docs", "browser": "inspect UI"}}
        with tempfile.TemporaryDirectory() as directory:
            with patch("jevkit.providers.resolve_provider", return_value=profile), \
                 patch("jevkit.client.evaluate", return_value=response) as evaluate:
                record = engine.run("01-skill-routing", state=state, mode="live", root=RUNTIME,
                                    persist=True, state_root=Path(directory),
                                    workspace_id="ws-1", revision="rev-1",
                                    adapter_version="1.0.0", request_id="req-1")
        self.assertEqual(record["policy"]["status"], "RECOMMEND")
        self.assertEqual(record["model_requested"], profile.model)
        self.assertEqual(record["grant_id"], "grant-1")
        self.assertEqual(record["provider_id"], "typesafe")
        self.assertEqual(record["workspace_id"], "ws-1")
        self.assertEqual(record["revision"], "rev-1")
        self.assertEqual(record["adapter_version"], "1.0.0")
        self.assertEqual(record["request_id"], "req-1")
        self.assertEqual(record["transport"], response["_transport"])
        evaluate.assert_called_once()

    def test_live_mode_rejects_an_explicit_request_sha256_that_does_not_match(self):
        from jevkit import engine
        from jevkit.providers import provider_profile
        from jevkit.security import SafeError

        profile = provider_profile("typesafe")
        state = {"request": "Run and diagnose the checkout regression tests.",
                 "skills": {"testing": "unit tests", "docs": "edit docs", "browser": "inspect UI"}}
        with patch("jevkit.providers.resolve_provider", return_value=profile), \
             patch("jevkit.client.evaluate", side_effect=AssertionError("CLIENT_TOUCHED")):
            with self.assertRaisesRegex(SafeError, "REQUEST_HASH_MISMATCH"):
                engine.run("01-skill-routing", state=state, mode="live", root=RUNTIME,
                          persist=False, request_sha256="0" * 64)

    def test_live_mode_accepts_a_matching_precomputed_request_sha256(self):
        from jevkit import engine
        from jevkit.engine import questions_for, spec
        from jevkit.providers import provider_profile
        from jevkit.security import canonical

        profile = provider_profile("typesafe")
        state = {"request": "Run and diagnose the checkout regression tests.",
                 "skills": {"testing": "unit tests", "docs": "edit docs", "browser": "inspect UI"}}
        qs = questions_for(spec("01-skill-routing", RUNTIME), state)
        expected_hash = hashlib.sha256(
            canonical({"model": profile.model, "state": state, "questions": qs})).hexdigest()
        response = {"model": profile.model, "answers": self._fake_live_answers(),
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        with patch("jevkit.providers.resolve_provider", return_value=profile), \
             patch("jevkit.client.evaluate", return_value=response):
            record = engine.run("01-skill-routing", state=state, mode="live", root=RUNTIME,
                                persist=False, request_sha256=expected_hash)
        self.assertEqual(record["request_sha256"], expected_hash)


class MacKeychainPublicApiTests(unittest.TestCase):
    """MacKeychain's public API, exercised entirely through an injected fake
    backend -- the class's own supported seam. Never touches
    Security.framework."""

    class _FakeBackend:
        def __init__(self):
            self.store = {}
            self.calls = []

        def put(self, service, account, secret):
            self.calls.append(("put", service, account, secret))
            self.store[(service, account)] = secret

        def get(self, service, account):
            self.calls.append(("get", service, account))
            return self.store[(service, account)]

        def delete(self, service, account):
            self.calls.append(("delete", service, account))
            del self.store[(service, account)]

    def test_constructor_rejects_backend_and_keychain_path_together(self):
        from src.adl.api.keychain import MacKeychain

        with self.assertRaises(ValueError):
            MacKeychain(backend=self._FakeBackend(), keychain_path=Path("/tmp/whatever"))

    def test_constructor_rejects_a_keychain_path_that_is_not_a_real_file(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeychainError):
                MacKeychain(keychain_path=Path(directory))  # a directory, not a file
            missing = Path(directory) / "does-not-exist"
            with self.assertRaises(KeychainError):
                MacKeychain(keychain_path=missing)
            real_file = Path(directory) / "real.keychain"
            real_file.write_text("not a real keychain, just bytes")
            link = Path(directory) / "linked.keychain"
            link.symlink_to(real_file)
            with self.assertRaises(KeychainError):
                MacKeychain(keychain_path=link)

    def test_unsupported_provider_is_rejected(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        keychain = MacKeychain(backend=self._FakeBackend())
        for bogus in ("not-a-provider", ["unhashable"]):
            with self.subTest(provider=bogus):
                with self.assertRaises(KeychainError):
                    keychain.get(bogus)

    def test_credential_validation_rejects_bad_values_before_touching_the_backend(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        backend = self._FakeBackend()
        keychain = MacKeychain(backend=backend)
        bad_values = {
            "too_short": "short",
            "too_long": "x" * 4097,
            "non_ascii": "café-café-café-café",
            "embedded_space": "abc defgh ijklmnop",
            "embedded_null": "abcdefgh\x00ijklmnop",
        }
        for name, value in bad_values.items():
            with self.subTest(case=name):
                with self.assertRaises(KeychainError):
                    keychain.put("typesafe", value)
        self.assertEqual(backend.calls, [])

    def test_put_get_delete_round_trip_through_the_injected_backend(self):
        from src.adl.api.keychain import MacKeychain

        backend = self._FakeBackend()
        keychain = MacKeychain(backend=backend)
        keychain.put("typesafe", "S" * 32)
        self.assertEqual(keychain.get("typesafe"), "S" * 32)
        keychain.delete("typesafe")
        self.assertEqual(backend.calls[0][:3], ("put", "ai.qualixar.adl.typesafe", str(os.getuid())))

    def test_get_rejects_non_ascii_bytes_returned_by_the_backend(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        class BadBackend:
            def get(self, service, account):
                return b"\xff\xfe-not-ascii-\xff"

        keychain = MacKeychain(backend=BadBackend())
        with self.assertRaises(KeychainError):
            keychain.get("typesafe")

    def test_get_rejects_a_backend_value_with_no_decode_method(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        class BadBackend:
            def get(self, service, account):
                return None

        keychain = MacKeychain(backend=BadBackend())
        with self.assertRaises(KeychainError):
            keychain.get("typesafe")

    def test_get_rejects_an_ascii_value_that_is_the_wrong_shape(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        class TooShortBackend:
            def get(self, service, account):
                return b"short"

        keychain = MacKeychain(backend=TooShortBackend())
        with self.assertRaises(KeychainError):
            keychain.get("typesafe")

    def test_native_backend_is_reused_once_constructed(self):
        from src.adl.api.keychain import MacKeychain

        sentinel = self._FakeBackend()
        keychain = MacKeychain(backend=sentinel)
        self.assertIs(keychain._native(), sentinel)
        self.assertIs(keychain._native(), sentinel)

    def test_native_backend_is_constructed_lazily_when_none_is_injected(self):
        """The real production branch: no backend was injected, so _native()
        must build a real _SecurityFrameworkBackend. Constructing one is a
        pure dlopen (see SecurityFrameworkBackendTests) -- this test never
        calls get/put/delete on it, so no keychain item is ever touched."""
        from src.adl.api.keychain import MacKeychain, _SecurityFrameworkBackend

        keychain = MacKeychain()
        backend = keychain._native()
        self.assertIsInstance(backend, _SecurityFrameworkBackend)
        self.assertIs(keychain._native(), backend)

    def test_non_macos_platform_is_refused_even_with_a_working_backend(self):
        from src.adl.api.keychain import KeychainError, MacKeychain

        keychain = MacKeychain(backend=self._FakeBackend())
        with patch("src.adl.api.keychain.platform.system", return_value="Linux"):
            with self.assertRaises(KeychainError):
                keychain.put("typesafe", "T" * 32)
            with self.assertRaises(KeychainError):
                keychain.get("typesafe")
            with self.assertRaises(KeychainError):
                keychain.delete("typesafe")


class SecurityFrameworkBackendTests(unittest.TestCase):
    """_SecurityFrameworkBackend talks to Security.framework via ctypes.
    Every test builds the instance with object.__new__ (skipping __init__,
    so no framework is even loaded) and replaces _security/_core with
    in-memory mocks -- except the two __init__-specific tests, which load
    the real, always-present system frameworks but never call a single
    keychain function on them."""

    NOT_FOUND = -25300

    def _bare_backend(self, keychain_path=None):
        from src.adl.api.keychain import _SecurityFrameworkBackend

        backend = object.__new__(_SecurityFrameworkBackend)
        backend._keychain_path = keychain_path
        backend._security = MagicMock(name="Security")
        backend._core = MagicMock(name="CoreFoundation")
        return backend

    def test_init_loads_the_real_frameworks_and_configures_argtypes(self):
        """Loading Security.framework/CoreFoundation.framework is a pure
        in-process dlopen: it reads no keychain item and stores no secret."""
        from src.adl.api.keychain import _SecurityFrameworkBackend

        backend = _SecurityFrameworkBackend()
        self.assertIsNotNone(backend._security.SecKeychainFindGenericPassword.argtypes)
        self.assertIsNotNone(backend._core.CFRelease)

    def test_init_wraps_a_framework_load_failure(self):
        from src.adl.api.keychain import KeychainError, _SecurityFrameworkBackend

        with patch("src.adl.api.keychain.ctypes.CDLL", side_effect=OSError("synthetic dlopen failure")):
            with self.assertRaises(KeychainError):
                _SecurityFrameworkBackend()

    def test_opened_yields_the_default_keychain_reference_without_releasing(self):
        backend = self._bare_backend(keychain_path=None)
        with backend._opened() as reference:
            self.assertIsInstance(reference, ctypes.c_void_p)
        backend._core.CFRelease.assert_not_called()

    def test_opened_with_an_explicit_path_opens_and_releases(self):
        backend = self._bare_backend(keychain_path=Path("/synthetic/login.keychain"))

        def open_side_effect(path_bytes, ref):
            ref._obj.value = 0xABCDEF
            return 0

        backend._security.SecKeychainOpen.side_effect = open_side_effect
        with backend._opened() as reference:
            self.assertEqual(reference.value, 0xABCDEF)
        backend._core.CFRelease.assert_called_once()

    def test_opened_with_an_explicit_path_raises_when_open_fails(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend(keychain_path=Path("/synthetic/login.keychain"))
        backend._security.SecKeychainOpen.return_value = -1
        with self.assertRaises(KeychainError):
            with backend._opened():
                pass
        backend._core.CFRelease.assert_not_called()

    def test_put_adds_a_new_item_when_none_is_found(self):
        backend = self._bare_backend()
        backend._security.SecKeychainFindGenericPassword.return_value = self.NOT_FOUND
        backend._security.SecKeychainAddGenericPassword.return_value = 0
        backend.put("svc", "acct", b"12345678")
        backend._security.SecKeychainAddGenericPassword.assert_called_once()
        backend._security.SecKeychainItemModifyAttributesAndData.assert_not_called()

    def test_put_modifies_an_existing_item_and_releases_it(self):
        backend = self._bare_backend()

        def find_side_effect(keychain, sl, s, al, a, attrs, data, item_ref):
            item_ref._obj.value = 0xDEAD0000
            return 0

        backend._security.SecKeychainFindGenericPassword.side_effect = find_side_effect
        backend._security.SecKeychainItemModifyAttributesAndData.return_value = 0
        backend.put("svc", "acct", b"12345678")
        backend._security.SecKeychainItemModifyAttributesAndData.assert_called_once()
        backend._core.CFRelease.assert_called_once()

    def test_put_raises_when_add_or_modify_fails(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend()
        backend._security.SecKeychainFindGenericPassword.return_value = self.NOT_FOUND
        backend._security.SecKeychainAddGenericPassword.return_value = -1
        with self.assertRaises(KeychainError):
            backend.put("svc", "acct", b"12345678")

    def test_get_reads_back_the_exact_bytes_the_backend_reports(self):
        backend = self._bare_backend()
        payload = b"fake-credential-bytes"
        buffer = ctypes.create_string_buffer(payload, len(payload))
        address = ctypes.cast(buffer, ctypes.c_void_p).value

        def find_side_effect(keychain, sl, s, al, a, length_ref, data_ref, item_ref):
            length_ref._obj.value = len(payload)
            data_ref._obj.value = address
            return 0

        backend._security.SecKeychainFindGenericPassword.side_effect = find_side_effect
        self.assertEqual(backend.get("svc", "acct"), payload)
        backend._security.SecKeychainItemFreeContent.assert_called_once()

    def test_get_raises_item_missing_when_not_found(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend()
        backend._security.SecKeychainFindGenericPassword.return_value = self.NOT_FOUND
        with self.assertRaises(KeychainError):
            backend.get("svc", "acct")
        backend._security.SecKeychainItemFreeContent.assert_not_called()

    def test_get_frees_content_if_not_found_status_still_reports_data(self):
        """Defensive branch: even a NOT_FOUND status is followed by freeing
        `data` if the out-parameter came back non-null."""
        backend = self._bare_backend()
        payload = b"leftover"
        buffer = ctypes.create_string_buffer(payload, len(payload))
        address = ctypes.cast(buffer, ctypes.c_void_p).value

        def find_side_effect(keychain, sl, s, al, a, length_ref, data_ref, item_ref):
            data_ref._obj.value = address
            return self.NOT_FOUND

        backend._security.SecKeychainFindGenericPassword.side_effect = find_side_effect
        from src.adl.api.keychain import KeychainError

        with self.assertRaises(KeychainError):
            backend.get("svc", "acct")
        backend._security.SecKeychainItemFreeContent.assert_called_once()

    def test_get_raises_read_failed_for_an_out_of_range_length(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend()
        payload = b"ab"
        buffer = ctypes.create_string_buffer(payload, len(payload))
        address = ctypes.cast(buffer, ctypes.c_void_p).value

        def find_side_effect(keychain, sl, s, al, a, length_ref, data_ref, item_ref):
            length_ref._obj.value = len(payload)  # 2 bytes: below the 8-byte floor
            data_ref._obj.value = address
            return 0

        backend._security.SecKeychainFindGenericPassword.side_effect = find_side_effect
        with self.assertRaises(KeychainError):
            backend.get("svc", "acct")
        backend._security.SecKeychainItemFreeContent.assert_called_once()

    def test_delete_releases_a_found_item_and_raises_if_delete_fails(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend()

        def find_side_effect(keychain, sl, s, al, a, attrs, data, item_ref):
            item_ref._obj.value = 0xBEEF0000
            return 0

        backend._security.SecKeychainFindGenericPassword.side_effect = find_side_effect
        backend._security.SecKeychainItemDelete.return_value = -1
        with self.assertRaises(KeychainError):
            backend.delete("svc", "acct")
        backend._core.CFRelease.assert_called_once()

    def test_delete_raises_when_item_is_not_found(self):
        from src.adl.api.keychain import KeychainError

        backend = self._bare_backend()
        backend._security.SecKeychainFindGenericPassword.return_value = self.NOT_FOUND
        with self.assertRaises(KeychainError):
            backend.delete("svc", "acct")
        backend._core.CFRelease.assert_not_called()


class LocalAttestorHashArtifactTests(unittest.TestCase):
    """_hash_artifact opens files under model_dir via a dir_fd chain with
    O_NOFOLLOW, so it can only ever hash a real, already-open, non-symlink
    file -- every test uses real (small) files in a TemporaryDirectory."""

    def test_rejects_unsafe_artifact_names(self):
        from src.adl.api.local_attestor import AttestationError, _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_names = ["/absolute/path", "a\\b", "a//b", "..", ".", "a/../b", "a/./b", ""]
            for name in bad_names:
                with self.subTest(name=name):
                    with self.assertRaises(AttestationError):
                        _hash_artifact(root, name, remaining_bytes=1_000_000)

    # Fixed in 1.0.7; kept as a regression guard.
    def test_defect_non_string_artifact_name_crashes_before_the_type_guard(self):
        """FILE: plugins/qualixar-jev-decision-layer/runtime/src/adl/api/local_attestor.py
        _hash_artifact, lines 119-129.

        Current behaviour: line 120 (`path = PurePosixPath(name)`) runs
        UNCONDITIONALLY, before the `not isinstance(name, str)` guard on
        line 122 that was clearly written to catch exactly this input. A
        non-string `name` makes PurePosixPath() raise a raw TypeError, so
        the isinstance check that names this case can never actually fire.

        Correct behaviour: the isinstance(name, str) check should run (or
        the type should be re-checked) BEFORE PurePosixPath(name) is
        constructed, so a non-string name raises the documented
        AttestationError('LOCAL_ARTIFACT_MISMATCH') like every other
        malformed name, not an undocumented TypeError.

        Concrete failing input -> wrong output: _hash_artifact(root, 42,
        remaining_bytes=...) raises TypeError("argument should be a str or
        an os.PathLike object...") instead of AttestationError.

        Severity: LOW, not HIGH. The only production caller
        (attest_local_config, via manifest.get('files').items()) gets
        `name` from a JSON object's keys, and JSON object keys are always
        strings -- so this specific ordering gap is not reachable through
        the real manifest-loading path today. It is reported because it
        is a real, reproducible mismatch between the check's intent and
        its effect, and because a future caller of _hash_artifact that is
        less careful about its input would trip over an undocumented
        exception type instead of the module's own error contract.
        """
        from src.adl.api.local_attestor import AttestationError, _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AttestationError):
                _hash_artifact(Path(directory), 42, remaining_bytes=1_000_000)

    def test_hashes_a_nested_file_and_matches_hashlib(self):
        from src.adl.api.local_attestor import _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            payload = b"synthetic model bytes for hashing"
            (root / "sub" / "weights.bin").write_bytes(payload)
            digest, size = _hash_artifact(root, "sub/weights.bin", remaining_bytes=1_000_000)
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(size, len(payload))

    def test_rejects_a_file_larger_than_the_remaining_byte_budget(self):
        from src.adl.api.local_attestor import AttestationError, _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.bin").write_bytes(b"x" * 100)
            with self.assertRaises(AttestationError):
                _hash_artifact(root, "big.bin", remaining_bytes=10)

    def test_rejects_an_empty_file(self):
        from src.adl.api.local_attestor import AttestationError, _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty.bin").write_bytes(b"")
            with self.assertRaises(AttestationError):
                _hash_artifact(root, "empty.bin", remaining_bytes=1_000_000)

    def test_rejects_a_name_that_resolves_to_a_directory(self):
        from src.adl.api.local_attestor import AttestationError, _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "not-a-file").mkdir()
            with self.assertRaises(AttestationError):
                _hash_artifact(root, "not-a-file", remaining_bytes=1_000_000)

    def test_refuses_to_follow_a_symlinked_target_file(self):
        from src.adl.api.local_attestor import _hash_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.bin"
            real.write_bytes(b"data")
            (root / "link.bin").symlink_to(real)
            with self.assertRaises(OSError):
                _hash_artifact(root, "link.bin", remaining_bytes=1_000_000)


class LocalAttestorRuntimeProbeTests(unittest.TestCase):
    """_runtime_commit_from_python spawns `python -I -c ...` and reads its
    stdout through a bounded selector loop. Every test replaces
    subprocess.Popen with _FakePipeProcess, a real OS pipe with no process
    behind it -- selectors needs a real file descriptor, not a Mock."""

    COMMIT = "0a859518634112655cb97c745dbf04f5191aaf13"

    def test_success_path_returns_the_reported_commit(self):
        from src.adl.api.local_attestor import _runtime_commit_from_python

        payload = json.dumps({"vcs_info": {"commit_id": self.COMMIT}}).encode()
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=_FakePipeProcess(payload)):
            self.assertEqual(_runtime_commit_from_python(Path("/usr/bin/python3")), self.COMMIT)

    def test_nonzero_exit_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        payload = json.dumps({"vcs_info": {"commit_id": self.COMMIT}}).encode()
        with patch("src.adl.api.local_attestor.subprocess.Popen",
                  return_value=_FakePipeProcess(payload, exit_code=1)):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_non_json_output_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        with patch("src.adl.api.local_attestor.subprocess.Popen",
                  return_value=_FakePipeProcess(b"not-json-at-all")):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_empty_output_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=_FakePipeProcess(b"")):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_json_missing_vcs_info_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        payload = json.dumps({"other": "stuff"}).encode()
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=_FakePipeProcess(payload)):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_commit_id_failing_the_revision_shape_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        payload = json.dumps({"vcs_info": {"commit_id": "NOT-40-HEX-CHARS"}}).encode()
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=_FakePipeProcess(payload)):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_output_over_the_size_cap_is_rejected(self):
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        oversized = b" " * 5000 + json.dumps({"vcs_info": {"commit_id": self.COMMIT}}).encode()
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=_FakePipeProcess(oversized)):
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))

    def test_deadline_expiring_is_rejected_without_a_real_wait(self):
        """Forces the remaining<=0 branch by advancing the mocked clock past
        the (hard-coded, non-injectable) 5-second deadline -- this test
        costs microseconds, not five real seconds. os.killpg is patched so
        the cleanup path (process still 'alive') never sends a real signal
        to the synthetic pid."""
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        process = _FakePipeProcess(b"", stays_alive=True)
        clock = iter([100.0] + [200.0] * 20)
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=process), \
             patch("src.adl.api.local_attestor.time.monotonic", side_effect=lambda: next(clock)), \
             patch("src.adl.api.local_attestor.os.killpg") as killpg:
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))
        killpg.assert_called_once_with(process.pid, __import__("signal").SIGKILL)

    def test_killpg_race_where_the_process_already_exited_is_swallowed(self):
        """Between checking process.poll() is None and calling os.killpg,
        the process can legitimately exit on its own -- os.killpg then
        raises ProcessLookupError, which must be swallowed, not surfaced."""
        from src.adl.api.local_attestor import AttestationError, _runtime_commit_from_python

        process = _FakePipeProcess(b"", stays_alive=True)
        clock = iter([100.0] + [200.0] * 20)
        with patch("src.adl.api.local_attestor.subprocess.Popen", return_value=process), \
             patch("src.adl.api.local_attestor.time.monotonic", side_effect=lambda: next(clock)), \
             patch("src.adl.api.local_attestor.os.killpg", side_effect=ProcessLookupError()) as killpg:
            with self.assertRaises(AttestationError):
                _runtime_commit_from_python(Path("/usr/bin/python3"))
        killpg.assert_called_once()


class LocalAttestorConfigTests(unittest.TestCase):
    """attest_local_config wires shape validation, pin approval, interpreter
    verification (via the injectable runtime_probe seam), and the manifest
    + per-file hash check together. approved_pins/expected_python/
    runtime_probe are always supplied explicitly, so this never depends on
    a real Laya install or a real subprocess."""

    def _make_fixture(self, directory):
        from src.adl.api.local_attestor import ApprovedPin

        root = Path(directory)
        python_path = root / "fake-python"
        python_path.write_text("#!/bin/sh\n")
        python_path.chmod(0o755)
        model_dir = root / "model"
        model_dir.mkdir()
        weight_bytes = b"synthetic weight payload for hashing"
        (model_dir / "model.safetensors").write_bytes(weight_bytes)
        weight_hash = hashlib.sha256(weight_bytes).hexdigest()
        revision = "a" * 40
        runtime_commit = "b" * 40
        repository = "synthetic/repo"
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "repository": repository, "revision": revision,
            "files": {"model.safetensors": weight_hash},
        }))
        manifest_path.chmod(0o600)
        pin = ApprovedPin(repository, revision, weight_hash, runtime_commit)
        config = {
            "repository": repository, "revision": revision, "weight_sha256": weight_hash,
            "model_dir": str(model_dir), "artifact_manifest": str(manifest_path),
            "python": str(python_path), "runtime_commit": runtime_commit,
        }
        return config, (pin,), python_path, runtime_commit

    def test_happy_path_returns_the_config_unchanged(self):
        from src.adl.api.local_attestor import attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            result = attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                        runtime_probe=lambda p: commit)
        self.assertEqual(result, config)

    def test_rejects_a_non_dict_or_unknown_keyed_config(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with self.assertRaises(AttestationError):
            attest_local_config("not-a-dict")
        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            with self.assertRaises(AttestationError):
                attest_local_config({**config, "unexpected_key": "x"}, approved_pins=pins,
                                    expected_python=python_path, runtime_probe=lambda p: commit)

    def test_rejects_when_secure_file_open_is_unsupported(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            with patch("src.adl.api.local_attestor._supports_secure_file_open", return_value=False):
                with self.assertRaisesRegex(AttestationError, "LOCAL_PLATFORM_UNVERIFIED"):
                    attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                        runtime_probe=lambda p: commit)

    def test_rejects_malformed_identity_fields(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            broken = {
                "short_revision": {**config, "revision": "a" * 39},
                "uppercase_revision": {**config, "revision": "A" * 40},
                "short_weight_hash": {**config, "weight_sha256": "b" * 63},
                "non_string_model_dir": {**config, "model_dir": 123},
                "non_string_manifest": {**config, "artifact_manifest": 123},
            }
            for name, bad_config in broken.items():
                with self.subTest(case=name):
                    with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                        attest_local_config(bad_config, approved_pins=pins, expected_python=python_path,
                                            runtime_probe=lambda p: commit)

    def test_rejects_an_identity_not_in_the_approved_pin_list(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            with self.assertRaisesRegex(AttestationError, "LOCAL_PIN_NOT_APPROVED"):
                attest_local_config({**config, "revision": "c" * 40}, approved_pins=pins,
                                    expected_python=python_path, runtime_probe=lambda p: commit)

    def test_rejects_a_python_path_that_does_not_match_the_expected_interpreter(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            with self.assertRaisesRegex(AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                attest_local_config({**config, "python": "/some/other/python"}, approved_pins=pins,
                                    expected_python=python_path, runtime_probe=lambda p: commit)

    def test_rejects_a_non_executable_interpreter_file(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            python_path.chmod(0o644)
            with self.assertRaisesRegex(AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: commit)

    def test_rejects_a_symlinked_ancestor_of_the_expected_interpreter(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            root = Path(directory)
            real_dir = root / "real-bin-dir"
            real_dir.mkdir()
            moved = real_dir / "fake-python"
            python_path.rename(moved)
            moved.chmod(0o755)
            linked_dir = root / "linked-bin-dir"
            linked_dir.symlink_to(real_dir)
            evil_expected_python = linked_dir / "fake-python"
            with self.assertRaisesRegex(AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                attest_local_config({**config, "python": str(evil_expected_python)}, approved_pins=pins,
                                    expected_python=evil_expected_python, runtime_probe=lambda p: commit)

    def test_rejects_when_the_runtime_probe_raises(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)

            def blow_up(_python):
                raise RuntimeError("synthetic probe failure")

            with self.assertRaisesRegex(AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=blow_up)

    def test_rejects_a_runtime_commit_that_does_not_match_the_pin(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, _commit = self._make_fixture(directory)
            with self.assertRaisesRegex(AttestationError, "LOCAL_RUNTIME_NOT_APPROVED"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: "different-commit")

    def test_rejects_non_absolute_model_dir_or_manifest(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config({**config, "model_dir": "relative/model"}, approved_pins=pins,
                                    expected_python=python_path, runtime_probe=lambda p: commit)

    def test_rejects_a_model_dir_that_does_not_exist(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            missing = str(Path(directory) / "does-not-exist")
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config({**config, "model_dir": missing}, approved_pins=pins,
                                    expected_python=python_path, runtime_probe=lambda p: commit)

    def test_rejects_manifest_field_mismatches(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            manifest_path = Path(config["artifact_manifest"])
            base_manifest = json.loads(manifest_path.read_text())
            broken_manifests = {
                "wrong_schema_version": {**base_manifest, "schema_version": 2},
                "wrong_repository": {**base_manifest, "repository": "someone/else"},
                "wrong_revision": {**base_manifest, "revision": "f" * 40},
                "files_not_a_dict": {**base_manifest, "files": []},
                "too_many_files": {**base_manifest, "files": {f"f{i}": "a" * 64 for i in range(65)}},
                "weight_hash_mismatch": {**base_manifest, "files": {"model.safetensors": "0" * 64}},
            }
            for name, manifest in broken_manifests.items():
                with self.subTest(case=name):
                    manifest_path.write_text(json.dumps(manifest))
                    manifest_path.chmod(0o600)
                    with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                        attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                            runtime_probe=lambda p: commit)

    def test_rejects_a_manifest_that_is_not_a_json_object(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            manifest_path = Path(config["artifact_manifest"])
            manifest_path.write_text(json.dumps([1, 2, 3]))
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: commit)

    def test_rejects_a_manifest_entry_whose_content_does_not_match_its_hash(self):
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            model_dir = Path(config["model_dir"])
            (model_dir / "model.safetensors").write_bytes(b"tampered content, different from the manifest hash")
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: commit)

    def test_rejects_a_manifest_hash_value_with_the_wrong_shape(self):
        """Distinct from test_rejects_a_manifest_entry_whose_content_does_not_match_its_hash:
        here the manifest's recorded value for model.safetensors is not
        even a well-formed 64-hex-char SHA256 string, so this is caught by
        the per-file shape check inside the loop (line ~219) rather than
        the config-vs-manifest cross-check or the hash-mismatch compare."""
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            manifest_path = Path(config["artifact_manifest"])
            manifest = json.loads(manifest_path.read_text())
            # Two files: the first keeps the config's declared weight_hash
            # (so the earlier cross-check passes), the second has a
            # malformed hash string and is what line ~219 must catch.
            extra_file = Path(config["model_dir"]) / "extra.bin"
            extra_file.write_bytes(b"irrelevant")
            manifest["files"]["extra.bin"] = "not-a-valid-sha256-hash"
            manifest_path.write_text(json.dumps(manifest))
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: commit)

    def test_wraps_a_read_private_failure_while_loading_the_manifest(self):
        """A loosely-permissioned manifest file makes read_private() raise
        jev_auto.common.AutoError; attest_local_config must fold that into
        its own AttestationError contract rather than let a foreign
        exception type escape."""
        from src.adl.api.local_attestor import AttestationError, attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, python_path, commit = self._make_fixture(directory)
            Path(config["artifact_manifest"]).chmod(0o644)  # group/other readable: rejected by read_private
            with self.assertRaisesRegex(AttestationError, "LOCAL_ARTIFACT_MISMATCH"):
                attest_local_config(config, approved_pins=pins, expected_python=python_path,
                                    runtime_probe=lambda p: commit)

    def test_default_expected_python_branch_current_vs_legacy(self):
        """Covers the `expected_python is None` default-computation branch,
        which normally resolves against home_root()/Path.home(); both are
        redirected into the tempdir so nothing touches the real $HOME."""
        from src.adl.api.local_attestor import attest_local_config

        with tempfile.TemporaryDirectory() as directory:
            config, pins, _python_path, commit = self._make_fixture(directory)
            state_home = Path(directory) / "state-home"
            home_dir = Path(directory) / "home-dir"
            current_python = state_home / "mlx-env" / "bin" / "python"
            legacy_python = home_dir / ".local" / "state" / "qualixar-jev-auto" / "mlx-env" / "bin" / "python"
            for label, target, python_value in (
                ("current", current_python, str(current_python)),
                ("legacy", legacy_python, str(legacy_python)),
            ):
                with self.subTest(case=label):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("#!/bin/sh\n")
                    target.chmod(0o755)
                    with patch("src.adl.api.local_attestor.home_root", return_value=state_home), \
                         patch.object(Path, "home", return_value=home_dir):
                        attest_local_config({**config, "python": python_value}, approved_pins=pins,
                                            expected_python=None, runtime_probe=lambda p: commit)


if __name__ == "__main__":
    unittest.main()
