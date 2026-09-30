"""Freeze one LIVE market snapshot, compact CaseView and paper geometry."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from tracefold.trading.engine.case_view import BaseRates, CaseView, build_case_view
from tracefold.trading.engine.features import extract_features, price_plan_window
from tracefold.trading.engine.forecast import LegProbabilities
from tracefold.trading.engine.marketdata import (
    Dataset,
    MarketDataPort,
    MarketDataRequest,
    MarketDataResult,
    analysis_market_request,
)
from tracefold.trading.engine.paper import BAR_MS, Bar, geometry


@dataclass(frozen=True, slots=True)
class PreparedCase:
    view: CaseView
    raw_snapshot_ref: str
    reference_price: Decimal


def write_snapshot(root: Path, payload: dict[str, Any]) -> str:
    """One atomic, content-addressed gzip per Case; the caller owns retention."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    if len(data) > 4_194_304:
        raise ValueError("analysis_snapshot_oversized")
    digest = hashlib.sha256(data).hexdigest()
    target = root / digest[:2] / f"{digest}.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(gzip.decompress(target.read_bytes())).hexdigest() != digest:
            raise ValueError("analysis_snapshot_digest_mismatch")
        return digest
    descriptor, name = tempfile.mkstemp(prefix=".case-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                compressed.write(data)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return digest


def prune_snapshots(root: Path, *, now_s: float) -> int:
    """Keep only the new content-addressed raw snapshots for thirty days."""
    cutoff = now_s - 30 * 86_400
    removed = 0
    if not root.exists():
        return 0
    for directory in root.iterdir():
        if not directory.is_dir() or len(directory.name) != 2:
            continue
        for path in directory.glob("*.json.gz"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed


def _book_request(symbol: str, deadline: float) -> MarketDataRequest:
    return MarketDataRequest(
        dataset="book_ticker",
        native_symbol=symbol,
        venue="binance.usdm",
        environment="live",
        product="perpetual",
        source_identity="binance_public_v1",
        unit_definition="bid_ask_quote_and_base_size_v2",
        start_ms=None,
        end_ms=None,
        interval_ms=None,
        max_age_ms=10_000,
        deadline_at_monotonic=deadline,
    )


def _baseline(side: Literal["long", "short"], value: tuple[int, dict[str, Decimal] | None]) -> BaseRates:
    count, rates = value
    return BaseRates(
        side,
        count,
        None if rates is None else LegProbabilities(rates["tp"], rates["sl"], rates["timeout"]),
    )


class CasePreparer:
    def __init__(self, market_data: MarketDataPort, raw_root: Path, io_executor: Executor) -> None:
        self.market_data = market_data
        self.raw_root = raw_root
        self.io_executor = io_executor

    async def prepare(
        self,
        *,
        case: dict[str, Any],
        source_fact: dict[str, Any],
        base_rates: dict[str, tuple[int, dict[str, Decimal] | None]],
        recent_context: tuple[dict[str, Any], ...] = (),
    ) -> PreparedCase:
        native = str(case["native_symbol"])
        end = int(time.time() * 1000) // BAR_MS * BAR_MS
        deadline = time.monotonic() + 8
        requests = {
            name: analysis_market_request(
                dataset=cast(Dataset, name),
                native_symbol=native,
                end_ms=end if name in ("perp_bars", "spot_bars", "market_bars") else None,
                window_minutes=240 if name == "perp_bars" else 60 if name in ("spot_bars", "market_bars") else None,
                deadline_at_monotonic=deadline,
            )
            for name in ("perp_bars", "spot_bars", "market_bars", "open_interest", "funding_basis")
        }
        requests["book_ticker"] = _book_request(native, deadline)
        responses = await asyncio.gather(*(self.market_data.fetch(item) for item in requests.values()))
        results: dict[str, MarketDataResult] = dict(zip(requests, responses, strict=True))
        cutoff = int(time.time() * 1000)
        if any(
            item.source_identity != requests[name].source_identity
            or (item.received_at_ms is not None and item.received_at_ms > cutoff)
            for name, item in results.items()
        ):
            raise ValueError("market_snapshot_identity_invalid")
        price_rows = price_plan_window(
            results["perp_bars"],
            end_ms=end,
            cutoff_ms=cutoff,
            source_identity=requests["perp_bars"].source_identity,
            unit_definition=requests["perp_bars"].unit_definition,
        )
        if len(price_rows) < 16:
            raise ValueError("geometry_history_incomplete")
        bars = tuple(
            Bar(
                int(row["event_at_ms"]), Decimal(str(row["high"])), Decimal(str(row["low"])), Decimal(str(row["close"]))
            )
            for row in price_rows[-16:]
        )
        leg_geometry = geometry(bars)
        quote = results["book_ticker"]
        if (
            quote.status != "ok"
            or not quote.payload
            or quote.received_at_ms is None
            or cutoff - quote.received_at_ms > 10_000
        ):
            raise ValueError("live_spread_missing")
        bid = Decimal(str(quote.payload[0]["bid"]))
        ask = Decimal(str(quote.payload[0]["ask"]))
        if bid <= 0 or ask < bid:
            raise ValueError("live_spread_invalid")
        half_spread_bps = (ask - bid) / (ask + bid) * 10_000
        features = extract_features(
            results,
            source_fact,
            expected_ends={name: end for name in ("perp_bars", "spot_bars", "market_bars")},
            cutoff_ms=cutoff,
        )
        view = build_case_view(
            case_id=str(case["case_id"]),
            asset_id=str(case["asset_id"]),
            trigger_kind=case["trigger_kind"],
            decided_at_ms=cutoff,
            source_fact=source_fact,
            features=features,
            geometry=leg_geometry,
            half_spread_bps=half_spread_bps,
            base_rates=(_baseline("long", base_rates["long"]), _baseline("short", base_rates["short"])),
            recent_context=recent_context,
            native_symbol=native,
            units_per_contract=None
            if case.get("units_per_contract") is None
            else Decimal(str(case["units_per_contract"])),
            observations={
                name: {
                    "status": result.status,
                    "missing_reasons": result.missing_reasons,
                    "received_age_s": None
                    if result.received_at_ms is None
                    else (cutoff - result.received_at_ms) // 1000,
                    "event_age_s": None if result.event_end_ms is None else (cutoff - result.event_end_ms) // 1000,
                    "source_identity": result.source_identity,
                    "unit_definition": result.unit_definition,
                }
                for name, result in results.items()
            },
            episode={"role": case.get("episode_role") or "unknown", "contract": "episode_v1"},
        )
        view = replace(view, intake_context=case.get("intake_context"))
        snapshot = {
            "intake_context": view.intake_context,
            "snapshot_version": "live_case_snapshot_v1",
            "case_id": case["case_id"],
            "knowledge_cutoff_ms": cutoff,
            "data_environment": "live",
            "source_fact": source_fact,
            "market": {
                name: {
                    "status": result.status,
                    "payload": result.payload,
                    "source_identity": result.source_identity,
                    "received_at_ms": result.received_at_ms,
                    "missing_reasons": result.missing_reasons,
                }
                for name, result in results.items()
            },
            "features": features,
        }
        snapshot_ref = await asyncio.get_running_loop().run_in_executor(
            self.io_executor, write_snapshot, self.raw_root, snapshot
        )
        return PreparedCase(view, snapshot_ref, bars[-1].close)
