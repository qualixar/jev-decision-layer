"""Live end-to-end check of the three setup choices: Jev, Laya, and Jev + Laya.

Run by hand before a release, on an Apple-Silicon Mac that has a hosted key in
the credential store and local Laya installed (`scripts/jev laya-install`):

    python3 tools/e2e_matrix.py --modes jev,laya,hybrid --report /tmp/e2e.json

For each mode it enrolls a temporary folder through the setup wizard's own
controller (the same preview and apply the page calls), starts the shipped MCP
server the way the Claude desktop app does (from `/`, with a reduced
environment), calls every tool, runs every recipe on its own sample input,
runs every hook launcher, checks that every command and skill names only tools
that exist, and finally revokes the grant and removes its state. Hosted modes
make real, billed provider calls. Nothing here runs in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "qualixar-jev-decision-layer"
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(RUNTIME))

MODES = {
    "jev": {"provider": "typesafe", "classification": "public", "decision_mode": "jev-public", "laya": False},
    "laya": {"provider": "laya-mlx", "classification": "restricted", "decision_mode": "laya-only", "laya": False},
    "hybrid": {"provider": "typesafe", "classification": "internal-minimized", "decision_mode": "hybrid", "laya": True},
}
DESKTOP_ENV = ("HOME", "PATH", "USER", "LOGNAME", "SHELL")


class McpSession:
    """The shipped launcher over stdio, started like the Claude desktop app starts it."""

    def __init__(self) -> None:
        environment = {name: os.environ[name] for name in DESKTOP_ENV if name in os.environ}
        self.process = subprocess.Popen([str(PLUGIN / "scripts" / "launch-jev")], cwd="/", env=environment,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "jev-e2e", "version": "1"}})

    def request(self, method: str, params: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                             "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("MCP server exited")
            message = json.loads(line)
            if message.get("id") == self.next_id:
                return message
        raise TimeoutError(method)

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        message = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in message:
            return False, message["error"].get("message")
        result = message["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return False, text
        try:
            return True, json.loads(text)
        except ValueError:
            return True, text

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def enroll(workspace: Path, mode: str) -> dict[str, Any]:
    from src.adl.api.setup_controller import SetupChoice
    from src.adl.api.setup_server import build_controller

    shape = MODES[mode]
    choice = SetupChoice(provider=shape["provider"], data_classification=shape["classification"], days=1,
                         daily_calls=500, daily_bytes=5_000_000, generic_query_enabled=True,
                         local_laya_enabled=shape["laya"], decision_mode=shape["decision_mode"])
    controller = build_controller(workspace)
    controller.preview(choice)
    return controller.apply(choice, credential=None, confirmed=True)


def cleanup(workspace: Path) -> None:
    from jev_auto.common import AutoError, home_root, state_dir, workspace_id
    from jev_auto.ipc import request
    from jev_auto.settings import revoke

    for action in (lambda: request(workspace, {"op": "shutdown"}), lambda: revoke(workspace)):
        try:
            action()
        except (AutoError, OSError):
            pass
    target = state_dir(workspace)
    # Remove only the state folder this run created for its own temporary workspace.
    if target.parent == home_root() and target.name == workspace_id(workspace) and target.is_dir():
        shutil.rmtree(target)
    shutil.rmtree(workspace, ignore_errors=True)


def summarize(value: Any) -> Any:
    """Keep a report readable: field names and short decision fields, never whole payloads."""
    if not isinstance(value, dict):
        return str(value)[:160]
    keep = ("selected", "unknown", "trustworthy", "should_abstain", "action", "host_action", "confidence",
            "provider", "status", "calibration_status", "receipt_id", "all_passed", "cases")
    return {key: value[key] for key in keep if key in value} or sorted(value)[:12]


def tool_checks(session: McpSession, workspace: Path, mode: str) -> list[dict[str, Any]]:
    shape = MODES[mode]
    base = {"workspace_path": str(workspace)}
    classification = {"data_classification": shape["classification"]}
    calls: list[tuple[str, dict[str, Any]]] = [
        ("jev_auto_status", base),
        ("jev_recipe_selftest", {}),
        ("jev_route", {**base, **classification, "kind": "task",
                       "task": "Rename one variable across a single file",
                       "candidates": [{"id": "small_model", "description": "Fast model for mechanical edits"},
                                      {"id": "large_model", "description": "Strong model for architecture work"}]}),
        ("jev_verify", {**base, **classification, "source_text": "The meeting is on Tuesday at 10:00 in room B.",
                        "extraction": {"day": "Tuesday", "time": "10:00", "room": "B"}}),
        ("jev_rerank", {**base, **classification, "query": "When is the meeting?",
                        "memories": [{"content": "The meeting is on Tuesday at 10:00."},
                                     {"content": "The office closes at 18:00."}]}),
        ("jev_review_diff", {**base, **classification, "goal": "Find risky changes",
                             "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-timeout = 30\n+timeout = 0\n"}),
        ("jev_typed_decide", {**base, **classification, "provider": shape["provider"],
                              "state": "A customer asks for a refund 40 days after purchase; policy allows 30.",
                              "questions": {"eligible": {"type": "choice",
                                                         "instructions": "Is the refund within policy?",
                                                         "criteria": {"yes": "Within 30 days", "no": "Outside 30 days"}}}}),
        ("jev_prepare", {**base, "goal": "Fix the login timeout"}),
        ("jev_reduce", {**base, "goal": "timeouts", "text": "Line one about lunch.\nThe timeout is 30 seconds.\n"}),
    ]
    results = []
    for name, arguments in calls:
        passed, value = session.call(name, arguments)
        results.append({"check": name, "passed": passed, "result": summarize(value)})
    if mode == "hybrid":
        # Hybrid keeps restricted decisions on this Mac (Laya) and sends the rest to Jev.
        restricted = {"data_classification": "restricted"}
        for name, arguments in calls:
            if "data_classification" in arguments:
                # jev_typed_decide names its provider: a restricted question goes to Laya only when asked.
                local_provider = {"provider": "laya-mlx"} if name == "jev_typed_decide" else {}
                passed, value = session.call(name, {**arguments, **restricted, **local_provider})
                local = isinstance(value, dict) and value.get("provider") == "laya-mlx"
                results.append({"check": name + ":restricted-stays-local", "passed": passed and local,
                                "result": summarize(value)})
    receipt = next((r["result"].get("receipt_id") for r in results
                    if isinstance(r["result"], dict) and r["result"].get("receipt_id")), None)
    if receipt:
        passed, value = session.call("jev_recall", {**base, "receipt_id": receipt})
        results.append({"check": "jev_recall", "passed": passed, "result": summarize(value)})
    return results


def recipe_checks(session: McpSession, workspace: Path, mode: str) -> list[dict[str, Any]]:
    classification = MODES[mode]["classification"]
    results = []
    for path in sorted((ROOT / "recipes").glob("*/*.json")):
        recipe = json.loads(path.read_text())
        passed, value = session.call("jev_recipe_try", {"workspace_path": str(workspace), "recipe_id": recipe["id"],
                                                        "input": recipe["sample"],
                                                        "data_classification": classification})
        results.append({"check": "recipe:" + recipe["id"], "passed": passed, "result": summarize(value)})
    return results


def hook_checks(workspace: Path) -> list[dict[str, Any]]:
    scripts = PLUGIN / "scripts"
    payloads = [
        ("claude:SessionStart", [str(scripts / "launch-claude-hook")],
         {"hook_event_name": "SessionStart", "cwd": str(workspace), "session_id": "e2e", "source": "startup"}),
        ("claude:UserPromptSubmit", [str(scripts / "launch-claude-hook")],
         {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace), "session_id": "e2e",
          "prompt": "Which of two approaches should I take?"}),
        ("claude:SubagentStart", [str(scripts / "launch-claude-hook")],
         {"hook_event_name": "SubagentStart", "cwd": str(workspace), "session_id": "e2e", "agent_type": "general"}),
        ("codex:SessionStart", [sys.executable, str(PLUGIN / "hooks" / "jev_hook.py")],
         {"hook_event_name": "SessionStart", "cwd": str(workspace), "session_id": "e2e"}),
        ("antigravity:PreInvocation", [str(scripts / "launch-agy-hook")],
         {"invocationNum": 0, "workspacePaths": [str(workspace)]}),
        ("hermes:pre_llm_call", [str(scripts / "launch-hermes-hook")],
         {"cwd": str(workspace), "user_message": "Choose a tool"}),
    ]
    results = []
    for name, command, payload in payloads:
        started = time.monotonic()
        process = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                                 cwd=str(workspace), env={**os.environ, "PLUGIN_ROOT": str(PLUGIN),
                                                          "CLAUDE_PLUGIN_ROOT": str(PLUGIN)})
        results.append({"check": "hook:" + name, "passed": process.returncode == 0 and "Traceback" not in process.stderr,
                        "seconds": round(time.monotonic() - started, 2),
                        "result": {"stdout_chars": len(process.stdout), "mentions_workspace": str(workspace) in process.stdout,
                                   "stderr": process.stderr[-200:]}})
    return results


def static_checks(tool_names: set[str]) -> list[dict[str, Any]]:
    results = []
    for path in sorted([*PLUGIN.glob("commands/*.md"), *PLUGIN.glob("skills/*/SKILL.md")]):
        named = set(re.findall(r"\b(jev_[a-z_]+)\b", path.read_text()))
        unknown = sorted(name for name in named if name not in tool_names)
        results.append({"check": "names:" + str(path.relative_to(PLUGIN)), "passed": not unknown,
                        "result": {"tools_named": sorted(named), "unknown": unknown}})
    return results


def run_mode(mode: str) -> dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix=f"jev-e2e-{mode}-"))
    report: dict[str, Any] = {"mode": mode, "checks": []}
    session = None
    try:
        enrolled = enroll(workspace, mode)
        report["checks"].append({"check": "wizard:apply", "passed": True, "result": summarize(enrolled)})
        session = McpSession()
        listed = session.request("tools/list", {})["result"]["tools"]
        names = {tool["name"] for tool in listed}
        report["checks"].append({"check": "tools/list", "passed": len(names) >= 20, "result": {"count": len(names)}})
        report["checks"] += tool_checks(session, workspace, mode)
        report["checks"] += recipe_checks(session, workspace, mode)
        report["checks"] += hook_checks(workspace)
        report["checks"] += static_checks(names)
    except Exception as error:  # a harness failure is itself a failed check
        report["checks"].append({"check": "harness", "passed": False, "result": f"{type(error).__name__}: {error}"[:300]})
    finally:
        if session is not None:
            session.close()
        cleanup(workspace)
    report["passed"] = sum(1 for check in report["checks"] if check["passed"])
    report["failed"] = sum(1 for check in report["checks"] if not check["passed"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--modes", default="jev,laya,hybrid")
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args()
    reports = [run_mode(mode.strip()) for mode in arguments.modes.split(",") if mode.strip()]
    Path(arguments.report).write_text(json.dumps(reports, indent=2) + "\n")
    for report in reports:
        print(f"{report['mode']:7} passed {report['passed']:3}  failed {report['failed']:3}")
        for check in report["checks"]:
            if not check["passed"]:
                print(f"   FAIL {check['check']}: {json.dumps(check['result'])[:220]}")
    return 0 if all(report["failed"] == 0 for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
