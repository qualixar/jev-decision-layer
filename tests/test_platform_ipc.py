"""Native-peer and private-state checks for the portable broker boundary."""
from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))
SHORT_TMP = "/private/tmp" if sys.platform == "darwin" else "/tmp"

from jev_auto import ipc, platform_fs, settings  # noqa: E402
from jev_auto.common import AutoError  # noqa: E402


class PeerIdentityTests(unittest.TestCase):
    def test_native_linux_peer_identity(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("requires Linux SO_PEERCRED")
        a, b = socket.socketpair()
        try:
            ipc.authenticate_unix_peer(a)
            ipc.authenticate_unix_peer(b)
        finally:
            a.close()
            b.close()

    def test_native_macos_peer_identity(self):
        if sys.platform != "darwin":
            self.skipTest("requires macOS getpeereid")
        a, b = socket.socketpair()
        try:
            ipc.authenticate_unix_peer(a)
            ipc.authenticate_unix_peer(b)
        finally:
            a.close()
            b.close()

    @unittest.skipIf(os.name == "nt", "POSIX peer API")
    def test_linux_peer_uid_must_match(self):
        class FakeSocket:
            def getsockopt(self, *_):
                return struct.pack("3i", 123, os.geteuid() + 1, 123)

        with patch.object(ipc.sys, "platform", "linux"), patch.object(socket, "SO_PEERCRED", 17, create=True):
            with self.assertRaisesRegex(AutoError, "IPC_PEER_UID"):
                ipc.authenticate_unix_peer(FakeSocket())

    @unittest.skipIf(os.name == "nt", "POSIX peer API")
    def test_linux_unavailable_kernel_credentials_fail_closed(self):
        class FakeSocket:
            def getsockopt(self, *_):
                raise OSError("synthetic kernel failure")

        with patch.object(ipc.sys, "platform", "linux"), patch.object(socket, "SO_PEERCRED", 17, create=True):
            with self.assertRaisesRegex(AutoError, "IPC_PEER_UNVERIFIED"):
                ipc.authenticate_unix_peer(FakeSocket())

    @unittest.skipIf(os.name == "nt", "POSIX peer API")
    def test_other_posix_without_peer_api_fails_closed(self):
        with patch.object(ipc.sys, "platform", "freebsd"):
            with self.assertRaisesRegex(AutoError, "IPC_PEER_UNVERIFIED"):
                ipc.authenticate_unix_peer(object())


class PlatformStateTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX runtime directory")
    def test_linux_uses_verified_xdg_runtime_dir(self):
        with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            with patch.object(ipc.sys, "platform", "linux"), patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                endpoint = ipc.address(workspace, root / "state")
            self.assertEqual(endpoint.parent.parent, root)

    @unittest.skipIf(os.name == "nt", "POSIX runtime directory")
    def test_world_readable_xdg_runtime_dir_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=SHORT_TMP) as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            root.chmod(0o755)
            try:
                with patch.object(ipc.sys, "platform", "linux"), patch.object(ipc.tempfile, "gettempdir", return_value=SHORT_TMP), patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                    endpoint = ipc.address(workspace, root / "state")
                self.assertNotEqual(endpoint.parent.parent, root)
            finally:
                root.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_private_lock_rejects_broad_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock"
            lock.write_text("x")
            lock.chmod(0o644)
            with self.assertRaisesRegex(AutoError, "UNSAFE_PRIVATE_FILE"):
                with platform_fs.file_lock(lock):
                    pass

    def test_windows_native_transport_rejects_on_non_windows_host(self):
        if os.name == "nt":
            self.skipTest("native Windows integration runs in the Windows CI job")
        from jev_auto.transports import windows_pipe
        with self.assertRaisesRegex(AutoError, "WINDOWS_PIPE_PLATFORM"):
            windows_pipe.current_identity()

    @unittest.skipUnless(os.name == "nt", "native Windows ACL and lock integration")
    def test_windows_policy_round_trip_uses_private_acl_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            base = root / "state"
            policy = settings.make_policy(workspace, "typesafe", credential_store="os")
            settings.save_policy_new(workspace, policy, base)
            self.assertEqual(settings.load_policy(workspace, base), policy)
            platform_fs.verify_private_dir(settings.state_dir(workspace, base))

    def test_new_os_store_policy_literal_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            policy = settings.make_policy(workspace, "typesafe", credential_store="os")
            self.assertEqual(settings.validate_policy(policy, workspace)["credential_store"], "os")


if __name__ == "__main__":
    unittest.main()
