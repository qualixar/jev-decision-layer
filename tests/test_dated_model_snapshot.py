"""A provider may name the dated snapshot it served; nothing else is accepted.

OpenRouter answers a request for `typesafe/jev-1.13` with
`"model": "typesafe/jev-1.13-20260917"`, naming the dated snapshot behind the
id. The exact-match check refused every OpenRouter answer as MODEL_MISMATCH.

The rule now: the exact requested id, or the requested id followed by `-` and
exactly eight ASCII digits. A longer version, a letter suffix, an alias, a
second suffix, or another vendor's model reusing the same suffix is still
refused, so a threshold measured on one model never silently applies to
another. The accepted answer carries the requested id as its `model`, and the
exact served id is kept in provenance so the local receipt names the snapshot.

No test here makes a network call: HTTP is replaced at HTTPSConnection.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_engine_coverage import _EngineTestCase  # noqa: E402

from jev_auto.common import AutoError  # noqa: E402
from jevkit.security import SafeError  # noqa: E402

REQUESTED = "typesafe/jev-1.13"
SNAPSHOT = "typesafe/jev-1.13-20260917"

# Every id here must be refused for a request of REQUESTED.
REFUSED = (
    "typesafe/jev-2.0",                       # another model
    "typesafe/jev-1.13x",                     # longer id sharing the prefix
    "typesafe/jev-1.13-latest",               # alias suffix
    "other/jev-1.13-20260917",                # another vendor, same dated suffix
    "",                                       # empty
    "typesafe/jev-1.130",                     # longer version, no separator
    "typesafe/jev-1.13.1-20260917",           # longer version, then a date
    "typesafe/jev-1.135-20260917",
    "typesafe/jev-1.13-2026091a",             # letter in the date
    "typesafe/jev-1.13-2026O917",             # letter O for zero
    "typesafe/jev-1.13-2026091",              # seven digits
    "typesafe/jev-1.13-202609170",            # nine digits
    "typesafe/jev-1.13-20260917\n",           # trailing newline (a `$` regex trap)
    "typesafe/jev-1.13-20260917 ",            # trailing space
    " typesafe/jev-1.13-20260917",            # leading space
    "typesafe/jev-1.13-２０２６０９１７",  # full-width digits (a `\d` trap)
    "typesafe/jev-1.13-٢٠٢٦٠٩١٧",  # Arabic-Indic digits
    "typesafe/jev-1.13_20260917",             # wrong separator
    "typesafe/jev-1.13--20260917",            # doubled separator
    "typesafe/jev-1.13-",                     # separator, no date
    "typesafe/jev-1.13-20260917-20261001",    # a second suffix
    "typesafe/jev-1.13-20260917:free",        # a routing variant
    "typesafe/jev-1.13:free",
    "TypeSafe/jev-1.13-20260917",             # case differs
    "typesafe/jev-1.13-20260917/other/jev-1.13",
)


def _noul_raw(model):
    return {"model": model, "answers": {"q": {"type": "noul", "noul": 0.4}},
            "usage": {"input_tokens": 1, "output_tokens": 1}}


NOUL_QUESTIONS = {"q": {"type": "noul", "instructions": "Is this synthetic?"}}

# A Jev Score answer explainable only by two-decimal provider rounding: the
# expectation of the reported probabilities is 1.01, the score is 1.024. The
# generic rule (within 0.01 of the expectation) refuses it; the Jev rounding
# rule accepts it. A dated Jev snapshot must get the Jev rule.
SCORE_QUESTIONS = {"s": {"type": "score", "instructions": "Rate it", "criteria": ["low", "mid", "high"]}}
SCORE_ANSWER = {"s": {"type": "score", "score": 1.024, "confidence": 0.5,
                      "probabilities": {"0": 0.33, "1": 0.33, "2": 0.34}}}


class SharedRuleTests(unittest.TestCase):
    def test_exact_and_dated_snapshot_are_the_requested_model(self):
        from jevkit.model_identity import is_requested_model

        self.assertTrue(is_requested_model(REQUESTED, REQUESTED))
        self.assertTrue(is_requested_model(SNAPSHOT, REQUESTED))
        self.assertTrue(is_requested_model("jev-1.13.0", "jev-1.13.0"))

    def test_everything_else_is_refused(self):
        from jevkit.model_identity import is_requested_model

        for served in REFUSED:
            with self.subTest(served=served):
                self.assertFalse(is_requested_model(served, REQUESTED))

    def test_a_pinned_snapshot_request_accepts_only_that_snapshot(self):
        from jevkit.model_identity import is_requested_model

        self.assertTrue(is_requested_model(SNAPSHOT, SNAPSHOT))
        for served in ("typesafe/jev-1.13-20261001", "typesafe/jev-1.13-20260917-20261001", REQUESTED):
            with self.subTest(served=served):
                self.assertFalse(is_requested_model(served, SNAPSHOT))

    def test_non_strings_and_an_empty_request_are_refused(self):
        from jevkit.model_identity import is_requested_model

        for served, requested in ((None, REQUESTED), (123, REQUESTED), ([SNAPSHOT], REQUESTED),
                                  (b"typesafe/jev-1.13", REQUESTED), (SNAPSHOT, ""), (SNAPSHOT, None),
                                  ("", "")):
            with self.subTest(served=served, requested=requested):
                self.assertFalse(is_requested_model(served, requested))


class ProtocolTests(unittest.TestCase):
    """jev_auto/protocol.py: the live path behind every jev_* tool."""

    def test_dated_snapshot_is_accepted_under_the_requested_id(self):
        from jev_auto.protocol import validate_response

        result = validate_response(_noul_raw(SNAPSHOT), NOUL_QUESTIONS, REQUESTED)
        self.assertEqual(result["model"], REQUESTED)
        self.assertEqual(result["answers"], {"q": {"type": "noul", "noul": 0.4}})

    def test_every_other_id_is_a_model_mismatch(self):
        from jev_auto.protocol import validate_response

        for served in REFUSED:
            with self.subTest(served=served):
                with self.assertRaises(AutoError) as caught:
                    validate_response(_noul_raw(served), NOUL_QUESTIONS, REQUESTED)
                self.assertEqual(str(caught.exception), "MODEL_MISMATCH")

    def test_a_dated_snapshot_keeps_the_jev_score_rounding_rule(self):
        from jev_auto.protocol import validate_response

        raw = {"model": SNAPSHOT, "answers": SCORE_ANSWER, "usage": {}}
        result = validate_response(raw, SCORE_QUESTIONS, REQUESTED)
        self.assertEqual(result["answers"]["s"]["score"], 1.024)

    def test_the_score_rule_still_follows_the_model_not_the_suffix(self):
        """The same answer from a model that is not Jev gets the generic rule."""
        from jev_auto.protocol import validate_response

        for model, expected in (("other/model-20260917", None), ("laya-mlx@abc", None)):
            with self.subTest(model=model):
                raw = {"model": model, "answers": SCORE_ANSWER, "usage": {}}
                with self.assertRaises(AutoError) as caught:
                    validate_response(raw, SCORE_QUESTIONS, expected)
                self.assertEqual(str(caught.exception), "SCORE_EXPECTATION")


class LegacyContractTests(unittest.TestCase):
    """jevkit/contract.py: the legacy client path, same rule."""

    def test_dated_snapshot_is_returned_unchanged_for_the_record(self):
        from jevkit.contract import validate_response_model

        raw = _noul_raw(SNAPSHOT)
        self.assertIs(validate_response_model(raw, REQUESTED), raw)

    def test_every_other_id_is_refused(self):
        from jevkit.contract import validate_response_model

        for served in REFUSED + ("typesafe/jev-1.13-20261001",):
            requested = SNAPSHOT if served == "typesafe/jev-1.13-20261001" else REQUESTED
            with self.subTest(served=served):
                with self.assertRaises(SafeError) as caught:
                    validate_response_model(_noul_raw(served), requested)
                self.assertEqual(str(caught.exception), "MODEL_ID_MISMATCH")


def _connection(body):
    connection = MagicMock(name="HTTPSConnection")
    connection.sock = MagicMock()
    response = MagicMock(name="HTTPResponse")
    response.status = 200
    response.read1.side_effect = [json.dumps(body).encode(), b""]
    connection.getresponse.return_value = response
    return connection


class OpenRouterTransportTests(unittest.TestCase):
    POLICY = {"provider": "openrouter", "timeout_seconds": 30}

    def remote(self, body, questions=NOUL_QUESTIONS):
        from jev_auto.providers import Providers

        with patch("jev_auto.providers.http.client.HTTPSConnection", return_value=_connection(body)), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            return Providers().remote(self.POLICY, {"k": "v"}, questions)

    def test_snapshot_answer_is_accepted_and_the_snapshot_recorded(self):
        result = self.remote(_noul_raw(SNAPSHOT))
        self.assertEqual(result["model"], REQUESTED)
        self.assertEqual(result["provenance"], {
            "provider": "openrouter", "model_requested": REQUESTED, "model_served": SNAPSHOT,
            "confidence_kind": "provider-reported",
        })

    def test_snapshot_score_answer_is_accepted(self):
        result = self.remote({"model": SNAPSHOT, "answers": SCORE_ANSWER, "usage": {}}, SCORE_QUESTIONS)
        self.assertEqual(result["answers"]["s"]["score"], 1.024)

    def test_another_model_with_a_dated_suffix_is_refused(self):
        for served in ("typesafe/jev-2.0-20260917", "other/jev-1.13-20260917", "typesafe/jev-1.13-latest"):
            with self.subTest(served=served):
                with self.assertRaises(AutoError) as caught:
                    self.remote(_noul_raw(served))
                self.assertEqual(str(caught.exception), "MODEL_MISMATCH")


class EngineReceiptTests(_EngineTestCase):
    """End to end through the broker engine: answer accepted, receipt names the snapshot."""

    def test_typed_query_via_openrouter_accepts_the_snapshot_and_records_it(self):
        from jev_auto.providers import Providers

        self.enroll(provider="openrouter", generic_query_enabled=True)
        engine = self.build_engine(Providers())
        with patch("jev_auto.providers.http.client.HTTPSConnection",
                   return_value=_connection(_noul_raw(SNAPSHOT))), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            result = engine.dispatch({"op": "typed_query", "state": {"case": 1}, "questions": NOUL_QUESTIONS,
                                      "provider": "openrouter", "data_classification": "public"})
        self.assertEqual(result["model"], REQUESTED)
        self.assertEqual(result["answers"]["q"]["noul"], 0.4)
        receipt = engine.store.get(result["receipt_id"])
        self.assertEqual(receipt["result"]["model"], REQUESTED)
        self.assertEqual(receipt["result"]["provider_metadata"]["model_served"], SNAPSHOT)
        self.assertEqual(receipt["result"]["provider_metadata"]["model_requested"], REQUESTED)

    def test_typed_query_via_openrouter_refuses_another_model(self):
        from jev_auto.providers import Providers

        self.enroll(provider="openrouter", generic_query_enabled=True)
        engine = self.build_engine(Providers())
        with patch("jev_auto.providers.http.client.HTTPSConnection",
                   return_value=_connection(_noul_raw("typesafe/jev-2.0-20260917"))), \
             patch("jevkit.providers.get_provider_credential", return_value="A" * 32):
            with self.assertRaises(AutoError) as caught:
                engine.dispatch({"op": "typed_query", "state": {"case": 1}, "questions": NOUL_QUESTIONS,
                                 "provider": "openrouter", "data_classification": "public"})
        self.assertEqual(str(caught.exception), "MODEL_MISMATCH")


if __name__ == "__main__":
    unittest.main()
