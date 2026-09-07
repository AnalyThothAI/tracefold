"""#604 R0: two pre-registered exit conventions, replayed offline over the sealed #535 corpus.

```yaml
channel: B  # A live read-only | B frozen artifact | C committed snapshot
purpose: "Does changing only the exit — a time exit at 60/120/240 min, or a break-even stop after
  +1R — turn the deployed OI entry rule into a positive-expectancy rule on the frames it already saw?
  It does not answer whether any entry threshold should change, and it does not search for a better
  exit: the two conventions and the adopt/report criterion were fixed in Issue #604 §5 before the run."
window: "The #535 sealed window, unchanged: 310 OpenNews OI frames observed_at_ms in
  [1788267180000, 1788471261000] (2026-09-01T12:53Z .. 2026-09-03T21:34Z) over 63 venue x symbol
  pairs, and 5-minute candles cut off at 1788475500000 (2026-09-03T22:45Z). Neither extended nor
  narrowed; the receipt records the per-file digests of the corpus it actually read."
identity: "The frames, their pre_move_bps and their policy_pass flags are read from the committed
  #535 receipt (docs/research/oi-chain-backtest-2026-09-03.json, sha in the output), which froze
  trading_admission_v8 / source_native_oi_smart_money_long_v4 / oi_signal_v1 / opennews_oi_source_v1.
  No policy or admission code is re-evaluated here, so no threshold can drift between the two runs."
safety: "Offline. Reads two files: the committed #535 receipt, and the operator-owned candle cache
  under ~/.tracefold/research/oi_backtest_cache/ (overridable via TRACEFOLD_OI_BACKTEST_CACHE).
  No exchange endpoint, no PostgreSQL, no credential, no import of `tracefold`. Writes exactly one
  file, docs/research/oi-exit-rules-replay-2026-09-07.json."
```

Run:

    uv run python notebooks/research/oi_exit_rules_replay_2026_09_07.py

Output: `docs/research/oi-exit-rules-replay-2026-09-07.json`, the receipt every table in
`docs/research/oi-exit-rules-replay-2026-09-07.md` cites.

Why the scoring machinery is copied rather than imported. `notebooks/oi_chain_backtest_2026_09_03.py`
is the #535 script and stays untouched, but it no longer imports on `main`: #537 PR-3 (295f3fc5f)
deleted `tracefold.trading.market_context.DEFAULT_PRICE_WINDOW`, which its module body reads. The
five functions this file needs — `_series`, `_move_bps`, entry resolution, `_stopped_return`,
`_valid_entries` — are therefore transcribed verbatim below, and the transcription is not trusted:
`_calibration()` re-derives all 310 frames' `stopped_bps["100_4h"]` from the cache and refuses to
continue unless every one equals the value the #535 receipt already published.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Pinned constants. Everything above the pre-registration line is copied from
# `notebooks/oi_chain_backtest_2026_09_03.py` so the two runs measure the same
# population the same way; everything below it is Issue #604 §5, fixed before
# the first number was computed.
# ---------------------------------------------------------------------------

WINDOW_START_MS = 1_788_267_180_000  # 2026-09-01T12:53:00Z, the first frame
WINDOW_END_MS = 1_788_471_261_000  # 2026-09-03T21:34:21Z, the last frame
CANDLE_CUTOFF_MS = 1_788_475_500_000  # 2026-09-03T22:45:00Z; a bar counts only if it closed by then
BAR_MS = 300_000

SOURCE_RECEIPT_PATH = REPO_ROOT / "docs" / "research" / "oi-chain-backtest-2026-09-03.json"
RECEIPT_PATH = REPO_ROOT / "docs" / "research" / "oi-exit-rules-replay-2026-09-07.json"
CACHE_DIR = Path(os.environ.get("TRACEFOLD_OI_BACKTEST_CACHE", "~/.tracefold/research/oi_backtest_cache"))

# --- Issue #604 §5, pre-registered ----------------------------------------

STOP_BPS = 100  # the deployed `stop_distance_bps`; frozen, this study changes only the exit
TIME_EXIT_MINUTES = (60, 120, 240)  # convention A
BREAKEVEN_TRIGGER_BPS = 100  # convention B arms at +1R = +1 x STOP_BPS
BREAKEVEN_HOLD_MINUTES = 240  # convention B is otherwise identical to A-240
COST_BPS = 9.6  # live-measured round trip: 7 stop-outs at -10.963 USD on 1000 USD notional
ADOPT_MEAN_FLOOR_BPS = 50.0  # criterion (ii)
PERMUTATION_DRAWS = 2_000
BOOTSTRAP_DRAWS = 10_000
SEED = 20_260_907

# The #535 convention, used only to reproduce its deployed cell as a machinery check.
CALIBRATION_COST_BPS = 10
CALIBRATION_TOLERANCE_BPS = 5.0
CALIBRATION_TARGET_BPS = 0.0

STOP_REASON = "stop"
BREAKEVEN_REASON = "breakeven_stop"
TIME_REASON = "time"


# ---------------------------------------------------------------------------
# Candles. Transcribed from the #535 script: `Bar`, `_series`, `_move_bps`, the
# entry rule inside `_outcome`, `_stopped_return`, `_valid_entries`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    open_at_ms: int
    close_at_ms: int
    open: float
    high: float
    low: float
    close: float


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


def _move_bps(p0: float | None, p1: float | None) -> int | None:
    if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
        return None
    return round((p1 / p0 - 1) * 10_000)


def _entry(series: Mapping[int, Bar], *, observed_at_ms: int) -> tuple[int, float] | None:
    """The deployed entry model as #535 scored it: long at the first 5-minute close after the frame.

    The Runtime places a market order, and the receipt's friction assumption is a flat round-trip
    cost in bps subtracted from the gross return rather than a per-side price adjustment, so the
    entry mark is the bar close itself. This is deliberately *not* the Case's frozen mark, which
    #535 measured drifting 730 bps away from the fill on BULLA.
    """

    close_at = (observed_at_ms // BAR_MS + 1) * BAR_MS
    bar = series.get(close_at)
    return None if bar is None else (close_at, bar.close)


def _forward(series: Mapping[int, Bar], *, entry_close_at_ms: int, bars: int) -> list[Bar | None]:
    return [series.get(entry_close_at_ms + step * BAR_MS) for step in range(1, bars + 1)]


def _stopped_return(
    forward: Sequence[Bar | None], *, entry: float, level: float, stop: int, hold_bars: int
) -> int | None:
    """#535 verbatim: exit at the stop level on the first bar that trades through it, else at the close."""

    for i in range(hold_bars):
        bar = forward[i] if i < len(forward) else None
        if bar is None:
            return None
        if bar.low <= level:
            return -stop
    exit_bar = forward[hold_bars - 1] if hold_bars - 1 < len(forward) else None
    return None if exit_bar is None else _move_bps(entry, exit_bar.close)


def _valid_entries(series: Mapping[int, Bar], *, horizon_bars: int) -> list[int]:
    """#535 verbatim: every 5-minute close inside the frame window with `horizon_bars` of forward data."""

    out = []
    for close_at in sorted(series):
        if not (WINDOW_START_MS <= close_at <= WINDOW_END_MS):
            continue
        if close_at + horizon_bars * BAR_MS in series:
            out.append(close_at)
    return out


# ---------------------------------------------------------------------------
# The two pre-registered exit conventions. Both are long-only, both start from
# the same 100 bps reduce-only stop the Runtime actually places, and neither
# touches an entry threshold.
#
# Intrabar ordering: a 5-minute bar carries no sequence, so both conventions
# resolve the adverse side first — the stop in force at the start of the bar is
# tested against `low` before `high` is allowed to arm anything. That is the
# conservative reading and it is also exactly what `_stopped_return` does, which
# is what makes A-240 reproduce the #535 deployed cell bar for bar.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Exit:
    gross_bps: int
    reason: str
    held_bars: int
    mfe_bps: int
    mae_bps: int


def _exit_time(forward: Sequence[Bar | None], *, entry: float, hold_bars: int) -> Exit | None:
    """Convention A: 100 bps stop, else out at the close of the `hold_bars`-th bar."""

    level = entry * (1 - STOP_BPS / 10_000)
    high, low = entry, entry
    for i in range(hold_bars):
        bar = forward[i] if i < len(forward) else None
        if bar is None:
            return None
        low = min(low, bar.low)
        high = max(high, bar.high)
        if bar.low <= level:
            return _exit(-STOP_BPS, STOP_REASON, i + 1, entry, high, low)
        if i == hold_bars - 1:
            move = _move_bps(entry, bar.close)
            return None if move is None else _exit(move, TIME_REASON, i + 1, entry, high, low)
    return None


def _exit_breakeven(forward: Sequence[Bar | None], *, entry: float, hold_bars: int) -> Exit | None:
    """Convention B: A-240, except that touching +1R moves the stop to the entry price."""

    level = entry * (1 - STOP_BPS / 10_000)
    trigger = entry * (1 + BREAKEVEN_TRIGGER_BPS / 10_000)
    armed = False
    high, low = entry, entry
    for i in range(hold_bars):
        bar = forward[i] if i < len(forward) else None
        if bar is None:
            return None
        low = min(low, bar.low)
        high = max(high, bar.high)
        if bar.low <= level:
            reason = BREAKEVEN_REASON if armed else STOP_REASON
            return _exit(0 if armed else -STOP_BPS, reason, i + 1, entry, high, low)
        if not armed and bar.high >= trigger:
            # Armed by this bar; the moved stop is in force from the next bar on, because within one
            # bar we already spent the only ordering assumption we are willing to make.
            armed, level = True, entry
        if i == hold_bars - 1:
            move = _move_bps(entry, bar.close)
            return None if move is None else _exit(move, TIME_REASON, i + 1, entry, high, low)
    return None


def _exit(gross: int, reason: str, held: int, entry: float, high: float, low: float) -> Exit:
    """MFE/MAE are the extremes over the *realised* holding window, entry bar through exit bar.

    On a stopped exit the exit bar's low is included, so MAE can read further than the stop distance:
    that is how far the price actually went, not what the position was assumed to be filled at.
    """

    return Exit(
        gross_bps=gross,
        reason=reason,
        held_bars=held,
        mfe_bps=_move_bps(entry, high) or 0,
        mae_bps=_move_bps(entry, low) or 0,
    )


@dataclass(frozen=True, slots=True)
class Convention:
    name: str
    hold_bars: int
    breakeven: bool

    def score(self, forward: Sequence[Bar | None], *, entry: float) -> Exit | None:
        if self.breakeven:
            return _exit_breakeven(forward, entry=entry, hold_bars=self.hold_bars)
        return _exit_time(forward, entry=entry, hold_bars=self.hold_bars)


CONVENTIONS: tuple[Convention, ...] = (
    *(Convention(f"A-{m}", m * 60_000 // BAR_MS, False) for m in TIME_EXIT_MINUTES),
    Convention("B", BREAKEVEN_HOLD_MINUTES * 60_000 // BAR_MS, True),
)


# ---------------------------------------------------------------------------
# Corpus. Two frozen inputs, both digested into the receipt; the run refuses to
# start if the cache does not cover every frame the #535 receipt named.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Corpus:
    frames: list[dict[str, Any]]
    series: dict[tuple[str, str], dict[int, Bar]]
    completeness: dict[str, Any]
    source_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_corpus() -> Corpus:
    source_bytes = SOURCE_RECEIPT_PATH.read_bytes()
    frames = list(json.loads(source_bytes)["frames"])
    cache_dir = CACHE_DIR.expanduser()
    pairs = sorted({(str(f["source_venue"]), str(f["symbol"])) for f in frames})

    missing: list[str] = []
    errored: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    series: dict[tuple[str, str], dict[int, Bar]] = {}
    for venue, symbol in pairs:
        path = cache_dir / f"{venue}__{symbol}.json"
        if not path.exists():
            missing.append(f"{venue}:{symbol}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("error"):
            errored.append({"pair": f"{venue}:{symbol}", "error": str(payload["error"])})
        series[(venue, symbol)] = _series(payload)
        files.append(
            {
                "pair": f"{venue}:{symbol}",
                "sha256": _sha256(path),
                "bars_raw": len(payload.get("bars") or []),
                "bars_kept": len(series[(venue, symbol)]),
            }
        )

    # A frame whose venue x symbol is absent or errored cannot be scored under any convention, and a
    # study of exits must not quietly average over a different population than the one it names.
    if missing or errored:
        print(f"corpus incomplete: missing={missing} errored={errored}", file=sys.stderr)
        raise SystemExit(2)

    digest = hashlib.sha256()
    for entry in files:
        digest.update(f"{entry['pair']}:{entry['sha256']}\n".encode())
    without_entry = [
        f"{f['source_venue']}:{f['symbol']}@{f['observed_at_ms']}"
        for f in frames
        if _entry(series[(str(f["source_venue"]), str(f["symbol"]))], observed_at_ms=int(f["observed_at_ms"])) is None
    ]
    completeness = {
        "frames": len(frames),
        "pairs_named_by_receipt": len(pairs),
        "pairs_present_in_cache": len(files),
        "pairs_missing": missing,
        "pairs_with_fetch_error": errored,
        "frames_without_entry_bar": without_entry,
        "corpus_sha256": digest.hexdigest(),
        "cache_dir": str(cache_dir),
        "files": files,
    }
    return Corpus(
        frames=frames,
        series=series,
        completeness=completeness,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Calibration. The transcription above is only worth as much as this check: the
# same corpus, the same functions, and every one of the 310 published per-frame
# numbers reproduced exactly, before any new convention is scored.
# ---------------------------------------------------------------------------


def _calibration(corpus: Corpus) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    scored: list[int] = []
    scored_policy_pass: list[int] = []
    for frame in corpus.frames:
        key = (str(frame["source_venue"]), str(frame["symbol"]))
        entry = _entry(corpus.series[key], observed_at_ms=int(frame["observed_at_ms"]))
        published = frame["stopped_bps"].get("100_4h")
        if entry is None:
            if frame["entry_price"] is not None:
                mismatches.append({"pair": f"{key[0]}:{key[1]}", "field": "entry", "ours": None})
            continue
        close_at, price = entry
        if close_at != frame["entry_close_at_ms"] or price != frame["entry_price"]:
            mismatches.append(
                {
                    "pair": f"{key[0]}:{key[1]}",
                    "field": "entry",
                    "ours": [close_at, price],
                    "published": [frame["entry_close_at_ms"], frame["entry_price"]],
                }
            )
            continue
        ours = _stopped_return(
            _forward(corpus.series[key], entry_close_at_ms=close_at, bars=48),
            entry=price,
            level=price * (1 - STOP_BPS / 10_000),
            stop=STOP_BPS,
            hold_bars=48,
        )
        if ours != published:
            mismatches.append(
                {"pair": f"{key[0]}:{key[1]}", "field": "stopped_100_4h", "ours": ours, "published": published}
            )
            continue
        if ours is not None:
            scored.append(ours)
            if frame["policy_pass"]:
                scored_policy_pass.append(ours)

    mean = statistics.fmean(v - CALIBRATION_COST_BPS for v in scored_policy_pass) if scored_policy_pass else None
    deviation = None if mean is None else abs(mean - CALIBRATION_TARGET_BPS)
    return {
        "definition": "the #535 deployed cell: policy_pass, 100 bps stop, 4 h hold, 10 bps round trip",
        "per_frame_rows_checked": len(corpus.frames),
        "per_frame_mismatches": mismatches,
        "policy_pass_n_scored": len(scored_policy_pass),
        "policy_pass_mean_net_bps": None if mean is None else round(mean, 1),
        "all_frames_n_scored": len(scored),
        "all_frames_mean_net_bps": (
            round(statistics.fmean(v - CALIBRATION_COST_BPS for v in scored), 1) if scored else None
        ),
        "published_mean_net_bps": CALIBRATION_TARGET_BPS,
        "deviation_bps": None if deviation is None else round(deviation, 1),
        "tolerance_bps": CALIBRATION_TOLERANCE_BPS,
        "matches": bool(not mismatches and deviation is not None and deviation <= CALIBRATION_TOLERANCE_BPS),
    }


# ---------------------------------------------------------------------------
# Statistics.
# ---------------------------------------------------------------------------


def _bootstrap_ci(values: Sequence[float], rng: random.Random) -> tuple[float, float]:
    n = len(values)
    means = sorted(statistics.fmean(values[rng.randrange(n)] for _ in range(n)) for _ in range(BOOTSTRAP_DRAWS))
    return means[int(0.025 * BOOTSTRAP_DRAWS)], means[int(0.975 * BOOTSTRAP_DRAWS) - 1]


def _describe(values: Sequence[float], rng: random.Random | None = None) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    out: dict[str, Any] = {
        "n": len(values),
        "mean_bps": round(statistics.fmean(values), 1),
        "median_bps": round(statistics.median(values), 1),
        "win_rate": round(sum(1 for v in values if v > 0) / len(values), 4),
    }
    if len(values) > 1:
        out["sd_bps"] = round(statistics.stdev(values), 1)
        out["se_bps"] = round(statistics.stdev(values) / len(values) ** 0.5, 1)
        if rng is not None:
            low, high = _bootstrap_ci(values, rng)
            out["mean_ci95_bps"] = [round(low, 1), round(high, 1)]
    return out


def _counts(reasons: Iterable[str]) -> dict[str, int]:
    out = {STOP_REASON: 0, BREAKEVEN_REASON: 0, TIME_REASON: 0}
    for reason in reasons:
        out[reason] = out.get(reason, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Null. Same symbol, same venue, same window, uniformly random 5-minute entry —
# and the same exit convention applied to the draw, which is the part that makes
# a stop-and-time-exit rule comparable to anything at all.
#
# The decision input is the distribution of *cohort means* over 2000 re-draws
# (`_permutation_stop_p` in #535), because criterion (i) compares one cohort mean
# to a 95th percentile. The distribution of single draws is reported alongside it
# as `null_single_draw`, and is never the input to a decision.
# ---------------------------------------------------------------------------


def _null_table(
    corpus: Corpus, convention: Convention
) -> tuple[dict[tuple[str, str], list[int]], dict[tuple[str, str], dict[int, Exit]]]:
    """Every admissible random entry for this convention, scored once."""

    entries: dict[tuple[str, str], list[int]] = {}
    scored: dict[tuple[str, str], dict[int, Exit]] = {}
    for key, series in corpus.series.items():
        candidates = _valid_entries(series, horizon_bars=convention.hold_bars)
        table: dict[int, Exit] = {}
        for close_at in candidates:
            result = convention.score(
                _forward(series, entry_close_at_ms=close_at, bars=convention.hold_bars),
                entry=series[close_at].close,
            )
            if result is not None:
                table[close_at] = result
        entries[key] = [close_at for close_at in candidates if close_at in table]
        scored[key] = table
    return entries, scored


def _null(
    keys: Sequence[tuple[str, str]],
    entries: Mapping[tuple[str, str], list[int]],
    scored: Mapping[tuple[str, str], dict[int, Exit]],
    observed_mean_net: float,
    rng: random.Random,
) -> dict[str, Any]:
    usable = [key for key in keys if entries.get(key)]
    if not usable:
        return {"draws": 0}
    means: list[float] = []
    ge = 0
    for _ in range(PERMUTATION_DRAWS):
        total = 0.0
        for key in usable:
            candidates = entries[key]
            total += scored[key][candidates[rng.randrange(len(candidates))]].gross_bps
        mean = total / len(usable) - COST_BPS
        means.append(mean)
        ge += mean >= observed_mean_net
    means.sort()
    singles: list[float] = []
    single_reasons: list[str] = []
    for _ in range(PERMUTATION_DRAWS):
        key = usable[rng.randrange(len(usable))]
        candidates = entries[key]
        result = scored[key][candidates[rng.randrange(len(candidates))]]
        singles.append(result.gross_bps - COST_BPS)
        single_reasons.append(result.reason)
    return {
        "definition": "same venue and symbol, a uniformly random 5-minute close in the frame window, "
        "scored under the same exit convention, net of the same round-trip cost",
        "permutations": len(means),
        "cohort_keys_used": len(usable),
        "cohort_keys_without_candidates": len(keys) - len(usable),
        "null_mean_net_bps": round(statistics.fmean(means), 1),
        "null_p05_net_bps": round(means[int(0.05 * len(means))], 1),
        "null_p95_net_bps": round(means[int(0.95 * len(means)) - 1], 1),
        "p_one_sided_greater": round((1 + ge) / (1 + len(means)), 4),
        "null_single_draw": {
            **_describe(singles),
            "exit_reasons": _counts(single_reasons),
        },
    }


# ---------------------------------------------------------------------------
# One cell: one cohort x one convention, scored, nulled, and decided.
# ---------------------------------------------------------------------------


def _cell(
    corpus: Corpus,
    cohort_name: str,
    cohort: Sequence[Mapping[str, Any]],
    convention: Convention,
    entries: Mapping[tuple[str, str], list[int]],
    scored: Mapping[tuple[str, str], dict[int, Exit]],
) -> dict[str, Any]:
    rng = random.Random(f"{SEED}:{cohort_name}:{convention.name}")  # noqa: S311
    keys: list[tuple[str, str]] = []
    results: list[Exit] = []
    open_at_horizon = 0
    fixed_results: list[Exit] = []  # the subset with a complete 4 h forward window, for comparability
    for frame in cohort:
        key = (str(frame["source_venue"]), str(frame["symbol"]))
        entry = _entry(corpus.series[key], observed_at_ms=int(frame["observed_at_ms"]))
        if entry is None:
            open_at_horizon += 1
            continue
        close_at, price = entry
        result = convention.score(
            _forward(corpus.series[key], entry_close_at_ms=close_at, bars=convention.hold_bars),
            entry=price,
        )
        if result is None:
            open_at_horizon += 1
            continue
        keys.append(key)
        results.append(result)
        if int(frame["bars_after_entry"]) == 48:
            fixed_results.append(result)

    gross = [float(r.gross_bps) for r in results]
    net = [g - COST_BPS for g in gross]
    stats = _describe(net, rng)
    observed_mean = stats.get("mean_bps")
    null = _null(keys, entries, scored, observed_mean if observed_mean is not None else 0.0, rng)
    beats_null = (
        observed_mean is not None
        and null.get("null_p95_net_bps") is not None
        and observed_mean > null["null_p95_net_bps"]
    )
    above_floor = observed_mean is not None and observed_mean > ADOPT_MEAN_FLOOR_BPS
    fixed_net = [float(r.gross_bps) - COST_BPS for r in fixed_results]
    return {
        "cohort": cohort_name,
        "convention": convention.name,
        "hold_minutes": convention.hold_bars * BAR_MS // 60_000,
        "breakeven_after_bps": BREAKEVEN_TRIGGER_BPS if convention.breakeven else None,
        "stop_bps": STOP_BPS,
        "cost_bps": COST_BPS,
        "net": stats,
        "gross": _describe(gross),
        "exit_reasons": _counts(r.reason for r in results),
        "open_at_horizon": open_at_horizon,
        "mfe_bps": {
            "mean": round(statistics.fmean(r.mfe_bps for r in results), 1) if results else None,
            "median": round(statistics.median(r.mfe_bps for r in results), 1) if results else None,
        },
        "mae_bps": {
            "mean": round(statistics.fmean(r.mae_bps for r in results), 1) if results else None,
            "median": round(statistics.median(r.mae_bps for r in results), 1) if results else None,
        },
        "held_bars": {
            "mean": round(statistics.fmean(r.held_bars for r in results), 1) if results else None,
            "median": round(statistics.median(r.held_bars for r in results), 1) if results else None,
        },
        "null": null,
        "robustness_complete_4h_cohort": _describe(fixed_net),
        "decision": {
            "criterion": "ADOPT only if mean net > null p95 AND mean net > +50 bps; otherwise REPORT ONLY",
            "beats_null_p95": bool(beats_null),
            "above_floor_bps": bool(above_floor),
            "verdict": "ADOPT" if (beats_null and above_floor) else "REPORT ONLY",
        },
    }


# ---------------------------------------------------------------------------


def _repo_head() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def main() -> int:
    corpus = _load_corpus()
    calibration = _calibration(corpus)
    if not calibration["matches"]:
        print(json.dumps(calibration, indent=2, ensure_ascii=False)[:4000], file=sys.stderr)
        print(
            "calibration failed: the copied machinery does not reproduce the #535 deployed cell. "
            "Stopping rather than tuning until it agrees.",
            file=sys.stderr,
        )
        return 1

    cohorts: dict[str, list[dict[str, Any]]] = {
        "policy_pass": [f for f in corpus.frames if f["policy_pass"]],
        "all_frames": list(corpus.frames),
    }
    cells: list[dict[str, Any]] = []
    for convention in CONVENTIONS:
        entries, scored = _null_table(corpus, convention)
        for cohort_name, cohort in cohorts.items():
            cells.append(_cell(corpus, cohort_name, cohort, convention, entries, scored))

    receipt = {
        "meta": {
            "generated_at_utc": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "issue": "#604 R0",
            "repo_head": _repo_head(),
            "script": "notebooks/research/oi_exit_rules_replay_2026_09_07.py",
            "script_sha256": _sha256(Path(__file__).resolve()),
            "source_receipt": str(SOURCE_RECEIPT_PATH.relative_to(REPO_ROOT)),
            "source_receipt_sha256": corpus.source_sha256,
            "seed": SEED,
            "seed_derivation": "random.Random(f'{SEED}:{cohort}:{convention}') per cell, so a cell is "
            "reproducible independently of the order the cells are evaluated in",
            "permutation_draws": PERMUTATION_DRAWS,
            "bootstrap_draws": BOOTSTRAP_DRAWS,
            "window": {
                "frames_from_ms": WINDOW_START_MS,
                "frames_to_ms": WINDOW_END_MS,
                "candles_cutoff_ms": CANDLE_CUTOFF_MS,
            },
        },
        "pre_registration": {
            "source": "Issue #604 §5, written before this run",
            "entry_rule": "unchanged: the deployed policy's own pass/fail, read from the #535 receipt; "
            "no threshold is re-evaluated or varied here",
            "entry_model": "long, market, at the first 5-minute close after the frame's observed_at_ms",
            "conventions": {
                "A": f"{STOP_BPS} bps reduce-only stop + time exit at {list(TIME_EXIT_MINUTES)} minutes",
                "B": f"{STOP_BPS} bps stop; on touching +{BREAKEVEN_TRIGGER_BPS} bps (+1R) the stop moves to "
                f"the entry price; otherwise identical to A-{BREAKEVEN_HOLD_MINUTES}",
            },
            "primary_cohort": "policy_pass",
            "secondary_cohort": "all_frames",
            "primary_metric": "mean net bps per frame, net of a 9.6 bps round trip",
            "null": "same symbol and window random entries, 2000 draws, same exit convention applied",
            "criterion": "ADOPT only if mean > null 95th percentile AND mean > +50 bps; otherwise REPORT ONLY",
            "intrabar_convention": "adverse side first: the stop in force at the start of a bar is tested "
            "against the low before the high may arm the break-even move",
        },
        "corpus": corpus.completeness,
        "calibration": calibration,
        "cells": cells,
    }
    RECEIPT_PATH.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {RECEIPT_PATH.relative_to(REPO_ROOT)}")
    print(
        f"calibration: {calibration['policy_pass_n_scored']} frames, "
        f"mean net {calibration['policy_pass_mean_net_bps']} bps vs published "
        f"{calibration['published_mean_net_bps']} bps, {len(calibration['per_frame_mismatches'])} per-frame mismatches"
    )
    print(f"{'cohort':<12} {'cell':<6} {'N':>4} {'mean':>8} {'median':>8} {'win':>6} {'null p95':>9} {'p':>7}  verdict")
    for cell in cells:
        net = cell["net"]
        print(
            f"{cell['cohort']:<12} {cell['convention']:<6} {net.get('n', 0):>4} "
            f"{net.get('mean_bps'):>8} {net.get('median_bps'):>8} {net.get('win_rate'):>6} "
            f"{cell['null'].get('null_p95_net_bps'):>9} {cell['null'].get('p_one_sided_greater'):>7}  "
            f"{cell['decision']['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
