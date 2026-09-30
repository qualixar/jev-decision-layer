"""Install local Laya so the Laya decision modes can be chosen.

The setup wizard offers Laya only when src/adl/api/local_attestor.py verifies
an installation record: an approved, pinned model whose files match their
hashes, run by Jev's own Python environment with the Laya runtime installed
from git at an approved commit. This module builds exactly that, then asks the
attestor to verify it. It never loosens the check: a download that does not
match its pin is refused.

Laya runs on MLX, so it needs an Apple-Silicon Mac with macOS 14 or later,
Python 3.11 or later, and git. The runtime comes from github.com and the model
from huggingface.co, unless the model files are supplied from a folder.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .common import AutoError, home_root, private_dir, write_private

RUNTIME_REPOSITORY = "https://github.com/mizorewww/laya-mlx"
MODELS = {"english": "aac6fef/laya-mlx", "multilingual": "aac6fef/laya-multilingual-mlx"}
MIN_MACOS = 14
MAX_FILES = 64
MAX_TOTAL_BYTES = 2_000_000_000
STEP_TIMEOUT = 1800
# Interpreters tried in order when none is given. /usr/bin/python3 is left out:
# on macOS it is 3.9, and without developer tools it opens an install dialog.
PYTHON_CANDIDATES = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
_DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")

Runner = Callable[..., subprocess.CompletedProcess]


def pin_for(model: str):
    """The approved pin for a model name; unknown names are refused."""
    from src.adl.api.local_attestor import PRODUCTION_PINS

    repository = MODELS.get(model)
    for pin in PRODUCTION_PINS:
        if pin.repository == repository:
            return pin
    raise AutoError("LAYA_MODEL_UNKNOWN")


def paths(model: str, root: Path | None = None) -> dict[str, Path]:
    pin = pin_for(model)
    base = home_root() if root is None else root
    name = pin.repository.split("/", 1)[1]
    return {
        "env": base / "mlx-env",
        "python": base / "mlx-env" / "bin" / "python",
        "models": base / "laya-models",
        "model_dir": base / "laya-models" / name / pin.revision,
        "manifest": base / "laya-models" / f"{name}-{pin.revision}.manifest.json",
        "record": base / "mlx-installation.json",
    }


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def preflight(*, system: Callable[[], str] = platform.system, machine: Callable[[], str] = platform.machine,
              mac_version: Callable[[], str] = lambda: platform.mac_ver()[0],
              which: Callable[[str], str | None] = shutil.which) -> None:
    if system() != "Darwin" or machine() != "arm64":
        raise AutoError("LAYA_REQUIRES_APPLE_SILICON")
    release = mac_version() or ""
    try:
        major = int(release.split(".", 1)[0])
    except ValueError:
        raise AutoError("LAYA_REQUIRES_MACOS_14") from None
    if major < MIN_MACOS:
        raise AutoError("LAYA_REQUIRES_MACOS_14")
    if which("git") is None:
        raise AutoError("LAYA_REQUIRES_GIT")


def _version(python: str, runner: Runner) -> tuple[int, int]:
    try:
        result = runner([python, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                        capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        major, minor = result.stdout.strip().split(".")
        return (int(major), int(minor)) if result.returncode == 0 else (0, 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)


def find_python(explicit: str | None = None, *, runner: Runner = subprocess.run,
                which: Callable[[str], str | None] = shutil.which) -> str:
    """A Python 3.11+ to build the environment with: the one given, else the first found."""
    if explicit is not None:
        candidates: Sequence[str | None] = [explicit]
    else:
        found = which("python3")
        candidates = [*PYTHON_CANDIDATES, found if found != "/usr/bin/python3" else None, sys.executable]
    for candidate in candidates:
        if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK) \
                and _version(candidate, runner) >= (3, 11):
            return candidate
    raise AutoError("PYTHON_3_11_REQUIRED")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's environment (proxies and certificate settings included), minus
    variables that would redirect the new environment's imports."""
    environment = {name: value for name, value in os.environ.items() if name not in _DROPPED_ENV}
    environment.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
    environment.update(extra or {})
    return environment


def _run(command: list[str], code: str, runner: Runner, **options: Any) -> subprocess.CompletedProcess:
    try:
        result = runner(command, capture_output=True, text=True, timeout=STEP_TIMEOUT,
                        stdin=subprocess.DEVNULL, env=_environment(), **options)
    except (OSError, subprocess.SubprocessError):
        raise AutoError(code) from None
    if result.returncode != 0:
        raise AutoError(code)
    return result


def create_environment(python: str, where: Mapping[str, Path], runner: Runner) -> None:
    if where["python"].exists():
        if _version(str(where["python"]), runner) < (3, 11):
            raise AutoError("LAYA_ENVIRONMENT_UNUSABLE")
        return
    private_dir(where["env"].parent)
    # Copies, not links: the attestor refuses any symlink on the interpreter's path.
    _run([python, "-m", "venv", "--copies", str(where["env"])], "LAYA_ENVIRONMENT_FAILED", runner)


def install_runtime(pin, where: Mapping[str, Path], runner: Runner) -> None:
    requirement = f"laya-mlx @ git+{RUNTIME_REPOSITORY}@{pin.runtime_commit}"
    _run([str(where["python"]), "-m", "pip", "install", "--no-input", requirement], "LAYA_RUNTIME_INSTALL_FAILED", runner)


def download_model(pin, where: Mapping[str, Path], runner: Runner) -> Path:
    target = where["model_dir"]
    private_dir(target.parent)
    script = ("import sys\nfrom huggingface_hub import snapshot_download\n"
              "snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3])\n")
    _run([str(where["python"]), "-I", "-c", script, pin.repository, pin.revision, str(target)],
         "LAYA_MODEL_DOWNLOAD_FAILED", runner)
    return target


def adopt_folder(pin, source: Path, where: Mapping[str, Path]) -> Path:
    """Copy supplied model files into Jev's private model folder, resolving links.

    A Hugging Face cache snapshot is a folder of links named after its revision;
    that name must be the pinned revision. When the folder carries the model's
    own manifest.json, every file it lists must match it.
    """
    if not source.is_absolute() or not source.is_dir():
        raise AutoError("LAYA_MODEL_FOLDER_INVALID")
    if source.parent.name == "snapshots" and len(source.name) == 40 and source.name != pin.revision:
        raise AutoError("LAYA_REVISION_MISMATCH")
    own = source / "manifest.json"
    if own.exists():
        import json

        try:
            listed = json.loads(own.read_text(encoding="utf-8")).get("files")
        except (OSError, ValueError, AttributeError):
            raise AutoError("LAYA_MODEL_FOLDER_INVALID") from None
        if not isinstance(listed, dict):
            raise AutoError("LAYA_MODEL_FOLDER_INVALID")
        for name, entry in listed.items():
            expected = entry.get("sha256") if isinstance(entry, dict) else entry
            candidate = source / name
            if not isinstance(expected, str) or ".." in Path(name).parts or not candidate.exists() \
                    or _hash(candidate.resolve()) != expected:
                raise AutoError("LAYA_WEIGHTS_MISMATCH")
    target = where["model_dir"]
    if target.exists():
        raise AutoError("LAYA_MODEL_FOLDER_EXISTS")
    private_dir(target.parent)
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        raise AutoError("LAYA_MODEL_FOLDER_EXISTS")
    for current, folders, names in os.walk(source, followlinks=False):
        folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            origin = (Path(current) / name).resolve()
            if not origin.is_file():
                raise AutoError("LAYA_MODEL_FOLDER_INVALID")
            destination = staging / Path(current).relative_to(source) / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
    os.replace(staging, target)
    return target


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(pin, model_dir: Path) -> dict[str, Any]:
    """Hash every model file; hidden files (the downloader's own cache) are left out."""
    if not model_dir.is_absolute() or not model_dir.is_dir() or model_dir.is_symlink():
        raise AutoError("LAYA_MODEL_FOLDER_INVALID")
    files: dict[str, str] = {}
    total = 0
    for current, folders, names in os.walk(model_dir, followlinks=False):
        folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise AutoError("LAYA_MODEL_FOLDER_INVALID")
            total += path.stat().st_size
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise AutoError("LAYA_MODEL_FOLDER_TOO_LARGE")
            files[path.relative_to(model_dir).as_posix()] = _hash(path)
    if files.get("model.safetensors") != pin.weight_sha256:
        raise AutoError("LAYA_WEIGHTS_MISMATCH")
    return {"schema_version": 1, "repository": pin.repository, "revision": pin.revision, "files": files}


def record_for(pin, where: Mapping[str, Path], model_dir: Path) -> dict[str, Any]:
    return {
        "repository": pin.repository,
        "revision": pin.revision,
        "weight_sha256": pin.weight_sha256,
        "runtime_commit": pin.runtime_commit,
        "python": str(where["python"]),
        "model_dir": str(model_dir),
        "artifact_manifest": str(where["manifest"]),
    }


def install(model: str = "english", *, model_dir: str | None = None, python: str | None = None,
            runner: Runner = subprocess.run, root: Path | None = None,
            attest: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
            check_platform: Callable[[], None] = preflight,
            progress: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    """Build and verify a local Laya installation. Returns the verified record."""
    from src.adl.api.local_attestor import AttestationError, attest_local_config

    pin = pin_for(model)
    check_platform()
    supplied = Path(model_dir) if model_dir is not None else None
    if supplied is not None and not supplied.is_absolute():
        raise AutoError("LAYA_MODEL_FOLDER_INVALID")
    where = paths(model, root)
    progress("Checking for Python 3.11 or later")
    interpreter = find_python(python, runner=runner)
    progress("Preparing Jev's own Python environment")
    create_environment(interpreter, where, runner)
    progress("Installing the pinned Laya runtime from github.com")
    install_runtime(pin, where, runner)
    if supplied is None:
        progress("Downloading the pinned Laya model from huggingface.co")
        folder = download_model(pin, where, runner)
    else:
        progress("Copying the supplied model files into Jev's private folder")
        folder = adopt_folder(pin, supplied, where)
    progress("Checking every model file against its pinned hash")
    manifest = build_manifest(pin, folder)
    record = record_for(pin, where, folder)
    private_dir(where["models"])
    write_private(where["manifest"], manifest)
    try:
        verified = (attest or attest_local_config)(record)
    except AttestationError as error:
        raise AutoError(str(error)) from None
    write_private(where["record"], record)
    progress("Laya is installed and verified")
    return verified
