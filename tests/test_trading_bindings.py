from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.trading_bindings import (
    inspect_binding_credentials,
    load_binance_demo_credential_snapshot,
)
from tracefold.platform.config.models import Settings
from tracefold.trading import require_execution_binding_enabled
from tracefold.trading.storage.authority import AuthorityStorage
from tracefold.trading.storage.bindings import BindingStorage
from tracefold.trading.storage.capabilities import CapabilityStorage


def _secure(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _settings(path: Path) -> Settings:
    settings = Settings(
        trading={
            "enabled": True,
            "bindings": {
                "binance_demo": {
                    "api_key_file": "binance-key",
                    "api_secret_file": "binance-secret",
                },
            },
        }
    )
    settings.set_config_dir(path)
    return settings


def test_no_key_is_two_explicit_unconfigured_bindings(tmp_path: Path) -> None:
    facts = inspect_binding_credentials(_settings(tmp_path))

    assert [(fact.binding, fact.state, fact.fingerprint, fact.reason) for fact in facts] == [
        ("BINANCE_USDM", "unconfigured", None, "credentials_unconfigured"),
        ("HYPERLIQUID_PERP", "unconfigured", None, "execution_binding_disabled"),
    ]


def test_empty_init_placeholders_are_unconfigured_not_invalid(tmp_path: Path) -> None:
    for name in ("binance-key", "binance-secret"):
        _secure(tmp_path / name, "")

    facts = inspect_binding_credentials(_settings(tmp_path))

    assert [fact.state for fact in facts] == ["unconfigured", "unconfigured"]


def test_one_key_configures_only_its_closed_binding(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-value")
    _secure(tmp_path / "binance-secret", "secret-value")

    binance, hyperliquid = inspect_binding_credentials(_settings(tmp_path))

    assert binance.state == "configured" and len(binance.fingerprint or "") == 64
    assert hyperliquid.state == "unconfigured" and hyperliquid.fingerprint is None


def test_demo_credentials_are_one_secret_safe_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.app import trading_bindings

    _secure(tmp_path / "binance-key", "key-value")
    _secure(tmp_path / "binance-secret", "secret-value")
    reads: list[Path] = []
    original = trading_bindings.read_secure_secret_text

    def read_once(path: Path) -> str:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(trading_bindings, "read_secure_secret_text", read_once)
    snapshot = load_binance_demo_credential_snapshot(_settings(tmp_path))

    assert reads == [tmp_path / "binance-key", tmp_path / "binance-secret"]
    assert (snapshot.api_key, snapshot.api_secret) == ("key-value", "secret-value")
    assert snapshot.fact.state == "configured"
    assert "key-value" not in repr(snapshot)
    assert "secret-value" not in repr(snapshot)


def test_retired_hyperliquid_secret_cannot_configure_execution(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-value")
    _secure(tmp_path / "binance-secret", "secret-value")
    _secure(tmp_path / "hyperliquid-key", "11" * 32)
    binance, hyperliquid = inspect_binding_credentials(_settings(tmp_path))

    assert (binance.state, hyperliquid.state) == ("configured", "unconfigured")
    assert hyperliquid.reason == "execution_binding_disabled"
    assert hyperliquid.fingerprint is None
    assert "key-value" not in repr((binance, hyperliquid))


def test_incomplete_or_malformed_credentials_are_explicitly_invalid(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-without-secret")
    _secure(tmp_path / "hyperliquid-key", "not-a-private-key")

    binance, hyperliquid = inspect_binding_credentials(_settings(tmp_path))

    assert (binance.state, binance.reason, binance.fingerprint) == ("invalid", "credentials_invalid", None)
    assert (hyperliquid.state, hyperliquid.reason, hyperliquid.fingerprint) == (
        "unconfigured",
        "execution_binding_disabled",
        None,
    )


def test_execution_enablement_is_one_closed_domain_boundary() -> None:
    require_execution_binding_enabled("BINANCE_USDM")

    with pytest.raises(ValueError, match="execution_binding_disabled:HYPERLIQUID_PERP"):
        require_execution_binding_enabled("HYPERLIQUID_PERP")


@pytest.mark.parametrize(
    "write",
    [
        lambda value: BindingStorage().append_and_activate_execution_binding(value),
        lambda value: CapabilityStorage().append_and_activate_execution_capability_snapshot(
            value,
            created_at_ms=1,
        ),
        lambda value: CapabilityStorage().mark_execution_capability_compile_error(
            binding=value.binding,
            reason="disabled",
            now_ms=1,
        ),
        lambda value: AuthorityStorage().append_production_promotion_grant(value, created_at_ms=1),
        lambda value: AuthorityStorage().append_operator_arm_receipt(value, created_at_ms=1),
        lambda value: AuthorityStorage().insert_authorized_intent_bundle(
            reservation=object(),  # type: ignore[arg-type]
            receipt=object(),  # type: ignore[arg-type]
            intent=value,
            now_ms=1,
        ),
    ],
)
def test_every_private_execution_write_rejects_disabled_binding_before_sql(
    write: Callable[[Any], Any],
) -> None:
    disabled = SimpleNamespace(binding="HYPERLIQUID_PERP")

    with pytest.raises(ValueError, match="execution_binding_disabled:HYPERLIQUID_PERP"):
        write(disabled)
