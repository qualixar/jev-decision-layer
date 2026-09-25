"""Private one-session loopback wizard for first-run workspace setup.

The server does not expose consent creation through MCP. It keeps session and
CSRF values in process memory, accepts bounded form bodies, and never logs or
reflects a credential. Same-user local processes are outside this boundary.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from . import SetupSession
from .host_inventory import inventory_hosts
from .local_attestor import attest_local_config
from .setup_controller import SetupChoice, SetupController, SetupError


_MAX_BODY = 16_384
_FORM_KEYS = frozenset({"csrf", "provider", "mode", "classification", "days", "daily_calls", "daily_bytes", "generic", "auto_prepare", "local_laya", "confirm", "confirm_external_scope", "credential", "review_nonce"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
}


def _safe(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, content: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_safe(title)} · Qualixar</title>"
        "<style>body{background:#0e1220;color:#eef2ff;font:16px/1.5 system-ui;margin:0;padding:2rem}"
        "main{max-width:740px;margin:auto;background:#1a2234;border:1px solid #354260;border-radius:18px;padding:2rem}"
        "h1{margin-top:0}label{display:block;margin:1rem 0}.hint{color:#b9c8e2}input,select,button{font:inherit;padding:.6rem;border-radius:8px}"
        "input,select{max-width:100%}select[name=mode]{width:100%}select[name=mode] option{padding:.3rem}button{background:#75e3b0;color:#10231b;border:0;font-weight:700;cursor:pointer}"
        "button:focus,input:focus,select:focus{outline:3px solid #f6d365}code{overflow-wrap:anywhere}</style></head>"
        f"<body><main><h1>{_safe(title)}</h1>{content}</main></body></html>"
    ).encode("utf-8")


class SetupServer(HTTPServer):
    def __init__(self, address: tuple[str, int], controller: SetupController,
                 *, host_inventory: Callable[[], list[dict[str, object]]] = inventory_hosts):
        if address[0] != "127.0.0.1":
            raise ValueError("SETUP_LOOPBACK_ONLY")
        super().__init__(address, _SetupHandler)
        self.controller = controller
        self.host_inventory = host_inventory
        self.setup_session = SetupSession.create(port=self.server_port)
        self.applied = False
        self.request_count = 0
        self.review: tuple[str, str, float] | None = None
        self._watch_stop = threading.Event()

    def serve_forever(self, poll_interval: float = 0.2) -> None:
        def expire_session() -> None:
            while not self._watch_stop.wait(0.05):
                if time.monotonic() >= self.setup_session.expires_at:
                    self.shutdown()
                    return

        watcher = threading.Thread(target=expire_session, daemon=True, name="adl-setup-expiry")
        watcher.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._watch_stop.set()
            watcher.join(timeout=1)


def _choice_digest(choice: SetupChoice) -> str:
    encoded = json.dumps(dataclasses.asdict(choice), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requires_external_scope_confirmation(choice: SetupChoice) -> bool:
    """Keep the hosted-data acknowledgement independent of client payload shape."""
    return choice.provider != "laya-mlx" and choice.data_classification == "restricted"


class _SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Never log request paths, form data, cookies, or credentials.
        return

    def _send(self, status: int, body: bytes, *, cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        if cookie:
            self.send_header("Set-Cookie", f"adl_setup={self.server.setup_session.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=600")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str) -> None:
        self._send(status, _page("Setup needs attention", f"<p role='alert'>{_safe(code)}</p>"))

    def _host_ok(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _limited(self) -> bool:
        self.server.request_count += 1
        if self.server.request_count > 100:
            self._error(429, "SETUP_REQUEST_LIMIT")
            return False
        return True

    def do_GET(self) -> None:
        if not self._limited():
            return
        if not self._host_ok():
            self._error(403, "CROSS_ORIGIN_REJECTED")
            return
        if self.path == "/status":
            cookies = [part.strip()[len("adl_setup="):] for part in self.headers.get("Cookie", "").split(";")
                       if part.strip().startswith("adl_setup=")]
            if len(cookies) != 1 or not self.server.setup_session.matches(
                    session=cookies[0], csrf=self.server.setup_session.csrf):
                self._error(403, "SESSION_INVALID")
                return
            try:
                policy = self.server.controller.load_existing()
            except Exception:
                self._send(200, _page("Jev workspace status", "<p>Not enrolled. Return to <a href='/setup'>setup</a>.</p>"))
                return
            provider = {"typesafe": "TypeSafe", "openrouter": "OpenRouter", "laya-mlx": "Laya local"}.get(
                policy.get("provider"), "Unknown")
            content = (
                f"<p>Provider: <strong>{_safe(provider)}</strong>.</p>"
                f"<p>Decision mode: <strong>{_safe(policy.get('decision_mode') or 'legacy reviewed scope')}</strong>.</p>"
                f"<p>Reviewed text scope: <strong>{_safe(policy.get('data_classification', 'unknown'))}</strong>.</p>"
                f"<p>Limit: {_safe(policy.get('max_calls_per_day', 'unknown'))} attempts/day; "
                f"{_safe(policy.get('max_bytes_per_day', 'unknown'))} request bytes/day.</p>"
                f"<p>Automatic prompt guidance: {'Enabled' if policy.get('auto_prepare_jev') is True else 'Disabled'}. "
                f"Optional local Laya: {'Enabled' if policy.get('local_laya_enabled') is True else 'Disabled'}.</p>"
                "<p>No provider key or prompt text is shown here. <a href='/setup'>Review settings</a>.</p>"
            )
            self._send(200, _page("Jev workspace status", content))
            return
        if self.path != "/setup":
            self._error(404, "ROUTE_NOT_FOUND")
            return
        if not self.server.setup_session.matches(session=self.server.setup_session.session, csrf=self.server.setup_session.csrf):
            self._error(410, "SETUP_SESSION_EXPIRED")
            return
        workspace_name = _safe(self.server.controller.workspace.name or self.server.controller.workspace)
        csrf = _safe(self.server.setup_session.csrf)
        local_check = getattr(self.server.controller, "local_ready", None)
        local_available = bool(local_check()) if callable(local_check) else getattr(self.server.controller, "local_attestor", None) is not None
        local_disabled = "" if local_available else " disabled"
        try:
            current = self.server.controller.load_existing() if self.server.controller.policy_exists() else {}
        except Exception:
            current = {}
        selected_mode = current.get("decision_mode")
        if selected_mode not in ("jev-public", "jev-internal", "jev-maximum", "hybrid", "laya-only"):
            selected_mode = ("laya-only" if current.get("provider") == "laya-mlx" else
                             "hybrid" if current.get("local_laya_enabled") is True else
                             "jev-maximum" if current.get("data_classification") == "restricted" else
                             "jev-internal" if current.get("data_classification") == "internal-minimized" else
                             "jev-public")
        selected_provider = current.get("provider") if current.get("provider") in ("typesafe", "openrouter") else "typesafe"
        generic_on = current.get("generic_query_enabled", True) is True
        auto_on = current.get("auto_prepare_jev", False) is True
        def selected(value: str, actual: str) -> str:
            return " selected" if value == actual else ""
        mode_options = (
            f"<option value='jev-public'{selected('jev-public', selected_mode)}>Jev public — public text only</option>"
            f"<option value='jev-internal'{selected('jev-internal', selected_mode)}>Jev reviewed internal — minimized internal text</option>"
            f"<option value='jev-maximum'{selected('jev-maximum', selected_mode)}>Jev maximum — reviewed workspace text to hosted Jev</option>"
            f"<option value='hybrid'{local_disabled}{selected('hybrid', selected_mode)}>Jev + Laya — private decisions local when selected</option>"
            f"<option value='laya-only'{local_disabled}{selected('laya-only', selected_mode)}>Laya only — local decisions, no Jev calls</option>"
        )
        provider_options = (
            f"<option value='typesafe'{selected('typesafe', selected_provider)}>TypeSafe</option>"
            f"<option value='openrouter'{selected('openrouter', selected_provider)}>OpenRouter</option>"
        )
        generic_options = (
            f"<option value='on'{' selected' if generic_on else ''}>On (recommended)</option>"
            f"<option value='off'{' selected' if not generic_on else ''}>Off</option>"
        )
        auto_options = (
            f"<option value='off'{' selected' if not auto_on else ''}>Off (default)</option>"
            f"<option value='on'{' selected' if auto_on else ''}>On for eligible Jev prompts</option>"
        )
        try:
            hosts = self.server.host_inventory()
        except Exception:
            hosts = []
        host_rows = "".join(
            f"<li><strong>{_safe(host.get('label', 'Unknown host'))}</strong>: "
            f"{'Detected, not verified' if host.get('detected') is True else 'Not detected'}. "
            "Native adapter not tested; Auto disabled.</li>"
            for host in hosts if isinstance(host, dict)
        )
        content = (
            "<p class='hint'>Step 1 of 2 · Choose how Jev will answer decisions in this workspace.</p>"
            + ("<p><a href='/status'>View current workspace settings</a></p>" if self.server.controller.policy_exists() else "") +
            "<p>This does not change Codex hook trust or grant access to your whole computer. "
            "Your provider key is entered only after you review this setup.</p>"
            f"<p><strong>Workspace:</strong> <code>{workspace_name}</code> "
            "<span class='hint'>Full folder path appears before you confirm.</span></p>"
            "<form method='post' action='/preview' autocomplete='off'>"
            f"<input type='hidden' name='csrf' value=\"{csrf}\">"
            f"<label>Decision mode <select name='mode' size='5' required>{mode_options}</select></label>"
            f"<label>Hosted Jev provider (unused for Laya-only) <select name='provider'>{provider_options}</select></label>"
            + ("" if local_available else "<p class='hint'>Laya local requires a verified model before it can be selected. Jev setup is available now.</p>")
            +
            "<p class='hint'>Guided hosted setup currently requires macOS Keychain. Optional Laya needs a verified Apple-Silicon installation.</p>"
            f"<label>Advisory Jev tools <select name='generic'>{generic_options}</select></label>"
            f"<label>Automatic Jev prompt guidance <select name='auto_prepare'>{auto_options}</select>. Turning this on sends minimized prompt terms and candidate titles to your selected provider and uses your daily call budget; it never executes a tool. Laya-only makes no Jev call.</label>"
            "<p class='hint'>The next screen shows your exact choices before saving.</p>"
            "<details><summary>Advanced limits</summary>"
            "<label>Permission days <input name='days' type='number' min='1' max='365' value='30' required></label>"
            "<label>Maximum attempts per day <input name='daily_calls' type='number' min='1' max='100000' value='100' required></label>"
            "<label>Maximum bytes per day <input name='daily_bytes' type='number' min='1000' value='2000000' required></label>"
            "</details>"
            "<p class='hint'>The selected Jev mode sends reviewed text to your chosen hosted provider. Credentials and obvious secrets are screened only on a best-effort basis, not by comprehensive DLP; do not submit credentials. Jev-only never silently switches to Laya."
            " Native host permissions and hook trust are separate.</p><button type='submit'>Review before saving</button></form>"
            "<details><summary>Other agent integrations</summary><section aria-label='Agent frameworks'>"
            "<h2>Agent frameworks on this computer</h2>"
            f"<ul>{host_rows}</ul><p class='hint'>Detection is not native adapter proof. No Auto mode is enabled by this page.</p></section></details>"
        )
        self._send(200, _page("Qualixar decision setup", content), cookie=True)

    def _form(self) -> dict[str, str] | None:
        if not self._host_ok() or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
            self._error(403, "CROSS_ORIGIN_REJECTED")
            return None
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
            self._error(415, "SETUP_FORM_REQUIRED")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "SETUP_LENGTH_REQUIRED")
            return None
        if length < 1 or length > _MAX_BODY:
            self._error(413, "SETUP_BODY_TOO_LARGE")
            return None
        self.connection.settimeout(3)
        try:
            raw = self.rfile.read(length).decode("utf-8")
            parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
        except (OSError, UnicodeDecodeError, ValueError):
            self._error(400, "SETUP_FORM_INVALID")
            return None
        if set(parsed) - _FORM_KEYS or any(len(values) != 1 for values in parsed.values()):
            self._error(400, "SETUP_FORM_INVALID")
            return None
        cookie_values = [part.strip()[len("adl_setup="):] for part in self.headers.get("Cookie", "").split(";") if part.strip().startswith("adl_setup=")]
        if len(cookie_values) != 1 or not self.server.setup_session.matches(session=cookie_values[0], csrf=parsed.get("csrf", [""])[0]):
            self._error(403, "SESSION_INVALID")
            return None
        return {name: values[0] for name, values in parsed.items()}

    @staticmethod
    def _choice(form: dict[str, str]) -> SetupChoice:
        try:
            mode = form.get("mode")
            shapes = {
                "jev-public": ("public", False),
                "jev-internal": ("internal-minimized", False),
                "jev-maximum": ("restricted", False),
                "hybrid": ("internal-minimized", True),
                "laya-only": ("restricted", False),
            }
            if mode is not None and mode not in shapes:
                raise ValueError("invalid mode")
            if mode is not None and ("classification" in form or "local_laya" in form):
                raise ValueError("conflicting scope")
            classification, local_laya = shapes[mode] if mode is not None else (
                form["classification"], form.get("local_laya") == "on")
            return SetupChoice(
                provider="laya-mlx" if mode == "laya-only" else form["provider"],
                data_classification=classification,
                days=int(form["days"]),
                daily_calls=int(form["daily_calls"]),
                daily_bytes=int(form["daily_bytes"]),
                generic_query_enabled=form.get("generic", "on" if mode else "") == "on",
                auto_prepare_jev=mode != "laya-only" and form.get("auto_prepare") == "on",
                local_laya_enabled=local_laya,
                decision_mode=mode,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SetupError("SETUP_SCOPE_INVALID") from error

    def do_POST(self) -> None:
        if not self._limited():
            return
        if self.path not in ("/preview", "/apply"):
            self._error(404, "ROUTE_NOT_FOUND")
            return
        form = self._form()
        if form is None:
            return
        if self.path == "/preview" and ("credential" in form or "confirm" in form):
            self._error(400, "SETUP_FORM_INVALID")
            return
        try:
            choice = self._choice(form)
            if self.path == "/preview":
                plan = self.server.controller.preview(choice)
                nonce = secrets.token_urlsafe(24)
                self.server.review = (nonce, _choice_digest(choice), time.monotonic() + 300)
                csrf = _safe(self.server.setup_session.csrf)
                inputs = "".join(
                    f"<input type='hidden' name='{name}' value=\"{_safe(form[name])}\">"
                    for name in (("provider", "mode", "days", "daily_calls", "daily_bytes") if "mode" in form else
                                 ("provider", "classification", "days", "daily_calls", "daily_bytes"))
                )
                if "mode" in form:
                    for name in ("generic", "auto_prepare"):
                        if name in form:
                            inputs += f"<input type='hidden' name='{name}' value=\"{_safe(form[name])}\">"
                else:
                    if choice.generic_query_enabled:
                        inputs += "<input type='hidden' name='generic' value='on'>"
                    if choice.auto_prepare_jev:
                        inputs += "<input type='hidden' name='auto_prepare' value='on'>"
                if choice.local_laya_enabled and "mode" not in form:
                    inputs += "<input type='hidden' name='local_laya' value='on'>"
                if choice.provider == "laya-mlx":
                    credential_step = "<p class='hint'>Step 2 of 2 · Check the local model and scope.</p>"
                    credential_field = "<p>No provider key needed for local Laya. Its model must be prepared and verified before setup can finish.</p>"
                elif plan["upgrade"]:
                    credential_step = "<p class='hint'>Step 2 of 2 · Review the existing workspace permission change.</p>"
                    credential_field = "<p>The existing provider key stays in macOS Keychain; this review does not replace it.</p>"
                else:
                    credential_step = "<p class='hint'>Step 2 of 2 · Check the scope and add your provider key.</p>"
                    credential_field = (
                        "<label>Provider key (leave blank only if already stored in Keychain) "
                        "<input name='credential' type='password' autocomplete='new-password'></label>"
                    )
                external_scope_field = (
                    "<p role='alert'>Jev maximum can send reviewed workspace text, including client or confidential material, to the selected hosted provider. Only use it when you have authority to share that material. Secret screening is best-effort and cannot catch everything.</p>"
                    "<label><input name='confirm_external_scope' type='checkbox' value='yes' required> I understand this broader hosted-data scope.</label>"
                    if _requires_external_scope_confirmation(choice) else ""
                )
                content = (
                    credential_step +
                    f"<p><strong>Workspace:</strong> <code>{_safe(plan['workspace'])}</code></p>"
                    + (f"<p><strong>Current scope: {_safe(plan['current_data_classification'])}</strong>. "
                       f"<strong>New scope: {_safe(choice.data_classification)}</strong>.</p>" if plan["upgrade"] else "") +
                    f"<p><strong>Provider:</strong> {_safe(plan['provider'])}. "
                    f"Decision mode: {_safe(choice.decision_mode or 'legacy reviewed scope')}. "
                    f"Data scope: {_safe(choice.data_classification)}. Limit: {_safe(choice.daily_calls)} attempts/day for {_safe(choice.days)} days; "
                    f"{_safe(f'{choice.daily_bytes:,}')} bytes per day.</p>"
                    f"<p>Generic typed queries: {'Enabled' if choice.generic_query_enabled else 'Disabled'}.</p>"
                    f"<p>Automatic Jev prompt guidance: {'Enabled' if choice.auto_prepare_jev else 'Disabled'}. "
                    "When enabled, eligible coding prompts send up to 24 short terms and 12 candidate titles to the provider; the answer is advisory, not permission to act.</p>"
                    f"<p>Optional local Laya: {'Enabled for private routing and local context reduction' if choice.local_laya_enabled else 'Disabled'}.</p>"
                    "<p>User review required: Codex hook trust and other host permissions are separate. No live call has run.</p>"
                    "<form method='post' action='/apply' autocomplete='off'>"
                    f"<input type='hidden' name='csrf' value=\"{csrf}\">"
                    f"<input type='hidden' name='review_nonce' value=\"{_safe(nonce)}\">{inputs}"
                    f"{credential_field}{external_scope_field}"
                    "<label><input name='confirm' type='checkbox' value='yes' required> I approve this exact workspace, provider, data scope and budget.</label>"
                    "<button type='submit'>Confirm setup</button></form>"
                )
                self._send(200, _page("Review Qualixar setup", content))
                return
            if self.server.applied:
                self._error(409, "SETUP_ALREADY_APPLIED")
                return
            review = self.server.review
            supplied_nonce = form.get("review_nonce", "")
            if (
                review is None
                or time.monotonic() >= review[2]
                or not hmac.compare_digest(supplied_nonce, review[0])
                or not hmac.compare_digest(_choice_digest(choice), review[1])
            ):
                self._error(403, "SETUP_REVIEW_REQUIRED")
                return
            if _requires_external_scope_confirmation(choice) and form.get("confirm_external_scope") != "yes":
                self._error(400, "EXTERNAL_SCOPE_CONFIRMATION_REQUIRED")
                return
            self.server.review = None
            result = self.server.controller.apply(choice, credential=form.get("credential") or None, confirmed=form.get("confirm") == "yes")
            self.server.applied = True
        except SetupError as error:
            code = str(error)
            status = 409 if code == "EXISTING_POLICY_REVIEW_REQUIRED" else 503 if code.startswith(("SETUP_PARTIAL", "KEYCHAIN_")) else 400
            self._error(status, code)
            return
        except Exception:
            self.server.review = None
            self._error(500, "SETUP_INTERNAL_FAILURE")
            return
        self._send(200, _page("Setup saved", f"<p>Provider: {_safe(result['provider'])}.</p><p>Review Codex hook trust in Codex. Other host cells remain unverified until their native checks pass.</p>"))
        threading.Thread(target=self.server.shutdown, daemon=True, name="adl-setup-finished").start()


def build_controller(workspace: Path) -> SetupController:
    return SetupController(workspace, local_attestor=attest_local_config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open private Qualixar setup in your browser")
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    controller = build_controller(args.workspace)
    with SetupServer(("127.0.0.1", 0), controller) as server:
        url = f"http://127.0.0.1:{server.server_port}/setup"
        if os.environ.get("ADL_SETUP_NO_BROWSER") != "1":
            webbrowser.open(url)
        print(f"Qualixar setup: {url}. Enter keys only in the local browser, never in chat.", flush=True)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
