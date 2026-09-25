"""Verification and reranking: absence of evidence must never read as a pass.

Both tools answer a question the host would otherwise answer expensively in its
own context — is this extraction supported by its source, and do these passages
answer the question at all. Both have the same failure mode, and it is the one
that matters: a model that returned nothing looking exactly like a clean
result.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import rerank as rr  # noqa: E402
from jev_auto import verify as vf  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402

SOURCE = "The invoice totals 420 EUR and was issued on 3 March. No purchase order is referenced."


def noul(value):
    return {"type": "noul", "noul": value}


class QuestionShape(unittest.TestCase):
    def test_a_populated_field_is_asked_the_hallucination_question(self):
        _, questions = vf.compile_verify(SOURCE, {"total": "420 EUR"})
        self.assertIn("contradicted by the source", questions["f0"]["instructions"])

    def test_an_empty_field_is_asked_the_omission_question(self):
        """One phrasing for both scored a correctly-empty field at p_wrong 0.98.

        For an empty value, 'absent from the source' is trivially true, so the
        two cases have to be asked differently or the number means nothing.
        """
        _, questions = vf.compile_verify(SOURCE, {"purchase_order": ""})
        self.assertIn("DOES state a value", questions["f0"]["instructions"])

    def test_a_populated_falsy_scalar_is_not_treated_as_empty(self):
        for value in (0, 0.0, False):
            with self.subTest(value=value):
                _, questions = vf.compile_verify(SOURCE, {"count": value})
                self.assertIn("contradicted by the source", questions["f0"]["instructions"])

    def test_an_empty_container_is_treated_as_empty(self):
        for value in ([], {}, ()):
            with self.subTest(value=repr(value)):
                _, questions = vf.compile_verify(SOURCE, {"items": value})
                self.assertIn("DOES state a value", questions["f0"]["instructions"])

    def test_question_keys_are_positional_not_caller_text(self):
        """A field name is caller-controlled and must not become a protocol key."""
        _, questions = vf.compile_verify(SOURCE, {"weird key: with stuff": "x", "b": "y"})
        self.assertEqual(sorted(questions), ["f0", "f1"])

    def test_malformed_input_is_refused(self):
        for source, extraction in (("", {"a": 1}), (SOURCE, {}), (SOURCE, None),
                                   (None, {"a": 1}), (SOURCE, {"a" * 200: 1})):
            with self.subTest(source=str(source)[:10], extraction=str(extraction)[:20]):
                with self.assertRaises(AutoError):
                    vf.compile_verify(source, extraction)


class AbsenceIsNotCleanliness(unittest.TestCase):
    def test_a_field_the_model_never_answered_is_unknown_not_ok(self):
        out = vf.summarise({"total": "420", "date": "3 March"}, {"f0": noul(0.02)})
        statuses = {item["field"]: item["status"] for item in out["fields"]}
        self.assertEqual(statuses, {"total": vf.OK, "date": vf.UNKNOWN})
        self.assertFalse(out["trustworthy"])
        self.assertEqual(out["unknown_fields"], ["date"])
        self.assertEqual(out["suspect_fields"], [], "empty suspects must not imply clean")

    def test_a_total_outage_is_not_an_all_clear(self):
        out = vf.summarise({"a": 1, "b": 2}, {})
        self.assertFalse(out["trustworthy"])
        self.assertEqual(out["measured_fields"], 0)
        self.assertEqual(len(out["unknown_fields"]), 2)

    def test_a_non_finite_or_out_of_range_score_is_not_a_measurement(self):
        for value in (float("nan"), float("inf"), 1.5, -0.2, "high", True, None):
            with self.subTest(value=repr(value)):
                out = vf.summarise({"a": 1}, {"f0": noul(value)})
                self.assertEqual(out["fields"][0]["status"], vf.UNKNOWN)
                self.assertFalse(out["trustworthy"])

    def test_an_empty_extraction_is_never_trustworthy(self):
        self.assertFalse(vf.summarise({}, {})["trustworthy"])

    def test_a_measured_clean_record_is_trustworthy(self):
        out = vf.summarise({"total": "420", "date": "3 March"},
                           {"f0": noul(0.01), "f1": noul(0.03)})
        self.assertTrue(out["trustworthy"])
        self.assertEqual(out["max_p_wrong"], 0.03)
        self.assertFalse(out["execution_authorized"])

    def test_the_threshold_that_was_asked_for_is_the_one_applied(self):
        answers = {"f0": noul(0.5)}
        self.assertEqual(vf.summarise({"a": 1}, answers, 0.7)["fields"][0]["status"], vf.OK)
        self.assertEqual(vf.summarise({"a": 1}, answers, 0.4)["fields"][0]["status"], vf.SUSPECT)


class RerankAbstains(unittest.TestCase):
    def memories(self, count=2):
        return [{"content": f"passage {index}", "fact_id": f"id{index}", "score": 0.9}
                for index in range(count)]

    def test_each_passage_is_scored_and_the_set_is_asked_separately(self):
        _, questions = rr.compile_rerank("what invalidates a cache entry?", self.memories())
        self.assertEqual(sorted(questions), ["m0", "m1", "set_answers_question"])
        self.assertEqual(questions["set_answers_question"]["type"], "noul")
        self.assertEqual(questions["m0"]["type"], "score")

    def test_an_unscored_passage_is_not_usable(self):
        out = rr.summarise(self.memories(), {"m0": {"score": 3.0}, "set_answers_question": noul(0.9)})
        usable = {item["fact_id"]: item["usable"] for item in out["memories"]}
        self.assertEqual(usable, {"id0": True, "id1": False})

    def test_an_unmeasured_set_abstains(self):
        out = rr.summarise(self.memories(), {"m0": {"score": 3.0}, "m1": {"score": 3.0}})
        self.assertTrue(out["should_abstain"], "no set answer means no answer")

    def test_a_topical_but_unanswering_set_abstains(self):
        out = rr.summarise(self.memories(), {"m0": {"score": 3.0}, "m1": {"score": 3.0},
                                             "set_answers_question": noul(0.1)})
        self.assertTrue(out["should_abstain"])

    def test_a_set_with_nothing_usable_abstains_even_when_it_claims_to_answer(self):
        out = rr.summarise(self.memories(), {"m0": {"score": 1.0}, "m1": {"score": 0.0},
                                             "set_answers_question": noul(0.95)})
        self.assertTrue(out["should_abstain"])

    def test_a_measured_answering_set_does_not_abstain(self):
        out = rr.summarise(self.memories(), {"m0": {"score": 3.0}, "m1": {"score": 1.0},
                                             "set_answers_question": noul(0.9)})
        self.assertFalse(out["should_abstain"])
        self.assertEqual(out["usable_count"], 1)
        self.assertEqual(out["memories"][0]["fact_id"], "id0", "ranked best first")
        self.assertFalse(out["execution_authorized"])

    def test_malformed_input_is_refused(self):
        for query, memories in (("", [{"content": "x"}]), ("q", []), ("q", None),
                                ("q", [{"content": ""}]), ("q", ["not a dict"]),
                                ("q", [{"content": "x"}] * 13)):
            with self.subTest(query=query, memories=str(memories)[:24]):
                with self.assertRaises(AutoError):
                    rr.compile_rerank(query, memories)


class ToolSurface(unittest.TestCase):
    def test_both_tools_are_exposed_and_require_explicit_consent(self):
        from jev_auto.mcp import definitions
        from jevkit import mcp_server

        tools = {tool["name"]: tool for tool in definitions(mcp_server)}
        for name in ("jev_verify", "jev_rerank"):
            with self.subTest(name):
                self.assertIn(name, tools)
                required = tools[name]["inputSchema"]["required"]
                self.assertIn("workspace_path", required)
                self.assertIn("data_classification", required)


if __name__ == "__main__":
    unittest.main()
