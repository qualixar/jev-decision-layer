"""The failure branches of this release's new safety code, each driven directly.

Every branch here is a refusal or a fallback: a link owned by someone else,
an unreadable folder, a damaged socket-folder record, a busy service that
cannot even read the request, a grant walk that hits an unreadable choice.
Each must fail closed, and each is reached on purpose rather than by luck.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto import common, ipc, server, settings  # noqa: E402
from jev_auto.common import AutoError, canonical, read_private, write_private  # noqa: E402
from jev_auto.settings import DESCENDANT_COVERAGE_APPROVED, make_policy, save_policy_new  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        common._ROOTS.clear()
        self.addCleanup(common._ROOTS.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)


class LinkTrustTests(_Tmp):
    def link(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        return link

    def test_a_link_owned_by_another_account_is_not_trusted(self):
        link = self.link()
        real = os.lstat(link)
        other = SimpleNamespace(st_uid=os.getuid() + 4242, st_mode=real.st_mode)
        with patch.object(common.os, "lstat", return_value=other):
            self.assertFalse(common._link_is_trusted(link))

    def test_a_link_that_cannot_be_inspected_is_not_trusted(self):
        self.assertFalse(common._link_is_trusted(self.root / "missing" / "link"))

    def test_links_are_never_trusted_where_ownership_cannot_be_checked(self):
        link = self.link()
        with patch.object(common.os, "name", "nt"):
            self.assertFalse(common._link_is_trusted(link))

    def test_a_private_file_larger_than_its_limit_is_refused(self):
        path = self.root / "private" / "big.json"
        write_private(path, {"text": "x" * 200})
        with self.assertRaises(AutoError) as caught:
            read_private(path, 50)
        self.assertEqual(str(caught.exception), "UNSAFE_PRIVATE_FILE")

    def test_a_git_failure_leaves_the_folder_as_its_own_workspace(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch.object(common.subprocess, "run", side_effect=OSError("git missing")):
            self.assertEqual(common.workspace(repo), repo)

    def test_the_remembered_roots_are_cleared_when_full(self):
        for index in range(common._ROOT_ENTRIES):
            common._ROOTS[f"/synthetic/{index}"] = (common.time.monotonic(), None)
        common.workspace(self.root)
        self.assertEqual(list(common._ROOTS), [str(self.root)])

    def test_non_finite_json_is_refused(self):
        with self.assertRaises(AutoError) as caught:
            canonical({"x": float("nan")})
        self.assertEqual(str(caught.exception), "INVALID_FINITE_JSON")


class SocketFolderRecordTests(_Tmp):
    def record(self):
        return common.home_root() / "socket-folder.json"

    def test_a_damaged_record_is_refused_not_trusted(self):
        common.private_dir(common.home_root())
        write_private(self.record(), {"leaf": "../elsewhere"})
        with self.assertRaises(AutoError) as caught:
            ipc._private_leaf(os.getuid())
        self.assertEqual(str(caught.exception), "SOCKET_FOLDER_RECORD_INVALID")

    def test_a_record_written_by_a_parallel_process_is_used(self):
        leaf = f"qualixar-jev-{os.getuid()}-{'a' * 16}"
        reads = iter([FileNotFoundError(), {"leaf": leaf}])

        def read(*_args, **_kwargs):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

        with patch("jev_auto.common.read_private", side_effect=read), \
             patch("jev_auto.platform_fs.atomic_write_private", side_effect=AutoError("WORKSPACE_ALREADY_ENROLLED")):
            self.assertEqual(ipc._private_leaf(os.getuid()), leaf)

    def test_a_record_that_never_appears_is_refused(self):
        with patch("jev_auto.common.read_private", side_effect=FileNotFoundError()), \
             patch("jev_auto.platform_fs.atomic_write_private", side_effect=AutoError("WORKSPACE_ALREADY_ENROLLED")):
            with self.assertRaises(AutoError) as caught:
                ipc._private_leaf(os.getuid())
        self.assertEqual(str(caught.exception), "SOCKET_FOLDER_RECORD_INVALID")

    def test_any_other_write_failure_is_reported(self):
        with patch("jev_auto.common.read_private", side_effect=FileNotFoundError()), \
             patch("jev_auto.platform_fs.atomic_write_private", side_effect=AutoError("POLICY_WRITE_FAILED")):
            with self.assertRaises(AutoError) as caught:
                ipc._private_leaf(os.getuid())
        self.assertEqual(str(caught.exception), "POLICY_WRITE_FAILED")


class ClientTests(unittest.TestCase):
    def test_an_out_of_range_provider_timeout_falls_back_to_the_default_wait(self):
        for value in (0, 31, True, "12"):
            with self.subTest(value=value), patch.object(ipc, "load_policy", return_value={"timeout_seconds": value}):
                self.assertEqual(ipc.call_timeout(Path("/x")), 16)

    def test_a_connection_reset_after_sending_is_unavailable(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as folder:
            addr = Path(folder) / "b.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(addr))
            os.chmod(addr, 0o600)
            listener.listen(1)
            held = []  # keep the accepted end open, so only the read can fail
            accepted = threading.Event()
            threading.Thread(target=lambda: (held.append(listener.accept()), accepted.set()), daemon=True).start()
            sent = []
            real_exchange_read = ConnectionResetError()

            def read(sock):
                accepted.wait(2)
                sent.append(True)
                raise real_exchange_read

            try:
                with patch.object(ipc, "read_message", side_effect=read):
                    with self.assertRaises(AutoError) as caught:
                        ipc._exchange(addr, b"{}\n", 1)
            finally:
                for connection, _ in held:
                    connection.close()
                listener.close()
        self.assertEqual(str(caught.exception), "BROKER_UNAVAILABLE")
        self.assertEqual(sent, [True], "the request was sent; only the read failed")


class BusyRefusalTests(unittest.TestCase):
    def make(self, folder):
        srv = server.Server(Path(folder) / "s.sock", SimpleNamespace(dispatch=lambda r: {}))
        self.addCleanup(srv.server_close)
        return srv

    def test_with_no_room_even_to_refuse_the_connection_is_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            srv = self.make(folder)
            srv.slots._value = 0
            srv.refusals._value = 0
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            with b:
                b.settimeout(2)
                srv.process_request(a, ("peer",))
                self.assertEqual(b.recv(100), b"")

    def test_a_client_that_hangs_up_is_refused_quietly(self):
        with tempfile.TemporaryDirectory() as folder:
            srv = self.make(folder)
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            b.close()
            srv.refusals.acquire()
            srv._refuse(a)  # must not raise
            self.assertTrue(srv.refusals.acquire(False))


class GrantWalkTests(_Tmp):
    def test_a_file_path_finds_the_grant_through_its_folder(self):
        granted = self.root / "granted"
        (granted / "sub").mkdir(parents=True)
        save_policy_new(granted, make_policy(granted, "typesafe", covers_descendants=True,
                                             descendant_approval=DESCENDANT_COVERAGE_APPROVED))
        target = granted / "sub" / "notes.txt"
        target.write_text("x")
        self.assertEqual(settings._inherited_root(target), common.workspace(granted))


    def test_an_unusable_query_path_has_no_covering_grant(self):
        with patch.object(settings, "trusted_path", side_effect=AutoError("SYMLINK_NOT_ALLOWED")):
            self.assertIsNone(settings._inherited_root(self.root))

    def test_an_unreadable_choice_on_the_way_fails_closed(self):
        granted = self.root / "granted"
        inner = granted / "client" / "app"
        inner.mkdir(parents=True)
        save_policy_new(granted, make_policy(granted, "typesafe", covers_descendants=True,
                                             descendant_approval=DESCENDANT_COVERAGE_APPROVED))
        real = settings._load_exact
        client = (granted / "client").resolve()

        def load(path, base=None):
            if Path(path).resolve() == client:
                raise PermissionError("synthetic")
            return real(path, base)

        with patch.object(settings, "_load_exact", side_effect=load):
            with self.assertRaises(AutoError) as caught:
                settings._inherited_root(inner)
        self.assertEqual(str(caught.exception), "AUTO_DISABLED_OR_EXPIRED")

    def test_a_parallel_refusal_that_landed_first_is_accepted(self):
        folder = self.root / "folder"
        folder.mkdir()
        refusal = {**make_policy(folder, "typesafe", days=1), "enabled": False, "consent": "refused"}
        write_private(common.state_dir(folder) / "policy.json", refusal)
        settings._write_descendant_refusal(folder)  # must not raise

    def test_a_revoke_whose_walk_fails_for_another_reason_reports_it(self):
        folder = self.root / "plain"
        folder.mkdir()
        with patch.object(settings, "_inherited_root", side_effect=AutoError("POLICY_LOCK_UNSAFE")):
            with self.assertRaises(AutoError) as caught:
                settings.revoke(folder)
        self.assertEqual(str(caught.exception), "POLICY_LOCK_UNSAFE")


if __name__ == "__main__":
    unittest.main()
