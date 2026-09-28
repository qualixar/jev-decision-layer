"""Fail-closed adapters for the host user's native credential store.

Linux uses the system `secret-tool` and sends keys through stdin only; Jev
never falls back to a file or environment variable. Windows uses the current
user's Credential Manager. macOS keeps the existing Keychain service/account
identity so policies created by earlier versions continue to resolve the
same item.
"""
from __future__ import annotations

import ctypes
import hmac
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .keychain import KeychainError, MacKeychain


class CredentialStoreError(KeychainError):
    """Fixed, non-sensitive credential-store status code."""


@runtime_checkable
class CredentialStore(Protocol):
    def put(self, provider: str, credential: str) -> None: ...
    def get(self, provider: str) -> str: ...
    def delete(self, provider: str) -> None: ...
    def available(self) -> bool: ...


_PROVIDERS = frozenset(("typesafe", "openrouter"))
_MIN_CREDENTIAL = 8
_MAX_CREDENTIAL = 4096


def _trusted_system_executable(path: str) -> str | None:
    """Accept only root-owned executables protected by every parent directory.

    `secret-tool` receives provider credentials on stdin. Resolving it through
    PATH is safe only when the resolved executable and its complete directory
    chain cannot be replaced by the current user or another unprivileged user.
    Resolve symlinks before checking so an apparently trusted symlink cannot
    redirect execution into a writable directory.
    """
    try:
        executable = Path(path).resolve(strict=True)
        info = executable.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            return None
        if info.st_mode & 0o022 or not info.st_mode & 0o111:
            return None

        parent = executable.parent
        while True:
            parent_info = parent.stat()
            if parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
                return None
            if parent == parent.parent:
                break
            parent = parent.parent
        # Return the path we checked, never the possibly swappable PATH entry.
        return str(executable)
    except (OSError, RuntimeError):
        return None


def _validate_provider(provider: str) -> str:
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise CredentialStoreError("CREDENTIAL_PROVIDER_UNSUPPORTED")
    return provider


def validate_credential(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _MIN_CREDENTIAL <= len(value) <= _MAX_CREDENTIAL
        or not value.isascii()
        or any(character.isspace() or character == "\x00" for character in value)
    ):
        raise CredentialStoreError("CREDENTIAL_INVALID")
    return value


class MacCredentialStore(MacKeychain):
    """OS-selector facade retaining the established macOS item identity."""

    def put(self, provider: str, credential: str) -> None:
        value = validate_credential(credential)
        try:
            previous = self.get(provider)
        except (KeychainError, KeyError) as error:
            if isinstance(error, KeychainError) and str(error) != "KEYCHAIN_ITEM_MISSING":
                raise
            previous = None
        super().put(provider, value)
        try:
            actual = self.get(provider)
        except KeychainError:
            actual = None
        if actual is not None and hmac.compare_digest(actual, value):
            return
        try:
            if previous is None:
                self.delete(provider)
            else:
                super().put(provider, previous)
        except KeychainError:
            pass
        raise CredentialStoreError("CREDENTIAL_STORE_VERIFY_FAILED")


class LinuxSecretService:
    """Secret Service adapter through the system libsecret `secret-tool`.

    `secret-tool` must be installed and the user's D-Bus Secret Service must
    be available. Provider keys are written through stdin only. Reads use
    `search --all` without `--unlock`, so the broker will not request a GUI
    unlock prompt. Every child process has a deadline and its stderr is
    discarded because native diagnostics are not a safe UI surface.
    """

    _ATTRIBUTES = {
        "application": "qualixar-jev-decision-layer",
    }
    _LABEL = "Qualixar Jev Decision Layer provider credential"
    _UNAVAILABLE_CODES = {"SECRET_SERVICE_DEPENDENCY_MISSING", "SECRET_SERVICE_UNAVAILABLE"}

    def __init__(self, *, backend: Any | None = None, interactive: bool = False, timeout: float = 30.0):
        self._backend = backend
        self._interactive = bool(interactive)
        self._timeout = timeout
        self._availability_error = "SECRET_SERVICE_DEPENDENCY_MISSING"

    @property
    def availability_error(self) -> str:
        return self._availability_error

    def _secret_tool(self) -> str:
        if self._backend is not None:
            return ""
        tool = shutil.which("secret-tool")
        if not tool or not os.path.isabs(tool):
            raise CredentialStoreError("SECRET_SERVICE_DEPENDENCY_MISSING")
        trusted_tool = _trusted_system_executable(tool)
        if trusted_tool is None:
            raise CredentialStoreError("SECRET_SERVICE_DEPENDENCY_MISSING")
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise CredentialStoreError("SECRET_SERVICE_UNAVAILABLE")
        return trusted_tool

    def _run(self, args: list[str], *, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                args,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED") from None
        except OSError:
            raise CredentialStoreError("SECRET_SERVICE_UNAVAILABLE") from None

    @staticmethod
    def _attributes(provider: str) -> list[str]:
        return ["application", "qualixar-jev-decision-layer", "provider", provider]

    def _search(self, provider: str) -> bytes:
        if self._backend is not None:
            raise RuntimeError("backend search uses the collection path")
        result = self._run([
            self._secret_tool(), "search", "--all", *self._attributes(provider)
        ])
        if result.returncode != 0:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED")
        lines = (result.stdout or b"").splitlines()
        item_count = sum(line.startswith(b"[/") and line.endswith(b"]") for line in lines)
        secrets = [line[len(b"secret = "):] for line in lines if line.startswith(b"secret = ")]
        if item_count == 0 and not secrets:
            raise CredentialStoreError("CREDENTIAL_ITEM_MISSING")
        if item_count > 1 or len(secrets) > 1:
            raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
        if item_count != 1 or len(secrets) != 1:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED")
        return secrets[0]

    def available(self) -> bool:
        try:
            if self._backend is None:
                # Use an unmatchable marker, so readiness checks cannot fetch
                # a real provider item or create an empty Secret Service item.
                marker = ["application", "qualixar-jev-decision-layer", "provider", "jev-readiness-check"]
                result = self._run([self._secret_tool(), "search", "--all", *marker])
                if result.returncode != 0:
                    raise CredentialStoreError("SECRET_SERVICE_UNAVAILABLE")
            self._availability_error = ""
            return True
        except CredentialStoreError as error:
            self._availability_error = str(error) if str(error) in self._UNAVAILABLE_CODES else "SECRET_SERVICE_UNAVAILABLE"
            return False

    def _items(self, collection: Any, provider: str) -> list[Any]:
        attributes = {**self._ATTRIBUTES, "provider": provider}
        return list(collection.search_items(attributes))

    def _unlock_for_write(self, collection: Any) -> None:
        if not collection.is_locked():
            return
        if not self._interactive:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED")
        try:
            dismissed = collection.unlock(timeout=self._timeout)
            if dismissed or collection.is_locked():
                raise CredentialStoreError("SECRET_SERVICE_LOCKED")
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED") from None

    def put(self, provider: str, credential: str) -> None:
        provider = _validate_provider(provider)
        value = validate_credential(credential)
        if not self._interactive:
            raise CredentialStoreError("CREDENTIAL_STORE_INTERACTION_REQUIRED")
        if self._backend is not None:
            collection = self._backend
            self._unlock_for_write(collection)
            existing = self._items(collection, provider)
            if len(existing) > 1:
                raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
            previous = existing[0].get_secret() if existing else None
            attributes = {**self._ATTRIBUTES, "provider": provider}
            collection.create_item(self._LABEL, attributes, value.encode("ascii"), replace=True)
            verified_items = self._items(collection, provider)
            actual = verified_items[0].get_secret() if len(verified_items) == 1 else None
            if not isinstance(actual, bytes) or not hmac.compare_digest(actual, value.encode("ascii")):
                self._restore_backend(collection, provider, attributes, previous, value.encode("ascii"), verified_items)
                raise CredentialStoreError("CREDENTIAL_STORE_VERIFY_FAILED")
            return
        tool = self._secret_tool()
        previous: str | None
        try:
            previous = self.get(provider)
        except CredentialStoreError as error:
            if str(error) != "CREDENTIAL_ITEM_MISSING":
                raise
            previous = None
        result = self._run([
            tool, "store", "--label", self._LABEL, *self._attributes(provider)
        ], input_data=value.encode("ascii"))
        if result.returncode != 0:
            raise CredentialStoreError("SECRET_SERVICE_LOCKED")
        try:
            actual = self.get(provider)
        except CredentialStoreError:
            actual = None
        if actual is not None and hmac.compare_digest(actual, value):
            return
        try:
            if previous is not None:
                self._run([tool, "store", "--label", self._LABEL, *self._attributes(provider)],
                          input_data=previous.encode("ascii"))
            else:
                self._clear_matching_value(tool, provider, value)
        except CredentialStoreError:
            pass
        raise CredentialStoreError("CREDENTIAL_STORE_VERIFY_FAILED")

    def _restore_backend(self, collection: Any, provider: str, attributes: dict[str, str],
                         previous: bytes | None, expected: bytes, items: list[Any]) -> None:
        try:
            if previous is not None:
                collection.create_item(self._LABEL, attributes, previous, replace=True)
            else:
                for item in items:
                    if hmac.compare_digest(item.get_secret(), expected):
                        item.delete()
        except Exception:
            pass

    def _clear_matching_value(self, tool: str, provider: str, expected: str) -> None:
        try:
            current = self.get(provider)
        except CredentialStoreError:
            return
        if hmac.compare_digest(current, expected):
            result = self._run([tool, "clear", *self._attributes(provider)])
            if result.returncode != 0:
                raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED")

    def get(self, provider: str) -> str:
        provider = _validate_provider(provider)
        if self._backend is not None:
            collection = self._backend
            if collection.is_locked():
                raise CredentialStoreError("SECRET_SERVICE_LOCKED")
            items = self._items(collection, provider)
            if not items:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING")
            if len(items) != 1:
                raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
            raw = items[0].get_secret()
        else:
            raw = self._search(provider)
        try:
            value = bytes(raw).decode("ascii")
        except (TypeError, ValueError, UnicodeDecodeError):
            raise CredentialStoreError("CREDENTIAL_INVALID") from None
        return validate_credential(value)

    def delete(self, provider: str) -> None:
        provider = _validate_provider(provider)
        if self._backend is not None:
            collection = self._backend
            self._unlock_for_write(collection)
            items = self._items(collection, provider)
            if not items:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING")
            if len(items) != 1:
                raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
            items[0].delete()
            return
        if not self._interactive:
            raise CredentialStoreError("CREDENTIAL_STORE_INTERACTION_REQUIRED")
        tool = self._secret_tool()
        self.get(provider)  # Refuse locked items rather than report a false delete.
        result = self._run([tool, "clear", *self._attributes(provider)])
        if result.returncode != 0:
            raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED")
        try:
            self.get(provider)
        except CredentialStoreError as error:
            if str(error) == "CREDENTIAL_ITEM_MISSING":
                return
            raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED") from None
        raise CredentialStoreError("CREDENTIAL_STORE_DELETE_FAILED")


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):
    """WinCred CREDENTIALW layout; pointer fields use native pointer width."""

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialManager:
    """Current-user Windows Credential Manager generic-credential adapter."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    _PREFIX = "Qualixar/JevDecisionLayer/provider/"

    def __init__(self, *, backend: Any | None = None):
        self._backend = backend
        self._advapi32: Any | None = None

    @classmethod
    def target_for(cls, provider: str) -> str:
        return cls._PREFIX + _validate_provider(provider)

    def _native(self) -> Any:
        if self._backend is not None:
            return self._backend
        if platform.system() != "Windows":
            raise CredentialStoreError("CREDENTIAL_STORE_UNSUPPORTED")
        if self._advapi32 is None:
            try:
                advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            except (AttributeError, OSError):
                raise CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE") from None
            advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
            advapi32.CredWriteW.restype = ctypes.c_int
            advapi32.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
            advapi32.CredReadW.restype = ctypes.c_int
            advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            advapi32.CredDeleteW.restype = ctypes.c_int
            advapi32.CredFree.argtypes = [ctypes.c_void_p]
            advapi32.CredFree.restype = None
            self._advapi32 = advapi32
        return self._advapi32

    def available(self) -> bool:
        if self._backend is not None:
            return True
        if platform.system() != "Windows":
            return False
        try:
            self._native()
            return True
        except CredentialStoreError:
            return False

    def _read(self, provider: str) -> bytes:
        api = self._native()
        target = self.target_for(provider)
        if self._backend is not None:
            try:
                value = api.get(target)
            except KeyError:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING") from None
            return value.encode("ascii") if isinstance(value, str) else bytes(value)
        pointer = ctypes.POINTER(_CREDENTIAL)()
        if not api.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING")
            raise CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
        try:
            record = pointer.contents
            size = int(record.CredentialBlobSize)
            if record.CredentialBlob is None or not _MIN_CREDENTIAL <= size <= _MAX_CREDENTIAL:
                raise CredentialStoreError("CREDENTIAL_INVALID")
            return ctypes.string_at(record.CredentialBlob, size)
        finally:
            api.CredFree(ctypes.cast(pointer, ctypes.c_void_p))

    def get(self, provider: str) -> str:
        provider = _validate_provider(provider)
        try:
            value = self._read(provider).decode("ascii")
        except CredentialStoreError:
            raise
        except (UnicodeDecodeError, TypeError, ValueError):
            raise CredentialStoreError("CREDENTIAL_INVALID") from None
        return validate_credential(value)

    def _write(self, provider: str, credential: str) -> None:
        api = self._native()
        target = self.target_for(provider)
        raw = credential.encode("ascii")
        if self._backend is not None:
            api.put(target, credential)
            return
        buffer = ctypes.create_string_buffer(raw, len(raw))
        record = _CREDENTIAL()
        record.Flags = 0
        record.Type = self.CRED_TYPE_GENERIC
        record.TargetName = target
        record.Comment = "Qualixar Jev Decision Layer provider credential"
        record.CredentialBlobSize = len(raw)
        record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        record.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        record.AttributeCount = 0
        record.Attributes = None
        record.TargetAlias = None
        record.UserName = ""
        try:
            succeeded = api.CredWriteW(ctypes.byref(record), 0)
        except Exception:
            raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED") from None
        finally:
            ctypes.memset(buffer, 0, len(raw))
        if not succeeded:
            raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED")

    def put(self, provider: str, credential: str) -> None:
        provider = _validate_provider(provider)
        value = validate_credential(credential)
        try:
            previous = self.get(provider)
        except CredentialStoreError as error:
            if str(error) != "CREDENTIAL_ITEM_MISSING":
                raise
            previous = None
        self._write(provider, value)
        try:
            actual = self.get(provider)
        except CredentialStoreError:
            actual = None
        if actual is not None and hmac.compare_digest(actual, value):
            return
        # Restore an existing value if the native write cannot be verified;
        # remove only the entry created by this operation otherwise.
        if previous is not None:
            try:
                self._write(provider, previous)
            except CredentialStoreError:
                pass
        else:
            try:
                self._delete_if_value_matches(provider, value)
            except CredentialStoreError:
                pass
        raise CredentialStoreError("CREDENTIAL_STORE_VERIFY_FAILED")

    def _delete_if_value_matches(self, provider: str, expected: str) -> None:
        try:
            current = self.get(provider)
        except CredentialStoreError:
            return
        if not hmac.compare_digest(current, expected):
            return
        self._delete(provider)

    def _delete(self, provider: str) -> None:
        api = self._native()
        target = self.target_for(provider)
        if self._backend is not None:
            try:
                api.delete(target)
            except KeyError:
                raise CredentialStoreError("CREDENTIAL_ITEM_MISSING") from None
            return
        if not api.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            code = "CREDENTIAL_ITEM_MISSING" if error == self.ERROR_NOT_FOUND else "CREDENTIAL_STORE_DELETE_FAILED"
            raise CredentialStoreError(code)

    def delete(self, provider: str) -> None:
        self._delete(_validate_provider(provider))


def credential_store_for_platform(
    *, platform_name: str | None = None, backend: Any | None = None, interactive: bool = False
) -> CredentialStore:
    name = platform_name or platform.system()
    if name == "Darwin":
        return MacCredentialStore(backend=backend)
    if name == "Linux":
        return LinuxSecretService(backend=backend, interactive=interactive)
    if name == "Windows":
        return WindowsCredentialManager(backend=backend)
    raise CredentialStoreError("CREDENTIAL_STORE_UNSUPPORTED")


__all__ = [
    "CredentialStore", "CredentialStoreError", "LinuxSecretService", "MacCredentialStore", "MacKeychain",
    "WindowsCredentialManager", "credential_store_for_platform", "validate_credential",
]
