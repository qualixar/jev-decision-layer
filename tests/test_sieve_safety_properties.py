"""The four safety properties of the text sieve, each pinned by the input that breaks it.

Every test here fails if its guard is removed from sieve.py: error output is
never reduced, a block that states a constraint is never hidden, text with
contact details or home paths is never sent for a reduction decision, and a
model that sees an error keeps the text whole. Synthetic data only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import sieve  # noqa: E402

POLICY = {"min_chars": 2000, "max_blocks": 48, "block_lines": 5, "timeout_seconds": 5,
          "drop_probability": 0.10, "min_reduction": 0.05, "provider": "typesafe",
          "data_classification": "restricted", "decision_mode": "jev-maximum"}
GOAL = "Summarise the release checklist for the team."


def _line(number: int) -> str:
    # No error, constraint or sensitive trigger words.
    return f"alpha bravo charlie delta item {number} of the synthetic checklist body for sieve tests"


def _text(lines: int = 35, replace: dict[int, str] | None = None) -> str:
    rows = [_line(number) for number in range(lines)]
    for index, row in (replace or {}).items():
        rows[index] = row
    text = "\n".join(rows)
    assert len(text) >= 2500, len(text)
    return text


class _Engine:
    """Records every decision request; answers each block with a fixed probability."""

    def __init__(self, *, block=0.0, error=0.0):
        self.calls = []
        self.block, self.error = block, error
        self.stored = []
        self.store = SimpleNamespace(put=lambda record: self.stored.append(record) or "a" * 64,
                                     event=lambda *_args: None)

    def effective_policy(self, policy, recipe):
        return {**policy, "provider": "typesafe"}

    def judge(self, recipe, state, questions, policy):
        self.calls.append((recipe, state, questions))
        answers = {name: {"type": "noul", "noul": self.block} for name in questions}
        answers["error"] = {"type": "noul", "noul": self.error}
        return {"answers": answers}


class SieveSafetyTests(unittest.TestCase):
    def test_error_output_is_never_reduced_or_sent(self):
        text = _text(replace={14: "Traceback (most recent call last):"})
        engine = _Engine()
        result = sieve.reduce_text(engine, POLICY, GOAL, text)
        self.assertEqual(result, {"changed": False, "text": text, "reason": "error_preserved", "withheld_chars": 0})
        self.assertEqual(engine.calls, [])

    def test_a_block_that_states_a_constraint_is_never_hidden(self):
        constraint = "the release notes must keep this exact wording for the audit"
        text = _text(replace={12: constraint})
        engine = _Engine(block=0.0, error=0.0)  # the model calls every block irrelevant
        result = sieve.reduce_text(engine, POLICY, GOAL, text)
        self.assertTrue(result["changed"], result["reason"])
        self.assertEqual(len(engine.calls), 1)
        self.assertIn(constraint, result["text"])
        self.assertNotIn(_line(7), result["text"])  # an ordinary middle block was hidden

    def test_contact_details_and_home_paths_are_never_sent_for_a_decision(self):
        # Restricted scope lets the provider screen accept these fields, so the
        # sieve's own check is the only thing keeping them on this computer.
        text = _text(replace={10: "Owner: someone@example.com", 20: "Notes: /Users/example-user/project/notes.txt"})
        engine = _Engine()
        result = sieve.reduce_text(engine, POLICY, GOAL, text)
        self.assertEqual(result, {"changed": False, "text": text, "reason": "sensitive_data_not_sent",
                                  "withheld_chars": 0})
        self.assertEqual(engine.calls, [])

    def test_a_model_that_sees_an_error_keeps_the_text_whole(self):
        text = _text()
        engine = _Engine(block=0.0, error=0.5)
        result = sieve.reduce_text(engine, POLICY, GOAL, text)
        self.assertEqual(result, {"changed": False, "text": text, "reason": "error_or_constraint_uncertain",
                                  "withheld_chars": 0})
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.stored, [])

    def test_the_same_text_is_reduced_when_nothing_guards_it(self):
        """Control: without an error, constraint or sensitive field the sieve does reduce,
        so each test above fails for its own guard and not for a broken fixture."""
        engine = _Engine(block=0.0, error=0.0)
        result = sieve.reduce_text(engine, POLICY, GOAL, _text())
        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "extractive_reduction")


if __name__ == "__main__":
    unittest.main()
