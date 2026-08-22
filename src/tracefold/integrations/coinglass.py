"""Pinned coinglass-cli adapter for the internal liquidation-level shadow (#144).

The upstream CLI is intentionally isolated in a killable subprocess: its synchronous retry/protocol-probe
stack can exceed one HTTP timeout, while the Tracefold cold loop owns a hard per-turn deadline. No News
consumer imports or waits on this adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, cast

from tracefold.news import (
    LiquidationFreshness,
    LiquidationTarget,
    LiquidationZone,
    ProviderLiquidationSnapshot,
)

COINGLASS_CLI_COMMIT: Final = "dc8f9d253a8dc1fded6fabcef93c96feeaa4b826"
_PROVIDER: Final = "coinglass_web"
_CONTRACT: Final = "undocumented_public_web_http"
_LEVEL_MAX: Final = 64
_PACKAGED_EXECUTABLE: Final = "/opt/coinglass-cli/bin/coinglass-cli"
_EXECUTABLE_ENV: Final = "TRACEFOLD_COINGLASS_CLI"
_SUBPROCESS_TIMEOUT_SECONDS: Final = 40.0
_STDOUT_MAX_BYTES: Final = 5 * 1024 * 1024


class CoinglassCliLiquidationProvider:
    """No-key CoinGlass web adapter. Shadow-only; no commercial/reader entitlement is implied."""

    def __init__(self, *, timeout_seconds: float = _SUBPROCESS_TIMEOUT_SECONDS) -> None:
        self.timeout = float(timeout_seconds)

    async def fetch(
        self,
        target: LiquidationTarget,
        *,
        model_version: str,
        range_key: str,
    ) -> ProviderLiquidationSnapshot:
        received_at_ms = _clock_ms()
        executable = _coinglass_executable()
        if executable is None:
            return _unavailable_snapshot(
                target,
                received_at_ms=received_at_ms,
                error_class="adapter_unavailable",
                model_version=model_version,
                range_key=range_key,
            )
        command = (
            executable,
            "liquidation-levels",
            "--symbol",
            target.base_symbol,
            "--exchange",
            target.provider_exchange,
            "--quote",
            target.quote_asset,
            "--range",
            range_key,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_MAX_BYTES + 1,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return _unavailable_snapshot(
                target,
                received_at_ms=_clock_ms(),
                error_class="upstream_timeout",
                model_version=model_version,
                range_key=range_key,
            )
        received_at_ms = _clock_ms()
        if len(stdout) > _STDOUT_MAX_BYTES:
            return _unavailable_snapshot(
                target,
                received_at_ms=received_at_ms,
                error_class="payload_too_large",
                model_version=model_version,
                range_key=range_key,
            )
        try:
            envelope = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _unavailable_snapshot(
                target,
                received_at_ms=received_at_ms,
                error_class="subprocess_payload_invalid" if process.returncode == 0 else "subprocess_failed",
                model_version=model_version,
                range_key=range_key,
            )
        return snapshot_from_envelope(
            envelope,
            target=target,
            received_at_ms=received_at_ms,
            model_version=model_version,
            range_key=range_key,
        )


def snapshot_from_envelope(
    envelope: Any,
    *,
    target: LiquidationTarget,
    received_at_ms: int,
    model_version: str,
    range_key: str,
) -> ProviderLiquidationSnapshot:
    """Validate one coinglass-cli envelope and discard all but the strongest 64 raw levels."""

    if not isinstance(envelope, Mapping):
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class="provider_envelope_invalid",
            model_version=model_version,
            range_key=range_key,
        )
    meta = envelope.get("meta") if isinstance(envelope.get("meta"), Mapping) else {}
    upstream = meta.get("upstream_status") if isinstance(meta.get("upstream_status"), Mapping) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), Mapping) else {}
    error_class = str(upstream.get("class") or error.get("code") or "provider_unavailable")
    if envelope.get("ok") is not True or not isinstance(envelope.get("data"), Mapping):
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class=error_class,
            model_version=model_version,
            range_key=range_key,
        )
    data = envelope["data"]
    expected_symbol = f"{target.provider_exchange}_{target.venue_symbol}"
    source = data.get("source") if isinstance(data.get("source"), Mapping) else {}
    if (
        str(data.get("symbol") or "") != expected_symbol
        or str(data.get("range") or "") != range_key
        or str(source.get("provider") or "") != "coinglass"
        or str(source.get("endpoint") or "") != "liquidation_levels_v2"
    ):
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class="provider_contract_drift",
            model_version=model_version,
            range_key=range_key,
        )
    if not isinstance(data.get("levels"), list) or not isinstance(data.get("prices"), list):
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class="provider_contract_drift",
            model_version=model_version,
            range_key=range_key,
        )
    raw_levels = data["levels"]
    raw_prices = data["prices"]
    zones = [zone for raw in raw_levels if (zone := _zone(raw)) is not None]
    if raw_levels and not zones:
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class="provider_payload_invalid",
            model_version=model_version,
            range_key=range_key,
        )
    zones.sort(key=lambda zone: (-abs(zone.size), zone.price, zone.begin_at_ms, zone.x))
    freshness = str(meta.get("freshness") or "fresh")
    if freshness not in {"fresh", "stale"}:
        freshness = "stale"
    try:
        payload_sha256 = hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        return _unavailable_snapshot(
            target,
            received_at_ms=received_at_ms,
            error_class="provider_payload_invalid",
            model_version=model_version,
            range_key=range_key,
        )
    return ProviderLiquidationSnapshot(
        target=target,
        provider=_PROVIDER,
        contract=_CONTRACT,
        authenticated=False,
        completeness="unknown",
        model_version=model_version,
        range_key=range_key,
        zones=tuple(zones[:_LEVEL_MAX]),
        source_at_ms=_latest_source_ms(raw_levels, raw_prices),
        received_at_ms=int(received_at_ms),
        freshness=cast(LiquidationFreshness, freshness),
        degraded=bool(meta.get("degraded")) or freshness != "fresh",
        error_class=None if freshness == "fresh" else error_class,
        payload_sha256=payload_sha256,
        raw_level_count=len(raw_levels),
        raw_price_count=len(raw_prices),
    )


def _zone(raw: Any) -> LiquidationZone | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        price = Decimal(str(raw["price"]))
        size = Decimal(str(raw["size"]))
        begin_at_ms = _timestamp_ms(raw["begin_date"])
        raw_side = int(raw["side"])
        model_level = int(raw["level"])
        x = int(raw["x"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not price.is_finite() or price <= 0 or not size.is_finite() or size < 0 or begin_at_ms is None:
        return None
    return LiquidationZone(
        price=price,
        size=size,
        raw_side=raw_side,
        model_level=model_level,
        model_level2=str(raw.get("level2") or ""),
        begin_at_ms=begin_at_ms,
        x=x,
    )


def _latest_source_ms(levels: Sequence[Any], prices: Sequence[Any]) -> int | None:
    candidates: list[int] = []
    for value in levels:
        if isinstance(value, Mapping):
            stamp = _timestamp_ms(value.get("begin_date"))
            if stamp is not None:
                candidates.append(stamp)
    for value in prices:
        if isinstance(value, Mapping):
            stamp = _timestamp_ms(value.get("timestamp"))
            if stamp is not None:
                candidates.append(stamp)
    return max(candidates) if candidates else None


def _timestamp_ms(value: Any) -> int | None:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    if stamp < 10_000_000_000:
        stamp *= 1000
    while stamp > 10_000_000_000_000:
        stamp /= 1000
    return int(stamp)


def _unavailable_snapshot(
    target: LiquidationTarget,
    *,
    received_at_ms: int,
    error_class: str,
    model_version: str,
    range_key: str,
) -> ProviderLiquidationSnapshot:
    return ProviderLiquidationSnapshot(
        target=target,
        provider=_PROVIDER,
        contract=_CONTRACT,
        authenticated=False,
        completeness="unknown",
        model_version=model_version,
        range_key=range_key,
        zones=(),
        source_at_ms=None,
        received_at_ms=int(received_at_ms),
        freshness="unavailable",
        degraded=True,
        error_class=str(error_class or "unavailable"),
        payload_sha256=None,
        raw_level_count=0,
        raw_price_count=0,
    )


def _clock_ms() -> int:
    import time

    return int(time.time() * 1000)


def _coinglass_executable() -> str | None:
    override = str(os.environ.get(_EXECUTABLE_ENV) or "").strip()
    if override:
        return override
    if os.path.isfile(_PACKAGED_EXECUTABLE) and os.access(_PACKAGED_EXECUTABLE, os.X_OK):
        return _PACKAGED_EXECUTABLE
    return shutil.which("coinglass-cli")


__all__ = ["COINGLASS_CLI_COMMIT", "CoinglassCliLiquidationProvider", "snapshot_from_envelope"]
