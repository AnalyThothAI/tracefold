"""Project operator credential presence into redacted durable binding facts (#350).

This module never constructs a provider client.  #356 and #357 own read-only preflight and adapter
construction; this seam only distinguishes absent, locally valid, and invalid operator inputs.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import VenueBinding

_ETH_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ETH_PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class BindingCredentialFact:
    binding: VenueBinding
    state: Literal["unconfigured", "configured", "invalid"]
    fingerprint: str | None
    reason: str


class BindingDatabasePort(Protocol):
    async def tx[T](self, name: str, fn: Any, *, timeout_seconds: float) -> T: ...


def inspect_binding_credentials(settings: Settings) -> tuple[BindingCredentialFact, BindingCredentialFact]:
    return (_binance_credentials(settings), _hyperliquid_credentials(settings))


async def project_binding_credentials(settings: Settings, db: BindingDatabasePort) -> None:
    facts = inspect_binding_credentials(settings)

    def _write(repos: Any) -> None:
        now_ms = int(time.time() * 1_000)
        for fact in facts:
            updated = repos.trading.set_binding_runtime(
                binding=fact.binding,
                credential_state=fact.state,
                credential_fingerprint=fact.fingerprint,
                runtime_state="faulted" if fact.state == "invalid" else "stopped",
                account_state="unknown",
                heartbeat_at_ms=None,
                reason=fact.reason,
                now_ms=now_ms,
            )
            if not updated:
                raise RuntimeError(f"trading_binding_runtime_missing:{fact.binding}")

    await db.tx("trading_binding_credentials", _write, timeout_seconds=10.0)


def _binance_credentials(settings: Settings) -> BindingCredentialFact:
    key = _read(settings.trading_binance_usdm_api_key_file())
    secret = _read(settings.trading_binance_usdm_api_secret_file())
    if key[0] == secret[0] == "missing":
        return BindingCredentialFact("BINANCE_USDM", "unconfigured", None, "credentials_unconfigured")
    if key[0] != "value" or secret[0] != "value":
        return BindingCredentialFact("BINANCE_USDM", "invalid", None, "credentials_invalid")
    return BindingCredentialFact(
        "BINANCE_USDM",
        "configured",
        _fingerprint("BINANCE_USDM", key[1], secret[1]),
        "binding_adapter_unavailable",
    )


def _hyperliquid_credentials(settings: Settings) -> BindingCredentialFact:
    private_key = _read(settings.trading_hyperliquid_private_key_file())
    address = settings.trading.bindings.hyperliquid_perp.account_address
    if private_key[0] == "missing" and address is None:
        return BindingCredentialFact("HYPERLIQUID_PERP", "unconfigured", None, "credentials_unconfigured")
    if (
        private_key[0] != "value"
        or address is None
        or _ETH_PRIVATE_KEY.fullmatch(private_key[1]) is None
        or _ETH_ADDRESS.fullmatch(address) is None
    ):
        return BindingCredentialFact("HYPERLIQUID_PERP", "invalid", None, "credentials_invalid")
    return BindingCredentialFact(
        "HYPERLIQUID_PERP",
        "configured",
        _fingerprint("HYPERLIQUID_PERP", private_key[1], address.lower()),
        "binding_adapter_unavailable",
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


__all__ = ["BindingCredentialFact", "inspect_binding_credentials", "project_binding_credentials"]
