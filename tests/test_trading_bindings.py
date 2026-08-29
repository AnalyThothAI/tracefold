from __future__ import annotations

from pathlib import Path

from tracefold.app.trading_bindings import inspect_binding_credentials
from tracefold.platform.config.models import Settings


def _secure(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _settings(path: Path, *, hyperliquid_address: str | None = None) -> Settings:
    settings = Settings(
        trading={
            "enabled": True,
            "bindings": {
                "binance_usdm": {
                    "api_key_file": "binance-key",
                    "api_secret_file": "binance-secret",
                },
                "hyperliquid_perp": {
                    "private_key_file": "hyperliquid-key",
                    "account_address": hyperliquid_address,
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
        ("HYPERLIQUID_PERP", "unconfigured", None, "credentials_unconfigured"),
    ]


def test_empty_init_placeholders_are_unconfigured_not_invalid(tmp_path: Path) -> None:
    for name in ("binance-key", "binance-secret", "hyperliquid-key"):
        _secure(tmp_path / name, "")

    facts = inspect_binding_credentials(_settings(tmp_path))

    assert [fact.state for fact in facts] == ["unconfigured", "unconfigured"]


def test_one_key_configures_only_its_closed_binding(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-value")
    _secure(tmp_path / "binance-secret", "secret-value")

    binance, hyperliquid = inspect_binding_credentials(_settings(tmp_path))

    assert binance.state == "configured" and len(binance.fingerprint or "") == 64
    assert hyperliquid.state == "unconfigured" and hyperliquid.fingerprint is None


def test_dual_key_configuration_has_two_distinct_redacted_fingerprints(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-value")
    _secure(tmp_path / "binance-secret", "secret-value")
    _secure(tmp_path / "hyperliquid-key", "11" * 32)
    settings = _settings(tmp_path, hyperliquid_address="0x" + "22" * 20)

    binance, hyperliquid = inspect_binding_credentials(settings)

    assert (binance.state, hyperliquid.state) == ("configured", "configured")
    assert binance.fingerprint != hyperliquid.fingerprint
    assert "key-value" not in repr((binance, hyperliquid))


def test_incomplete_or_malformed_credentials_are_explicitly_invalid(tmp_path: Path) -> None:
    _secure(tmp_path / "binance-key", "key-without-secret")
    _secure(tmp_path / "hyperliquid-key", "not-a-private-key")

    binance, hyperliquid = inspect_binding_credentials(_settings(tmp_path, hyperliquid_address="0x" + "22" * 20))

    assert (binance.state, binance.reason, binance.fingerprint) == ("invalid", "credentials_invalid", None)
    assert (hyperliquid.state, hyperliquid.reason, hyperliquid.fingerprint) == (
        "invalid",
        "credentials_invalid",
        None,
    )
