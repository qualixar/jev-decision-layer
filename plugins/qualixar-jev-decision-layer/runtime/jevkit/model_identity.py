"""Whether a provider's answer came from the model that was requested.

Exact identity, with one exception: a provider may name the dated snapshot it
served. OpenRouter answers a request for `typesafe/jev-1.13` with
`typesafe/jev-1.13-20260917`. That is the requested model, named precisely.

Everything else is refused: aliases (`-latest`), longer versions that share
the prefix (`jev-1.130`, `jev-1.13.1-...`), routing variants (`:free`), a
second suffix, and another vendor's model reusing the same dated suffix.
Thresholds are measured per model, so accepting any of those would apply one
model's thresholds to another.

A request that already names a snapshot accepts only that snapshot.
"""

from __future__ import annotations

import re

# ASCII digits only: `\d` also matches full-width and other Unicode digits.
# Always used with fullmatch: `$` would also match before a trailing newline.
_SNAPSHOT_SUFFIX = re.compile(r"-[0-9]{8}")


def is_requested_model(served: object, requested: object) -> bool:
    """True when `served` is `requested`, or `requested` plus `-` and eight digits."""
    if not isinstance(served, str) or not isinstance(requested, str) or not requested:
        return False
    if served == requested:
        return True
    if _SNAPSHOT_SUFFIX.fullmatch(requested[-9:]):
        return False
    return served.startswith(requested) and _SNAPSHOT_SUFFIX.fullmatch(served[len(requested):]) is not None
