"""Size limits a recipe must fit before it can run on every provider route.

Two limits bind a recipe's own text, independent of what a caller supplies:

* The hosted query path (``src/adl/queries/typed.py``) rejects any string over
  8,000 characters and any request over 48,000 bytes. A recipe that declares
  a larger input accepts text locally that the provider step then refuses.
* The local Laya route (``jev_auto/mlx_preflight.py``) refuses a question whose
  instructions and options do not fit the model's question budget of
  ``head_max_len`` tokens (192 by default), any single option over 48 tokens,
  and a whole request over ``max_len`` tokens (512 on the English checkpoint).

The builder has no tokenizer, and must not need one, so it uses a deliberately
pessimistic estimate of a byte-level BPE's token count: every letter run of up
to six letters is one token and each further six letters one more, every digit
and every punctuation character is one token, every non-ASCII character costs
its UTF-8 byte count, runs of capitals cost extra, and whitespace other than a
single leading space is one token per character. Measured against a
ModernBERT-family vocabulary over every recipe question, it counted at least
11% more tokens than the tokenizer did and about 20% more on average, so a
recipe it accepts has room to spare; a recipe it rejects might have just
fitted, and should be shortened anyway.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_STRING_CHARS = 8_000          # src/adl/queries/typed.py _MAX_STRING_CHARS
MAX_REQUEST_BYTES = 48_000        # src/adl/queries/typed.py _MAX_REQUEST_BYTES
# The longest model identity the hosted or local route writes into the payload:
# "laya-mlx@" plus a 40-character revision.
MODEL_ID_BYTES = len("laya-mlx@") + 40
LAYA_HEAD_TOKENS = 192            # mlx_preflight: cfg.get('head_max_len', 192)
LAYA_OPTION_TOKENS = 48           # mlx_preflight: any option over 48 tokens is refused
LAYA_MAX_TOKENS = 512             # mlx_preflight: cfg.get('max_len', 512)
_WORD_CHARS = 6

_PIECE = re.compile(r" ?[A-Za-z]+| ?[0-9]| ?[^\sA-Za-z0-9]+|\s+")


def estimate_tokens(text: str) -> int:
    """A pessimistic token count for one string. Never negative."""
    total = 0
    for piece in _PIECE.findall(text):
        body = piece[1:] if piece.startswith(" ") and len(piece) > 1 and not piece.isspace() else piece
        if body.isascii() and body.isalpha():
            total += 1 + (len(body) - 1) // _WORD_CHARS
            capitals = sum(char.isupper() for char in body)
            if capitals > 1:  # SHOUTED_NAMES and CamelCase split into more pieces
                total += (capitals - 1) // 2
        elif body.isdigit() and body.isascii():
            total += 1
        elif body.isspace():
            total += len(body)
        else:
            total += sum(len(char.encode("utf-8")) for char in body)
    return total


class EstimatingTokenizer:
    """Stands in for the Laya tokenizer, counting with ``estimate_tokens``.

    The builder hands it to the runtime's own ``mlx_preflight.preflight`` rather
    than re-deriving the preflight's arithmetic, so the two cannot drift apart:
    whatever the worker would refuse with the real tokenizer, it refuses here
    with a count that is never smaller.
    """

    mask_token = "[MASK]"

    def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
        return {"input_ids": [0] * estimate_tokens(text)}


def question_head_tokens(question: dict[str, Any]) -> int:
    """Estimated tokens the Laya prefix spends before any state is added."""
    from jev_auto.mlx_preflight import options

    head = estimate_tokens(f"{question['type']} question: {question['instructions']}")
    return head + sum(1 + estimate_tokens(" " + option) for option in options(question))


def laya_problem(questions: dict[str, Any], state: Any = "") -> str | None:
    """Why the local Laya worker would refuse this request, or None.

    ``state`` is optional: with none, only the question itself is judged.
    """
    from jev_auto.common import AutoError
    from jev_auto.mlx_preflight import preflight

    config = {"max_len": LAYA_MAX_TOKENS, "head_max_len": LAYA_HEAD_TOKENS}
    try:
        preflight(EstimatingTokenizer(), config, state, questions)
    except AutoError as error:
        used = sum(question_head_tokens(question) for question in questions.values())
        return (f"the local Laya route would refuse it ({error}): the question needs about {used} of "
                f"{LAYA_HEAD_TOKENS} tokens, each option at most {LAYA_OPTION_TOKENS}, and question plus "
                f"input at most {LAYA_MAX_TOKENS}")
    return None


def worst_case_request_bytes(recipe: dict[str, Any]) -> int:
    """Largest hosted payload a valid input to this recipe can produce.

    Counts every field at its declared ``maxLength`` as one byte per character,
    which is exact for ASCII. Non-ASCII input is larger, and the runtime's own
    REQUEST_TOO_LARGE check still refuses it; this bound exists so that the
    common case never passes local validation only to fail at the provider.
    """
    properties = recipe["input_schema"]["properties"]
    state = {name: "" for name in properties}
    envelope = {"model": "m" * MODEL_ID_BYTES, "state": state, "questions": recipe["questions"]}
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(encoded.encode("utf-8")) + sum(rule["maxLength"] for rule in properties.values())
