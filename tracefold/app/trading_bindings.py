"""Project the Demo-only credential contract into redacted durable binding facts."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import VenueBinding
from tracefold.trading.storage.root import TradingRepositories


@dataclass(frozen=True, slots=True)
class BindingCredentialFact:
    binding: VenueBinding
    state: Literal["unconfigured", "configured", "invalid"]
    fingerprint: str | None
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class BinanceDemoCredentialSnapshot:
    """One redacted identity and its exact in-memory secret values."""

    fact: BindingCredentialFact
    api_key: str | None = field(repr=False)
    api_secret: str | None = field(repr=False)


class BindingDatabasePort(Protocol):
    async def tx[T](self, name: str, fn: Callable[[TradingRepositories], T], *, timeout_seconds: float) -> T: ...


def inspect_binding_credentials(settings: Settings) -> tuple[BindingCredentialFact, BindingCredentialFact]:
    return (
        load_binance_demo_credential_snapshot(settings).fact,
        BindingCredentialFact(
            "HYPERLIQUID_PERP",
            "unconfigured",
            None,
            "execution_binding_disabled",
        ),
    )


async def project_binding_credentials(settings: Settings, db: BindingDatabasePort) -> None:
    facts = inspect_binding_credentials(settings)

    def _write(repos: TradingRepositories) -> None:
        now_ms = int(time.time() * 1_000)
        for fact in facts:
            updated = repos.trading.project_binding_credentials(
                binding=fact.binding,
                credential_state=fact.state,
                credential_fingerprint=fact.fingerprint,
                runtime_state="faulted" if fact.state == "invalid" else "stopped",
                heartbeat_at_ms=None,
                reason=fact.reason,
                now_ms=now_ms,
            )
            if not updated:
                raise RuntimeError(f"trading_binding_runtime_missing:{fact.binding}")

    await db.tx("trading_binding_credentials", _write, timeout_seconds=10.0)


def load_binance_demo_credential_snapshot(settings: Settings) -> BinanceDemoCredentialSnapshot:
    """Read each Demo secret once so the client and fingerprint share one identity."""

    key = _read(settings.trading_binance_demo_api_key_file())
    secret = _read(settings.trading_binance_demo_api_secret_file())
    if key[0] == secret[0] == "missing":
        return BinanceDemoCredentialSnapshot(
            BindingCredentialFact("BINANCE_USDM", "unconfigured", None, "credentials_unconfigured"),
            None,
            None,
        )
    if key[0] != "value" or secret[0] != "value":
        return BinanceDemoCredentialSnapshot(
            BindingCredentialFact("BINANCE_USDM", "invalid", None, "credentials_invalid"),
            None,
            None,
        )
    return BinanceDemoCredentialSnapshot(
        BindingCredentialFact(
            "BINANCE_USDM",
            "configured",
            _fingerprint("BINANCE_USDM", key[1], secret[1]),
            "binance_demo_runtime_required",
        ),
        key[1],
        secret[1],
    )


def _read(path: Any) -> tuple[str, str]:
    if path is None:
        return "missing", ""
    try:
        return "value", read_secure_secret_text(path)
    except SecretFileError as exc:
        return ("missing" if exc.code in {"missing", "empty"} else exc.code), ""


def _fingerprint(binding: VenueBinding, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(binding.encode())
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode())
    return digest.hexdigest()


__all__ = [
    "BinanceDemoCredentialSnapshot",
    "BindingCredentialFact",
    "inspect_binding_credentials",
    "load_binance_demo_credential_snapshot",
    "project_binding_credentials",
]
