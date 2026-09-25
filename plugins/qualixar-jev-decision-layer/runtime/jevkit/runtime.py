"""Explicit, workspace-bound runtime boundary for Jev use in Codex."""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .build_mode import OFFLINE_ONLY
from .constants import ADAPTER_VERSION
from .engine import catalog, questions_for, run, spec
from .providers import credential_available, resolve_provider
from .security import SafeError, canonical, private_dir, validate_private_path

GLOBAL_OFFLINE = "global-offline"
GLOBAL_HYBRID = "global-hybrid"
PROJECT_LIVE = "project-live"
OFFLINE_TOOLS = ("jev_health", "jev_policy_status", "jev_policy_check", "jev_catalog", "jev_describe", "jev_run_fixture")
LIVE_TOOLS = OFFLINE_TOOLS + ("jev_evaluate",)
ALLOWED_CLASSIFICATIONS = frozenset({"public", "internal-minimized"})
BLOCKED_CLASSIFICATIONS = frozenset({"restricted", "prohibited"})


def _default_state_root() -> Path:
    """Use only standard-library paths; never persist below the package."""
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "qualixar-jev-decision-layer"
    return Path.home() / ".local" / "state" / "qualixar-jev-decision-layer"


def _inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _workspace_id(workspace_root: Path) -> str:
    """Hash canonical location with filesystem identity where it exists."""
    try:
        metadata = workspace_root.stat()
        filesystem_identity = f"{metadata.st_dev}:{metadata.st_ino}"
    except OSError:
        filesystem_identity = "missing"
    return hashlib.sha256(f"{workspace_root}\0{filesystem_identity}".encode("utf-8")).hexdigest()[:24]


def _revision_fingerprint(workspace_root: Path, workspace_id: str) -> str:
    """Bind a grant to Git HEAD, index, and working-tree status when available."""
    material = [f"workspace:{workspace_id}".encode("utf-8")]
    completed = 0
    for arguments in (
        ("rev-parse", "--verify", "HEAD"),
        ("write-tree",),
        ("status", "--porcelain=v2", "-z", "--untracked-files=all"),
    ):
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_root), *arguments],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            break
        if result.returncode != 0:
            break
        material.append(result.stdout)
        completed += 1
    if completed != 3:
        raise SafeError("WORKSPACE_REVISION_REQUIRED")
    return hashlib.sha256(b"\0".join(material)).hexdigest()


def workspace_binding(workspace: Path) -> dict[str, str]:
    """Resolve a directory to its canonical Git root and immutable review binding."""
    supplied = Path(workspace).expanduser()
    if supplied.is_symlink():
        raise SafeError("WORKSPACE_DIRECTORY_REQUIRED")
    supplied = supplied.resolve()
    if not supplied.is_dir():
        raise SafeError("WORKSPACE_DIRECTORY_REQUIRED")
    try:
        result = subprocess.run(
            ["git", "-C", str(supplied), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SafeError("WORKSPACE_REVISION_REQUIRED") from None
    if result.returncode != 0:
        raise SafeError("WORKSPACE_REVISION_REQUIRED")
    try:
        root = Path(result.stdout.decode("utf-8").strip()).resolve()
    except (UnicodeError, OSError):
        raise SafeError("WORKSPACE_REVISION_REQUIRED") from None
    if not root.is_dir() or not _inside(supplied, root):
        raise SafeError("WORKSPACE_DIRECTORY_REQUIRED")
    workspace_id = _workspace_id(root)
    return {
        "workspace_path": str(root),
        "workspace_id": workspace_id,
        "revision": _revision_fingerprint(root, workspace_id),
    }


@dataclass(frozen=True)
class RuntimeContext:
    """Globally discoverable offline tools plus explicitly bound live tools."""

    package_root: Path
    state_root: Path | None = None
    scope: str = GLOBAL_OFFLINE
    workspace_root: Path | None = None
    workspace_id: str | None = field(init=False, default=None)
    revision: str | None = field(init=False, default=None)
    grant_root: Path = field(init=False)
    evidence_root: Path = field(init=False)

    def __post_init__(self) -> None:
        package_root = Path(self.package_root).resolve()
        supplied_state_root = self.state_root
        explicit_state = supplied_state_root is not None
        if supplied_state_root is not None:
            raw_state_root = Path(supplied_state_root).expanduser()
            validate_private_path(raw_state_root)
            state_root = raw_state_root.resolve()
        else:
            state_root = _default_state_root().resolve()
        if self.scope not in (GLOBAL_OFFLINE, GLOBAL_HYBRID, PROJECT_LIVE):
            raise SafeError("INVALID_RUNTIME_SCOPE")
        if self.scope in (GLOBAL_HYBRID, PROJECT_LIVE) and OFFLINE_ONLY:
            raise SafeError("LIVE_RUNTIME_NOT_AVAILABLE")
        if explicit_state and _inside(state_root, package_root):
            raise SafeError("STATE_ROOT_INSIDE_PACKAGE")
        if self.scope == PROJECT_LIVE and self.workspace_root is None:
            raise SafeError("WORKSPACE_ID_REQUIRED")

        workspace_root = None
        workspace_id = None
        revision = None
        if self.scope == PROJECT_LIVE:
            supplied_workspace_root = self.workspace_root
            if supplied_workspace_root is None:
                raise SafeError("WORKSPACE_ID_REQUIRED")
            binding = workspace_binding(Path(supplied_workspace_root))
            workspace_root = Path(binding["workspace_path"])
            workspace_id = binding["workspace_id"]
            revision = binding["revision"]
            state_root = state_root / "workspaces" / workspace_id

        private_dir(state_root)
        grant_root = state_root / "grants"
        evidence_root = state_root / "evidence"
        private_dir(grant_root)
        private_dir(evidence_root)
        object.__setattr__(self, "package_root", package_root)
        object.__setattr__(self, "state_root", state_root)
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "grant_root", grant_root)
        object.__setattr__(self, "evidence_root", evidence_root)

    def tool_names(self) -> tuple[str, ...]:
        return LIVE_TOOLS if self.scope in (GLOBAL_HYBRID, PROJECT_LIVE) else OFFLINE_TOOLS

    def health(self) -> dict[str, Any]:
        base = {"service": "qualixar-jev-decision-layer", "scope": self.scope, "default": "fixture",
                "native_codex_interception": False, "production_side_effects": False,
                "model_configuration_modified": False, "adapter_version": ADAPTER_VERSION}
        if self.scope == GLOBAL_OFFLINE:
            return {**base, "credential_checked": False, "live_grant_checked": False}
        try:
            provider = resolve_provider()
            provider_status = {
                "provider_configured": True,
                "provider_id": provider.provider_id,
                "model": provider.model,
                "credential_available": credential_available(provider),
            }
        except SafeError:
            provider_status = {
                "provider_configured": False,
                "provider_id": None,
                "model": None,
                "credential_available": False,
            }
        if self.scope == GLOBAL_HYBRID:
            return {**base, **provider_status, "live_grant_checked": False}
        return {**base, "workspace_id": self.workspace_id, "revision": self.revision,
                **provider_status, "live_grant": self.read_live_grant()}

    def describe(self, case_id: str) -> dict:
        return spec(case_id, self.package_root)

    def catalog(self) -> list[dict]:
        return catalog(self.package_root)

    def run_fixture(self, case_id: str, variant: str = "nominal") -> dict:
        return run(case_id, variant=variant, root=self.package_root,
                   state_root=self.evidence_root, workspace_id=self.workspace_id,
                   revision=self.revision, adapter_version=ADAPTER_VERSION,
                   persist=self.scope == PROJECT_LIVE)

    def evaluate(self, case_id: str, state: dict | None = None, *, request_id: str | None = None,
                 data_classification: str | None = None,
                 workspace_root: Path | None = None) -> dict:
        if self.scope == GLOBAL_HYBRID:
            if workspace_root is None:
                raise SafeError("WORKSPACE_ID_REQUIRED")
            bound = RuntimeContext(
                self.package_root, self.state_root, PROJECT_LIVE, workspace_root
            )
            return bound.evaluate(
                case_id,
                state,
                request_id=request_id,
                data_classification=data_classification,
            )
        self._require_live_scope()
        self._assert_workspace_current()
        if state is not None:
            classification = self._validate_classification(data_classification)
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
                raise SafeError("REQUEST_ID_REQUIRED")
            questions = questions_for(spec(case_id, self.package_root), state)
            request_sha256 = self._request_hash(state, questions)
        else:
            classification = "synthetic"
            request_sha256 = None
        return run(case_id, mode="live", state=state, root=self.package_root,
                   state_root=self.evidence_root, grant_root=self.grant_root,
                   workspace_id=self.workspace_id, revision=self.revision,
                   adapter_version=ADAPTER_VERSION, request_id=request_id,
                   data_classification=classification, request_sha256=request_sha256)

    def create_live_grant(self, calls: int, minutes: int, allow_custom: bool = False, *,
                          case_id: str | None = None, state: dict | None = None,
                          request_id: str | None = None,
                          data_classification: str | None = None) -> dict:
        self._require_live_scope()
        self._assert_workspace_current()
        request_sha256 = None
        if allow_custom:
            classification = self._validate_classification(data_classification)
            if not isinstance(case_id, str) or state is None:
                raise SafeError("REQUEST_GRANT_REQUIRED")
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
                raise SafeError("REQUEST_ID_REQUIRED")
            request_sha256 = self._request_hash(state, questions_for(spec(case_id, self.package_root), state))
        else:
            classification = None
        return self._budget().grant(calls, minutes, allow_custom, case_id=case_id,
                                    data_classification=classification, request_id=request_id,
                                    request_sha256=request_sha256)

    def read_live_grant(self) -> dict:
        self._require_live_scope()
        self._assert_workspace_current()
        return self._budget().status()

    def revoke_live_grant(self) -> None:
        self._require_live_scope()
        self._budget().revoke()

    def _budget(self):
        from .authorization import CallBudget
        provider = resolve_provider()
        return CallBudget(self.package_root, storage_root=self.grant_root,
                          workspace_id=self.workspace_id, revision=self.revision,
                          provider_id=provider.provider_id,
                          provider_profile_sha256=provider.profile_sha256)

    def _assert_workspace_current(self) -> None:
        if self.workspace_root is None or self.workspace_id is None or self.revision is None:
            raise SafeError("WORKSPACE_ID_REQUIRED")
        current_workspace_id = _workspace_id(self.workspace_root)
        current_revision = _revision_fingerprint(self.workspace_root, current_workspace_id)
        if current_workspace_id != self.workspace_id or current_revision != self.revision:
            raise SafeError("WORKSPACE_REVISION_CHANGED")

    @staticmethod
    def _request_hash(state: dict, questions: dict) -> str:
        model = resolve_provider().model
        return hashlib.sha256(canonical({"model": model, "state": state,
                                         "questions": questions})).hexdigest()

    @staticmethod
    def _validate_classification(classification: str | None) -> str:
        if classification is None:
            raise SafeError("CLASSIFICATION_REQUIRED")
        if classification in BLOCKED_CLASSIFICATIONS or classification not in ALLOWED_CLASSIFICATIONS:
            raise SafeError("DATA_CLASSIFICATION_NOT_ALLOWED")
        return classification

    def _require_live_scope(self) -> None:
        if self.scope != PROJECT_LIVE:
            raise SafeError("LIVE_SCOPE_REQUIRED")


def revoke_workspace_grant(package_root: Path, workspace_root: Path,
                           state_root: Path | None = None) -> None:
    """Revoke a workspace ledger even when its reviewed revision is stale."""
    context = RuntimeContext(package_root, state_root, PROJECT_LIVE, workspace_root)
    context.revoke_live_grant()
