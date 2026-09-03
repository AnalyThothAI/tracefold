"""OI chain strategy audit and vendor-frame backtest (2026-09-03).

```yaml
channel: A  # A live read-only | B frozen artifact | C committed snapshot
purpose: "What every rule between an OpenNews OI frame and a Binance order actually is, what the
  deployed chain did to the 310 frames it saw, and what those frames were worth as forward returns.
  It does not answer whether any threshold should change: N=310 over 57 h is far too small to fit on."
window: "Frames observed_at_ms in [1788267180000, 1788471261000] (2026-09-01T12:53Z .. 2026-09-03T21:34Z),
  310 rows. Candles [1788263280000, 1788475500000) (2026-09-01T11:48Z .. 2026-09-03T22:45Z), pinned
  constants below, so a re-run reports the same population rather than a sliding one."
identity: "trading_admission_v8 / source_native_oi_smart_money_long_v4 / oi_signal_v1 /
  opennews_oi_source_v1; the policy and admission config digests the frames were actually decided under
  are read from the ledger and re-derived from code, and the receipt fails the run when they disagree."
safety: "Reads production PostgreSQL in a session pinned default_transaction_read_only=on, plus public
  Binance USD-M and Hyperliquid REST candles cached under ~/.tracefold/research/oi_backtest_cache/.
  It writes exactly one file, the docs/research receipt. No business table, no private venue endpoint,
  no credential is read or printed; the database password is read from the operator's own config path
  and never leaves the process."
```

Run:

    ALL_PROXY= all_proxy= uv run python notebooks/oi_chain_backtest_2026_09_03.py

Output: `docs/research/oi-chain-backtest-2026-09-03.json`, the receipt every table in
`docs/research/oi-chain-backtest-2026-09-03.md` cites.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tracefold.integrations.venues.candles import (  # noqa: E402
    BINANCE_FUTURES_BASE_URL,
    HYPERLIQUID_BASE_URL,
)
from tracefold.integrations.venues.http import get_json, post_json, price_client  # noqa: E402
from tracefold.trading.admission import ADMISSION_VERSION, AdmissionConfig  # noqa: E402
from tracefold.trading.market_context import DEFAULT_PRICE_WINDOW  # noqa: E402
from tracefold.trading.policy import ALPHA_POLICY  # noqa: E402

# ---------------------------------------------------------------------------
# Pinned constants. A live read has no identity of its own, so the window is a
# constant here rather than "the last three days".
# ---------------------------------------------------------------------------

WINDOW_START_MS = 1_788_267_180_000  # 2026-09-01T12:53:00Z, the first frame
WINDOW_END_MS = 1_788_471_261_000  # 2026-09-03T21:34:21Z, the last frame
CANDLE_START_MS = 1_788_263_280_000  # window start minus 1 h lookback and one bar
CANDLE_CUTOFF_MS = 1_788_475_500_000  # 2026-09-03T22:45:00Z; a bar counts only if it closed by then

BAR_MS = 300_000
ROUND_TRIP_COST_BPS = 10  # both sides together; Binance USD-M taker is ~4.5 bps per side
HORIZON_BARS = {"15m": 3, "1h": 12, "4h": 48, "24h": 288}
MAE_MFE_BARS = 48  # 4 h
STOP_BPS = (100, 200, 300)
PERMUTATION_DRAWS = 2_000
BOOTSTRAP_DRAWS = 10_000
SEED = 20_260_903

CACHE_DIR = Path(os.environ.get("TRACEFOLD_OI_BACKTEST_CACHE", "~/.tracefold/research/oi_backtest_cache"))
RECEIPT_PATH = REPO_ROOT / "docs" / "research" / "oi-chain-backtest-2026-09-03.json"
PG_HOST = os.environ.get("TRACEFOLD_RESEARCH_PG_HOST", "172.18.0.4")
PG_PORT = os.environ.get("TRACEFOLD_RESEARCH_PG_PORT", "5432")
CONFIG_DIR = Path(os.environ.get("TRACEFOLD_HOME", "~/.tracefold")).expanduser()

BINANCE_DEMO_BASE_URL = "https://demo-fapi.binance.com"  # BinanceEnvironment.DEMO, what `mode: paper` discovers
_USDT_PERP_CONTRACTS = ("PERPETUAL", "TRADIFI_PERPETUAL")

BINANCE_SOURCE_VENUE = "binance"
HYPERLIQUID_SOURCE_VENUE = "hyperliquid"


# ---------------------------------------------------------------------------
# Database: one read-only session, one query per ledger.
# ---------------------------------------------------------------------------


def _read_rows() -> dict[str, list[dict[str, Any]]]:
    import psycopg
    from psycopg.rows import dict_row

    password = (CONFIG_DIR / "postgres_database_password").read_text(encoding="utf-8").strip()
    conninfo = f"host={PG_HOST} port={PG_PORT} user=tracefold dbname=tracefold"
    out: dict[str, list[dict[str, Any]]] = {}
    with (
        psycopg.connect(
            conninfo,
            password=password,
            connect_timeout=5,
            options="-c default_transaction_read_only=on",
            row_factory=dict_row,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SHOW default_transaction_read_only")
        row = cur.fetchone()
        if row is None or row["default_transaction_read_only"] != "on":
            raise RuntimeError("research_session_not_read_only")
        cur.execute(
            """
            SELECT event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd,
                   whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, available_at_ms,
                   measurement_window_ms, source_venue, source_strategy_id, source_contract_version
            FROM news_oi_signals
            WHERE observed_at_ms BETWEEN %s AND %s
            ORDER BY observed_at_ms, event_id
            """,
            (WINDOW_START_MS, WINDOW_END_MS),
        )
        out["frames"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT source_key, gate_version, gate_config_digest, status, stage, reason,
                   source_observed_at_ms, attempt_count, case_id, evidence
            FROM trading_candidate_gate_decisions
            WHERE source_observed_at_ms BETWEEN %s AND %s
            """,
            (WINDOW_START_MS, WINDOW_END_MS),
        )
        out["gate"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT case_id, underlying_key, state, policy_decision, policy_reason,
                   observed_at_ms, source_observed_at_ms, manifest, strategy_id,
                   strategy_version, strategy_config_digest
            FROM trading_cases
            WHERE source_observed_at_ms BETWEEN %s AND %s
            """,
            (WINDOW_START_MS, WINDOW_END_MS),
        )
        out["cases"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT signal_id, case_id, market_key, direction, observed_at_ns, expires_at_ns
            FROM trading_trade_signals ORDER BY observed_at_ns
            """
        )
        out["signals"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT signal_id, normalized_kind, occurred_at_ns, summary
            FROM trading_execution_observations
            WHERE normalized_kind IN ('signal_disposition', 'order', 'fill', 'position', 'protection')
            ORDER BY occurred_at_ns
            """
        )
        out["observations"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT account_slot, mode, routes, started_at_ns FROM trading_execution_runtime_state")
        out["runtime"] = [dict(r) for r in cur.fetchall()]
    return out


# ---------------------------------------------------------------------------
# Candles. The repository's own adapters return only `close` (`reaction_v1`
# keeps no OHLC), and MAE/MFE and a stop-trigger rate need the high and the
# low, so this reads the same providers through the same `price_client` /
# `get_json` / `post_json` policy and the same base URLs, and applies the same
# "a bar whose high is below its own open or close is a row the provider did
# not mean" filter that `_fetch_binance_reaction_candles` applies.
# ---------------------------------------------------------------------------


async def _exchange_info(client: Any, base_url: str, venue: str) -> set[str]:
    """USDT-settled perpetual base assets a Runtime could route, on one Binance environment."""

    payload = await get_json(client, f"{base_url}/fapi/v1/exchangeInfo", venue=venue)
    return {
        str(row["baseAsset"])
        for row in payload.get("symbols", [])
        if row.get("contractType") in _USDT_PERP_CONTRACTS
        and row.get("quoteAsset") == "USDT"
        and row.get("marginAsset") == "USDT"
        and row.get("status") == "TRADING"
    }


async def _catalogues() -> dict[str, Any]:
    """The two listings the route rule could read. Cached like the candles, for the same reason."""

    path = _cache_dir() / "binance_catalogues.json"
    if path.exists():
        return dict(json.loads(path.read_text(encoding="utf-8")))
    async with price_client() as client:
        live = await _exchange_info(client, BINANCE_FUTURES_BASE_URL, "binance.perp")
        demo = await _exchange_info(client, BINANCE_DEMO_BASE_URL, "binance.demo")
    out = {
        "fetched_at_utc": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "live": sorted(live),
        "demo": sorted(demo),
    }
    path.write_text(json.dumps(out), encoding="utf-8")
    return out


@dataclass(frozen=True, slots=True)
class Bar:
    open_at_ms: int
    close_at_ms: int
    open: float
    high: float
    low: float
    close: float


async def _fetch_bars(client: Any, venue: str, symbol: str) -> list[list[Any]]:
    if venue == BINANCE_SOURCE_VENUE:
        rows = await get_json(
            client,
            f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines",
            venue="binance.perp",
            params={
                "symbol": f"{symbol}USDT",
                "interval": "5m",
                "startTime": CANDLE_START_MS,
                "endTime": CANDLE_CUTOFF_MS,
                "limit": 1000,
            },
        )
        return [[int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4])] for r in rows]
    rows = await post_json(
        client,
        f"{HYPERLIQUID_BASE_URL}/info",
        {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": "5m",
                "startTime": CANDLE_START_MS,
                "endTime": CANDLE_CUTOFF_MS,
            },
        },
        venue="hl.perp",
    )
    return [[int(r["t"]), str(r["o"]), str(r["h"]), str(r["l"]), str(r["c"])] for r in rows]


def _cache_dir() -> Path:
    """Resolved outside the coroutine: a blocking filesystem call inside one is what ASYNC240 names."""

    directory = CACHE_DIR.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def _load_cache(pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    cache_dir = _cache_dir()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    missing = [pair for pair in pairs if not (cache_dir / f"{pair[0]}__{pair[1]}.json").exists()]
    if missing:
        async with price_client() as client:
            for venue, symbol in missing:
                try:
                    rows, error = await _fetch_bars(client, venue, symbol), None
                except Exception as exc:
                    rows, error = [], f"{type(exc).__name__}:{exc}"
                payload = {
                    "venue": venue,
                    "symbol": symbol,
                    "start_ms": CANDLE_START_MS,
                    "end_ms": CANDLE_CUTOFF_MS,
                    "bars": rows,
                    "error": error,
                }
                (cache_dir / f"{venue}__{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")
                await asyncio.sleep(0.3)
    for venue, symbol in pairs:
        out[(venue, symbol)] = json.loads((cache_dir / f"{venue}__{symbol}.json").read_text(encoding="utf-8"))
    return out


def _series(payload: Mapping[str, Any]) -> dict[int, Bar]:
    """`close_at_ms -> Bar`, dropping the provider rows a real trade never printed."""

    out: dict[int, Bar] = {}
    for row in payload.get("bars") or []:
        open_at_ms = int(row[0])
        close_at_ms = open_at_ms + BAR_MS
        if close_at_ms > CANDLE_CUTOFF_MS:
            continue
        o, h, low, c = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        if min(o, h, low, c) <= 0 or h < max(o, c) or low > min(o, c):
            continue
        out[close_at_ms] = Bar(open_at_ms, close_at_ms, o, h, low, c)
    return out


def _select_close(series: Mapping[int, Bar], *, target_ms: int, tolerance_ms: int) -> float | None:
    """`market_context.select_bar`, over a dict keyed by the same exclusive close instant."""

    aligned = (target_ms // BAR_MS) * BAR_MS
    for step in range(int(tolerance_ms // BAR_MS) + 2):
        close_at = aligned - step * BAR_MS
        if target_ms - close_at > tolerance_ms:
            return None
        bar = series.get(close_at)
        if bar is not None:
            return bar.close
    return None


def _move_bps(p0: float | None, p1: float | None) -> int | None:
    if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
        return None
    return round((p1 / p0 - 1) * 10_000)


# ---------------------------------------------------------------------------
# One frame's forward outcome.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Outcome:
    entry_close_at_ms: int | None = None
    entry_price: float | None = None
    forward_bps: dict[str, int | None] = None  # type: ignore[assignment]
    mae_bps: int | None = None
    mfe_bps: int | None = None
    stop_hit: dict[int, bool | None] = None  # type: ignore[assignment]
    stopped_bps: dict[tuple[int, str], int | None] = None  # type: ignore[assignment]
    bars_after_entry: int = 0


def _outcome(series: Mapping[int, Bar], *, observed_at_ms: int) -> Outcome:
    """Enter at the first 5-minute close strictly after the frame, long, and measure forward."""

    out = Outcome(forward_bps={}, stop_hit={}, stopped_bps={})
    entry_close_at = (observed_at_ms // BAR_MS + 1) * BAR_MS
    entry_bar = series.get(entry_close_at)
    if entry_bar is None:
        return out
    entry = entry_bar.close
    out.entry_close_at_ms, out.entry_price = entry_close_at, entry

    for name, bars in HORIZON_BARS.items():
        exit_bar = series.get(entry_close_at + bars * BAR_MS)
        out.forward_bps[name] = None if exit_bar is None else _move_bps(entry, exit_bar.close)

    forward = [series.get(entry_close_at + step * BAR_MS) for step in range(1, MAE_MFE_BARS + 1)]
    present = [bar for bar in forward if bar is not None]
    out.bars_after_entry = len(present)
    # MAE/MFE only over a complete 4 h window: a truncated extreme is a smaller number for a shorter
    # window, which would read as a calmer market rather than as missing data.
    if len(present) == MAE_MFE_BARS:
        out.mae_bps = _move_bps(entry, min(bar.low for bar in present))
        out.mfe_bps = _move_bps(entry, max(bar.high for bar in present))

    for stop in STOP_BPS:
        level = entry * (1 - stop / 10_000)
        hit_index = next((i for i, bar in enumerate(forward) if bar is not None and bar.low <= level), None)
        complete = len(present) == MAE_MFE_BARS
        out.stop_hit[stop] = True if hit_index is not None else (False if complete else None)
        for name, bars in (("1h", 12), ("4h", 48)):
            out.stopped_bps[(stop, name)] = _stopped_return(
                forward, entry=entry, level=level, stop=stop, hold_bars=bars
            )
    return out


def _stopped_return(
    forward: Sequence[Bar | None], *, entry: float, level: float, stop: int, hold_bars: int
) -> int | None:
    """Long, exit at the stop level on the first bar that trades through it, else at the horizon close."""

    for i in range(hold_bars):
        bar = forward[i] if i < len(forward) else None
        if bar is None:
            return None
        if bar.low <= level:
            return -stop
    exit_bar = forward[hold_bars - 1] if hold_bars - 1 < len(forward) else None
    return None if exit_bar is None else _move_bps(entry, exit_bar.close)


# ---------------------------------------------------------------------------
# Statistics. Small-N honesty: every table carries N, and every mean carries a
# bootstrap interval rather than a point estimate on its own.
# ---------------------------------------------------------------------------


def _describe(values: Sequence[float], *, rng: random.Random | None = None) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0}
    out: dict[str, Any] = {
        "n": len(clean),
        "mean_bps": round(statistics.fmean(clean), 1),
        "median_bps": round(statistics.median(clean), 1),
        "win_rate": round(sum(1 for v in clean if v > 0) / len(clean), 4),
    }
    if len(clean) > 1:
        out["sd_bps"] = round(statistics.stdev(clean), 1)
        out["se_bps"] = round(statistics.stdev(clean) / len(clean) ** 0.5, 1)
    if rng is not None and len(clean) > 1:
        low, high = _bootstrap_ci(clean, rng)
        out["mean_ci95_bps"] = [round(low, 1), round(high, 1)]
    return out


def _bootstrap_ci(values: Sequence[float], rng: random.Random) -> tuple[float, float]:
    n = len(values)
    means = sorted(statistics.fmean(values[rng.randrange(n)] for _ in range(n)) for _ in range(BOOTSTRAP_DRAWS))
    return means[int(0.025 * BOOTSTRAP_DRAWS)], means[int(0.975 * BOOTSTRAP_DRAWS) - 1]


def _rate(flags: Iterable[bool | None]) -> dict[str, Any]:
    known = [f for f in flags if f is not None]
    if not known:
        return {"n": 0}
    return {"n": len(known), "rate": round(sum(known) / len(known), 4)}


# ---------------------------------------------------------------------------
# The chain, rule by rule, as data the report renders.
# ---------------------------------------------------------------------------


def _rule_table() -> list[dict[str, Any]]:
    admission = AdmissionConfig(min_oi_value_usd=5_000_000)
    policy = ALPHA_POLICY.config
    return [
        {
            "step": 1,
            "module": "tracefold/news/oi_signals.py",
            "rule": "parse the vendor title into oi_change/oi_value/whale_long_profit/whale_oi_ratio",
            "threshold": "regex on `OI Rise x%, OI Value yM, Whale Long Profit z%, Whale/OI Ratio w%`",
            "configurable": False,
            "on_failure": "no news_oi_signals row exists",
        },
        {
            "step": 2,
            "module": "tracefold/trading/sources.py::normalize_oi_source",
            "rule": (
                "symbol canonical and ASCII-identity, ingest_mode=live, both clocks and all four "
                "measurements present, direction in {rise,fall}"
            ),
            "threshold": "^[A-Z0-9][A-Z0-9:_-]{0,110}$",
            "configurable": False,
            "on_failure": "REJECTED/source/source_contract_invalid",
        },
        {
            "step": 3,
            "module": "tracefold/trading/admission.py::admit_venue",
            "rule": "source venue resolves to binance.usdm / hyperliquid.perp / hyperliquid.xyz",
            "threshold": "closed map in sources._SOURCE_VENUES",
            "configurable": False,
            "on_failure": "REJECTED/venue/venue_unresolved",
        },
        {
            "step": 4,
            "module": "tracefold/trading/admission.py::admit_trigger",
            "rule": "idempotency: this source_key already produced a Case",
            "threshold": "-",
            "configurable": False,
            "on_failure": "REJECTED/eligibility/already_consumed",
        },
        {
            "step": 5,
            "module": "tracefold/trading/admission.py::admit_frame",
            "rule": "oi_value_usd >= min_oi_value_usd",
            "threshold": f"{admission.min_oi_value_usd} USD (code default {AdmissionConfig().min_oi_value_usd})",
            "configurable": True,
            "on_failure": "REJECTED/eligibility/oi_value_below_floor",
        },
        {
            "step": 6,
            "module": "tracefold/trading/admission.py::admit_trigger",
            "rule": "now - observed_at_ms <= max_age_ms",
            "threshold": f"{admission.max_age_ms} ms",
            "configurable": True,
            "on_failure": "EXPIRED/eligibility/trigger_stale",
        },
        {
            "step": 7,
            "module": "tracefold/trading/admission.py::admit_trigger",
            "rule": "this underlying has no undecided Case",
            "threshold": "1 in flight per underlying",
            "configurable": False,
            "on_failure": "DEFERRED/eligibility/underlying_busy (retried)",
        },
        {
            "step": 8,
            "module": "tracefold/trading/signal_lane.py::_admit",
            "rule": "market_key present in the Runtime's published route catalogue",
            "threshold": "trading_execution_runtime_state.routes",
            "configurable": False,
            "on_failure": "REJECTED/eligibility/instrument_unmapped",
        },
        {
            "step": 9,
            "module": "tracefold/trading/signal_lane.py::_admit",
            "rule": "one frame per underlying per turn, newest observed_at wins",
            "threshold": "-",
            "configurable": False,
            "on_failure": "DEFERRED/eligibility/superseded_by_newer_trigger",
        },
        {
            "step": 10,
            "module": "tracefold/trading/signal_lane.py::_advance_turn",
            "rule": "at most _MAX_FREEZES_PER_TURN Cases frozen per lane turn",
            "threshold": "1 per turn",
            "configurable": False,
            "on_failure": "DEFERRED/eligibility/lane_capacity_exhausted",
        },
        {
            "step": 11,
            "module": "tracefold/trading/signal_lane.py::_freeze",
            "rule": "a candle closed at or before observed_at_ms, within the gap tolerance",
            "threshold": f"{DEFAULT_PRICE_WINDOW.bar_gap_tolerance_ms} ms",
            "configurable": False,
            "on_failure": "DEFERRED market_data_unavailable / REJECTED market_data_invalid",
        },
        {
            "step": 12,
            "module": "tracefold/trading/policy.py",
            "rule": "measurement_window_ms == the policy's window",
            "threshold": f"{policy.measurement_window_ms} ms",
            "configurable": False,
            "on_failure": "NO_TRADE/source_window_mismatch",
        },
        {
            "step": 13,
            "module": "tracefold/trading/policy.py",
            "rule": "oi_direction == rise",
            "threshold": "rise",
            "configurable": False,
            "on_failure": "NO_TRADE/not_oi_rise",
        },
        {
            "step": 14,
            "module": "tracefold/trading/policy.py",
            "rule": "oi_change_bps >= min_oi_change_bps",
            "threshold": f"{policy.min_oi_change_bps} bps",
            "configurable": False,
            "on_failure": "NO_TRADE/smart_money_oi_change_below_floor",
        },
        {
            "step": 15,
            "module": "tracefold/trading/policy.py",
            "rule": "whale_oi_ratio_bps > min_whale_oi_ratio_bps",
            "threshold": f"{policy.min_whale_oi_ratio_bps} bps (strict)",
            "configurable": False,
            "on_failure": "NO_TRADE/smart_money_ratio_below_or_equal_floor",
        },
        {
            "step": 16,
            "module": "tracefold/trading/policy.py",
            "rule": "whale_long_profit_bps > min_whale_long_profit_bps",
            "threshold": f"{policy.min_whale_long_profit_bps} bps (strict)",
            "configurable": False,
            "on_failure": "NO_TRADE/smart_money_profit_not_positive",
        },
        {
            "step": 17,
            "module": "tracefold/trading/policy.py + market_context.py",
            "rule": "pre_move_bps >= min_price_move_bps over a 1 h lookback",
            "threshold": f"{policy.min_price_move_bps} bps, lookback {DEFAULT_PRICE_WINDOW.lookback_ms} ms",
            "configurable": False,
            "on_failure": "NO_TRADE/price_direction_not_confirmed",
        },
        {
            "step": 18,
            "module": "tracefold/trading/policy.py",
            "rule": "pre_move_bps <= max_price_move_bps",
            "threshold": f"{policy.max_price_move_bps} bps",
            "configurable": False,
            "on_failure": "NO_TRADE/move_above_band_chasing",
        },
        {
            "step": 19,
            "module": "tracefold/trading/signal_lane.py::_decide_one",
            "rule": "Signal TTL, clamped to the source deadline",
            "threshold": "min(180000 ms, observed_at + max_age_ms)",
            "configurable": False,
            "on_failure": "runtime disposition `expired`",
        },
        {
            "step": 20,
            "module": "oi_runtime/entry.py::handle",
            "rule": "market_key present in this Runtime's own route map",
            "threshold": "the discovered catalogue (paper => Binance demo listing)",
            "configurable": False,
            "on_failure": "disposition instrument_unmapped",
        },
        {
            "step": 21,
            "module": "oi_runtime/entry.py::handle",
            "rule": "no other active execution on the same instrument",
            "threshold": "1",
            "configurable": False,
            "on_failure": "disposition instrument_busy",
        },
        {
            "step": 22,
            "module": "oi_runtime/entry.py::handle",
            "rule": "instrument and quote tick in cache after the subscription warm-up",
            "threshold": "QUOTE_WARMUP_NS",
            "configurable": False,
            "on_failure": "market_subscription_pending / instrument_or_market_missing",
        },
        {
            "step": 23,
            "module": "oi_runtime/risk.py::evaluate_entry",
            "rule": "no unexpected exposure; market/account/reconciliation clocks fresh",
            "threshold": "market 5 s; account 2x, reconciliation 3x the 5 s scan period",
            "configurable": True,
            "on_failure": "halt: unexpected_exposure / market_stale / account_stale / reconciliation_stale",
        },
        {
            "step": 24,
            "module": "oi_runtime/risk.py::evaluate_entry",
            "rule": "day-start equity minus current equity < max_daily_loss_usd",
            "threshold": "25 USD",
            "configurable": True,
            "on_failure": "halt: daily_loss_limit",
        },
        {
            "step": 25,
            "module": "oi_runtime/risk.py::evaluate_entry",
            "rule": "open positions < max_positions; leverage <= max_leverage",
            "threshold": "1 position, 1x",
            "configurable": True,
            "on_failure": "deny: position_limit / leverage_limit",
        },
        {
            "step": 26,
            "module": "oi_runtime/risk.py::evaluate_entry",
            "rule": "risk budget = min(1% equity, 10 USD, 25 USD total - aggregate risk)",
            "threshold": "1% / 10 USD / 25 USD",
            "configurable": True,
            "on_failure": "deny: aggregate_risk_limit; or reduce",
        },
        {
            "step": 27,
            "module": "oi_runtime/entry.py::_sized_quantity",
            "rule": "spread <= 30 bps; entry priced at ask x (1 + 25 bps)",
            "threshold": "30 bps / 25 bps",
            "configurable": False,
            "on_failure": "disposition spread_limit",
        },
        {
            "step": 28,
            "module": "oi_runtime/risk.py::fixed_risk_quantity",
            "rule": "quantity = risk / stop_fraction / price, clamped by leverage, floored to the increment",
            "threshold": "stop 100 bps => notional = risk x 100",
            "configurable": True,
            "on_failure": "quantity_below_increment / _minimum / notional_below_minimum",
        },
        {
            "step": 29,
            "module": "oi_runtime/entry.py",
            "rule": "market BUY, reduce_only=False, deterministic client order id",
            "threshold": "-",
            "configurable": False,
            "on_failure": "unknown_query_first",
        },
        {
            "step": 30,
            "module": "oi_runtime/protection.py",
            "rule": "reduce-only stop at avg_entry x (1 - stop_distance_bps)",
            "threshold": "100 bps",
            "configurable": True,
            "on_failure": "protection pending/unprotected; entries disarmed",
        },
        {
            "step": 31,
            "module": "oi_runtime/exit.py",
            "rule": "no take-profit and no time-based exit; only the stop or an operator flatten closes",
            "threshold": "-",
            "configurable": False,
            "on_failure": "-",
        },
    ]


# ---------------------------------------------------------------------------
# Segments.
# ---------------------------------------------------------------------------


def _bucket(value: int, edges: Sequence[tuple[str, Callable[[int], bool]]]) -> str:
    for name, test in edges:
        if test(value):
            return name
    return "other"


OI_CHANGE_BUCKETS: tuple[tuple[str, Callable[[int], bool]], ...] = (
    ("<3%", lambda v: v < 300),
    ("3-5%", lambda v: v < 500),
    ("5-10%", lambda v: v < 1000),
    (">=10%", lambda v: True),
)
RATIO_BUCKETS: tuple[tuple[str, Callable[[int], bool]], ...] = (
    ("<=50%", lambda v: v <= 5000),
    ("50-80%", lambda v: v <= 8000),
    (">80%", lambda v: True),
)
PROFIT_BUCKETS: tuple[tuple[str, Callable[[int], bool]], ...] = (
    ("<=0%", lambda v: v <= 0),
    ("0-50%", lambda v: v <= 5000),
    (">50%", lambda v: True),
)
PRE_MOVE_BUCKETS: tuple[tuple[str, Callable[[int], bool]], ...] = (
    ("<0%", lambda v: v < 0),
    ("0-3%", lambda v: v <= 300),
    ("3-6%", lambda v: v <= 600),
    ("6-10%", lambda v: v <= 1000),
    (">10%", lambda v: True),
)


@dataclass(slots=True)
class Row:
    frame: dict[str, Any]
    series_key: tuple[str, str]
    pre_move_bps: int | None
    outcome: Outcome
    policy_pass: bool
    policy_reason: str


def _policy_replay(frame: Mapping[str, Any], pre_move: int | None) -> tuple[bool, str]:
    policy = ALPHA_POLICY.config
    if int(frame["measurement_window_ms"] or 0) != policy.measurement_window_ms:
        return False, "source_window_mismatch"
    if str(frame["direction"]) != "rise":
        return False, "not_oi_rise"
    if int(frame["oi_change_bps"]) < policy.min_oi_change_bps:
        return False, "smart_money_oi_change_below_floor"
    if int(frame["whale_oi_ratio_bps"]) <= policy.min_whale_oi_ratio_bps:
        return False, "smart_money_ratio_below_or_equal_floor"
    if int(frame["whale_long_profit_bps"]) <= policy.min_whale_long_profit_bps:
        return False, "smart_money_profit_not_positive"
    if pre_move is None or pre_move < policy.min_price_move_bps:
        return False, "price_direction_not_confirmed"
    if pre_move > policy.max_price_move_bps:
        return False, "move_above_band_chasing"
    return True, "smart_money_momentum_long"


def _segment_stats(rows: Sequence[Row], rng: random.Random) -> dict[str, Any]:
    out: dict[str, Any] = {"n_frames": len(rows)}
    out["n_with_entry"] = sum(1 for r in rows if r.outcome.entry_price is not None)
    out["n_complete_4h"] = sum(1 for r in rows if r.outcome.bars_after_entry == MAE_MFE_BARS)
    for name in HORIZON_BARS:
        values = [r.outcome.forward_bps.get(name) for r in rows if r.outcome.entry_price is not None]
        out[f"fwd_{name}_gross"] = _describe([v for v in values if v is not None], rng=rng)
        out[f"fwd_{name}_net"] = _describe(
            [v - ROUND_TRIP_COST_BPS for v in values if v is not None], rng=rng if name == "4h" else None
        )
    mae = [r.outcome.mae_bps for r in rows if r.outcome.mae_bps is not None]
    mfe = [r.outcome.mfe_bps for r in rows if r.outcome.mfe_bps is not None]
    out["mae_4h"] = {
        "n": len(mae),
        "median_bps": round(statistics.median(mae), 1) if mae else None,
        "mean_bps": round(statistics.fmean(mae), 1) if mae else None,
    }
    out["mfe_4h"] = {
        "n": len(mfe),
        "median_bps": round(statistics.median(mfe), 1) if mfe else None,
        "mean_bps": round(statistics.fmean(mfe), 1) if mfe else None,
    }
    for stop in STOP_BPS:
        out[f"stop_{stop}bps_hit_4h"] = _rate(r.outcome.stop_hit.get(stop) for r in rows)
        stopped = [r.outcome.stopped_bps.get((stop, "4h")) for r in rows]
        out[f"stopped_{stop}bps_4h_net"] = _describe([v - ROUND_TRIP_COST_BPS for v in stopped if v is not None])
    return out


# ---------------------------------------------------------------------------
# Baseline and permutation.
# ---------------------------------------------------------------------------


def _valid_entries(series: Mapping[int, Bar], *, horizon_bars: int) -> list[int]:
    """Every 5-minute close inside the frame window that still has `horizon_bars` of forward data."""

    out = []
    for close_at in sorted(series):
        if not (WINDOW_START_MS <= close_at <= WINDOW_END_MS):
            continue
        if close_at + horizon_bars * BAR_MS in series:
            out.append(close_at)
    return out


def _baseline(
    rows: Sequence[Row],
    series_by_key: Mapping[tuple[str, str], dict[int, Bar]],
    rng: random.Random,
) -> dict[str, Any]:
    pool = [r for r in rows if r.outcome.forward_bps.get("4h") is not None]
    entries = {key: _valid_entries(series, horizon_bars=48) for key, series in series_by_key.items()}
    draws: list[float] = []
    outcomes: list[Outcome] = []
    for _ in range(PERMUTATION_DRAWS):
        row = pool[rng.randrange(len(pool))]
        candidates = entries[row.series_key]
        if not candidates:
            continue
        close_at = candidates[rng.randrange(len(candidates))]
        series = series_by_key[row.series_key]
        value = _move_bps(series[close_at].close, series[close_at + 48 * BAR_MS].close)
        if value is None:
            continue
        draws.append(value)
        # The same random entry, scored the same way, so "85% of frames hit a 1% stop" has a base rate
        # to be 85% *of*. Entry is the bar itself here, so shift back one bar to reuse `_outcome`.
        outcomes.append(_outcome(series, observed_at_ms=close_at - BAR_MS))
    mae = [o.mae_bps for o in outcomes if o.mae_bps is not None]
    mfe = [o.mfe_bps for o in outcomes if o.mfe_bps is not None]
    return {
        "draws": len(draws),
        "definition": "same symbol and venue, a uniformly random 5-minute close in the frame window with 4 h forward",
        "fwd_4h_gross": _describe(draws, rng=rng),
        "fwd_4h_net": _describe([v - ROUND_TRIP_COST_BPS for v in draws]),
        "mae_4h": {"n": len(mae), "median_bps": round(statistics.median(mae), 1) if mae else None},
        "mfe_4h": {"n": len(mfe), "median_bps": round(statistics.median(mfe), 1) if mfe else None},
        **{f"stop_{stop}bps_hit_4h": _rate(o.stop_hit.get(stop) for o in outcomes) for stop in STOP_BPS},
        **{
            f"stopped_{stop}bps_4h_net": _describe(
                [
                    o.stopped_bps[(stop, "4h")] - ROUND_TRIP_COST_BPS
                    for o in outcomes
                    if o.stopped_bps.get((stop, "4h")) is not None
                ]
            )
            for stop in STOP_BPS
        },
    }


def _permutation_p(
    rows: Sequence[Row],
    series_by_key: Mapping[tuple[str, str], dict[int, Bar]],
    rng: random.Random,
    *,
    horizon: str = "4h",
) -> dict[str, Any]:
    """Null: entry timing carries no information; the cohort's symbol mix is held fixed."""

    bars = HORIZON_BARS[horizon]
    pool = [r for r in rows if r.outcome.forward_bps.get(horizon) is not None]
    if len(pool) < 2:
        return {"n": len(pool)}
    observed = statistics.fmean(float(r.outcome.forward_bps[horizon]) for r in pool)  # type: ignore[arg-type]
    entries = {key: _valid_entries(series, horizon_bars=bars) for key, series in series_by_key.items()}
    ge = 0
    means: list[float] = []
    for _ in range(PERMUTATION_DRAWS):
        total, count = 0.0, 0
        for row in pool:
            candidates = entries[row.series_key]
            if not candidates:
                continue
            close_at = candidates[rng.randrange(len(candidates))]
            series = series_by_key[row.series_key]
            value = _move_bps(series[close_at].close, series[close_at + bars * BAR_MS].close)
            if value is not None:
                total += value
                count += 1
        if count:
            mean = total / count
            means.append(mean)
            ge += mean >= observed
    means.sort()
    return {
        "n": len(pool),
        "horizon": horizon,
        "observed_mean_bps": round(observed, 1),
        "permutations": len(means),
        "null_mean_bps": round(statistics.fmean(means), 1) if means else None,
        "null_p05_bps": round(means[int(0.05 * len(means))], 1) if means else None,
        "null_p95_bps": round(means[int(0.95 * len(means)) - 1], 1) if means else None,
        "p_one_sided_greater": round((1 + ge) / (1 + len(means)), 4) if means else None,
    }


def _permutation_stop_p(
    rows: Sequence[Row],
    series_by_key: Mapping[tuple[str, str], dict[int, Bar]],
    rng: random.Random,
    *,
    stop: int = 100,
    hold: str = "4h",
) -> dict[str, Any]:
    """The same null, scored under the exit the Runtime actually executes: market in, stop out."""

    bars = HORIZON_BARS[hold]
    pool = [r for r in rows if r.outcome.stopped_bps.get((stop, hold)) is not None]
    if len(pool) < 2:
        return {"n": len(pool)}
    observed = statistics.fmean(float(r.outcome.stopped_bps[(stop, hold)]) for r in pool)  # type: ignore[arg-type]
    entries = {key: _valid_entries(series, horizon_bars=bars) for key, series in series_by_key.items()}
    ge = 0
    means: list[float] = []
    for _ in range(PERMUTATION_DRAWS):
        total, count = 0.0, 0
        for row in pool:
            candidates = entries[row.series_key]
            if not candidates:
                continue
            close_at = candidates[rng.randrange(len(candidates))]
            outcome = _outcome(series_by_key[row.series_key], observed_at_ms=close_at - BAR_MS)
            value = outcome.stopped_bps.get((stop, hold))
            if value is not None:
                total += value
                count += 1
        if count:
            mean = total / count
            means.append(mean)
            ge += mean >= observed
    means.sort()
    return {
        "n": len(pool),
        "stop_bps": stop,
        "hold": hold,
        "observed_mean_bps": round(observed, 1),
        "permutations": len(means),
        "null_mean_bps": round(statistics.fmean(means), 1) if means else None,
        "null_p05_bps": round(means[int(0.05 * len(means))], 1) if means else None,
        "null_p95_bps": round(means[int(0.95 * len(means)) - 1], 1) if means else None,
        "p_one_sided_greater": round((1 + ge) / (1 + len(means)), 4) if means else None,
    }


# ---------------------------------------------------------------------------
# Sensitivity grid. Exploratory only: 4 x 3 x 4 x 2 x 2 = 192 cells over one
# 310-frame sample. The report says so in the same breath as the numbers.
# ---------------------------------------------------------------------------

GRID_OI = (300, 500, 800, 1000)
GRID_RATIO = (5000, 6000, 8000)
GRID_BAND = ((0, 300), (0, 600), (0, 1000), (100, 600))
GRID_STOP = (100, 200, 300)
GRID_HOLD = ("1h", "4h")


def _grid(rows: Sequence[Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for min_oi in GRID_OI:
        for min_ratio in GRID_RATIO:
            for low, high in GRID_BAND:
                cohort = [
                    r
                    for r in rows
                    if r.pre_move_bps is not None
                    and int(r.frame["oi_change_bps"]) >= min_oi
                    and int(r.frame["whale_oi_ratio_bps"]) > min_ratio
                    and int(r.frame["whale_long_profit_bps"]) > 0
                    and low <= r.pre_move_bps <= high
                ]
                for stop in GRID_STOP:
                    for hold in GRID_HOLD:
                        values = [
                            r.outcome.stopped_bps.get((stop, hold))
                            for r in cohort
                            if r.outcome.stopped_bps.get((stop, hold)) is not None
                        ]
                        net = [v - ROUND_TRIP_COST_BPS for v in values]
                        hits = [
                            r.outcome.stopped_bps.get((stop, hold)) == -stop
                            for r in cohort
                            if r.outcome.stopped_bps.get((stop, hold)) is not None
                        ]
                        out.append(
                            {
                                "min_oi_change_bps": min_oi,
                                "min_ratio_bps": min_ratio,
                                "pre_move_band_bps": [low, high],
                                "stop_bps": stop,
                                "hold": hold,
                                "n_frames": len(cohort),
                                "n_scored": len(net),
                                "mean_net_bps": round(statistics.fmean(net), 1) if net else None,
                                "median_net_bps": round(statistics.median(net), 1) if net else None,
                                "win_rate": round(sum(1 for v in net if v > 0) / len(net), 4) if net else None,
                                "stop_rate": round(sum(hits) / len(hits), 4) if hits else None,
                            }
                        )
    return out


# ---------------------------------------------------------------------------


def _stop_bps() -> int:
    return 100


def main() -> int:
    rng = random.Random(SEED)  # noqa: S311 - reproducible resampling, not a security decision
    data = _read_rows()
    frames = data["frames"]
    if len(frames) != 310:
        print(f"warning: frame population is {len(frames)}, the pinned window described 310", file=sys.stderr)

    pairs = sorted({(str(f["source_venue"]), str(f["symbol"])) for f in frames})
    cache = asyncio.run(_load_cache(pairs))
    catalogues = asyncio.run(_catalogues())
    series_by_key = {key: _series(payload) for key, payload in cache.items()}
    candle_errors = {f"{k[0]}:{k[1]}": v.get("error") for k, v in cache.items() if v.get("error")}

    rows: list[Row] = []
    for frame in frames:
        key = (str(frame["source_venue"]), str(frame["symbol"]))
        series = series_by_key[key]
        observed = int(frame["observed_at_ms"])
        tol = DEFAULT_PRICE_WINDOW.bar_gap_tolerance_ms
        pre = _move_bps(
            _select_close(series, target_ms=observed - DEFAULT_PRICE_WINDOW.lookback_ms, tolerance_ms=tol),
            _select_close(series, target_ms=observed, tolerance_ms=tol),
        )
        passed, reason = _policy_replay(frame, pre)
        rows.append(
            Row(
                frame=frame,
                series_key=key,
                pre_move_bps=pre,
                outcome=_outcome(series, observed_at_ms=observed),
                policy_pass=passed,
                policy_reason=reason,
            )
        )

    receipt: dict[str, Any] = {}
    receipt["meta"] = _meta(data, frames, rows, candle_errors)
    receipt["chain_rules"] = _rule_table()
    receipt["funnel"] = _funnel(data, frames, rows)
    receipt["signals"] = _signal_rows(data, series_by_key)
    receipt["policy_replay_reconciliation"] = _reconcile(data, rows)
    receipt["frame_backtest"] = _backtest(rows, rng)
    receipt["baseline"] = _baseline(rows, series_by_key, rng)
    receipt["permutation"] = {
        "all_frames": _permutation_p(rows, series_by_key, rng),
        "policy_pass": _permutation_p([r for r in rows if r.policy_pass], series_by_key, rng),
        "policy_pass_deployed_exit": _permutation_stop_p(
            [r for r in rows if r.policy_pass], series_by_key, rng, stop=100, hold="4h"
        ),
        "all_frames_deployed_exit": _permutation_stop_p(rows, series_by_key, rng, stop=100, hold="4h"),
    }
    receipt["sensitivity_grid"] = _grid(rows)
    receipt["friction"] = _friction(data, frames, rows, catalogues)
    receipt["frames"] = [_frame_row(r) for r in rows]

    RECEIPT_PATH.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"wrote {RECEIPT_PATH.relative_to(REPO_ROOT)}")
    print(json.dumps(receipt["frame_backtest"]["all"], indent=2, ensure_ascii=False))
    return 0


def _frame_row(row: Row) -> dict[str, Any]:
    """One frame's inputs and its forward outcome, so every table above is recomputable from here."""

    frame, outcome = row.frame, row.outcome
    return {
        "symbol": frame["symbol"],
        "source_venue": frame["source_venue"],
        "observed_at_ms": int(frame["observed_at_ms"]),
        "available_at_ms": int(frame["available_at_ms"]),
        "oi_change_bps": int(frame["oi_change_bps"]),
        "oi_value_usd": int(frame["oi_value_usd"]),
        "whale_oi_ratio_bps": int(frame["whale_oi_ratio_bps"]),
        "whale_long_profit_bps": int(frame["whale_long_profit_bps"]),
        "pre_move_bps": row.pre_move_bps,
        "policy_pass": row.policy_pass,
        "policy_reason": row.policy_reason,
        "entry_close_at_ms": outcome.entry_close_at_ms,
        "entry_price": outcome.entry_price,
        "bars_after_entry": outcome.bars_after_entry,
        "fwd_bps": outcome.forward_bps,
        "mae_4h_bps": outcome.mae_bps,
        "mfe_4h_bps": outcome.mfe_bps,
        "stop_hit_4h": {str(k): v for k, v in outcome.stop_hit.items()},
        "stopped_bps": {f"{k[0]}_{k[1]}": v for k, v in outcome.stopped_bps.items()},
    }


def _meta(
    data: Mapping[str, list[dict[str, Any]]],
    frames: Sequence[Mapping[str, Any]],
    rows: Sequence[Row],
    candle_errors: Mapping[str, Any],
) -> dict[str, Any]:
    admission = AdmissionConfig(min_oi_value_usd=5_000_000)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    ledger_digests = sorted({str(r["gate_config_digest"]) for r in data["gate"]})
    return {
        "generated_at_utc": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "repo_head": head,
        "window": {
            "frames_from_ms": WINDOW_START_MS,
            "frames_to_ms": WINDOW_END_MS,
            "frames_from_utc": datetime.fromtimestamp(WINDOW_START_MS / 1000, tz=UTC).isoformat(),
            "frames_to_utc": datetime.fromtimestamp(WINDOW_END_MS / 1000, tz=UTC).isoformat(),
            "candles_from_ms": CANDLE_START_MS,
            "candles_cutoff_ms": CANDLE_CUTOFF_MS,
            "candles_cutoff_utc": datetime.fromtimestamp(CANDLE_CUTOFF_MS / 1000, tz=UTC).isoformat(),
        },
        "identity": {
            "admission_version": ADMISSION_VERSION,
            "admission_config": admission.snapshot,
            "admission_config_digest": admission.digest,
            "admission_digest_matches_ledger": [admission.digest] == ledger_digests,
            "ledger_gate_config_digests": ledger_digests,
            "ledger_gate_versions": sorted({str(r["gate_version"]) for r in data["gate"]}),
            "policy_id": ALPHA_POLICY.policy_id,
            "policy_config": ALPHA_POLICY.config_snapshot,
            "policy_config_digest": ALPHA_POLICY.config_digest,
            "ledger_policy_digests": sorted({str(c["strategy_config_digest"]) for c in data["cases"]}),
            "metric_versions": sorted({str(f["metric_version"]) for f in frames}),
            "source_contract_versions": sorted({str(f["source_contract_version"]) for f in frames}),
        },
        "population": {
            "frames": len(frames),
            "symbols": len({str(f["symbol"]) for f in frames}),
            "venue_symbol_pairs": len({(str(f["source_venue"]), str(f["symbol"])) for f in frames}),
            "by_source_venue": _count(str(f["source_venue"]) for f in frames),
            "by_direction": _count(str(f["direction"]) for f in frames),
            "frames_with_entry_bar": sum(1 for r in rows if r.outcome.entry_price is not None),
            "frames_with_15m": sum(1 for r in rows if r.outcome.forward_bps.get("15m") is not None),
            "frames_with_1h": sum(1 for r in rows if r.outcome.forward_bps.get("1h") is not None),
            "frames_with_4h": sum(1 for r in rows if r.outcome.forward_bps.get("4h") is not None),
            "frames_with_24h": sum(1 for r in rows if r.outcome.forward_bps.get("24h") is not None),
            "frames_with_pre_move": sum(1 for r in rows if r.pre_move_bps is not None),
            "candle_fetch_errors": dict(candle_errors),
        },
        "cost_model": {
            "round_trip_bps": ROUND_TRIP_COST_BPS,
            "entry": "the first 5-minute close strictly after observed_at_ms",
            "note": "no slippage beyond the cost constant; a stop is assumed filled at its own level",
        },
        "resampling": {
            "seed": SEED,
            "bootstrap_draws": BOOTSTRAP_DRAWS,
            "permutation_draws": PERMUTATION_DRAWS,
        },
    }


def _count(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _funnel(
    data: Mapping[str, list[dict[str, Any]]],
    frames: Sequence[Mapping[str, Any]],
    rows: Sequence[Row],
) -> dict[str, Any]:
    gate = data["gate"]
    cases = data["cases"]
    admission = {str(r["source_key"]): f"{r['status']}/{r['reason']}" for r in gate}
    dispositions = {
        str(o["signal_id"]): str((o["summary"] or {}).get("disposition"))
        for o in data["observations"]
        if o["normalized_kind"] == "signal_disposition" and o["signal_id"]
    }
    fills = _count(
        str(o["signal_id"]) for o in data["observations"] if o["normalized_kind"] == "fill" and o["signal_id"]
    )
    return {
        "stage_1_frames": len(frames),
        "stage_2_admission": {
            "rows": len(gate),
            "by_status_reason": _count(f"{r['status']}/{r['reason']}" for r in gate),
            "re_evaluations_per_row": {
                "median_attempt_count": statistics.median([int(r["attempt_count"]) for r in gate]),
                "max_attempt_count": max(int(r["attempt_count"]) for r in gate),
            },
        },
        "stage_3_cases": {
            "rows": len(cases),
            "by_state_reason": _count(f"{c['state']}/{c['policy_reason']}" for c in cases),
        },
        "stage_4_signals": {
            "rows": len(data["signals"]),
            "by_disposition": _count(dispositions.values()),
        },
        "stage_5_fills": {
            "signals_with_a_fill": len(fills),
            "fills_by_signal": fills,
        },
        "replay_of_the_policy_on_all_frames": _count(r.policy_reason for r in rows),
        "policy_pass_frames_by_admission_outcome": _count(
            admission.get(f"oi:{r.frame['event_id']}:{r.frame['metric_version']}", "no_admission_row")
            for r in rows
            if r.policy_pass
        ),
    }


def _signal_rows(
    data: Mapping[str, list[dict[str, Any]]],
    series_by_key: Mapping[tuple[str, str], dict[int, Bar]],
) -> list[dict[str, Any]]:
    cases = {str(c["case_id"]): c for c in data["cases"]}
    dispositions = {
        str(o["signal_id"]): str((o["summary"] or {}).get("disposition"))
        for o in data["observations"]
        if o["normalized_kind"] == "signal_disposition" and o["signal_id"]
    }
    fills = {}
    for o in data["observations"]:
        if o["normalized_kind"] == "fill" and o["signal_id"]:
            fills.setdefault(str(o["signal_id"]), []).append(
                {
                    "leg": (o["summary"] or {}).get("leg"),
                    "price": (o["summary"] or {}).get("last_price"),
                    "quantity": (o["summary"] or {}).get("last_quantity"),
                    "at_utc": datetime.fromtimestamp(int(o["occurred_at_ns"]) / 1e9, tz=UTC).isoformat(),
                }
            )
    out = []
    for signal in data["signals"]:
        case = cases.get(str(signal["case_id"]))
        manifest = (case or {}).get("manifest") or {}
        oi = (manifest.get("contexts") or {}).get("oi") or {}
        market = (manifest.get("contexts") or {}).get("market") or {}
        trigger = manifest.get("primary_trigger") or {}
        venue = "binance" if str(trigger.get("venue")) == "binance.usdm" else "hyperliquid"
        symbol = str(manifest.get("base_symbol") or "")
        series = series_by_key.get((venue, symbol), {})
        observed_ms = int(trigger.get("observed_at_ms") or 0)
        outcome = _outcome(series, observed_at_ms=observed_ms)
        out.append(
            {
                "signal_id": str(signal["signal_id"])[:12],
                "market_key": str(signal["market_key"]),
                "emitted_at_utc": datetime.fromtimestamp(int(signal["observed_at_ns"]) / 1e9, tz=UTC).isoformat(),
                "trigger_observed_at_utc": datetime.fromtimestamp(observed_ms / 1000, tz=UTC).isoformat(),
                "source_venue": str(trigger.get("venue")),
                "frozen_mark_price": manifest.get("contexts", {}).get("market", {}).get("mark_price"),
                "frozen_pre_move_bps": market.get("pre_move_bps"),
                "oi_change_bps": oi.get("oi_change_bps"),
                "oi_value_usd": oi.get("oi_value_usd"),
                "whale_oi_ratio_bps": oi.get("whale_oi_ratio_bps"),
                "whale_long_profit_bps": oi.get("whale_long_profit_bps"),
                "runtime_disposition": dispositions.get(str(signal["signal_id"]), "no_observation"),
                "fills": fills.get(str(signal["signal_id"]), []),
                "hypothetical": {
                    "entry_close_at_utc": (
                        datetime.fromtimestamp(outcome.entry_close_at_ms / 1000, tz=UTC).isoformat()
                        if outcome.entry_close_at_ms
                        else None
                    ),
                    "entry_price": outcome.entry_price,
                    "fwd_4h_bps": outcome.forward_bps.get("4h"),
                    "mae_4h_bps": outcome.mae_bps,
                    "mfe_4h_bps": outcome.mfe_bps,
                    "stop_100bps_hit": outcome.stop_hit.get(100),
                    "pnl_4h_stop100_net_bps": (
                        None
                        if outcome.stopped_bps.get((100, "4h")) is None
                        else outcome.stopped_bps[(100, "4h")] - ROUND_TRIP_COST_BPS
                    ),
                },
            }
        )
    return out


def _reconcile(data: Mapping[str, list[dict[str, Any]]], rows: Sequence[Row]) -> dict[str, Any]:
    """My recomputed pre-move against the one the Case actually froze."""

    by_source_key: dict[str, Row] = {}
    for row in rows:
        key = f"oi:{row.frame['event_id']}:{row.frame['metric_version']}"
        by_source_key[key] = row
    matched = mismatched = missing = 0
    diffs: list[int] = []
    examples: list[dict[str, Any]] = []
    frozen_reason_agrees = frozen_reason_differs = 0
    for case in data["cases"]:
        manifest = case["manifest"] or {}
        trigger = manifest.get("primary_trigger") or {}
        source_key = str(trigger.get("source_key") or "")
        row = by_source_key.get(source_key)
        if row is None:
            missing += 1
            continue
        frozen = (manifest.get("contexts") or {}).get("market", {}).get("pre_move_bps")
        if frozen is None or row.pre_move_bps is None:
            missing += 1
            continue
        delta = int(row.pre_move_bps) - int(frozen)
        diffs.append(delta)
        if delta == 0:
            matched += 1
        else:
            mismatched += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "symbol": row.frame["symbol"],
                        "venue": row.frame["source_venue"],
                        "observed_at_ms": row.frame["observed_at_ms"],
                        "frozen_pre_move_bps": frozen,
                        "replayed_pre_move_bps": row.pre_move_bps,
                        "delta_bps": delta,
                    }
                )
        if row.policy_reason == str(case["policy_reason"]):
            frozen_reason_agrees += 1
        else:
            frozen_reason_differs += 1
    return {
        "cases_compared": matched + mismatched,
        "pre_move_exact_match": matched,
        "pre_move_mismatch": mismatched,
        "unmatched_or_missing": missing,
        "abs_delta_bps": {
            "max": max((abs(d) for d in diffs), default=0),
            "median": round(statistics.median([abs(d) for d in diffs]), 1) if diffs else 0,
        },
        "policy_reason_agrees": frozen_reason_agrees,
        "policy_reason_differs": frozen_reason_differs,
        "mismatch_examples": examples,
        "note": (
            "The lane fetches its bars in a narrow window around the trigger and this replay reads one "
            "contiguous per-symbol page; a difference is a provider revision or a bar the lane's own "
            "window did not contain, never a different rule."
        ),
    }


def _backtest(rows: Sequence[Row], rng: random.Random) -> dict[str, Any]:
    out: dict[str, Any] = {"all": _segment_stats(rows, rng)}
    out["policy_pass"] = _segment_stats([r for r in rows if r.policy_pass], rng)
    out["policy_fail"] = _segment_stats([r for r in rows if not r.policy_pass], rng)
    out["by_venue"] = {
        venue: _segment_stats([r for r in rows if r.frame["source_venue"] == venue], rng)
        for venue in sorted({str(r.frame["source_venue"]) for r in rows})
    }
    out["by_oi_change"] = _grouped(rows, lambda r: _bucket(int(r.frame["oi_change_bps"]), OI_CHANGE_BUCKETS), rng)
    out["by_whale_oi_ratio"] = _grouped(rows, lambda r: _bucket(int(r.frame["whale_oi_ratio_bps"]), RATIO_BUCKETS), rng)
    out["by_whale_long_profit"] = _grouped(
        rows, lambda r: _bucket(int(r.frame["whale_long_profit_bps"]), PROFIT_BUCKETS), rng
    )
    out["by_pre_move"] = _grouped(
        rows,
        lambda r: "missing" if r.pre_move_bps is None else _bucket(r.pre_move_bps, PRE_MOVE_BUCKETS),
        rng,
    )
    return out


def _grouped(rows: Sequence[Row], key: Callable[[Row], str], rng: random.Random) -> dict[str, Any]:
    buckets: dict[str, list[Row]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    return {name: _segment_stats(members, rng) for name, members in sorted(buckets.items())}


def _friction(
    data: Mapping[str, list[dict[str, Any]]],
    frames: Sequence[Mapping[str, Any]],
    rows: Sequence[Row],
    catalogues: Mapping[str, Any],
) -> dict[str, Any]:
    runtime = data["runtime"][0] if data["runtime"] else {}
    routes = set(runtime.get("routes") or [])
    routed = {m.split(":")[2] for m in routes}
    symbols = _count(str(f["symbol"]) for f in frames)
    unroutable = {s: n for s, n in symbols.items() if s not in routed}
    dispositions = _count(
        str((o["summary"] or {}).get("disposition"))
        for o in data["observations"]
        if o["normalized_kind"] == "signal_disposition"
    )
    hl_frames = [f for f in frames if str(f["source_venue"]) == HYPERLIQUID_SOURCE_VENUE]
    live_bases, demo_bases = set(catalogues["live"]), set(catalogues["demo"])
    frame_symbols = set(symbols)
    return {
        "binance_catalogues": {
            "fetched_at_utc": catalogues["fetched_at_utc"],
            "definition": "baseAsset of USDT-quoted, USDT-margined PERPETUAL or TRADIFI_PERPETUAL with status TRADING",
            "live_fapi": len(live_bases),
            "demo_fapi": len(demo_bases),
            "runtime_routes": len(routed),
            "routes_inside_demo": len(routed & demo_bases),
            "routes_outside_demo": sorted(routed - demo_bases),
            "frame_symbols_on_live": sorted(frame_symbols & live_bases),
            "frame_symbols_off_live": sorted(frame_symbols - live_bases),
            "frames_routable_on_live": sum(n for s, n in symbols.items() if s in live_bases),
        },
        "runtime": {
            "account_slot": runtime.get("account_slot"),
            "mode": runtime.get("mode"),
            "routes": len(routes),
            "catalogue_source": (
                "paper mode discovers instruments from BinanceEnvironment.DEMO, i.e. "
                "https://demo-fapi.binance.com; live mode would read https://fapi.binance.com"
            ),
        },
        "frames_by_routability": {
            "routed_frames": sum(n for s, n in symbols.items() if s in routed),
            "unrouted_frames": sum(unroutable.values()),
            "unrouted_symbols": dict(sorted(unroutable.items(), key=lambda kv: -kv[1])),
        },
        "signal_dispositions": dispositions,
        "hyperliquid": {
            "frames": len(hl_frames),
            "symbols": sorted({str(f["symbol"]) for f in hl_frames}),
            "symbols_in_binance_route_catalogue": sorted(
                {str(f["symbol"]) for f in hl_frames if str(f["symbol"]) in routed}
            ),
            "frames_executable_on_the_binance_runtime": sum(1 for f in hl_frames if str(f["symbol"]) in routed),
        },
        "oi_value_floor": {
            str(floor): {
                "frames_admitted": sum(1 for f in frames if int(f["oi_value_usd"]) >= floor),
                "frames_refused": sum(1 for f in frames if int(f["oi_value_usd"]) < floor),
                "policy_pass_frames_admitted": sum(
                    1 for r in rows if r.policy_pass and int(r.frame["oi_value_usd"]) >= floor
                ),
            }
            for floor in (5_000_000, 10_000_000, 20_000_000)
        },
        "ttl": {
            "signal_ttl_ms": 180_000,
            "max_age_ms": AdmissionConfig().max_age_ms,
            "expired_dispositions": dispositions.get("expired", 0),
            "signals": len(data["signals"]),
        },
        "stop_distance_bps": _stop_bps(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
