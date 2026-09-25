"""macOS Keychain boundary for the local setup wizard.

Credentials pass directly to Security.framework in process memory. No shell,
subprocess argv, browser storage, MCP response, or plaintext-file fallback is
involved. Legacy SecKeychain APIs are used for a dependency-free bridge; Apple
has deprecated them, so this platform cell requires ongoing native testing.
"""

from __future__ import annotations

import ctypes
import os
import platform
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class KeychainError(RuntimeError):
    """Screened setup failure that must never contain a credential."""


class _SecurityFrameworkBackend:
    _NOT_FOUND = -25300

    def __init__(self, keychain_path: Path | None = None):
        self._keychain_path = keychain_path
        try:
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError as error:
            raise KeychainError("KEYCHAIN_UNAVAILABLE") from error
        self._security = security
        self._core = core
        pointer = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        chars = ctypes.c_char_p
        security.SecKeychainOpen.argtypes = [chars, ctypes.POINTER(pointer)]
        security.SecKeychainOpen.restype = ctypes.c_int32
        security.SecKeychainFindGenericPassword.argtypes = [pointer, uint32, chars, uint32, chars, ctypes.POINTER(uint32), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainAddGenericPassword.argtypes = [pointer, uint32, chars, uint32, chars, uint32, pointer, ctypes.POINTER(pointer)]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint32, pointer]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainItemDelete.argtypes = [pointer]
        security.SecKeychainItemDelete.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        core.CFRelease.argtypes = [pointer]
        core.CFRelease.restype = None

    @contextmanager
    def _opened(self) -> Iterator[ctypes.c_void_p]:
        if self._keychain_path is None:
            yield ctypes.c_void_p()
            return
        reference = ctypes.c_void_p()
        status = self._security.SecKeychainOpen(os.fsencode(self._keychain_path), ctypes.byref(reference))
        if status != 0 or not reference.value:
            raise KeychainError("KEYCHAIN_UNAVAILABLE")
        try:
            yield reference
        finally:
            self._core.CFRelease(reference)

    def _find_item(self, keychain: ctypes.c_void_p, service: bytes, account: bytes) -> tuple[int, ctypes.c_void_p]:
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            keychain, len(service), service, len(account), account, None, None, ctypes.byref(item)
        )
        return status, item

    def put(self, service: str, account: str, secret: bytes) -> None:
        service_bytes, account_bytes = service.encode("ascii"), account.encode("ascii")
        secret_buffer = ctypes.create_string_buffer(secret)
        with self._opened() as keychain:
            status, item = self._find_item(keychain, service_bytes, account_bytes)
            try:
                if status == self._NOT_FOUND:
                    status = self._security.SecKeychainAddGenericPassword(
                        keychain, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                        len(secret), ctypes.cast(secret_buffer, ctypes.c_void_p), None,
                    )
                elif status == 0 and item.value:
                    status = self._security.SecKeychainItemModifyAttributesAndData(
                        item, None, len(secret), ctypes.cast(secret_buffer, ctypes.c_void_p)
                    )
                if status != 0:
                    raise KeychainError("KEYCHAIN_WRITE_FAILED")
            finally:
                if item.value:
                    self._core.CFRelease(item)

    def get(self, service: str, account: str) -> bytes:
        service_bytes, account_bytes = service.encode("ascii"), account.encode("ascii")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        with self._opened() as keychain:
            status = self._security.SecKeychainFindGenericPassword(
                keychain, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                ctypes.byref(length), ctypes.byref(data), None,
            )
            if status == self._NOT_FOUND:
                if data.value:
                    self._security.SecKeychainItemFreeContent(None, data)
                raise KeychainError("KEYCHAIN_ITEM_MISSING")
            if status != 0 or not data.value or not 8 <= length.value <= 4096:
                if data.value:
                    self._security.SecKeychainItemFreeContent(None, data)
                raise KeychainError("KEYCHAIN_READ_FAILED")
            try:
                return ctypes.string_at(data, length.value)
            finally:
                self._security.SecKeychainItemFreeContent(None, data)

    def delete(self, service: str, account: str) -> None:
        service_bytes, account_bytes = service.encode("ascii"), account.encode("ascii")
        with self._opened() as keychain:
            status, item = self._find_item(keychain, service_bytes, account_bytes)
            try:
                if status != 0 or not item.value or self._security.SecKeychainItemDelete(item) != 0:
                    raise KeychainError("KEYCHAIN_DELETE_FAILED")
            finally:
                if item.value:
                    self._core.CFRelease(item)


class MacKeychain:
    _SERVICES = {
        "typesafe": "ai.qualixar.adl.typesafe",
        "openrouter": "ai.qualixar.adl.openrouter",
    }

    def __init__(self, *, backend: Any | None = None, keychain_path: Path | None = None):
        if backend is not None and keychain_path is not None:
            raise ValueError("KEYCHAIN_BACKEND_CONFLICT")
        if keychain_path is not None and (keychain_path.is_symlink() or not keychain_path.is_file()):
            raise KeychainError("KEYCHAIN_UNAVAILABLE")
        self._backend = backend
        self._keychain_path = keychain_path

    @classmethod
    def _service(cls, provider: str) -> str:
        try:
            return cls._SERVICES[provider]
        except (KeyError, TypeError) as error:
            raise KeychainError("KEYCHAIN_PROVIDER_UNSUPPORTED") from error

    @staticmethod
    def _validate_credential(value: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value) <= 4096 or not value.isascii() or any(char.isspace() or char == "\x00" for char in value):
            raise KeychainError("KEYCHAIN_CREDENTIAL_INVALID")
        return value

    @staticmethod
    def _require_macos() -> None:
        if platform.system() != "Darwin":
            raise KeychainError("KEYCHAIN_UNAVAILABLE")

    def _native(self) -> Any:
        if self._backend is None:
            self._backend = _SecurityFrameworkBackend(self._keychain_path)
        return self._backend

    def put(self, provider: str, credential: str) -> None:
        service = self._service(provider)
        value = self._validate_credential(credential)
        self._require_macos()
        self._native().put(service, str(os.getuid()), value.encode("ascii"))

    def get(self, provider: str) -> str:
        service = self._service(provider)
        self._require_macos()
        data = self._native().get(service, str(os.getuid()))
        try:
            value = data.decode("ascii")
        except (AttributeError, UnicodeDecodeError) as error:
            raise KeychainError("KEYCHAIN_CREDENTIAL_INVALID") from error
        return self._validate_credential(value)

    def delete(self, provider: str) -> None:
        service = self._service(provider)
        self._require_macos()
        self._native().delete(service, str(os.getuid()))
