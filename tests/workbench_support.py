"""Shared fixtures for the recipe workbench coverage tests (not a test module)."""


from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, digest  # noqa: E402
from jev_auto.recipe_workbench import RecipeWorkbench  # noqa: E402
import src.adl.api.recipe_workbench_server as server_module  # noqa: E402
from src.adl.api.recipe_workbench_server import WorkbenchServer, _validate_json_shape, _WorkbenchHandler  # noqa: E402




_RECIPE_ID = "qualixar.brief-fit"
_VALUES = {"query": "A public synthetic project brief.", "candidate": "A short draft."}


def _base_policy(**overrides):
    policy = {
        "schema_version": 1,
        "enabled": True,
        "provider": "typesafe",
        "routes": {},
        "data_classification": "public",
        "decision_mode": "jev-public",
        "generic_query_enabled": True,
        "local_laya_enabled": False,
        "max_calls_per_day": 20,
        "max_bytes_per_day": 100_000,
        "max_request_bytes": 48_000,
        "expires_at": 4_000_000_000,
        "policy_id": "test-policy",
        "case_ids": [],
        "local_recipe_ids": [],
    }
    policy.update(overrides)
    return policy


def _query_mock(**overrides):
    fields = dict(
        provider="typesafe", expected_model="jev-1.13.0", request_sha256="a" * 64,
        payload_json='{"fixture":"payload"}', calibration_status="NOT_EVALUATED",
        policy_sha256="", policy_expires_at=4_000_000_000,
    )
    fields.update(overrides)
    return Mock(**fields)


# ---------------------------------------------------------------------------
# RecipeWorkbench facade -- guard branches not reached by test_recipe_workbench.py
# ---------------------------------------------------------------------------




@contextmanager
def _patched_workbench_asset(overrides):
    """Force stat()/is_symlink()/read_bytes() results for one workbench static file.

    ``overrides`` maps a file name ("index.html", "app.css", "app.js") living
    under .../api/workbench/ to a dict of any of {"stat_size", "is_symlink",
    "read_bytes"}. Anything not matching falls through to the real Path method.
    """
    original_stat = Path.stat
    original_is_symlink = Path.is_symlink
    original_read_bytes = Path.read_bytes

    def _cfg(self):
        return overrides.get(self.name) if self.parent.name == "workbench" else None

    def fake_stat(self, *args, **kwargs):
        cfg = _cfg(self)
        if cfg and "stat_size" in cfg:
            return SimpleNamespace(st_size=cfg["stat_size"])
        return original_stat(self, *args, **kwargs)

    def fake_is_symlink(self):
        cfg = _cfg(self)
        if cfg and "is_symlink" in cfg:
            return cfg["is_symlink"]
        return original_is_symlink(self)

    def fake_read_bytes(self):
        cfg = _cfg(self)
        if cfg and "read_bytes" in cfg:
            return cfg["read_bytes"]
        return original_read_bytes(self)

    with patch.object(Path, "stat", fake_stat), \
            patch.object(Path, "is_symlink", fake_is_symlink), \
            patch.object(Path, "read_bytes", fake_read_bytes):
        yield


class _LiveServerTestCase(unittest.TestCase):
    """Shared loopback server plumbing, modeled on tests/test_recipe_workbench_server.py."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.service = RecipeWorkbench(self.workspace, engine_factory=Mock())
        self.server = WorkbenchServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get_page(self):
        with urlopen(self.origin + "/", timeout=2) as response:
            body = response.read()
            headers = response.headers
            self.cookie = headers.get("Set-Cookie").split(";", 1)[0]
            self.csrf = body.decode().split('name="jev-csrf" content="', 1)[1].split('"', 1)[0]
            return response.status, headers, body

    def get(self, path, *, cookie=None):
        request = Request(self.origin + path, headers={"Cookie": cookie if cookie is not None else self.cookie})
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.headers, response.read()
        except HTTPError as response:
            return response.code, response.headers, response.read()

    def post(self, path, payload, *, origin=None, csrf=None, cookie=None):
        headers = {
            "Content-Type": "application/json",
            "Cookie": self.cookie if cookie is None else cookie,
            "Origin": origin or self.origin,
            "X-CSRF-Token": self.csrf if csrf is None else csrf,
        }
        request = Request(self.origin + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.headers, json.loads(response.read())
        except HTTPError as response:
            body = response.read()
            return response.code, response.headers, json.loads(body) if body else {}


