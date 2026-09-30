"""XDG base directories: an empty or relative value is ignored, never used.

The XDG Base Directory specification: "If an implementation encounters a
relative path in any of these variables it should consider the path invalid
and ignore it." Using one would write Jev's configuration or state into
whatever folder a host happened to start in.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import cli  # noqa: E402
from jevkit import policy_mode, providers, runtime  # noqa: E402

HOME = Path("/home/person")


class XdgTests(unittest.TestCase):
    CASES = (
        ("XDG_CONFIG_HOME", lambda: cli._config_root(), HOME / ".config"),
        ("XDG_CONFIG_HOME", lambda: policy_mode._config_root(), HOME / ".config"),
        ("XDG_CONFIG_HOME", lambda: providers.default_config_root(), HOME / ".config"),
        ("XDG_STATE_HOME", lambda: runtime._default_state_root(), HOME / ".local" / "state"),
    )

    def _run(self, variable, function, value):
        environment = {"HOME": str(HOME)}
        if value is not None:
            environment[variable] = value
        with patch.dict(os.environ, environment, clear=True), patch.object(Path, "home", return_value=HOME):
            return function()

    def test_an_absolute_value_is_used(self):
        for variable, function, _default in self.CASES:
            with self.subTest(variable=variable, function=function):
                self.assertEqual(self._run(variable, function, "/custom/base"),
                                 Path("/custom/base/qualixar-jev-decision-layer"))

    def test_a_missing_empty_or_relative_value_falls_back_to_the_default(self):
        for variable, function, default in self.CASES:
            for value in (None, "", "relative/base", "./x"):
                with self.subTest(variable=variable, value=value):
                    self.assertEqual(self._run(variable, function, value), default / "qualixar-jev-decision-layer")

    def test_a_home_relative_value_is_expanded(self):
        for variable, function, _default in self.CASES:
            with self.subTest(variable=variable):
                self.assertEqual(self._run(variable, function, "~/base"),
                                 HOME / "base" / "qualixar-jev-decision-layer")


if __name__ == "__main__":
    unittest.main()
