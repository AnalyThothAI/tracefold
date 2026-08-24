"""One fail-closed policy for operator-owned secret files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_MAX_SECRET_BYTES = 16 * 1024


class SecretFileError(ValueError):
    """A sanitized secret-file failure that never includes its path or contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _open_secure(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise SecretFileError("missing") from None
    except OSError:
        raise SecretFileError("invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretFileError("invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecretFileError("permissions")
        if metadata.st_size <= 0:
            raise SecretFileError("empty")
        if metadata.st_size > _MAX_SECRET_BYTES:
            raise SecretFileError("too_large")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def secret_file_configured(path: Path | None) -> bool:
    """Return whether the same file the worker would read passes the secret-file policy."""

    if path is None:
        return False
    try:
        read_secure_secret_text(path)
    except SecretFileError:
        return False
    return True


def read_secure_secret_text(path: Path) -> str:
    """Read a bounded regular file without following symlinks or exposing failure details."""

    descriptor = _open_secure(path)
    try:
        chunks: list[bytes] = []
        remaining = _MAX_SECRET_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_SECRET_BYTES:
        raise SecretFileError("too_large")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SecretFileError("encoding") from None
    if not value:
        raise SecretFileError("empty")
    return value


__all__ = ["SecretFileError", "read_secure_secret_text", "secret_file_configured"]
