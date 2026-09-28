"""Windows-only native broker smoke tests; a skip cannot certify Windows."""
from __future__ import annotations

import os
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))


@unittest.skipUnless(os.name == "nt", "requires native Windows named pipes")
class WindowsPipeNativeTests(unittest.TestCase):
    def test_ctypes_structs_match_win32_abi_layout(self):
        from jev_auto.transports import windows_pipe

        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(windows_pipe._SidAndAttributes.Sid.offset, 0)
        self.assertEqual(windows_pipe._SidAndAttributes.Attributes.offset, pointer)
        self.assertEqual(windows_pipe._TokenGroups.Groups.offset, 8 if pointer == 8 else 4)
        self.assertEqual(windows_pipe._SecurityAttributes.lpSecurityDescriptor.offset,
                         8 if pointer == 8 else 4)
        self.assertEqual(windows_pipe._SecurityAttributes.bInheritHandle.offset,
                         16 if pointer == 8 else 8)

    def test_current_user_and_logon_sid_are_available(self):
        from jev_auto.transports.windows_pipe import current_identity

        user, logon = current_identity()
        self.assertRegex(user, r"^S-1-")
        self.assertRegex(logon, r"^S-1-")
        self.assertNotEqual(user, logon)

    def test_pipe_descriptor_has_protected_logon_acl(self):
        from jev_auto.transports import windows_pipe

        kernel, advapi = windows_pipe._apis()
        user, logon = windows_pipe.current_identity()
        convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)]
        convert.restype = wt.BOOL
        with windows_pipe._pipe_security(user, logon, kernel, advapi) as attrs:
            text_ptr = ctypes.c_void_p()
            self.assertTrue(convert(attrs.lpSecurityDescriptor, 1, 0x5,
                                    ctypes.byref(text_ptr), None))
            try:
                sddl = ctypes.wstring_at(text_ptr.value)
            finally:
                kernel.LocalFree(text_ptr)
        self.assertIn("D:P", sddl)
        self.assertIn(logon, sddl)
        self.assertIn("SY", sddl)
        self.assertNotIn(";;;WD)", sddl)
        self.assertNotIn(";;;AN)", sddl)

    def test_protected_pipe_round_trip_and_shutdown(self):
        from jev_auto.common import AutoError
        from jev_auto.transports import windows_pipe

        name = r"\\.\pipe\qualixar-jev-native-test-" + uuid.uuid4().hex
        stop = threading.Event()

        class Engine:
            def dispatch(self, request):
                if request.get("op") == "health":
                    return {"version": "1.0.0"}
                return {"echo": request}

        failures = []

        def run():
            try:
                windows_pipe.serve(name, Engine(), stop, idle_seconds=10)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            response = None
            for _ in range(30):
                try:
                    response = windows_pipe.request(name, {"op": "health"}, timeout=0.2)
                    break
                except AutoError as error:
                    if str(error) != "BROKER_UNAVAILABLE":
                        raise
                    time.sleep(0.05)
            self.assertEqual(response, {"ok": True, "result": {"version": "1.0.0"}})
            self.assertEqual(windows_pipe.request(name, {"op": "shutdown"}, timeout=2),
                             {"ok": True, "result": {"stopping": True}})
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "server must stop after shutdown")
        self.assertEqual(failures, [])

    def test_oversized_client_payload_is_rejected_before_connect(self):
        from jev_auto.common import AutoError
        from jev_auto.transports import windows_pipe

        with self.assertRaisesRegex(AutoError, "IPC_MESSAGE_SIZE"):
            windows_pipe.request(r"\\.\pipe\qualixar-jev-no-listener", {"payload": "x" * 600_000})


if __name__ == "__main__":
    unittest.main()
