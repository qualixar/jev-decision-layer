"""Coverage-closing tests for jevkit's runtime/authorization/security/contract core.

These four modules are the legacy jevkit surface that still backs every host's
offline tool set and the live call-budget ledger. They were badly under-tested
(see baseline: runtime.py 42%, authorization.py 40%, security.py 56%,
contract.py 66%). This file targets the observable behaviour behind those gaps:
refusal paths in the grant ledger, symlink/permission defenses in the private
file helpers, and the RuntimeContext scope/classification/workspace-binding
rules. It never edits an existing test file or a product/runtime file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


# ---------------------------------------------------------------------------
# Shared test infrastructure (this file only; no product code depends on it).
# ---------------------------------------------------------------------------

def _git(args, cwd):
    """Run git fully isolated from Varun's real global config (no signing,
    no identity prompts) so workspace_binding()/_revision_fingerprint() tests
    are deterministic regardless of the host machine's git setup."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_AUTHOR_NAME"] = "Jev Coverage Test"
    env["GIT_AUTHOR_EMAIL"] = "jev-coverage-test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Jev Coverage Test"
    env["GIT_COMMITTER_EMAIL"] = "jev-coverage-test@example.invalid"
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, env=env, timeout=10)
    if result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr.decode(errors='replace')}")
    return result


def _committed_repo(path: Path) -> Path:
    """A throwaway git repo with exactly one commit, so rev-parse HEAD succeeds."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "initial"], path)
    return path


def _new_commit(path: Path, message: str = "second") -> None:
    _git(["-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", message], path)


# Forces resolve_provider() to ignore whatever real provider env Varun's shell
# happens to export, so tests are deterministic instead of host-dependent.
NO_PROVIDER_ENV = {"JEV_PROVIDER": "", "TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": ""}

# A minimal, self-consistent fake spec/questions pair used to exercise
# RuntimeContext.evaluate()/create_live_grant()'s OWN branch logic without
# depending on the real packaged use_cases/*.json fixtures (out of scope for
# this module and owned elsewhere).
FAKE_SPEC = {"required_fields": [], "questions": {}, "policy": {"kind": "gate"}}
FAKE_QUESTIONS = {"q1": {"type": "noul", "instructions": "Is this synthetic case urgent?"}}


# ---------------------------------------------------------------------------
# jevkit/security.py
# ---------------------------------------------------------------------------

class ScreenNestedStructureTests(unittest.TestCase):
    """screen() is the last filter before anything untrusted is written to a
    receipt or shown to a human. A miss on a container type (dict/list), or a
    falsy-credential value slipping past the truthiness guard, is a silent
    secret leak -- not a cosmetic bug."""

    def test_dict_with_truthy_sensitive_key_is_redacted(self):
        from jevkit.security import screen

        cleaned, found = screen({"api_key": "sk-should-not-appear", "note": "safe"})
        self.assertEqual(cleaned["api_key"], "[REDACTED:CREDENTIAL]")
        self.assertEqual(cleaned["note"], "safe")
        self.assertIn("CREDENTIAL", found)

    def test_dict_with_falsy_sensitive_key_is_recursed_not_redacted(self):
        from jevkit.security import screen

        cleaned, found = screen({"password": None, "secret": ""})
        self.assertEqual(cleaned, {"password": None, "secret": ""})
        self.assertNotIn("CREDENTIAL", found)

    def test_list_values_are_screened_recursively(self):
        from jevkit.security import screen

        cleaned, found = screen(["safe text", "contact me at person@example.com"])
        self.assertEqual(cleaned[0], "safe text")
        self.assertIn("[REDACTED:EMAIL]", cleaned[1])
        self.assertIn("EMAIL", found)

    def test_non_string_leaf_values_pass_through_unchanged(self):
        from jevkit.security import screen

        cleaned, found = screen({"count": 3, "ratio": 0.5, "flag": True, "missing": None})
        self.assertEqual(cleaned, {"count": 3, "ratio": 0.5, "flag": True, "missing": None})
        self.assertEqual(found, [])

    def test_configured_secret_literal_is_redacted_when_long_enough(self):
        from jevkit.security import screen

        cleaned, found = screen("token=ABCDEFGH12345 in the log line", secrets=("ABCDEFGH12345",))
        self.assertIn("[REDACTED:API_KEY]", cleaned)
        self.assertIn("API_KEY", found)

    def test_secret_literal_shorter_than_four_chars_is_ignored(self):
        from jevkit.security import screen

        cleaned, found = screen("the code is abc today", secrets=("abc",))
        self.assertEqual(cleaned, "the code is abc today")
        self.assertNotIn("API_KEY", found)


class CanonicalEncodingTests(unittest.TestCase):
    """canonical() is the hash input for every receipt and grant binding in
    this product. It must be deterministic and it must refuse the one class
    of value JSON is happy to mangle silently: non-finite floats."""

    def test_canonical_output_is_deterministic_and_sorted(self):
        from jevkit.security import canonical

        first = canonical({"b": 1, "a": 2})
        second = canonical({"a": 2, "b": 1})
        self.assertEqual(first, second)
        self.assertEqual(first, b'{"a":2,"b":1}')

    def test_non_finite_float_is_rejected_not_silently_serialized(self):
        from jevkit.security import SafeError, canonical

        with self.assertRaisesRegex(SafeError, "INVALID_JSON"):
            canonical(float("nan"))
        with self.assertRaisesRegex(SafeError, "INVALID_JSON"):
            canonical(float("inf"))


class LoadJsonTests(unittest.TestCase):
    """load_json() is the only way jevkit reads its own fixtures/catalog off
    disk. A symlinked or oversized or non-finite-constant file must be
    refused before json.loads ever sees it."""

    def test_symlink_target_is_refused_even_if_it_points_at_valid_json(self):
        from jevkit.security import SafeError, load_json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.json"
            real.write_text('{"a": 1}')
            link = root / "link.json"
            link.symlink_to(real)
            with self.assertRaisesRegex(SafeError, "UNSAFE_FILE"):
                load_json(link)

    def test_directory_path_is_refused(self):
        from jevkit.security import SafeError, load_json

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(SafeError, "UNSAFE_FILE"):
                load_json(Path(td))

    def test_oversized_file_is_refused_before_parsing(self):
        from jevkit.security import SafeError, load_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.json"
            path.write_text('{"a": "' + ("x" * 100) + '"}')
            with self.assertRaisesRegex(SafeError, "FILE_TOO_LARGE"):
                load_json(path, max_bytes=10)

    def test_malformed_json_is_refused(self):
        from jevkit.security import SafeError, load_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("{not json")
            with self.assertRaisesRegex(SafeError, "INVALID_JSON_FILE"):
                load_json(path)

    def test_nonfinite_constant_tokens_are_refused_even_though_json_loads_would_accept_them(self):
        from jevkit.security import SafeError, load_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nan.json"
            path.write_text('{"value": NaN}')
            with self.assertRaisesRegex(SafeError, "INVALID_JSON_FILE"):
                load_json(path)

    def test_valid_small_file_parses_to_the_matching_object(self):
        from jevkit.security import load_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ok.json"
            path.write_text('{"a": [1, 2, 3]}')
            self.assertEqual(load_json(path), {"a": [1, 2, 3]})


class ValidatePrivatePathTests(unittest.TestCase):
    """validate_private_path() is what stops a symlink anywhere in a private
    state/grant path from redirecting a write outside the intended directory."""

    def test_symlinked_ancestor_directory_is_refused(self):
        from jevkit.security import SafeError, validate_private_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_dir = root / "real"
            real_dir.mkdir()
            link_dir = root / "link"
            link_dir.symlink_to(real_dir)
            with self.assertRaisesRegex(SafeError, "UNSAFE_PATH"):
                validate_private_path(link_dir / "child" / "leaf")

    def test_symlinked_leaf_itself_is_refused(self):
        from jevkit.security import SafeError, validate_private_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_file = root / "real.txt"
            real_file.write_text("x")
            link_file = root / "link.txt"
            link_file.symlink_to(real_file)
            with self.assertRaisesRegex(SafeError, "UNSAFE_PATH"):
                validate_private_path(link_file)

    def test_ordinary_nonexistent_path_is_accepted(self):
        from jevkit.security import validate_private_path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "does" / "not" / "exist"
            validate_private_path(path)  # must not raise


class PrivateJsonTests(unittest.TestCase):
    """private_json() is the only write path for grants/receipts: it must be
    atomic (temp file + rename/link), 0600, and refuse to write through a
    symlinked destination."""

    def test_round_trip_write_is_private_mode_and_readable_back(self):
        from jevkit.security import private_json
        import stat

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "value.json"
            private_json(path, {"z": 1, "a": 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"z": 1, "a": 2})
            leftovers = [p for p in path.parent.iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftovers, [])

    def test_writing_through_a_symlink_destination_is_refused(self):
        from jevkit.security import SafeError, private_json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.json"
            real.write_text("{}")
            link = root / "link.json"
            link.symlink_to(real)
            with self.assertRaisesRegex(SafeError, "UNSAFE_PATH"):
                private_json(link, {"x": 1})

    def test_no_clobber_refuses_an_existing_destination_and_leaves_it_untouched(self):
        from jevkit.security import private_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            private_json(path, {"first": True})
            with self.assertRaises(FileExistsError):
                private_json(path, {"second": True}, no_clobber=True)
            self.assertEqual(json.loads(path.read_text()), {"first": True})

    def test_no_clobber_succeeds_via_atomic_link_when_destination_is_new(self):
        from jevkit.security import private_json
        import stat

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            private_json(path, {"only": "once"}, no_clobber=True)
            self.assertEqual(json.loads(path.read_text()), {"only": "once"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_overwrite_replaces_previous_content_atomically(self):
        from jevkit.security import private_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            private_json(path, {"version": 1})
            private_json(path, {"version": 2})
            self.assertEqual(json.loads(path.read_text()), {"version": 2})

    def test_a_failure_right_after_mkstemp_still_closes_the_descriptor_and_removes_the_temp_file(self):
        """If chmod/write/link/replace ever raises after mkstemp() has
        already created the temp file, the finally block must still close
        the fd and remove the temp file -- otherwise every failed write
        leaks a descriptor and litters the private directory."""
        from jevkit.security import private_json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            original_chmod = os.chmod

            def failing_chmod(target, mode, *a, **kw):
                if ".tmp-" in str(target):
                    raise OSError("simulated disk failure")
                return original_chmod(target, mode, *a, **kw)

            with patch("jevkit.security.os.chmod", side_effect=failing_chmod):
                with self.assertRaises(OSError):
                    private_json(path, {"x": 1})
            leftovers = [p for p in path.parent.iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# jevkit/contract.py
# ---------------------------------------------------------------------------

class ValidateQuestionsTests(unittest.TestCase):
    """validate_questions() is the schema gate on every rubric this product
    ever sends to a provider. Before this file, NOTHING called it -- every
    branch (type, instructions, choice/score criteria bounds, byte budget)
    was unverified."""

    @staticmethod
    def _well_formed():
        return {
            "risk": {"type": "score", "instructions": "Rate risk", "criteria": ["Low", "Medium", "High"]},
            "route": {"type": "choice", "instructions": "Pick a route", "criteria": {"a": "Route A", "b": "Route B"}},
            "urgent": {"type": "noul", "instructions": "Is this urgent?"},
        }

    def test_well_formed_questions_of_every_type_are_accepted(self):
        from jevkit.contract import validate_questions

        validate_questions(self._well_formed())  # must not raise

    def test_non_dict_or_out_of_range_count_is_rejected(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.subTest("not_a_dict"):
            with self.assertRaisesRegex(SafeError, "INVALID_QUESTIONS"):
                validate_questions([])
        with self.subTest("empty"):
            with self.assertRaisesRegex(SafeError, "INVALID_QUESTIONS"):
                validate_questions({})
        with self.subTest("too_many"):
            too_many = {f"q{i}": {"type": "noul", "instructions": "x"} for i in range(61)}
            with self.assertRaisesRegex(SafeError, "INVALID_QUESTIONS"):
                validate_questions(too_many)

    def test_non_string_key_or_non_dict_question_is_rejected(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "INVALID_QUESTION$"):
            validate_questions({"ok": "not-a-dict"})

    def test_unknown_type_is_rejected(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "INVALID_QUESTION_TYPE"):
            validate_questions({"q": {"type": "essay", "instructions": "x"}})

    def test_missing_or_blank_instructions_is_rejected(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.subTest("missing"):
            with self.assertRaisesRegex(SafeError, "MISSING_INSTRUCTIONS"):
                validate_questions({"q": {"type": "noul"}})
        with self.subTest("blank"):
            with self.assertRaisesRegex(SafeError, "MISSING_INSTRUCTIONS"):
                validate_questions({"q": {"type": "noul", "instructions": "   "}})

    def test_choice_criteria_must_be_a_dict_of_two_to_255(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.subTest("not_dict"):
            with self.assertRaisesRegex(SafeError, "INVALID_CHOICE_CRITERIA"):
                validate_questions({"q": {"type": "choice", "instructions": "x", "criteria": ["a", "b"]}})
        with self.subTest("too_few"):
            with self.assertRaisesRegex(SafeError, "INVALID_CHOICE_CRITERIA"):
                validate_questions({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}})

    def test_score_criteria_must_be_a_list_of_two_to_ten(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        with self.subTest("not_list"):
            with self.assertRaisesRegex(SafeError, "INVALID_SCORE_CRITERIA"):
                validate_questions({"q": {"type": "score", "instructions": "x", "criteria": {"a": "A"}}})
        with self.subTest("too_many"):
            with self.assertRaisesRegex(SafeError, "INVALID_SCORE_CRITERIA"):
                validate_questions({"q": {"type": "score", "instructions": "x",
                                          "criteria": [str(i) for i in range(11)]}})

    def test_oversized_question_payload_exceeds_the_byte_budget(self):
        from jevkit.contract import validate_questions
        from jevkit.security import SafeError

        big_criteria = {f"option-{i:03d}": "x" * 80 for i in range(200)}
        with self.assertRaisesRegex(SafeError, "QUESTION_BUDGET_EXCEEDED"):
            validate_questions({"q": {"type": "choice", "instructions": "Pick one", "criteria": big_criteria}})


class ValidateResponseModelTests(unittest.TestCase):
    """validate_response_model() rejects a provider alias/suffix/fallback
    before any answer is parsed -- never exercised before this file."""

    def test_non_dict_raw_is_rejected(self):
        from jevkit.contract import validate_response_model
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "MODEL_ID_MISMATCH"):
            validate_response_model("not-a-dict", "jev-1.13.0")

    def test_mismatched_model_id_is_rejected(self):
        from jevkit.contract import validate_response_model
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "MODEL_ID_MISMATCH"):
            validate_response_model({"model": "some-other-model"}, "jev-1.13.0")

    def test_matching_model_id_is_returned_unchanged(self):
        from jevkit.contract import validate_response_model

        raw = {"model": "jev-1.13.0", "answers": {}}
        self.assertIs(validate_response_model(raw, "jev-1.13.0"), raw)


class ValidateResponseNoulAndChoiceTests(unittest.TestCase):
    """The only jevkit.contract.validate_response() branch previously
    exercised was 'score'. noul and choice -- including the "claimed choice
    is not actually the max-probability one" forgery check -- were dead
    code from a test-coverage standpoint."""

    def test_valid_noul_answer_is_normalized_without_requiring_confidence(self):
        from jevkit.contract import validate_response

        questions = {"urgent": {"type": "noul", "instructions": "Is this urgent?"}}
        raw = {"model": "m", "answers": {"urgent": {"type": "noul", "noul": 0.8}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        result = validate_response(raw, questions)
        self.assertEqual(result["answers"], {"urgent": {"type": "noul", "noul": 0.8}})

    def test_out_of_range_noul_value_is_rejected(self):
        from jevkit.contract import validate_response
        from jevkit.security import SafeError

        questions = {"urgent": {"type": "noul", "instructions": "Is this urgent?"}}
        raw = {"model": "m", "answers": {"urgent": {"type": "noul", "noul": 1.5}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        with self.assertRaisesRegex(SafeError, "API_SCHEMA_MISMATCH: noul"):
            validate_response(raw, questions)

    def test_valid_choice_answer_is_normalized(self):
        from jevkit.contract import validate_response

        questions = {"route": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "b": "B"}}}
        raw = {"model": "m", "answers": {"route": {
                   "type": "choice", "choice": "a", "confidence": 0.9,
                   "probabilities": {"a": 0.9, "b": 0.1}}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        result = validate_response(raw, questions)
        self.assertEqual(result["answers"]["route"]["choice"], "a")
        self.assertNotIn("legend", result["answers"]["route"])

    def test_choice_not_present_in_probabilities_is_rejected(self):
        from jevkit.contract import validate_response
        from jevkit.security import SafeError

        questions = {"route": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "b": "B"}}}
        raw = {"model": "m", "answers": {"route": {
                   "type": "choice", "choice": "c", "confidence": 0.9,
                   "probabilities": {"a": 0.9, "b": 0.1}}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        with self.assertRaisesRegex(SafeError, "API_SCHEMA_MISMATCH: choice"):
            validate_response(raw, questions)

    def test_choice_not_matching_the_highest_probability_is_rejected(self):
        """A provider (or a forged response) cannot claim choice='b' while
        reporting choice='a' as the more likely outcome."""
        from jevkit.contract import validate_response
        from jevkit.security import SafeError

        questions = {"route": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "b": "B"}}}
        raw = {"model": "m", "answers": {"route": {
                   "type": "choice", "choice": "b", "confidence": 0.9,
                   "probabilities": {"a": 0.9, "b": 0.1}}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        with self.assertRaisesRegex(SafeError, "API_SCHEMA_MISMATCH: choice"):
            validate_response(raw, questions)

    def test_probabilities_not_summing_to_one_are_rejected(self):
        from jevkit.contract import validate_response
        from jevkit.security import SafeError

        questions = {"route": {"type": "choice", "instructions": "Pick", "criteria": {"a": "A", "b": "B"}}}
        raw = {"model": "m", "answers": {"route": {
                   "type": "choice", "choice": "a", "confidence": 0.9,
                   "probabilities": {"a": 0.9, "b": 0.5}}},
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        with self.assertRaisesRegex(SafeError, "API_SCHEMA_MISMATCH: probability normalization"):
            validate_response(raw, questions)


# ---------------------------------------------------------------------------
# jevkit/authorization.py
# ---------------------------------------------------------------------------

class AuthorizationLedgerValidationTests(unittest.TestCase):
    """_validate_ledger (reached only through the public connect()) is the
    last line of defense against a tampered or world-readable grant ledger.
    Before this file, connect() itself was covered but every refusal branch
    -- not-a-regular-file, extra hard link, group/other readable, wrong uid,
    a broken symlink, and legacy-schema migration -- was not."""

    def test_ledger_that_is_a_directory_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            (storage / "live-grants.sqlite3").mkdir()
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                budget.connect()

    def test_ledger_with_extra_hard_link_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.write_bytes(b"")
            ledger.chmod(0o600)
            os.link(ledger, storage / "extra-link")
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                budget.connect()

    def test_group_or_other_readable_ledger_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.write_bytes(b"")
            ledger.chmod(0o644)
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                budget.connect()

    def test_ledger_not_owned_by_current_uid_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.write_bytes(b"")
            ledger.chmod(0o600)
            budget = CallBudget(Path(td), storage_root=storage)
            with patch("jevkit.authorization.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                    budget.connect()

    def test_broken_symlink_ledger_is_refused_even_though_exists_returns_false(self):
        """exists() follows symlinks, so a dangling symlink makes the
        exists()-then-lstat() check in _validate_ledger short-circuit with
        nothing to inspect. connect()'s separate is_symlink() guard is the
        only thing left that still catches it -- this pins that it does."""
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.symlink_to(storage / "does-not-exist")
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                budget.connect()

    def test_valid_symlink_ledger_is_also_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            real = storage / "real.sqlite3"
            real.write_bytes(b"")
            real.chmod(0o600)
            ledger = storage / "live-grants.sqlite3"
            ledger.symlink_to(real)
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                budget.connect()

    def test_lstat_oserror_is_translated_to_a_safe_error(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.write_bytes(b"")
            ledger.chmod(0o600)
            budget = CallBudget(Path(td), storage_root=storage)
            original_lstat = Path.lstat

            def failing_lstat(self, *a, **kw):
                if self == ledger:
                    raise OSError("simulated race")
                return original_lstat(self, *a, **kw)

            with patch.object(Path, "lstat", failing_lstat):
                with self.assertRaisesRegex(SafeError, "UNSAFE_BUDGET_PATH"):
                    budget.connect()

    def test_legacy_schema_is_dropped_and_recreated_with_bound_columns(self):
        """Legacy grants were not workspace/request bound; connect() must
        invalidate them rather than preserve ambient authority under the
        new column layout."""
        from jevkit.authorization import CallBudget
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            legacy = sqlite3.connect(str(ledger))
            legacy.execute("CREATE TABLE grants (id TEXT PRIMARY KEY, calls INTEGER)")
            legacy.execute("INSERT INTO grants VALUES ('old-grant', 5)")
            legacy.commit()
            legacy.close()
            ledger.chmod(0o600)
            budget = CallBudget(Path(td), storage_root=storage)
            con = budget.connect()
            try:
                columns = tuple(row[1] for row in con.execute("PRAGMA table_info(grants)").fetchall())
                self.assertEqual(columns, CallBudget._COLUMNS)
                self.assertIsNone(con.execute("SELECT * FROM grants WHERE id='old-grant'").fetchone())
            finally:
                con.close()

    def test_corrupt_ledger_bytes_raise_and_the_connection_is_closed(self):
        from jevkit.authorization import CallBudget
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            storage.mkdir()
            ledger = storage / "live-grants.sqlite3"
            ledger.write_bytes(b"not a sqlite database at all, just garbage bytes")
            ledger.chmod(0o600)
            budget = CallBudget(Path(td), storage_root=storage)
            with self.assertRaises(sqlite3.DatabaseError):
                budget.connect()


class AuthorizationGrantTests(unittest.TestCase):
    """grant() is the only way a call budget comes into existence. Before
    this file it had zero coverage: the calls/minutes bounds and the
    allow_custom binding requirement were unverified."""

    def test_calls_and_minutes_are_bounds_checked(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            for calls, minutes in ((0, 10), (101, 10), (5, 0), (5, 121)):
                with self.subTest(calls=calls, minutes=minutes):
                    with self.assertRaisesRegex(SafeError, "INVALID_GRANT_LIMIT"):
                        budget.grant(calls, minutes)

    def test_allow_custom_requires_every_binding_field(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants",
                                 workspace_id="ws-1", revision="rev-1")
            with self.assertRaisesRegex(SafeError, "REQUEST_GRANT_REQUIRED"):
                budget.grant(5, 5, True)

    def test_allow_custom_succeeds_with_every_field_present(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants",
                                 workspace_id="ws-1", revision="rev-1",
                                 provider_id="typesafe", provider_profile_sha256="sha-1")
            status = budget.grant(5, 5, True, case_id="case-1", data_classification="public",
                                   request_id="req-1", request_sha256="hash-1")
            self.assertTrue(status["custom_data_allowed"])

    def test_a_second_grant_replaces_the_first_rather_than_stacking(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(5, 5)
            second = budget.grant(2, 5)
            self.assertEqual(second["remaining_calls"], 2)


class AuthorizationStatusTests(unittest.TestCase):
    """status() is what every host uses to show remaining budget. Before
    this file: no test ever called it, so the no-grant default, every
    binding mismatch, and the expiry comparison were unverified."""

    def test_no_grant_reports_disabled_defaults(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            self.assertEqual(budget.status(),
                              {"enabled": False, "remaining_calls": 0, "custom_data_allowed": False})

    def test_matching_binding_reports_enabled_with_remaining_calls(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants",
                                 workspace_id="ws-1", revision="rev-1",
                                 provider_id="typesafe", provider_profile_sha256="sha-1")
            budget.grant(3, 5)
            status = budget.status()
            self.assertTrue(status["enabled"])
            self.assertEqual(status["remaining_calls"], 3)

    def test_any_binding_mismatch_reports_disabled(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            owner = CallBudget(Path(td), storage_root=storage, workspace_id="ws-1", revision="rev-1",
                                provider_id="typesafe", provider_profile_sha256="sha-1")
            owner.grant(3, 5)
            base = dict(workspace_id="ws-1", revision="rev-1", provider_id="typesafe",
                        provider_profile_sha256="sha-1")
            mismatches = [
                ("workspace_id", "ws-OTHER"), ("revision", "rev-OTHER"),
                ("provider_id", "openrouter"), ("provider_profile_sha256", "sha-OTHER"),
            ]
            for field, bad_value in mismatches:
                with self.subTest(mismatch=field):
                    args = {**base, field: bad_value}
                    stranger = CallBudget(Path(td), storage_root=storage, **args)
                    self.assertFalse(stranger.status()["enabled"])

    def test_expired_grant_reports_disabled(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            with patch("jevkit.authorization.time.time", return_value=1_000_000.0):
                budget.grant(5, 1)
            with patch("jevkit.authorization.time.time", return_value=1_000_100.0):
                self.assertFalse(budget.status()["enabled"])

    def test_exhausted_grant_reports_disabled_with_zero_remaining(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(1, 5)
            budget.reserve(False)
            status = budget.status()
            self.assertFalse(status["enabled"])
            self.assertEqual(status["remaining_calls"], 0)


class AuthorizationReserveTests(unittest.TestCase):
    """reserve() is the actual gate a live HTTP attempt must pass. Before
    this file the ENTIRE method (lines 134-170 of authorization.py) had zero
    coverage: no grant, expiry, every binding mismatch, custom-data forgery,
    exhaustion, and the rollback/close recovery path were all unverified.
    A fail-open here is the highest-severity defect this brief calls out."""

    def test_no_grant_at_all_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            with self.assertRaisesRegex(SafeError, "LIVE_NOT_AUTHORIZED"):
                budget.reserve(False)

    def test_no_grant_with_custom_requested_reports_the_custom_specific_error(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            with self.assertRaisesRegex(SafeError, "REQUEST_GRANT_REQUIRED"):
                budget.reserve(True)

    def test_expired_grant_is_refused_on_reserve(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            with patch("jevkit.authorization.time.time", return_value=1000.0):
                budget.grant(5, 1)
            with patch("jevkit.authorization.time.time", return_value=5000.0):
                with self.assertRaisesRegex(SafeError, "LIVE_NOT_AUTHORIZED: use the interactive local grant helper"):
                    budget.reserve(False)

    def test_every_binding_mismatch_is_refused_with_a_distinct_message(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "grants"
            owner = CallBudget(Path(td), storage_root=storage, workspace_id="ws-1", revision="rev-1",
                                provider_id="typesafe", provider_profile_sha256="sha-1")
            owner.grant(5, 5)
            base = dict(workspace_id="ws-1", revision="rev-1", provider_id="typesafe",
                        provider_profile_sha256="sha-1")
            cases = [
                ("workspace_id", "ws-OTHER", "grant belongs to another workspace"),
                ("revision", "rev-OTHER", "grant revision does not match"),
                ("provider_id", "openrouter", "grant belongs to another provider"),
                ("provider_profile_sha256", "sha-OTHER", "provider profile changed"),
            ]
            for field, bad_value, message in cases:
                with self.subTest(mismatch=field):
                    args = {**base, field: bad_value}
                    stranger = CallBudget(Path(td), storage_root=storage, **args)
                    with self.assertRaisesRegex(SafeError, message):
                        stranger.reserve(False)

    def test_custom_reservation_against_a_non_custom_grant_is_refused(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(5, 5, allow_custom=False)
            with self.assertRaisesRegex(SafeError, "CUSTOM_DATA_NOT_AUTHORIZED"):
                budget.reserve(True)

    def test_custom_reservation_with_a_mismatched_tuple_is_refused_then_matching_tuple_succeeds(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants",
                                 workspace_id="ws-1", revision="rev-1")
            budget.grant(5, 5, True, case_id="case-1", data_classification="public",
                         request_id="req-1", request_sha256="hash-1")
            from jevkit.security import SafeError
            with self.assertRaisesRegex(SafeError, "REQUEST_GRANT_REQUIRED"):
                budget.reserve(True, case_id="case-WRONG", data_classification="public",
                                request_id="req-1", request_sha256="hash-1")
            grant_id = budget.reserve(True, case_id="case-1", data_classification="public",
                                       request_id="req-1", request_sha256="hash-1")
            self.assertTrue(grant_id)

    def test_exhaustion_is_refused_on_the_call_after_the_limit(self):
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(1, 5)
            budget.reserve(False)
            with self.assertRaisesRegex(SafeError, "CALL_BUDGET_EXHAUSTED"):
                budget.reserve(False)

    def test_a_failed_reservation_rolls_back_and_the_ledger_stays_usable(self):
        """BEGIN IMMEDIATE takes a write lock before the refusal is raised.
        If the failure path did not roll back and close the connection, the
        NEXT reserve() on the same ledger would hang or raise 'database is
        locked' instead of succeeding."""
        from jevkit.authorization import CallBudget
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(3, 5)
            with self.assertRaises(SafeError):
                budget.reserve(True)  # not allow_custom -> CUSTOM_DATA_NOT_AUTHORIZED
            budget.reserve(False)
            self.assertEqual(budget.status()["remaining_calls"], 2)


class AuthorizationRevokeTests(unittest.TestCase):
    """revoke() unconditionally clears the ledger regardless of binding --
    unverified before this file."""

    def test_revoke_clears_an_existing_grant(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.grant(5, 5)
            self.assertTrue(budget.status()["enabled"])
            budget.revoke()
            self.assertFalse(budget.status()["enabled"])

    def test_revoke_on_an_empty_ledger_is_a_harmless_no_op(self):
        from jevkit.authorization import CallBudget

        with tempfile.TemporaryDirectory() as td:
            budget = CallBudget(Path(td), storage_root=Path(td) / "grants")
            budget.revoke()  # must not raise even with nothing to delete
            self.assertFalse(budget.status()["enabled"])


# ---------------------------------------------------------------------------
# jevkit/runtime.py
# ---------------------------------------------------------------------------

class RuntimeHelperFunctionTests(unittest.TestCase):
    """_default_state_root/_inside/_workspace_id/_revision_fingerprint are the
    primitives every RuntimeContext security check is built from. Before this
    file, _inside() had never returned True, _workspace_id() and
    _revision_fingerprint() had zero coverage, and the XDG-unset default
    state root was never exercised."""

    def test_default_state_root_falls_back_to_home_when_xdg_unset(self):
        from jevkit.runtime import _default_state_root

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with patch.dict(os.environ, {"XDG_STATE_HOME": ""}), patch.object(Path, "home", return_value=fake_home):
                result = _default_state_root()
        self.assertEqual(result, fake_home / ".local" / "state" / "qualixar-jev-decision-layer")

    def test_inside_is_true_for_a_real_subpath_and_false_for_an_unrelated_path(self):
        from jevkit.runtime import _inside

        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            child = parent / "a" / "b"
            sibling = parent.parent / "definitely-unrelated-xyz"
            self.assertTrue(_inside(child, parent))
            self.assertFalse(_inside(sibling, parent))

    def test_workspace_id_is_stable_for_the_same_path_and_falls_back_when_missing(self):
        from jevkit.runtime import _workspace_id

        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            first = _workspace_id(path)
            second = _workspace_id(path)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 24)
        missing = _workspace_id(Path("/this/path/certainly/does/not/exist/xyz"))
        self.assertEqual(len(missing), 24)
        self.assertNotEqual(missing, first)

    def test_revision_fingerprint_requires_a_real_commit(self):
        from jevkit.runtime import _revision_fingerprint, _workspace_id
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            empty_repo = Path(td) / "empty"
            empty_repo.mkdir()
            _git(["init", "-q"], empty_repo)
            with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_REQUIRED"):
                _revision_fingerprint(empty_repo, _workspace_id(empty_repo))

    def test_revision_fingerprint_succeeds_for_a_committed_repo_and_changes_on_new_commit(self):
        from jevkit.runtime import _revision_fingerprint, _workspace_id

        with tempfile.TemporaryDirectory() as td:
            repo = _committed_repo(Path(td) / "repo")
            wsid = _workspace_id(repo)
            first = _revision_fingerprint(repo, wsid)
            self.assertEqual(len(first), 64)
            _new_commit(repo)
            second = _revision_fingerprint(repo, wsid)
        self.assertNotEqual(first, second)

    def test_revision_fingerprint_translates_a_missing_git_binary(self):
        from jevkit.runtime import _revision_fingerprint
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            with patch("jevkit.runtime.subprocess.run", side_effect=FileNotFoundError("no git binary")):
                with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_REQUIRED"):
                    _revision_fingerprint(Path(td), "ws-id")


class WorkspaceBindingTests(unittest.TestCase):
    """workspace_binding() turns a filesystem path into an authority-bearing
    (workspace_id, revision) pair. Before this file it had zero coverage:
    the symlink/non-directory/non-git/undecodable-output/outside-root
    refusals were all unverified."""

    def test_symlinked_workspace_argument_is_refused(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            real.mkdir()
            link = Path(td) / "link"
            link.symlink_to(real)
            with self.assertRaisesRegex(SafeError, "WORKSPACE_DIRECTORY_REQUIRED"):
                workspace_binding(link)

    def test_non_directory_workspace_argument_is_refused(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            file_path = Path(td) / "afile"
            file_path.write_text("x")
            with self.assertRaisesRegex(SafeError, "WORKSPACE_DIRECTORY_REQUIRED"):
                workspace_binding(file_path)

    def test_directory_that_is_not_a_git_repo_is_refused(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain"
            plain.mkdir()
            with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_REQUIRED"):
                workspace_binding(plain)

    def test_missing_git_binary_is_translated_to_a_safe_error(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain"
            plain.mkdir()
            with patch("jevkit.runtime.subprocess.run", side_effect=FileNotFoundError("no git")):
                with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_REQUIRED"):
                    workspace_binding(plain)

    def test_undecodable_toplevel_output_is_refused(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            repo = _committed_repo(Path(td) / "repo")
            bad_result = SimpleNamespace(returncode=0, stdout=b"\xff\xfe-not-utf8")
            with patch("jevkit.runtime.subprocess.run", return_value=bad_result):
                with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_REQUIRED"):
                    workspace_binding(repo)

    def test_toplevel_outside_the_supplied_directory_is_refused(self):
        from jevkit.runtime import workspace_binding
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            repo = _committed_repo(Path(td) / "repo")
            elsewhere = Path(td) / "elsewhere"
            elsewhere.mkdir()
            bad_result = SimpleNamespace(returncode=0, stdout=(str(elsewhere) + "\n").encode())
            with patch("jevkit.runtime.subprocess.run", return_value=bad_result):
                with self.assertRaisesRegex(SafeError, "WORKSPACE_DIRECTORY_REQUIRED"):
                    workspace_binding(repo)

    def test_committed_repo_binds_to_a_stable_workspace_id_and_revision(self):
        from jevkit.runtime import workspace_binding

        with tempfile.TemporaryDirectory() as td:
            repo = _committed_repo(Path(td) / "repo")
            bound = workspace_binding(repo)
        self.assertEqual(bound["workspace_path"], str(repo.resolve()))
        self.assertEqual(len(bound["workspace_id"]), 24)
        self.assertEqual(len(bound["revision"]), 64)


class RuntimeContextConstructionTests(unittest.TestCase):
    """__post_init__ enforces every RuntimeContext invariant. Before this
    file, INVALID_RUNTIME_SCOPE, LIVE_RUNTIME_NOT_AVAILABLE,
    STATE_ROOT_INSIDE_PACKAGE, and a real PROJECT_LIVE construction were all
    unverified."""

    def test_unknown_scope_is_rejected(self):
        from jevkit.runtime import RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(SafeError, "INVALID_RUNTIME_SCOPE"):
                RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope="not-a-real-scope")

    def test_live_scopes_are_rejected_when_the_build_is_offline_only(self):
        from jevkit.runtime import GLOBAL_HYBRID, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            with patch("jevkit.runtime.OFFLINE_ONLY", True):
                with self.assertRaisesRegex(SafeError, "LIVE_RUNTIME_NOT_AVAILABLE"):
                    RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_HYBRID)

    def test_state_root_may_not_live_inside_the_package(self):
        from jevkit.runtime import RuntimeContext
        from jevkit.security import SafeError

        with self.assertRaisesRegex(SafeError, "STATE_ROOT_INSIDE_PACKAGE"):
            RuntimeContext(RUNTIME, state_root=RUNTIME / "definitely-not-real-state")

    def test_project_live_without_a_workspace_root_is_rejected(self):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(SafeError, "WORKSPACE_ID_REQUIRED"):
                RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=PROJECT_LIVE)

    def test_global_offline_defaults_are_used_when_nothing_supplied(self):
        from jevkit.runtime import GLOBAL_OFFLINE, OFFLINE_TOOLS, RuntimeContext

        with tempfile.TemporaryDirectory() as fake_home:
            with patch.dict(os.environ, {"XDG_STATE_HOME": ""}), patch.object(Path, "home",
                                                                                return_value=Path(fake_home)):
                context = RuntimeContext(RUNTIME)
        self.assertEqual(context.scope, GLOBAL_OFFLINE)
        self.assertIsNone(context.workspace_id)
        self.assertEqual(context.tool_names(), OFFLINE_TOOLS)

    def test_global_hybrid_scope_constructs_without_a_workspace(self):
        from jevkit.runtime import GLOBAL_HYBRID, LIVE_TOOLS, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_HYBRID)
        self.assertEqual(context.scope, GLOBAL_HYBRID)
        self.assertEqual(context.tool_names(), LIVE_TOOLS)

    def test_project_live_binds_to_a_real_committed_workspace(self):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = _committed_repo(root / "workspace")
            context = RuntimeContext(RUNTIME, state_root=root / "state", scope=PROJECT_LIVE,
                                      workspace_root=workspace)
            self.assertEqual(context.workspace_root, workspace.resolve())
            self.assertEqual(len(context.workspace_id), 24)
            self.assertEqual(len(context.revision), 64)
            self.assertTrue(context.grant_root.is_dir())
            self.assertTrue(context.evidence_root.is_dir())
            self.assertIn(context.workspace_id, str(context.state_root))


class RuntimeContextHealthTests(unittest.TestCase):
    """health() is the single status call every host uses. Before this file,
    the GLOBAL_HYBRID provider-resolved and provider-unresolved (SafeError)
    branches, and the PROJECT_LIVE workspace+grant branch, were unverified."""

    def test_health_for_global_offline_never_checks_credentials(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            result = context.health()
        self.assertFalse(result["credential_checked"])
        self.assertFalse(result["live_grant_checked"])

    def test_health_for_global_hybrid_reports_unconfigured_provider_on_selection_conflict(self):
        from jevkit.runtime import GLOBAL_HYBRID, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "config"
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(config_dir),
                                          "TYPESAFE_API_KEY": "synthetic-typesafe-0001",
                                          "OPENROUTER_API_KEY": "synthetic-openrouter-0001"}):
                context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_HYBRID)
                result = context.health()
        self.assertFalse(result["provider_configured"])
        self.assertIsNone(result["provider_id"])
        self.assertFalse(result["credential_available"])
        self.assertFalse(result["live_grant_checked"])

    def test_health_for_global_hybrid_reports_a_resolved_provider(self):
        from jevkit.runtime import GLOBAL_HYBRID, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "config"
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(config_dir)}):
                context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_HYBRID)
                result = context.health()
        self.assertTrue(result["provider_configured"])
        self.assertEqual(result["provider_id"], "typesafe")
        self.assertFalse(result["credential_available"])

    def test_health_for_project_live_includes_workspace_identity_and_grant_status(self):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = _committed_repo(root / "workspace")
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(root / "config")}):
                context = RuntimeContext(RUNTIME, state_root=root / "state", scope=PROJECT_LIVE,
                                          workspace_root=workspace)
                result = context.health()
        self.assertEqual(result["workspace_id"], context.workspace_id)
        self.assertEqual(result["revision"], context.revision)
        self.assertFalse(result["live_grant"]["enabled"])


class RuntimeContextDelegationTests(unittest.TestCase):
    """describe/catalog/run_fixture are thin wrappers, but they are the ONLY
    place package_root/evidence_root/persist actually get forwarded to the
    engine -- worth pinning independently of engine.py's own tests."""

    def test_describe_forwards_case_id_and_package_root(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            with patch("jevkit.runtime.spec", return_value={"fake": "spec"}) as fake_spec:
                result = context.describe("01-skill-routing")
        fake_spec.assert_called_once_with("01-skill-routing", context.package_root)
        self.assertEqual(result, {"fake": "spec"})

    def test_catalog_forwards_package_root(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            with patch("jevkit.runtime.catalog", return_value=[{"id": "x"}]) as fake_catalog:
                result = context.catalog()
        fake_catalog.assert_called_once_with(context.package_root)
        self.assertEqual(result, [{"id": "x"}])

    def test_run_fixture_forwards_variant_and_evidence_root_and_never_persists_offline(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            with patch("jevkit.runtime.run", return_value={"ok": True}) as fake_run:
                result = context.run_fixture("01-skill-routing", "adversarial")
        kwargs = fake_run.call_args.kwargs
        self.assertEqual(fake_run.call_args[0][0], "01-skill-routing")
        self.assertEqual(kwargs["variant"], "adversarial")
        self.assertEqual(kwargs["root"], context.package_root)
        self.assertEqual(kwargs["state_root"], context.evidence_root)
        self.assertFalse(kwargs["persist"])
        self.assertEqual(result, {"ok": True})

    def test_run_fixture_persists_only_for_project_live(self):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = _committed_repo(root / "workspace")
            context = RuntimeContext(RUNTIME, state_root=root / "state", scope=PROJECT_LIVE,
                                      workspace_root=workspace)
            with patch("jevkit.runtime.run", return_value={"ok": True}) as fake_run:
                context.run_fixture("01-skill-routing")
        self.assertTrue(fake_run.call_args.kwargs["persist"])


class RuntimeContextEvaluateTests(unittest.TestCase):
    """evaluate() is the highest-stakes method in this module: it decides
    what classification/request-id/hash gets bound to a live provider call.
    Before this file, the GLOBAL_HYBRID delegation, every validation
    refusal, and the state=None synthetic path were all unverified."""

    @staticmethod
    def _live_context(tmp_path):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext

        workspace = _committed_repo(tmp_path / "workspace")
        return RuntimeContext(RUNTIME, state_root=tmp_path / "state", scope=PROJECT_LIVE,
                               workspace_root=workspace)

    def test_hybrid_evaluate_without_a_workspace_root_is_rejected(self):
        from jevkit.runtime import GLOBAL_HYBRID, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_HYBRID)
            with self.assertRaisesRegex(SafeError, "WORKSPACE_ID_REQUIRED"):
                context.evaluate("case", {"x": 1})

    def test_hybrid_evaluate_delegates_to_a_bound_project_live_context(self):
        from jevkit.runtime import GLOBAL_HYBRID, RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = _committed_repo(root / "workspace")
            context = RuntimeContext(RUNTIME, state_root=root / "state", scope=GLOBAL_HYBRID)
            with patch("jevkit.runtime.run", return_value={"delegated": True}) as fake_run:
                result = context.evaluate("case-1", workspace_root=workspace)
        self.assertEqual(result, {"delegated": True})
        self.assertEqual(fake_run.call_args.kwargs["data_classification"], "synthetic")

    def test_non_hybrid_evaluate_requires_live_scope(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            with self.assertRaisesRegex(SafeError, "LIVE_SCOPE_REQUIRED"):
                context.evaluate("case-1")

    def test_evaluate_with_state_none_uses_synthetic_classification_and_no_request_hash(self):
        with tempfile.TemporaryDirectory() as td:
            context = self._live_context(Path(td))
            with patch("jevkit.runtime.run", return_value={"ok": True}) as fake_run:
                context.evaluate("case-1")
        kwargs = fake_run.call_args.kwargs
        self.assertEqual(kwargs["data_classification"], "synthetic")
        self.assertIsNone(kwargs["request_sha256"])

    def test_evaluate_with_state_requires_a_valid_classification(self):
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            context = self._live_context(Path(td))
            with self.assertRaisesRegex(SafeError, "CLASSIFICATION_REQUIRED"):
                context.evaluate("case-1", {"x": 1}, request_id="req-1")
            with self.assertRaisesRegex(SafeError, "DATA_CLASSIFICATION_NOT_ALLOWED"):
                context.evaluate("case-1", {"x": 1}, request_id="req-1", data_classification="restricted")

    def test_evaluate_with_state_requires_a_bounded_request_id(self):
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            context = self._live_context(Path(td))
            for bad_id in (None, "", "   ", "x" * 129):
                with self.subTest(request_id=bad_id):
                    with self.assertRaisesRegex(SafeError, "REQUEST_ID_REQUIRED"):
                        context.evaluate("case-1", {"x": 1}, request_id=bad_id, data_classification="public")

    def test_evaluate_with_state_computes_a_deterministic_request_hash_and_forwards_it(self):
        from jevkit.runtime import RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(root / "config")}):
                context = self._live_context(root)
                with patch("jevkit.runtime.spec", return_value=FAKE_SPEC), \
                     patch("jevkit.runtime.questions_for", return_value=FAKE_QUESTIONS), \
                     patch("jevkit.runtime.run", return_value={"ok": True}) as fake_run:
                    context.evaluate("case-1", {"x": 1}, request_id="req-1", data_classification="public")
                    expected_hash = RuntimeContext._request_hash({"x": 1}, FAKE_QUESTIONS)
        kwargs = fake_run.call_args.kwargs
        self.assertEqual(kwargs["data_classification"], "public")
        self.assertEqual(kwargs["request_id"], "req-1")
        self.assertEqual(kwargs["request_sha256"], expected_hash)
        self.assertEqual(kwargs["mode"], "live")


class RuntimeContextGrantLifecycleTests(unittest.TestCase):
    """create_live_grant/read_live_grant/revoke_live_grant tie RuntimeContext
    to the real CallBudget ledger. Before this file, none of the three had
    ever been exercised through RuntimeContext, so the WORKSPACE_REVISION_CHANGED
    staleness check -- and revoke's deliberate exemption from it -- were both
    unverified."""

    @staticmethod
    def _live_context(tmp_path):
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext

        workspace = _committed_repo(tmp_path / "workspace")
        return RuntimeContext(RUNTIME, state_root=tmp_path / "state", scope=PROJECT_LIVE,
                               workspace_root=workspace)

    def test_grant_lifecycle_create_read_revoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(root / "config")}):
                context = self._live_context(root)
                self.assertFalse(context.read_live_grant()["enabled"])
                granted = context.create_live_grant(5, 10)
                self.assertTrue(granted["enabled"])
                self.assertEqual(granted["remaining_calls"], 5)
                context.revoke_live_grant()
                self.assertFalse(context.read_live_grant()["enabled"])

    def test_create_live_grant_custom_requires_matching_fields_then_succeeds(self):
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(root / "config")}):
                context = self._live_context(root)
                with self.assertRaisesRegex(SafeError, "REQUEST_GRANT_REQUIRED"):
                    context.create_live_grant(5, 10, allow_custom=True, data_classification="public")
                with patch("jevkit.runtime.spec", return_value=FAKE_SPEC), \
                     patch("jevkit.runtime.questions_for", return_value=FAKE_QUESTIONS):
                    with self.assertRaisesRegex(SafeError, "REQUEST_ID_REQUIRED"):
                        context.create_live_grant(5, 10, allow_custom=True, case_id="c1", state={"x": 1},
                                                   data_classification="public")
                    granted = context.create_live_grant(5, 10, allow_custom=True, case_id="c1",
                                                         state={"x": 1}, request_id="req-1",
                                                         data_classification="public")
                self.assertTrue(granted["custom_data_allowed"])

    def test_non_live_scope_is_rejected_for_every_grant_operation(self):
        from jevkit.runtime import GLOBAL_OFFLINE, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            context = RuntimeContext(RUNTIME, state_root=Path(td) / "state", scope=GLOBAL_OFFLINE)
            operations = (
                lambda: context.create_live_grant(1, 1),
                lambda: context.read_live_grant(),
                lambda: context.revoke_live_grant(),
            )
            for operation in operations:
                with self.assertRaisesRegex(SafeError, "LIVE_SCOPE_REQUIRED"):
                    operation()

    def test_stale_workspace_blocks_create_and_read_but_revoke_still_clears_it(self):
        """Documents a deliberate asymmetry: read/create refuse a workspace
        whose git revision has moved since binding, but revoke does not --
        so a user can always clear a ledger even when re-authorizing would
        require a fresh, successful workspace_binding()."""
        from jevkit.runtime import PROJECT_LIVE, RuntimeContext, revoke_workspace_grant
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(root / "config")}):
                context = self._live_context(root)
                context.create_live_grant(5, 10)
                _new_commit(context.workspace_root)
                with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_CHANGED"):
                    context.read_live_grant()
                with self.assertRaisesRegex(SafeError, "WORKSPACE_REVISION_CHANGED"):
                    context.create_live_grant(5, 10)
                revoke_workspace_grant(RUNTIME, context.workspace_root, state_root=root / "state")
                fresh = RuntimeContext(RUNTIME, state_root=root / "state", scope=PROJECT_LIVE,
                                        workspace_root=context.workspace_root)
                self.assertFalse(fresh.read_live_grant()["enabled"])


class RuntimeContextValidationHelperTests(unittest.TestCase):
    """_validate_classification/_require_live_scope/_request_hash are the
    static building blocks evaluate() and create_live_grant() rely on."""

    def test_validate_classification_rules(self):
        from jevkit.runtime import RuntimeContext
        from jevkit.security import SafeError

        cases = [
            (None, "CLASSIFICATION_REQUIRED"),
            ("restricted", "DATA_CLASSIFICATION_NOT_ALLOWED"),
            ("prohibited", "DATA_CLASSIFICATION_NOT_ALLOWED"),
            ("made-up-value", "DATA_CLASSIFICATION_NOT_ALLOWED"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(SafeError, expected):
                    RuntimeContext._validate_classification(value)
        for allowed in ("public", "internal-minimized"):
            with self.subTest(value=allowed):
                self.assertEqual(RuntimeContext._validate_classification(allowed), allowed)

    def test_require_live_scope_rejects_every_other_scope(self):
        from jevkit.runtime import GLOBAL_HYBRID, GLOBAL_OFFLINE, RuntimeContext
        from jevkit.security import SafeError

        with tempfile.TemporaryDirectory() as td:
            offline = RuntimeContext(RUNTIME, state_root=Path(td) / "s1", scope=GLOBAL_OFFLINE)
            hybrid = RuntimeContext(RUNTIME, state_root=Path(td) / "s2", scope=GLOBAL_HYBRID)
        for context in (offline, hybrid):
            with self.subTest(scope=context.scope):
                with self.assertRaisesRegex(SafeError, "LIVE_SCOPE_REQUIRED"):
                    context._require_live_scope()

    def test_request_hash_is_deterministic_and_sensitive_to_state(self):
        from jevkit.runtime import RuntimeContext

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {**NO_PROVIDER_ENV, "XDG_CONFIG_HOME": str(Path(td) / "config")}):
                first = RuntimeContext._request_hash({"a": 1}, FAKE_QUESTIONS)
                second = RuntimeContext._request_hash({"a": 1}, FAKE_QUESTIONS)
                third = RuntimeContext._request_hash({"a": 2}, FAKE_QUESTIONS)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
