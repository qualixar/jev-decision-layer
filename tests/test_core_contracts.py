"""Offline behavior tests for the shipped runtime; no provider key is needed."""

from __future__ import annotations

import os
import io
import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


class CoreContractTests(unittest.TestCase):
    def test_prompt_candidates_include_bounded_global_skill_names_without_workspace_git(self):
        from jev_auto.prepare import candidates

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            skills = root / "codex-home" / "skills"
            for name in ("review-routing", "test-routing"):
                folder = skills / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Review and test synthetic routing changes.\n---\n")
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}), patch.object(Path, "home", return_value=root):
                found = candidates(project, "Please review and test the synthetic routing change in this project.")
        self.assertTrue({"skill:review-routing", "skill:test-routing"} <= {item["id"] for item in found})
        self.assertTrue(all("/private/tmp/" not in item["description"] for item in found))

    def test_prompt_candidates_rank_specific_skill_above_generic_early_names(self):
        from jev_auto.prepare import candidates

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            skills = root / "codex-home" / "skills"
            for index in range(20):
                folder = skills / f"a-generic-{index:02d}"
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(
                    f"---\nname: {folder.name}\ndescription: Review and test ordinary changes.\n---\n")
            target = skills / "zz-routing-architecture"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "---\nname: zz-routing-architecture\ndescription: Review and test synthetic routing architecture decisions.\n---\n")
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-home")}), patch.object(Path, "home", return_value=root):
                found = candidates(project, "Please review and test the synthetic routing architecture decision.")
        self.assertEqual(found[0]["id"], "skill:zz-routing-architecture")
        self.assertNotIn("SKILL.md", found[0]["description"])

    def test_legacy_runtime_tools_use_the_new_plugin_state_namespace(self):
        from jevkit.runtime import RuntimeContext

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                context = RuntimeContext(RUNTIME)
        self.assertEqual(context.state_root.name, "qualixar-jev-decision-layer")

    def test_legacy_mcp_initializer_uses_the_new_plugin_identity(self):
        from jevkit.mcp_server import serve

        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), redirect_stdout(output):
                serve(root=RUNTIME, state_root=Path(directory))
        identity = json.loads(output.getvalue())["result"]["serverInfo"]
        self.assertEqual(identity, {"name": "qualixar-jev-decision-layer", "version": "1.0.0"})

    def test_legacy_health_does_not_misreport_new_workspace_credentials(self):
        from jevkit import mcp_server

        with tempfile.TemporaryDirectory() as directory:
            result = mcp_server.health(root=RUNTIME, scope="global-hybrid", state_root=Path(directory))
        self.assertEqual(result["version"], "1.0.0")
        self.assertEqual(result["current_workspace_status_tool"], "jev_auto_status")
        self.assertNotIn("credential_available", result)
        self.assertNotIn("policy_mode", result)

    def test_route_has_closed_options_and_an_unknown_path(self):
        from jev_auto.routing import compile_route

        state, questions = compile_route("skill", "Check a draft", [
            {"id": "style", "description": "Check style"},
            {"id": "sources", "description": "Check citations"},
        ])
        self.assertEqual(state["route_kind"], "skill")
        self.assertEqual(set(questions["selected"]["criteria"]), {"style", "sources", "unknown"})

    def test_route_rejects_duplicate_candidates(self):
        from jev_auto.common import AutoError
        from jev_auto.routing import compile_route

        with self.assertRaises(AutoError):
            compile_route("tool", "Choose", [
                {"id": "same", "description": "First"},
                {"id": "same", "description": "Second"},
            ])

    def test_public_recipe_catalog_compiles_a_creator_example(self):
        from jev_auto.recipe_runtime import catalog_preview, prepare_recipe

        catalog = catalog_preview()
        self.assertEqual(len(catalog["recipes"]), 36)
        prepared = prepare_recipe("qualixar.brief-fit", {
            "query": "Explain the idea plainly", "candidate": "This paragraph explains the idea plainly.",
        })
        self.assertEqual(prepared["questions"]["decision"]["type"], "score")
        self.assertNotIn("answer", prepared)

    def test_diff_triage_is_two_questions_not_a_patch_approval(self):
        from jev_auto.review import compile_review

        state, questions = compile_review("Review", "--- a/file.py\n+++ b/file.py\n-old\n+new\n")
        self.assertIn("diff", state)
        self.assertEqual({key: value["type"] for key, value in questions.items()},
                         {"risk": "score", "focus": "choice"})

    def test_rounded_score_is_accepted_but_wrong_score_is_rejected(self):
        from jev_auto.common import AutoError
        from jev_auto.protocol import validate_response

        question = {"decision": {"type": "score", "instructions": "Rate fit",
                                 "criteria": ["No fit", "Some fit", "Direct fit"]}}
        raw = {"model": "jev-1.13.0", "answers": {"decision": {
            "type": "score", "score": 1.69, "confidence": 0.7,
            "probabilities": {"0": 0.01, "1": 0.28, "2": 0.71},
        }}, "usage": {"input_tokens": 400, "output_tokens": 17}}
        self.assertEqual(validate_response(raw, question, "jev-1.13.0")["answers"]["decision"]["score"], 1.69)
        raw["answers"]["decision"]["score"] = 1.4
        with self.assertRaises(AutoError):
            validate_response(raw, question, "jev-1.13.0")

    def test_legacy_and_auto_jev_score_validation_agree_on_provider_rounding(self):
        from jev_auto.protocol import validate_response as auto_validate
        from jevkit.contract import validate_response as legacy_validate

        question = {"decision": {"type": "score", "instructions": "Rate fit",
                                 "criteria": ["No fit", "Some fit", "Direct fit"]}}
        raw = {"model": "jev-1.13.0", "answers": {"decision": {
            "type": "score", "score": 1.69, "confidence": 0.7,
            "probabilities": {"0": 0.01, "1": 0.28, "2": 0.71},
            "legend": {"0": "No fit", "1": "Some fit", "2": "Direct fit"},
        }}, "usage": {"input_tokens": 400, "output_tokens": 17}}
        self.assertEqual(auto_validate(raw, question)["answers"]["decision"]["score"], 1.69)
        self.assertEqual(legacy_validate(raw, question)["answers"]["decision"]["score"], 1.69)

    def test_private_loopback_addresses_are_screened_in_both_provider_paths(self):
        from jev_auto.common import screen as auto_screen
        from jevkit.security import screen as legacy_screen

        for text in ("http://127.0.0.1:54321/setup", "http://0.0.0.0:8000/",
                     "http://[::1]:8080/", "127.0.0.1"):
            with self.subTest(text=text):
                self.assertTrue(auto_screen(text))
                self.assertTrue(legacy_screen(text)[1])

    def test_live_grant_ledger_is_private_at_creation_not_only_after_chmod(self):
        import sqlite3
        import stat
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as directory:
            observed = []
            original_connect = sqlite3.connect

            def observe_connect(path, *args, **kwargs):
                connection = original_connect(path, *args, **kwargs)
                observed.append(stat.S_IMODE(Path(path).stat().st_mode))
                return connection

            previous_umask = os.umask(0o022)
            try:
                with patch("jevkit.authorization.sqlite3.connect", side_effect=observe_connect):
                    connection = CallBudget(Path(directory)).connect()
                    connection.close()
            finally:
                os.umask(previous_umask)
            self.assertEqual(observed, [0o600])

    def test_reviewed_internal_scope_also_accepts_explicit_public_queries(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        questions = {"decision": {"type": "choice", "instructions": "Pick the matching team",
                                  "criteria": {"billing": "Charges", "technical": "Software faults"}}}
        policy = {"provider": "typesafe", "generic_query_enabled": True,
                  "data_classification": "internal-minimized"}
        public = _prepare_query_with_policy("Synthetic public ticket", questions,
                                             provider="typesafe", policy=policy,
                                             data_classification="public")
        internal = _prepare_query_with_policy("Synthetic reviewed internal ticket", questions,
                                               provider="typesafe", policy=policy,
                                               data_classification="internal-minimized")
        self.assertEqual(public.data_classification, "public")
        self.assertEqual(internal.data_classification, "internal-minimized")
        with self.assertRaises(QueryError):
            _prepare_query_with_policy("restricted", questions, provider="typesafe",
                                       policy=policy, data_classification="restricted")

    def test_restricted_route_respects_jev_maximum_vs_selected_hybrid_mode(self):
        from jev_auto.engine import Engine

        policy = {"provider": "typesafe", "routes": {}, "local_laya_enabled": True,
                  "decision_mode": "hybrid", "mlx": {"revision": "a" * 40}}
        seen = []
        engine = object.__new__(Engine)
        result = {"answers": {"selected": {"type": "choice", "choice": "billing",
                                           "confidence": 0.9,
                                           "probabilities": {"billing": 0.9, "technical": 0.1, "unknown": 0.0}}},
                  "provider": "laya-mlx", "model": "laya-mlx@" + "a" * 40,
                  "receipt_id": "a" * 64, "calibration_status": "NOT_EVALUATED", "cache_hit": False}
        def evaluate(state, questions, provider, data_classification):
            seen.append((provider, data_classification))
            return result
        with patch.object(Engine, "policy", return_value=policy), patch.object(Engine, "evaluate_typed", side_effect=evaluate):
            engine.route({"kind": "task", "task": "Route synthetic ticket",
                          "candidates": [{"id": "billing", "description": "Refunds"},
                                         {"id": "technical", "description": "Software"}],
                          "data_classification": "restricted"})
        self.assertEqual(seen, [("laya-mlx", "restricted")])
        seen.clear()
        policy = {"provider": "typesafe", "routes": {}, "local_laya_enabled": False,
                  "decision_mode": "jev-maximum"}
        with patch.object(Engine, "policy", return_value=policy), patch.object(Engine, "evaluate_typed", side_effect=evaluate):
            engine.route({"kind": "task", "task": "Route synthetic private ticket",
                          "candidates": [{"id": "billing", "description": "Refunds"},
                                         {"id": "technical", "description": "Software"}],
                          "data_classification": "restricted"})
        self.assertEqual(seen, [("typesafe", "restricted")])

    def test_restricted_review_uses_selected_jev_or_explicit_local_route(self):
        from jev_auto.engine import Engine

        engine = object.__new__(Engine)
        response = {"answers": {"risk": {"type": "score", "score": 1.0},
                                "focus": {"type": "choice", "choice": "correctness", "confidence": 0.8}},
                    "provider": "typesafe", "model": "jev-1.13.0", "receipt_id": "a" * 64,
                    "calibration_status": "NOT_EVALUATED"}
        request = {"goal": "Review a synthetic change", "diff": "--- a/f\n+++ b/f\n-old\n+new\n",
                   "data_classification": "restricted"}
        seen = []

        def evaluate(_state, _questions, provider, classification):
            seen.append((provider, classification))
            return response

        for policy, expected in (({"provider": "typesafe", "routes": {}, "decision_mode": "jev-maximum",
                                  "local_laya_enabled": False}, "typesafe"),
                                 ({"provider": "typesafe", "routes": {}, "decision_mode": "hybrid",
                                   "local_laya_enabled": True, "mlx": {"revision": "a" * 40}}, "laya-mlx")):
            with patch.object(Engine, "policy", return_value=policy), patch.object(
                    Engine, "evaluate_typed", side_effect=evaluate):
                result = engine.review_diff(request)
            self.assertEqual(result["status"], "ADVISORY_NOT_REVIEW_VERDICT")
            self.assertEqual(seen[-1], (expected, "restricted"))

    def test_jev_maximum_allows_restricted_nonsecret_text_only_when_reviewed(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        questions = {"decision": {"type": "noul", "instructions": "Does this need review?"}}
        maximum = {"provider": "typesafe", "data_classification": "restricted",
                   "decision_mode": "jev-maximum", "generic_query_enabled": True}
        allowed = _prepare_query_with_policy("Synthetic private but non-secret request", questions,
                                              provider="typesafe", policy=maximum,
                                              data_classification="restricted")
        self.assertEqual(allowed.provider, "typesafe")
        with self.assertRaises(QueryError):
            _prepare_query_with_policy("Synthetic private request", questions,
                                       provider="typesafe", policy={**maximum, "decision_mode": "jev-public"},
                                       data_classification="restricted")
        with self.assertRaises(QueryError):
            _prepare_query_with_policy("API key sk-or-v1-synthetic-secret", questions,
                                       provider="typesafe", policy=maximum,
                                       data_classification="restricted")

    def test_jev_only_policy_cannot_hide_a_laya_or_other_hosted_route(self):
        from jev_auto.common import AutoError
        from jev_auto.settings import make_policy

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for route in ("laya-mlx", "openrouter"):
                with self.subTest(route=route), self.assertRaises(AutoError):
                    make_policy(project, "typesafe", decision_mode="jev-maximum",
                                data_classification="restricted", routes={"generic": route})

    def test_existing_wizard_policy_without_new_optional_laya_fields_still_loads(self):
        from jev_auto.settings import make_policy, validate_policy

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            legacy = make_policy(project, "typesafe", data_classification="public",
                                 generic_query_enabled=True, auto_prepare_jev=True)
            legacy.pop("local_laya_enabled")
            legacy.pop("decision_mode")
            self.assertIs(validate_policy(legacy, project), legacy)

    def test_restricted_local_query_requires_explicit_secondary_route_consent(self):
        from src.adl.queries.typed import QueryError, _prepare_query_with_policy

        questions = {"decision": {"type": "choice", "instructions": "Pick the matching team",
                                  "criteria": {"billing": "Charges", "technical": "Software faults"}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "model"
            model_dir.mkdir()
            weights = model_dir / "model.safetensors"
            weights.write_bytes(b"synthetic local model placeholder")
            digest = hashlib.sha256(weights.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                                            "files": {"model.safetensors": digest}}))
            manifest.chmod(0o600)
            model = {"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                     "weight_sha256": digest, "model_dir": str(model_dir),
                     "artifact_manifest": str(manifest)}
            policy = {"provider": "typesafe", "generic_query_enabled": True,
                      "data_classification": "public", "local_laya_enabled": True, "mlx": model}
            local = _prepare_query_with_policy("Synthetic private ticket", questions,
                                               provider="laya-mlx", policy=policy,
                                               data_classification="restricted")
            self.assertEqual(local.provider, "laya-mlx")
            with self.assertRaises(QueryError):
                _prepare_query_with_policy("Synthetic private ticket", questions,
                                           provider="laya-mlx", policy={**policy, "local_laya_enabled": False},
                                           data_classification="restricted")

    def test_engine_generic_local_query_uses_local_provider_override(self):
        from jev_auto.common import digest
        from jev_auto.engine import Engine

        policy = {"provider": "typesafe", "local_laya_enabled": True,
                  "mlx": {"revision": "a" * 40}, "routes": {}}
        expected_model = "laya-mlx@" + "a" * 40
        compiled = SimpleNamespace(provider="laya-mlx", expected_model=expected_model,
                                   policy_sha256=digest(policy), calibration_status="NOT_EVALUATED")
        engine = object.__new__(Engine)
        engine.workspace = Path("/synthetic")
        seen = []
        def judge(recipe, state, questions, current_policy, *, provider_override=None):
            seen.append(provider_override)
            return {"model": expected_model, "answers": {"decision": {"type": "noul", "noul": 0.9}},
                    "receipt_id": "a" * 64, "cache_hit": False}
        with patch("src.adl.queries.typed.prepare_query", return_value=compiled), patch.object(
                Engine, "policy", return_value=policy), patch.object(Engine, "judge", side_effect=judge):
            answer = engine.evaluate_typed("Synthetic private ticket",
                                           {"decision": {"type": "noul", "instructions": "Is it urgent?"}},
                                           "laya-mlx", "restricted")
        self.assertEqual(seen, ["laya-mlx"])
        self.assertEqual(answer["provider"], "laya-mlx")

    def test_local_provider_binds_worker_repository_to_verified_revision(self):
        from jev_auto.common import AutoError
        from jev_auto.providers import Providers

        questions = {"decision": {"type": "noul", "instructions": "Is this synthetic billing?"}}
        policy = {"provider": "laya-mlx", "timeout_seconds": 5,
                  "mlx": {"repository": "aac6fef/laya-mlx", "revision": "a" * 40,
                          "weight_sha256": "b" * 64}}
        provider = Providers()
        provider._ready.set()

        def worker_response(model):
            return {"model": model, "answers": {"decision": {"type": "noul", "noul": 0.8}}}

        provider._mlx = SimpleNamespace(
            predict=lambda *_: worker_response("aac6fef/laya-mlx"), last_telemetry=None)
        with patch.object(provider, "warmup", return_value={"ready": True}):
            result = provider.local(policy, "Synthetic duplicate charge", questions)
            self.assertEqual(result["model"], "laya-mlx@" + "a" * 40)
            provider._mlx = SimpleNamespace(
                predict=lambda *_: worker_response("unexpected-model"), last_telemetry=None)
            with self.assertRaises(AutoError):
                provider.local(policy, "Synthetic duplicate charge", questions)

    def test_mcp_lists_twenty_tools_without_provider_call(self):
        from jev_auto.mcp import definitions
        from jevkit import mcp_server

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                items = definitions(mcp_server)
                names = {tool["name"] for tool in items}
        self.assertEqual(len(names), 20)
        self.assertTrue({"jev_setup", "jev_route", "jev_recipe_catalog", "jev_recipe_try",
                         "jev_review_diff", "jev_recipe_selftest",
                         "jev_verify", "jev_rerank"} <= names)
        # The self-test must be reachable with no workspace: a host checking whether
        # the gate holds should not have to enrol first.
        selftest = next(item for item in items if item["name"] == "jev_recipe_selftest")
        self.assertEqual(selftest["inputSchema"].get("required", []), [])
        self.assertNotIn("workspace_path", selftest["inputSchema"]["properties"])
        review = next(item for item in items if item["name"] == "jev_review_diff")
        self.assertIn("restricted", review["inputSchema"]["properties"]["data_classification"]["enum"])

    def test_setup_tool_opens_private_wizard_without_existing_enrollment(self):
        from jev_auto.mcp import dispatch

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            observed = []
            result = dispatch("jev_setup", {"workspace_path": str(workspace)},
                              SimpleNamespace(tools=lambda scope: []),
                              setup_launcher=lambda path: observed.append(path) or {
                                  "status": "SETUP_WIZARD_OPEN", "url": "http://127.0.0.1:54321/setup"})
        self.assertEqual(observed, [workspace.resolve()])
        self.assertEqual(result["url"], "http://127.0.0.1:54321/setup")

    def test_setup_url_parser_accepts_only_the_packaged_loopback_line(self):
        from jev_auto.mcp import _extract_setup_url

        self.assertEqual(_extract_setup_url(
            b"Qualixar setup: http://127.0.0.1:54321/setup. Enter keys only in the local browser, never in chat.\n"),
            "http://127.0.0.1:54321/setup")
        with self.assertRaises(ValueError):
            _extract_setup_url(b"Qualixar setup: http://example.com/setup. Enter keys only in the local browser.\n")

    def test_setup_tool_returns_a_fixed_error_if_browser_process_cannot_start(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.subprocess.Popen", side_effect=OSError("synthetic private path")):
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)

    def test_setup_tool_accepts_only_a_loopback_url_from_packaged_launcher(self):
        from jev_auto.common import AutoError
        from jev_auto.mcp import _open_setup

        class FakeProcess:
            def __init__(self, line):
                read_fd, write_fd = os.pipe()
                os.write(write_fd, line)
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb")
                self.pid = 123456

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        good = FakeProcess(b"Qualixar setup: http://127.0.0.1:54321/setup. Enter keys only in the local browser, never in chat.\n")
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), patch(
                    "jev_auto.mcp.subprocess.Popen", return_value=good):
                result = _open_setup(directory)
            self.assertEqual(result["url"], "http://127.0.0.1:54321/setup")
            bad = FakeProcess(b"Qualixar setup: http://example.com/setup. Enter keys only in the local browser.\n")
            with patch("jev_auto.mcp.workspace", side_effect=lambda value: Path(value)), patch(
                    "jev_auto.mcp.subprocess.Popen", return_value=bad), patch("jev_auto.mcp.os.killpg") as killed:
                with self.assertRaisesRegex(AutoError, "SETUP_START_FAILED"):
                    _open_setup(directory)
                killed.assert_called_once_with(bad.pid, __import__("signal").SIGKILL)

    def test_mcp_route_forwards_only_closed_request(self):
        from jev_auto.mcp import dispatch

        legacy = SimpleNamespace(tools=lambda scope: [])
        seen = []
        dispatch("jev_route", {"workspace_path": "/synthetic/project", "kind": "task",
                               "task": "Find source", "candidates": [
                                   {"id": "read", "description": "Read file"},
                                   {"id": "search", "description": "Search text"}],
                               "data_classification": "public"},
                 legacy, caller=lambda request: seen.append(request) or {"status": "ADVISORY_UNCALIBRATED"})
        self.assertEqual(seen[0]["op"], "route")
        self.assertNotIn("execution_authorized", seen[0])

    def test_browser_and_context_components_remain_reversible(self):
        from jev_auto.sieve import bounded_blocks

        text = "first\nsecond\nthird\n"
        self.assertEqual("\n".join(block.text for block in bounded_blocks(text)), text)

    def test_agy_hook_is_empty_when_not_enrolled(self):
        from jev_auto.agy_hook import handle

        result = handle({"invocationNum": 0, "workspacePaths": ["/synthetic/project"]},
                        policy_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("not enrolled")))
        self.assertEqual(result, {})

    def test_hermes_hook_adds_only_a_bounded_shortlist(self):
        from jev_auto.hermes_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle(
                {"cwd": directory, "user_message": "Please implement and test the synthetic invoice routing change in this project."},
                policy_loader=lambda _path: {"prepare_context": True, "timeout_seconds": 10},
                starter=lambda _path: None,
                caller=lambda _path, _request, _timeout: {"selected": ["file:src/invoices.py"]},
            )
        self.assertEqual(result, {"context": "Qualixar Jev local shortlist (advisory):\n- file:src/invoices.py"})

    def test_hermes_hook_preserves_a_real_jev_receipt_when_auto_guidance_runs(self):
        from jev_auto.hermes_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            result = handle(
                {"cwd": directory, "user_message": "Please review and test the synthetic routing implementation for this project."},
                policy_loader=lambda _path: {"prepare_context": True, "timeout_seconds": 10},
                starter=lambda _path: None,
                caller=lambda _path, _request, _timeout: {
                    "selected": ["skill:skills/review/SKILL.md"], "receipt_id": "a" * 64,
                    "reason": "jev_candidate_selection"},
            )
        self.assertIn("Receipt: " + "a" * 64, result["context"])
        self.assertIn("skill:skills/review/SKILL.md", result["context"])

    def test_hermes_tool_bridge_rejects_unknown_name_and_keeps_errors_fixed(self):
        from jev_auto.hermes_tool import handle

        called = []
        def fake(name, args):
            called.append(name)
            return {"status": "ADVISORY_UNCALIBRATED", "receipt_id": "a" * 64}
        result = handle({"name": "jev_route", "arguments": {"workspace_path": "/synthetic"}}, dispatcher=fake)
        self.assertEqual(result["status"], "ADVISORY_UNCALIBRATED")
        self.assertEqual(called, ["jev_route"])
        self.assertEqual(handle({"name": "jev_evaluate", "arguments": {}}, dispatcher=fake),
                         {"error": "HERMES_TOOL_NOT_ALLOWED"})
        self.assertEqual(handle({"name": "jev_route", "arguments": "wrong"}, dispatcher=fake),
                         {"error": "HERMES_TOOL_ARGUMENTS"})
        self.assertEqual(handle({"name": "jev_route", "arguments": {"task": "x" * 33000}}, dispatcher=fake),
                         {"error": "HERMES_TOOL_TOO_LARGE"})
        def unavailable(_name, _args):
            raise RuntimeError("synthetic secret")
        self.assertEqual(handle({"name": "jev_route", "arguments": {}}, dispatcher=unavailable),
                         {"error": "HERMES_TOOL_UNAVAILABLE"})
        self.assertEqual(handle({"name": "jev_route", "arguments": {}}, dispatcher=lambda *_: {"x": "a" * 5000}),
                         {"error": "HERMES_TOOL_RESULT_INVALID"})

    def test_agy_hook_adds_a_hint_only_to_an_enrolled_first_invocation(self):
        from jev_auto.agy_hook import handle

        with tempfile.TemporaryDirectory() as directory:
            event = {"invocationNum": 0, "workspacePaths": [directory]}
            enabled = handle(event, policy_loader=lambda _path: {"enabled": True})
            later = handle({**event, "invocationNum": 1}, policy_loader=lambda _path: {"enabled": True})
        self.assertIn("injectSteps", enabled)
        self.assertEqual(later, {})

    def test_new_plugin_never_silently_reads_an_old_workbench_key(self):
        from jevkit.providers import get_provider_credential
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".config" / "jev-codex-workbench" / "api-key"
            old.parent.mkdir(parents=True, mode=0o700)
            old.parent.parent.chmod(0o700)
            old.write_text("synthetic-key-123456")
            old.chmod(0o600)
            with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
                    "XDG_CONFIG_HOME": str(root / "new-config"), "TYPESAFE_API_KEY": ""}):
                with self.assertRaisesRegex(SafeError, "NO_CREDENTIAL"):
                    get_provider_credential("typesafe")

    def test_local_laya_worker_accepts_only_the_attested_legacy_interpreter_path(self):
        from src.adl.providers.laya_worker import _approved_interpreter

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_python = root / ".local" / "state" / "qualixar-jev-auto" / "mlx-env" / "bin" / "python"
            old_python.parent.mkdir(parents=True, mode=0o700)
            old_python.write_text("#!/bin/sh\n")
            old_python.chmod(0o700)
            other = root / "other-python"
            other.write_text("#!/bin/sh\n")
            other.chmod(0o700)
            with patch.object(Path, "home", return_value=root), patch(
                    "src.adl.providers.laya_worker.home_root", return_value=root / "new-state"):
                self.assertTrue(_approved_interpreter(old_python))
                self.assertFalse(_approved_interpreter(other))

    def test_eligible_prompt_uses_one_real_jev_selection_only_with_auto_consent(self):
        from jev_auto.prepare import prepare

        items = [
            {"id": "skill:skills/review/SKILL.md", "kind": "optional_guidance", "description": "Review code"},
            {"id": "file:src/router.py", "kind": "file", "description": "src/router.py"},
        ]
        seen = []

        def judge(recipe, state, questions, policy):
            seen.append((recipe, state, questions))
            return {"answers": {"selected": {"type": "choice", "choice": "c0", "confidence": 0.91,
                                              "probabilities": {"c0": 0.91, "c1": 0.06, "unknown": 0.03}}},
                    "receipt_id": "a" * 64}

        engine = SimpleNamespace(workspace=Path("/synthetic"), judge=judge,
                                 effective_policy=lambda policy, recipe: {"provider": "typesafe"})
        policy = {"prepare_context": True, "auto_prepare_jev": True, "generic_query_enabled": True,
                  "provider": "typesafe"}
        goal = "Please review the router implementation and select the most useful existing guidance for this change."
        with patch("jev_auto.prepare.candidates", return_value=items):
            result = prepare(engine, policy, goal)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], "prepare")
            self.assertEqual(set(seen[0][2]["selected"]["criteria"]), {"c0", "c1", "unknown"})
            self.assertIn("review", seen[0][2]["selected"]["criteria"]["c0"])
            self.assertNotIn("src/router.py", str(seen[0][1:]))
            self.assertEqual(result["selected"], [items[0]["id"]])
            self.assertIn("a" * 64, result["packet"])
            self.assertIn("uncalibrated", result["packet"].lower())
            self.assertEqual(result["calibration_status"], "NOT_EVALUATED")
            seen.clear()
            result = prepare(engine, {**policy, "auto_prepare_jev": False}, goal)
            self.assertEqual(seen, [])
            self.assertEqual(result["reason"], "local_candidate_selection")
            result = prepare(engine, {**policy, "generic_query_enabled": False}, goal)
            self.assertEqual(seen, [])
            self.assertEqual(result["reason"], "local_candidate_selection")


if __name__ == "__main__":
    unittest.main()
