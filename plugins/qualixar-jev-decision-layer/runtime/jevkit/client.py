"""Dependency-free Jev HTTP adapters with fixed origins and bounded retries."""
from __future__ import annotations

import email.utils
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from .authorization import CallBudget
from .constants import MAX_REQUEST_BYTES
from .contract import validate_questions, validate_response, validate_response_model
from .providers import ProviderProfile, get_provider_credential, resolve_provider
from .security import SafeError, canonical, screen

MAX_BYTES = MAX_REQUEST_BYTES


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def retry_delay(header: str | None, attempt: int) -> float:
    if header:
        try:
            return min(20.0, max(0.0, float(header)))
        except ValueError:
            try:
                date = email.utils.parsedate_to_datetime(header)
                return min(20.0, max(0.0, date.timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                pass
    return min(4.0, 0.5 * 2**attempt)


def evaluate(
    root: Path,
    state,
    questions: dict,
    custom: bool,
    provider: ProviderProfile | None = None,
    transport=None,
    sleep=time.sleep,
    grant_root: Path | None = None,
    workspace_id: str | None = None,
    revision: str | None = None,
    case_id: str | None = None,
    data_classification: str | None = None,
    request_id: str | None = None,
    request_sha256: str | None = None,
) -> dict:
    validate_questions(questions)
    selected = provider or resolve_provider()
    model = selected.model
    if not isinstance(state, (str, dict, list)):
        raise SafeError("INVALID_STATE")
    key = get_provider_credential(selected)
    _safe_payload, findings = screen({"state": state, "questions": questions}, (key,))
    if findings:
        raise SafeError("OUTBOUND_DATA_BLOCKED: " + ",".join(findings))
    payload = {"model": model, "state": state, "questions": questions}
    body = canonical(payload)
    if len(body) > MAX_BYTES:
        raise SafeError(
            "REQUEST_BYTE_BUDGET_EXCEEDED: send a smaller, reviewed evidence package"
        )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    send = transport or opener.open
    budget = CallBudget(
        root,
        storage_root=grant_root,
        workspace_id=workspace_id,
        revision=revision,
        provider_id=selected.provider_id,
        provider_profile_sha256=selected.profile_sha256,
    )
    start = time.monotonic()
    for attempt in range(3):
        grant_id = budget.reserve(
            custom,
            case_id=case_id,
            data_classification=data_classification,
            request_id=request_id,
            request_sha256=request_sha256,
        )
        request = urllib.request.Request(
            selected.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "User-Agent": "Qualixar-Jev-Control/1.1",
            },
        )
        try:
            with send(request, timeout=20) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise SafeError("API_RESPONSE_TOO_LARGE")
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeError):
                raise SafeError("API_NON_JSON_RESPONSE") from None
            result = validate_response(
                validate_response_model(parsed, model), questions
            )
            result, findings = screen(result, (key,))
            if findings:
                raise SafeError("INBOUND_DATA_BLOCKED: " + ",".join(findings))
            result["_transport"] = {
                "attempts": attempt + 1,
                "observed_latency_ms": round((time.monotonic() - start) * 1000, 2),
            }
            result["_grant_id"] = grant_id
            result["_provider_id"] = selected.provider_id
            result["_provider_profile_sha256"] = selected.profile_sha256
            return result
        except urllib.error.HTTPError as error:
            status = error.code
            delay = retry_delay(
                error.headers.get("Retry-After") if error.headers else None, attempt
            )
            error.close()
            # Error bodies can echo submitted data; never print or store them.
            if status in (429, 500, 502, 503, 504, 529) and attempt < 2:
                if delay >= 20:
                    raise SafeError(
                        "API_BACKOFF_REQUIRED: retry later after the provider delay"
                    ) from None
                sleep(delay)
                continue
            raise SafeError(
                "API_HTTP_"
                + str(status)
                + ": review account access or API contract; body suppressed"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # An ambiguous transport failure may have been billed. Do not auto-retry it.
            raise SafeError(
                "API_TRANSPORT_FAILURE: request may have reached provider; no automatic retry"
            ) from None
    raise SafeError("API_ATTEMPTS_EXHAUSTED")
