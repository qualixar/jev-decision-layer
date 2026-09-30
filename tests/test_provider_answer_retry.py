"""A malformed provider answer is retried once; a real refusal never is.

A live run caught one hosted answer whose probabilities failed validation
(PROBABILITY_SUM) on an input that passed a dozen times since. Such an answer
is a transient provider fault, so the engine asks once more. Each attempt is
charged to the daily budget, and the fault is recorded locally as codes only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import FakeProviders, _EngineTestCase  # noqa: E402

from jev_auto.common import AutoError  # noqa: E402

QUESTIONS = {"pick": {"type": "choice", "instructions": "Pick one", "criteria": {"a": "first", "b": "second"}}}
GOOD = {"pick": {"type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.9, "b": 0.1}}}
BAD_SUM = {"pick": {"type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.9, "b": 0.3}}}


class _Sequence(FakeProviders):
    def __init__(self, *answers, model=None):
        super().__init__(model=model)
        self.queue = list(answers)

    def evaluate(self, p, state, questions):
        self._answers = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return super().evaluate(p, state, questions)


class RetryTests(_EngineTestCase):
    def ask(self, providers):
        self.enroll(generic_query_enabled=True)
        engine = self.build_engine(providers)
        return engine, engine.dispatch({"op": "typed_query", "state": {"case": 1}, "questions": QUESTIONS,
                                        "provider": "typesafe", "data_classification": "public"})

    def test_a_malformed_answer_is_retried_once_and_the_good_answer_returned(self):
        providers = _Sequence(BAD_SUM, GOOD)
        engine, result = self.ask(providers)
        self.assertEqual(len(providers.calls), 2)
        self.assertEqual(result["answers"]["pick"]["choice"], "a")
        import json

        with engine.store.connection() as connection:
            bodies = [json.loads(row[0]) for row in connection.execute(
                "SELECT body FROM events WHERE kind='provider_invalid_answer'")]
        self.assertEqual(bodies, [{"recipe": "generic", "provider": "typesafe", "code": "PROBABILITY_SUM"}])

    def test_a_second_malformed_answer_is_reported_not_retried_again(self):
        providers = _Sequence(BAD_SUM, BAD_SUM)
        with self.assertRaises(AutoError) as caught:
            self.ask(providers)
        self.assertEqual(str(caught.exception), "PROBABILITY_SUM")
        self.assertEqual(len(providers.calls), 2)

    def test_a_wrong_model_identity_is_never_retried(self):
        providers = _Sequence(GOOD, model="some-other-model")
        with self.assertRaises(AutoError) as caught:
            self.ask(providers)
        self.assertEqual(str(caught.exception), "MODEL_MISMATCH")
        self.assertEqual(len(providers.calls), 1)

    def test_each_attempt_is_charged_to_the_daily_budget(self):
        providers = _Sequence(BAD_SUM, GOOD)
        engine, _result = self.ask(providers)
        self.assertEqual(engine.store.stats()["budget_rows"][0]["reserved_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
