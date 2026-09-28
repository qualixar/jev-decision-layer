"""Private runtime files and locks across POSIX and Windows.

Windows uses token-SID ACL validation, protected DACLs, and reparse-point checks.
POSIX mode bits are deliberately never interpreted as Windows ACLs.
"""
from __future__ import annotations

import ctypes
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


_WINDOWS_OPS = None  # Replaced by focused tests; native APIs load only on Windows.


def _error(code: str):
    from .common import AutoError
    raise AutoError(code)


def _windows_ops():
    """Load the native Windows backend lazily (and fail closed off Windows)."""
    global _WINDOWS_OPS
    if _WINDOWS_OPS is not None:
        return _WINDOWS_OPS
    if os.name != "nt":
        _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    try:
        _WINDOWS_OPS = _WindowsOps()
    except Exception:
        _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    return _WINDOWS_OPS


def _verify_sddl(sddl: str, user_sid: str, *, directory: bool) -> bool:
    """Accept only the exact protected user+SYSTEM full-control DACL we create."""
    if not isinstance(sddl, str):
        return False
    owner = re.match(r"O:(.*?)(?:G:|D:|$)", sddl, re.IGNORECASE)
    dacl_start = sddl.find("D:")
    if not owner or owner.group(1).upper() != user_sid.upper() or dacl_start < 0:
        return False
    value = sddl[dacl_start + 2:]
    sacl_start = value.find("S:")
    if sacl_start >= 0:
        value = value[:sacl_start]
    if not value.startswith("P"):
        return False
    aces = re.findall(r"\(([^()]*)\)", value[1:])
    if len(aces) != 2 or "".join(f"({ace})" for ace in aces) != value[1:]:
        return False
    expected_flags = "OICI" if directory else ""
    trustees = set()
    for ace in aces:
        fields = ace.split(";")
        if len(fields) != 6:
            return False
        kind, flags, rights, object_guid, inherit_guid, trustee = fields
        if (kind.upper() != "A" or flags.upper() != expected_flags
                or rights.upper() != "FA" or object_guid or inherit_guid):
            return False
        trustees.add(trustee.upper())
    return trustees == {"SY", user_sid.upper()}


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _WindowsOps:
    """Thin ctypes wrapper over Win32 file, security, and byte-range-lock APIs.

    Every opened object is inspected by handle with FILE_FLAG_OPEN_REPARSE_POINT.
    A protected explicit DACL grants FILE_ALL_ACCESS only to the current token SID
    and LocalSystem. No chmod-derived claim is made about Windows security.
    """

    # Win32 constants (kept local so importing this module on POSIX is safe).
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    READ_CONTROL = 0x00020000
    WRITE_DAC = 0x00040000
    FILE_READ_ATTRIBUTES = 0x00000080
    FILE_SHARE_ALL = 0x00000007
    OPEN_EXISTING = 3
    CREATE_NEW = 1
    OPEN_ALWAYS = 4
    CREATE_ALWAYS = 2
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    MOVEFILE_REPLACE_EXISTING = 0x1
    MOVEFILE_WRITE_THROUGH = 0x8
    SECURITY_INFORMATION = 0x1 | 0x4
    DACL_SECURITY_INFORMATION = 0x4
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SE_FILE_OBJECT = 1
    TOKEN_QUERY = 0x8
    TOKEN_USER = 1
    LOCKFILE_FAIL_IMMEDIATELY = 0x1
    LOCKFILE_EXCLUSIVE_LOCK = 0x2
    ERROR_LOCK_VIOLATION = 33
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", ctypes.c_uint32), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", ctypes.c_int)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [("dwFileAttributes", ctypes.c_uint32), ("ftCreationTime", _FILETIME),
                    ("ftLastAccessTime", _FILETIME), ("ftLastWriteTime", _FILETIME),
                    ("dwVolumeSerialNumber", ctypes.c_uint32), ("nFileSizeHigh", ctypes.c_uint32),
                    ("nFileSizeLow", ctypes.c_uint32), ("nNumberOfLinks", ctypes.c_uint32),
                    ("nFileIndexHigh", ctypes.c_uint32), ("nFileIndexLow", ctypes.c_uint32)]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                    ("hEvent", ctypes.c_void_p)]

    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Win32 APIs are unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._locks: dict[int, object] = {}
        self._bind()
        self.user_sid = self._current_user_sid()

    def _bind(self):
        k, a = self.kernel32, self.advapi32
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(self._SECURITY_ATTRIBUTES), ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_void_p]
        k.CreateDirectoryW.restype = ctypes.c_int
        k.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(self._SECURITY_ATTRIBUTES)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetFileInformationByHandle.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(self._BY_HANDLE_FILE_INFORMATION)]
        k.GetFileInformationByHandle.restype = ctypes.c_int
        k.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        k.MoveFileExW.restype = ctypes.c_int
        k.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        k.DeleteFileW.restype = ctypes.c_int
        k.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        k.FlushFileBuffers.restype = ctypes.c_int
        k.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        k.GetFileAttributesW.restype = ctypes.c_uint32
        k.LockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(self._OVERLAPPED)]
        k.LockFileEx.restype = ctypes.c_int
        k.UnlockFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.POINTER(self._OVERLAPPED)]
        k.UnlockFileEx.restype = ctypes.c_int
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        a.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        a.OpenProcessToken.restype = ctypes.c_int
        a.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        a.GetTokenInformation.restype = ctypes.c_int
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        a.ConvertSidToStringSidW.restype = ctypes.c_int
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(ctypes.c_uint32)]
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int
        a.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(ctypes.c_void_p)]
        a.GetSecurityInfo.restype = ctypes.c_uint32
        a.SetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]
        a.SetSecurityInfo.restype = ctypes.c_uint32
        a.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)]
        a.GetSecurityDescriptorDacl.restype = ctypes.c_int
        a.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16),
                                                   ctypes.POINTER(ctypes.c_uint32)]
        a.GetSecurityDescriptorControl.restype = ctypes.c_int

    def _current_user_sid(self) -> str:
        token = ctypes.c_void_p()
        if not self.advapi32.OpenProcessToken(self.kernel32.GetCurrentProcess(), self.TOKEN_QUERY,
                                              ctypes.byref(token)):
            raise OSError(ctypes.get_last_error())
        try:
            needed = ctypes.c_uint32()
            self.advapi32.GetTokenInformation(token, self.TOKEN_USER, None, 0, ctypes.byref(needed))
            if not needed.value or needed.value > 65536:
                raise OSError("invalid token user size")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.advapi32.GetTokenInformation(token, self.TOKEN_USER, buffer,
                                                     needed.value, ctypes.byref(needed)):
                raise OSError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = ctypes.c_wchar_p()
            if not self.advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                raise OSError(ctypes.get_last_error())
            try:
                return text.value
            finally:
                self.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.kernel32.CloseHandle(token)

    def _sddl(self, directory: bool) -> str:
        inherit = "OICI" if directory else ""
        return f"O:{self.user_sid}D:P(A;{inherit};FA;;;SY)(A;{inherit};FA;;;{self.user_sid})"

    def _descriptor(self, directory: bool):
        descriptor = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                self._sddl(directory), 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error())
        return descriptor

    def _open_handle(self, path: Path, *, directory: bool, write_dac: bool = False):
        access = self.READ_CONTROL | self.FILE_READ_ATTRIBUTES | (self.WRITE_DAC if write_dac else 0)
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT | (self.FILE_FLAG_BACKUP_SEMANTICS if directory else 0)
        handle = self.kernel32.CreateFileW(str(path), access, self.FILE_SHARE_ALL, None,
                                           self.OPEN_EXISTING, flags, None)
        if handle in (None, self.INVALID_HANDLE_VALUE):
            raise OSError(ctypes.get_last_error())
        return ctypes.c_void_p(handle)

    @classmethod
    def _validate_handle_attributes(cls, attributes: int, links: int, *,
                                    directory: bool, single_link: bool = False):
        if attributes & cls.FILE_ATTRIBUTE_REPARSE_POINT:
            _error("SYMLINK_NOT_ALLOWED")
        if bool(attributes & cls.FILE_ATTRIBUTE_DIRECTORY) != directory:
            _error("PRIVATE_DIRECTORY_OWNER" if directory else "UNSAFE_PRIVATE_FILE")
        if single_link and links != 1:
            _error("UNSAFE_PRIVATE_FILE")

    def _inspect(self, handle, *, directory: bool, single_link: bool = False):
        info = self._BY_HANDLE_FILE_INFORMATION()
        if not self.kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error())
        attrs = info.dwFileAttributes
        self._validate_handle_attributes(attrs, info.nNumberOfLinks,
                                         directory=directory, single_link=single_link)

        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        rc = self.advapi32.GetSecurityInfo(handle, self.SE_FILE_OBJECT, self.SECURITY_INFORMATION,
                                           ctypes.byref(owner), None, None, None,
                                           ctypes.byref(descriptor))
        if rc != 0:
            raise OSError(rc)
        try:
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            if not self.advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                                              ctypes.byref(revision)):
                raise OSError(ctypes.get_last_error())
            if not control.value & 0x1000:  # SE_DACL_PROTECTED
                _error("PRIVATE_DIRECTORY_OWNER" if directory else "UNSAFE_PRIVATE_FILE")
            sddl = ctypes.c_wchar_p()
            length = ctypes.c_uint32()
            if not self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    descriptor, 1, self.SECURITY_INFORMATION, ctypes.byref(sddl), ctypes.byref(length)):
                raise OSError(ctypes.get_last_error())
            try:
                if not _verify_sddl(sddl.value, self.user_sid, directory=directory):
                    _error("PRIVATE_DIRECTORY_OWNER" if directory else "UNSAFE_PRIVATE_FILE")
            finally:
                self.kernel32.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))
        finally:
            self.kernel32.LocalFree(descriptor)

    def _set_private_dacl(self, handle, *, directory: bool):
        descriptor = self._descriptor(directory)
        try:
            present, dacl, defaulted = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_int()
            if not self.advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                           ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
                raise OSError("private DACL construction failed")
            rc = self.advapi32.SetSecurityInfo(
                handle, self.SE_FILE_OBJECT,
                self.DACL_SECURITY_INFORMATION | self.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None)
            if rc != 0:
                raise OSError(rc)
        finally:
            self.kernel32.LocalFree(descriptor)

    def _verify_path_ancestors(self, path: Path):
        """Inspect every existing path component without following its final reparse point."""
        absolute = Path(os.path.abspath(path))
        chain = list(reversed(absolute.parents)) + [absolute]
        for component in chain:
            attrs = self.kernel32.GetFileAttributesW(str(component))
            if attrs == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                if error in (2, 3):  # file/path not found; callers may be creating it
                    continue
                raise OSError(error)
            if attrs & self.FILE_ATTRIBUTE_REPARSE_POINT:
                _error("SYMLINK_NOT_ALLOWED")
            if not attrs & self.FILE_ATTRIBUTE_DIRECTORY:
                _error("PRIVATE_DIRECTORY_OWNER")

    def ensure_dir(self, path: Path) -> Path:
        p = Path(os.path.abspath(path))
        if p.parent != p:
            parent_attrs = self.kernel32.GetFileAttributesW(str(p.parent))
            if parent_attrs == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                if error not in (2, 3):
                    raise OSError(error)
                self.ensure_dir(p.parent)
            else:
                # Do not rewrite ACLs on existing ancestors such as LocalAppData.
                self._verify_path_ancestors(p.parent)
        descriptor = self._descriptor(True)
        try:
            attrs = self._SECURITY_ATTRIBUTES(ctypes.sizeof(self._SECURITY_ATTRIBUTES),
                                              descriptor, 0)
            if not self.kernel32.CreateDirectoryW(str(p), ctypes.byref(attrs)):
                error = ctypes.get_last_error()
                # ERROR_ALREADY_EXISTS is safe only after handle-based inspection and repair.
                if error != 183:
                    raise OSError(error)
        finally:
            self.kernel32.LocalFree(descriptor)
        handle = self._open_handle(p, directory=True, write_dac=True)
        try:
            self._set_private_dacl(handle, directory=True)
            self._inspect(handle, directory=True)
        finally:
            self.kernel32.CloseHandle(handle)
        return p

    def verify_dir(self, path: Path) -> None:
        self._verify_path_ancestors(path)
        handle = self._open_handle(path, directory=True)
        try:
            self._inspect(handle, directory=True)
        finally:
            self.kernel32.CloseHandle(handle)

    def _create_file_handle(self, path: Path, flags: int):
        access = self.GENERIC_READ
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
            access = self.GENERIC_WRITE if flags & os.O_WRONLY else self.GENERIC_READ | self.GENERIC_WRITE
        create = bool(flags & os.O_CREAT)
        exclusive = bool(flags & os.O_EXCL)
        truncate = bool(flags & os.O_TRUNC)
        disposition = (self.CREATE_NEW if exclusive else self.CREATE_ALWAYS if truncate
                       else self.OPEN_ALWAYS if create else self.OPEN_EXISTING)
        created = create
        descriptor = self._descriptor(False) if created else None
        attrs_pointer = None
        attrs = None
        if descriptor is not None:
            attrs = self._SECURITY_ATTRIBUTES(ctypes.sizeof(self._SECURITY_ATTRIBUTES), descriptor, 0)
            attrs_pointer = ctypes.byref(attrs)
        try:
            handle = self.kernel32.CreateFileW(
                str(path), access, self.FILE_SHARE_ALL, attrs_pointer, disposition,
                self.FILE_FLAG_OPEN_REPARSE_POINT, None)
            if handle in (None, self.INVALID_HANDLE_VALUE):
                error = ctypes.get_last_error()
                if create and not exclusive and not truncate and error == 183:
                    handle = self.kernel32.CreateFileW(
                        str(path), access, self.FILE_SHARE_ALL, None, self.OPEN_EXISTING,
                        self.FILE_FLAG_OPEN_REPARSE_POINT, None)
                    if handle in (None, self.INVALID_HANDLE_VALUE):
                        raise OSError(ctypes.get_last_error())
                else:
                    if create and exclusive and error in (80, 183):
                        raise FileExistsError(error, "file already exists", str(path))
                    raise OSError(error)
            return ctypes.c_void_p(handle)
        finally:
            if descriptor is not None:
                self.kernel32.LocalFree(descriptor)

    def _fd_from_handle(self, handle, flags: int) -> int:
        import msvcrt
        mode = os.O_BINARY
        if flags & os.O_RDWR:
            mode |= os.O_RDWR
        elif flags & os.O_WRONLY:
            mode |= os.O_WRONLY
        else:
            mode |= os.O_RDONLY
        if flags & os.O_APPEND:
            mode |= os.O_APPEND
        try:
            return msvcrt.open_osfhandle(handle.value, mode)
        except BaseException:
            self.kernel32.CloseHandle(handle)
            raise

    def open_file(self, path: Path, flags: int, mode: int = 0o600) -> int:
        del mode  # Windows access is defined by the explicit protected DACL, never POSIX mode bits.
        self._verify_path_ancestors(path.parent)
        handle = self._create_file_handle(path, flags)
        try:
            fd = self._fd_from_handle(handle, flags)
        except BaseException:
            raise
        try:
            self.verify_fd(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def verify_fd(self, fd: int) -> None:
        import msvcrt
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
        self._inspect(handle, directory=False, single_link=True)

    def _create_temp(self, path: Path) -> tuple[int, Path]:
        for _ in range(10):
            temporary = path.with_name(f".auto-{secrets.token_hex(12)}")
            try:
                handle = self._create_file_handle(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                fd = self._fd_from_handle(handle, os.O_RDWR)
                try:
                    self.verify_fd(fd)
                except BaseException:
                    os.close(fd)
                    self.kernel32.DeleteFileW(str(temporary))
                    raise
                return fd, temporary
            except FileExistsError:
                continue
        _error("PRIVATE_TEMP_CREATE_FAILED")

    def atomic_write(self, path: Path, data: bytes, *, replace: bool) -> None:
        p = Path(os.path.abspath(path))
        self.ensure_dir(p.parent)
        attributes = self.kernel32.GetFileAttributesW(str(p))
        if attributes != 0xFFFFFFFF:
            existing = self._open_handle(p, directory=False)
            try:
                self._inspect(existing, directory=False, single_link=True)
            finally:
                self.kernel32.CloseHandle(existing)
        elif ctypes.get_last_error() not in (2, 3):
            raise OSError(ctypes.get_last_error())
        fd, temporary = self._create_temp(p)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("short write")
                view = view[count:]
            os.fsync(fd)
            import msvcrt
            if not self.kernel32.FlushFileBuffers(ctypes.c_void_p(msvcrt.get_osfhandle(fd))):
                raise OSError(ctypes.get_last_error())
            os.close(fd)
            fd = -1
            flags = self.MOVEFILE_WRITE_THROUGH | (self.MOVEFILE_REPLACE_EXISTING if replace else 0)
            if not self.kernel32.MoveFileExW(str(temporary), str(p), flags):
                error = ctypes.get_last_error()
                if not replace and error in (80, 183):
                    _error("WORKSPACE_ALREADY_ENROLLED")
                raise OSError(error)
        finally:
            if fd >= 0:
                os.close(fd)
            self.kernel32.DeleteFileW(str(temporary))

    def lock(self, fd: int, *, blocking: bool) -> bool:
        import msvcrt
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
        overlap = self._OVERLAPPED()
        flags = self.LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else self.LOCKFILE_FAIL_IMMEDIATELY)
        if self.kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
            self._locks[fd] = overlap
            return True
        error = ctypes.get_last_error()
        if not blocking and error == self.ERROR_LOCK_VIOLATION:
            return False
        raise OSError(error)

    def unlock(self, fd: int) -> None:
        import msvcrt
        overlap = self._locks.pop(fd, None)
        if overlap is None:
            return
        if not self.kernel32.UnlockFileEx(ctypes.c_void_p(msvcrt.get_osfhandle(fd)), 0, 1, 0,
                                           ctypes.byref(overlap)):
            raise OSError(ctypes.get_last_error())


def user_state_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
        return Path(base) / "Qualixar" / "JevDecisionLayer"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "qualixar-jev-decision-layer"


def ensure_private_dir(path: Path) -> Path:
    if os.name == "nt":
        try:
            return _windows_ops().ensure_dir(path)
        except Exception as exc:
            from .common import AutoError
            if isinstance(exc, AutoError):
                raise
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    from .common import safe_path
    p = safe_path(path)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = p.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        _error("PRIVATE_DIRECTORY_OWNER")
    p.chmod(0o700)
    return p


def verify_private_dir(path: Path) -> None:
    if os.name == "nt":
        try:
            _windows_ops().verify_dir(path)
            return
        except Exception as exc:
            from .common import AutoError
            if isinstance(exc, AutoError):
                raise
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    from .common import safe_path
    p = safe_path(path)
    st = p.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        _error("PRIVATE_DIRECTORY_OWNER")


def open_private_file(path: Path, flags: int, mode: int = 0o600) -> int:
    if os.name == "nt":
        try:
            return _windows_ops().open_file(path, flags, mode)
        except Exception as exc:
            from .common import AutoError
            if isinstance(exc, AutoError):
                raise
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    from .common import safe_path
    p = safe_path(path)
    fd = os.open(p, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        verify_private_file_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def verify_private_file_fd(fd: int) -> None:
    if os.name == "nt":
        try:
            _windows_ops().verify_fd(fd)
            return
        except Exception as exc:
            from .common import AutoError
            if isinstance(exc, AutoError):
                raise
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
        _error("UNSAFE_PRIVATE_FILE")


def atomic_write_private(path: Path, data: bytes, *, replace: bool) -> None:
    if os.name == "nt":
        try:
            _windows_ops().atomic_write(path, data, replace=replace)
            return
        except Exception as exc:
            from .common import AutoError
            if isinstance(exc, AutoError):
                raise
            _error("WINDOWS_PRIVATE_STATE_UNVERIFIED")
    from .common import safe_path
    p = safe_path(path)
    ensure_private_dir(p.parent)
    fd, temporary = tempfile.mkstemp(prefix=".auto-", dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, p)
        else:
            try:
                os.link(temporary, p, follow_symlinks=False)
            except FileExistsError:
                _error("WORKSPACE_ALREADY_ENROLLED")
        directory_fd = os.open(p.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    """Yield False if a nonblocking POSIX lock is held elsewhere."""
    if os.name == "nt":
        ops = _windows_ops()
        fd = open_private_file(path, os.O_RDWR | os.O_CREAT)
        acquired = False
        try:
            acquired = ops.lock(fd, blocking=blocking)
            yield acquired
        finally:
            if acquired:
                ops.unlock(fd)
            os.close(fd)
        return
    import fcntl
    fd = open_private_file(path, os.O_RDWR | os.O_CREAT)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            if blocking:
                raise
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
