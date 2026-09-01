from __future__ import annotations

import os
from pathlib import Path

import pytest

from tracefold.platform.config.secret_file import (
    SecretFileError,
    read_secure_distinct_token,
    read_secure_secret_text,
    secret_file_configured,
)


def test_secure_secret_file_has_one_policy_for_status_and_worker_reads(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("secret-token\n", encoding="utf-8")
    path.chmod(0o600)

    assert secret_file_configured(path) is True
    assert read_secure_secret_text(path) == "secret-token"


def test_secure_distinct_token_rejects_weak_or_reused_credentials(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("short", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SecretFileError, match="too_short"):
        read_secure_distinct_token(path, forbidden_value="different")

    shared = "shared-bootstrap-write-token-" + "x" * 32
    path.write_text(shared, encoding="utf-8")
    with pytest.raises(SecretFileError, match="conflict"):
        read_secure_distinct_token(path, forbidden_value=shared)

    assert read_secure_distinct_token(path, forbidden_value="different") == shared


def test_secure_distinct_token_rejects_non_ascii_credentials(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("operator-write-" + "é" * 32, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SecretFileError, match="non_ascii"):
        read_secure_distinct_token(path, forbidden_value="different")


@pytest.mark.parametrize("mode", [0o604, 0o640, 0o666])
def test_secret_file_rejects_group_or_other_permissions(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "token"
    path.write_text("secret-token", encoding="utf-8")
    path.chmod(mode)

    assert secret_file_configured(path) is False
    with pytest.raises(SecretFileError, match="permissions"):
        read_secure_secret_text(path)


def test_secret_file_never_follows_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("secret-token", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "token"
    link.symlink_to(target)

    assert secret_file_configured(link) is False
    with pytest.raises(SecretFileError, match="invalid"):
        read_secure_secret_text(link)


def test_secret_file_rejects_a_fifo_without_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token"
    os.mkfifo(path, mode=0o600)
    real_open = os.open

    def nonblocking_open(candidate: Path, flags: int) -> int:
        assert flags & os.O_NONBLOCK
        return real_open(candidate, flags)

    monkeypatch.setattr(os, "open", nonblocking_open)
    assert secret_file_configured(path) is False
    with pytest.raises(SecretFileError, match="invalid"):
        read_secure_secret_text(path)


def test_secret_file_is_bounded_before_its_contents_are_read(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_bytes(b"x" * (16 * 1024 + 1))
    path.chmod(0o600)

    assert secret_file_configured(path) is False
    with pytest.raises(SecretFileError, match="too_large"):
        read_secure_secret_text(path)


@pytest.mark.parametrize("content", [b" \n\t", b"\xff"])
def test_configured_status_uses_the_same_decoding_and_nonempty_contract(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "token"
    path.write_bytes(content)
    path.chmod(0o600)

    assert secret_file_configured(path) is False
