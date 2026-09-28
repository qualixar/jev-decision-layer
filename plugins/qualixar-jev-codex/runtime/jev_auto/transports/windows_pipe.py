"""Windows local named-pipe transport with logon-SID ACL and token checks.

This module is imported only on Windows. Native CI must exercise its ACL,
client impersonation, and same-session server identity before release.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from ..common import AutoError, canonical, decode

MAX = 512_000
_TOKEN_QUERY = 0x0008
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SE_GROUP_LOGON_ID = 0xC0000000
_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_TYPE_MESSAGE = 0x00000004
_PIPE_READMODE_MESSAGE = 0x00000002
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _TokenGroups(ctypes.Structure):
    _fields_ = [("GroupCount", wt.DWORD), ("Groups", _SidAndAttributes * 1)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


def _apis():
    if os.name != "nt":
        raise AutoError("WINDOWS_PIPE_PLATFORM")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    funcs = [
        (kernel.GetCurrentProcess, [], wt.HANDLE),
        (kernel.GetCurrentThread, [], wt.HANDLE),
        (kernel.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        (kernel.CloseHandle, [wt.HANDLE], wt.BOOL),
        (kernel.LocalFree, [ctypes.c_void_p], ctypes.c_void_p),
        (kernel.CreateNamedPipeW, [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                   wt.DWORD, wt.DWORD, wt.DWORD,
                                   ctypes.POINTER(_SecurityAttributes)], wt.HANDLE),
        (kernel.CreateFileW, [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                              wt.DWORD, wt.DWORD, wt.HANDLE], wt.HANDLE),
        (kernel.GetNamedPipeServerProcessId, [wt.HANDLE, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (kernel.GetNamedPipeClientProcessId, [wt.HANDLE, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (advapi.OpenProcessToken, [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)], wt.BOOL),
        (advapi.OpenThreadToken, [wt.HANDLE, wt.DWORD, wt.BOOL, ctypes.POINTER(wt.HANDLE)], wt.BOOL),
        (advapi.GetTokenInformation, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wt.DWORD, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (advapi.ConvertSidToStringSidW, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], wt.BOOL),
        (advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW,
         [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (advapi.ImpersonateNamedPipeClient, [wt.HANDLE], wt.BOOL),
        (advapi.RevertToSelf, [], wt.BOOL),
    ]
    for function, args, result in funcs:
        function.argtypes = args
        function.restype = result
    return kernel, advapi


def _sid_string(sid, kernel, advapi):
    if not sid:
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    pointer = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(pointer)):
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    try:
        return ctypes.wstring_at(pointer.value)
    finally:
        kernel.LocalFree(pointer)


def _token_buffer(token, kind, advapi):
    needed = wt.DWORD()
    advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(needed))
    if not 0 < needed.value <= 65536:
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetTokenInformation(token, kind, buffer, needed.value, ctypes.byref(needed)):
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    return buffer


def _token_identity(token, kernel, advapi):
    user_buffer = _token_buffer(token, 1, advapi)
    user_sid = ctypes.cast(user_buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
    user = _sid_string(user_sid, kernel, advapi)
    group_buffer = _token_buffer(token, 2, advapi)
    groups = ctypes.cast(group_buffer, ctypes.POINTER(_TokenGroups)).contents
    if groups.GroupCount > 2048:
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    first_group = ctypes.addressof(groups.Groups)
    group_array = ctypes.cast(first_group, ctypes.POINTER(_SidAndAttributes))
    logons = [_sid_string(group_array[i].Sid, kernel, advapi)
              for i in range(groups.GroupCount)
              if group_array[i].Attributes & _SE_GROUP_LOGON_ID == _SE_GROUP_LOGON_ID]
    if len(logons) != 1:
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    return user, logons[0]


def current_identity():
    kernel, advapi = _apis()
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise AutoError("WINDOWS_PIPE_IDENTITY_UNVERIFIED")
    try:
        return _token_identity(token, kernel, advapi)
    finally:
        kernel.CloseHandle(token)


def _client_identity(pipe, kernel, advapi):
    # PID is secondary metadata only. Token SIDs below make the decision.
    pid = wt.DWORD()
    if not kernel.GetNamedPipeClientProcessId(pipe, ctypes.byref(pid)) or not pid.value:
        raise AutoError("WINDOWS_PIPE_CLIENT_UNVERIFIED")
    if not advapi.ImpersonateNamedPipeClient(pipe):
        raise AutoError("WINDOWS_PIPE_CLIENT_UNVERIFIED")
    try:
        token = wt.HANDLE()
        if not advapi.OpenThreadToken(kernel.GetCurrentThread(), _TOKEN_QUERY, True, ctypes.byref(token)):
            raise AutoError("WINDOWS_PIPE_CLIENT_UNVERIFIED")
        try:
            return _token_identity(token, kernel, advapi)
        finally:
            kernel.CloseHandle(token)
    finally:
        if not advapi.RevertToSelf():
            raise AutoError("WINDOWS_PIPE_CLIENT_UNVERIFIED")


def _server_identity(pipe, kernel, advapi):
    pid = wt.DWORD()
    if not kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(pid)):
        raise AutoError("WINDOWS_PIPE_SERVER_UNVERIFIED")
    process = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        raise AutoError("WINDOWS_PIPE_SERVER_UNVERIFIED")
    try:
        token = wt.HANDLE()
        if not advapi.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            raise AutoError("WINDOWS_PIPE_SERVER_UNVERIFIED")
        try:
            return _token_identity(token, kernel, advapi)
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(process)


@contextmanager
def _pipe_security(user_sid, logon_sid, kernel, advapi):
    # Protected DACL: only this logon session and LocalSystem may open pipe.
    sddl = f"O:{user_sid}G:{user_sid}D:P(A;;GA;;;SY)(A;;GA;;;{logon_sid})"
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise AutoError("WINDOWS_PIPE_ACL_UNVERIFIED")
    try:
        attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        yield attrs
    finally:
        kernel.LocalFree(descriptor)


def _new_instance(address, first, kernel, advapi, user_sid, logon_sid):
    with _pipe_security(user_sid, logon_sid, kernel, advapi) as attrs:
        flags = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
        if first:
            flags |= _FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = kernel.CreateNamedPipeW(
            address, flags,
            _PIPE_TYPE_MESSAGE | _PIPE_READMODE_MESSAGE | _PIPE_REJECT_REMOTE_CLIENTS,
            9, 65536, 65536, 0, ctypes.byref(attrs))
    if not handle or handle == _INVALID_HANDLE:
        raise AutoError("WINDOWS_PIPE_INSTANCE_UNAVAILABLE")
    return handle


def request(address, payload, timeout=16):
    """Send one bounded request and authenticate the connected server."""
    import _winapi
    from multiprocessing.connection import PipeConnection

    data = canonical(payload) + b"\n"
    if len(data) > MAX:
        raise AutoError("IPC_MESSAGE_SIZE")
    kernel, advapi = _apis()
    expected = current_identity()
    handle = None
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutoError("BROKER_UNAVAILABLE")
            try:
                _winapi.WaitNamedPipe(address, max(1, min(1000, int(remaining * 1000))))
            except OSError as error:
                if getattr(error, "winerror", None) in (121, 231) and time.monotonic() < deadline:
                    continue  # ERROR_SEM_TIMEOUT / ERROR_PIPE_BUSY
                raise AutoError("BROKER_UNAVAILABLE") from None
            handle = kernel.CreateFileW(address, _GENERIC_READ | _GENERIC_WRITE, 0,
                                        None, _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
            if handle and handle != _INVALID_HANDLE:
                break
            if ctypes.get_last_error() != 231:  # ERROR_PIPE_BUSY
                raise AutoError("BROKER_UNAVAILABLE")
        if _server_identity(handle, kernel, advapi) != expected:
            raise AutoError("WINDOWS_PIPE_SERVER_UNVERIFIED")
        _winapi.SetNamedPipeHandleState(handle, _PIPE_READMODE_MESSAGE, None, None)
        connection = PipeConnection(handle)
        handle = None  # PipeConnection now owns the OS handle.
        with connection:
            connection.send_bytes(data)
            if not connection.poll(timeout):
                raise AutoError("BROKER_UNAVAILABLE")
            response = connection.recv_bytes(MAX)
        if not response.endswith(b"\n"):
            raise AutoError("INVALID_JSON")
        return decode(response[:-1], MAX)
    except AutoError:
        raise
    except (OSError, EOFError, ValueError):
        raise AutoError("BROKER_UNAVAILABLE") from None
    finally:
        if handle and handle != _INVALID_HANDLE:
            kernel.CloseHandle(handle)


def _handle_client(handle, engine, stopping, expected, kernel, advapi):
    from multiprocessing.connection import PipeConnection

    connection = PipeConnection(handle)
    with connection:
        try:
            if not connection.poll(20):
                raise AutoError("IPC_TIMEOUT")
            raw = connection.recv_bytes(MAX)
            # Impersonation describes the last message read on this pipe.
            if _client_identity(handle, kernel, advapi) != expected:
                raise AutoError("WINDOWS_PIPE_CLIENT_UNVERIFIED")
            if not raw.endswith(b"\n"):
                raise AutoError("INVALID_JSON")
            message = decode(raw[:-1], MAX)
            if not isinstance(message, dict):
                raise AutoError("IPC_REQUEST_INVALID")
            if message.get("op") == "shutdown":
                stopping.set()
                result = {"stopping": True}
            else:
                result = engine.dispatch(message)
            out = {"ok": True, "result": result}
        except AutoError as error:
            out = {"ok": False, "error": str(error)}
        except Exception:
            out = {"ok": False, "error": "BROKER_INTERNAL_ERROR"}
        try:
            wire = canonical(out) + b"\n"
            if len(wire) > MAX:
                wire = canonical({"ok": False, "error": "IPC_MESSAGE_SIZE"}) + b"\n"
            connection.send_bytes(wire)
        except (OSError, EOFError):
            pass


def serve(address, engine, stopping, idle_seconds=900):
    """Accept at most eight concurrent clients; stop after idle timeout."""
    import _winapi

    kernel, advapi = _apis()
    expected = current_identity()
    slots = threading.BoundedSemaphore(8)
    last_activity = time.monotonic()
    first = True
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="jev-pipe") as pool:
        while not stopping.is_set() and time.monotonic() - last_activity < idle_seconds:
            handle = _new_instance(address, first, kernel, advapi, *expected)
            first = False
            try:
                try:
                    overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
                except OSError as error:
                    if getattr(error, "winerror", None) == 535:  # ERROR_PIPE_CONNECTED
                        overlapped = None
                        connected = True
                    elif getattr(error, "winerror", None) == 232:  # ERROR_NO_DATA
                        continue
                    else:
                        raise AutoError("WINDOWS_PIPE_CONNECT_FAILED") from None
                else:
                    connected = False
                if overlapped is not None:
                    try:
                        while not stopping.is_set() and time.monotonic() - last_activity < idle_seconds:
                            wait = _winapi.WaitForMultipleObjects([overlapped.event], False, 250)
                            if wait == 0:
                                _, error = overlapped.GetOverlappedResult(True)
                                if error:
                                    raise AutoError("WINDOWS_PIPE_CONNECT_FAILED")
                                connected = True
                                break
                            if wait != 258:  # WAIT_TIMEOUT
                                raise AutoError("WINDOWS_PIPE_CONNECT_FAILED")
                    finally:
                        if not connected:
                            overlapped.cancel()
                if not connected:
                    break
                last_activity = time.monotonic()
                if not slots.acquire(False):
                    from multiprocessing.connection import PipeConnection
                    with PipeConnection(handle) as connection:
                        handle = None
                        connection.send_bytes(canonical({"ok": False, "error": "BROKER_BUSY"}) + b"\n")
                    continue

                def run(client_handle):
                    try:
                        _handle_client(client_handle, engine, stopping, expected, kernel, advapi)
                    finally:
                        slots.release()

                pool.submit(run, handle)
                handle = None
            finally:
                if handle:
                    kernel.CloseHandle(handle)
