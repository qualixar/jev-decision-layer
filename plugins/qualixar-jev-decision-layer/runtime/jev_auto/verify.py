"""Check an extraction against the source it claims to come from.

WHY THIS IS A DECISION, NOT A READ
----------------------------------
A host that extracts structured data from a document has to decide whether to
trust it. Doing that in host context means re-reading the source against every
field — the most expensive way to answer a question a calibrated model settles
in one cheap call. One Noul per field, all in a single request, and the host
gets a per-field probability that the field is WRONG.

ZERO EVIDENCE IS NOT A CLEAN BILL OF HEALTH
-------------------------------------------
The failure this module exists to prevent: a model outage and a verified-clean
record looking identical. A field the model never answered used to be dropped
from the result, so a total outage reported "all fields below threshold". So
did an extractor that returned nothing, and so did a NaN score, because
`nan >= 0.70` is False.

Every field therefore gets a verdict. One that could not be measured is
`unknown`, which is never clean and always blocks `trustworthy`. Callers branch
on `trustworthy` — never on the absence of suspect fields, which is also empty
when nothing could be measured at all.

TWO QUESTION SHAPES, BECAUSE A FIELD HAS TWO WAYS TO BE WRONG
--------------------------------------------------------------
A populated field can be a hallucination; an empty one can be an omission.
Measured against jev-1.13: with a single phrasing — "unsupported by, or absent
from, the source" — a correctly-empty field scored p_wrong 0.98, because for an
empty value "absent from the source" is trivially true. Asking the two cases
differently is what makes the number mean anything.

Questions are phrased so TRUE means WRONG. That direction is deliberate: it is
P(error) the caller's threshold consumes, and inverting it invites the
documented Jev failure mode of mapping true to "no".
"""

from __future__ import annotations

import math
from typing import Any

from .common import AutoError

ESCALATE_THRESHOLD = 0.70
MAX_FIELDS = 32
MAX_SOURCE_CHARS = 20_000
MAX_VALUE_CHARS = 2_000

OK = "ok"            # measured, below threshold
SUSPECT = "suspect"  # measured, at or above threshold
UNKNOWN = "unknown"  # NOT measured: no answer, or a non-finite score


def _is_empty(value: Any) -> bool:
    """True for a value the extractor did not fill in.

    Deliberately includes empty CONTAINERS. It must NOT include populated
    falsy scalars: 0, 0.0 and False are real extracted values and belong on
    the hallucination question, not the omission one.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _key(index: int) -> str:
    """Question keys are positional, never the caller's field name.

    A field name is caller-controlled text; using it as a protocol key would
    let it collide with another key or carry characters the query compiler
    rejects.
    """
    return f"f{index}"


def compile_verify(source_text: Any, extraction: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """One Noul per extracted field, all evaluated in a single call."""
    if not isinstance(source_text, str) or not 1 <= len(source_text.strip()) <= MAX_SOURCE_CHARS:
        raise AutoError("VERIFY_SOURCE_INVALID")
    if not isinstance(extraction, dict) or not 1 <= len(extraction) <= MAX_FIELDS:
        raise AutoError("VERIFY_EXTRACTION_INVALID")

    questions: dict[str, dict[str, Any]] = {}
    presented: dict[str, Any] = {}
    for index, (field, value) in enumerate(extraction.items()):
        if not isinstance(field, str) or not 1 <= len(field.strip()) <= 120:
            raise AutoError("VERIFY_EXTRACTION_INVALID")
        rendered = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
        if isinstance(rendered, str) and len(rendered) > MAX_VALUE_CHARS:
            raise AutoError("VERIFY_EXTRACTION_INVALID")
        presented[field] = rendered
        if _is_empty(value):
            instructions = (
                f"The source text DOES state a value for '{field}', which this extraction "
                "failed to capture. Answer yes only if the source actually contains that "
                "information."
            )
        else:
            instructions = (
                f"The extracted value for '{field}' is contradicted by the source text, or "
                "does not appear in it at all. Answer yes if the source states something "
                "different, or never states this."
            )
        questions[_key(index)] = {"type": "noul", "instructions": instructions}

    state = {"source_text": source_text, "extraction": presented}
    return state, questions


def summarise(extraction: dict[str, Any], answers: Any, threshold: float = ESCALATE_THRESHOLD) -> dict[str, Any]:
    """Turn the answer battery into one verdict per field. Never raises on content."""
    if not isinstance(answers, dict):
        answers = {}
    fields = []
    for index, (field, value) in enumerate(extraction.items()):
        answer = answers.get(_key(index))
        probability = None
        if isinstance(answer, dict):
            candidate = answer.get("noul")
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                candidate = float(candidate)
                # A non-finite or out-of-range score is not a measurement.
                if math.isfinite(candidate) and 0.0 <= candidate <= 1.0:
                    probability = round(candidate, 4)
        status = UNKNOWN if probability is None else (SUSPECT if probability >= threshold else OK)
        fields.append({"field": field, "value": value, "p_wrong": probability, "status": status})

    measured = [item["p_wrong"] for item in fields if item["p_wrong"] is not None]
    return {
        "status": "ADVISORY",
        "threshold": threshold,
        "fields": fields,
        # The flag to branch on. An empty `suspect_fields` does NOT mean clean:
        # a field the model never answered is unknown, not ok.
        "trustworthy": bool(fields) and all(item["status"] == OK for item in fields),
        "suspect_fields": [item["field"] for item in fields if item["status"] == SUSPECT],
        "unknown_fields": [item["field"] for item in fields if item["status"] == UNKNOWN],
        "measured_fields": len(measured),
        "max_p_wrong": round(max(measured, default=0.0), 4),
        "execution_authorized": False,
    }
