"""#459 Stage A replay: one pre-registered rule, scored on the symbols it was not found on.

The hypothesis came out of a 36-symbol probe (#459 v3 table C): Binance **contract** open interest up
5% over an hour, while price is up 0-6% over the same hour, returned +266 bps at 4 h across 61 events.
That probe chose both the rule and the symbols, so it can only propose. This module is the test:
the same rule, unchanged, replayed over the whole USDT-perpetual universe from a sealed corpus, and
**scored only on the symbols the probe never saw**.

Everything the rule needs is fixed before the corpus is read, in `PRE_REGISTERED`. The robustness grid
below it is reported but never selected from -- a grid you pick the best cell of is the probe again with
more cells.

Measurement conventions, all of them conservative in the same direction:

* An open-interest point timestamped `T` is the reading at `T`. Entry is the close of the bar `[T, T+5m)`,
  so the position is taken up to five minutes *after* the reading is observable.
* `c60` compares contracts at `T` against `T-1h`: both readings precede entry.
* `pre1h` compares the entry bar's close against the close an hour earlier. It is the one input read at
  the entry instant rather than before it -- the decision is "buy this close if the last hour looks like
  this", which is placeable as a market order on that close and is how the probe measured it.
* The stop is checked against every subsequent low. A bar that gaps through it fills at that bar's open,
  not at the stop price.
* Costs are charged on both sides, once, as a flat basis-point haircut on the gross return.

The null is not "zero". It is "a random entry in the same universe over the same window, exited by the
same rule", drawn `PERMUTATION_TRIALS` times at the observed event count.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from oi_corpus import FIVE_MIN_MS, HOUR_MS, read_payload

BARS_PER_HOUR: Final = 12
HOLD_BARS: Final = 4 * BARS_PER_HOUR
# The probe needs an hour of history before a bar can be judged and four after it can be scored.
WARMUP_BARS: Final = BARS_PER_HOUR

PERMUTATION_TRIALS: Final = 2_000
PERMUTATION_SEED: Final = 459


@dataclass(frozen=True, slots=True)
class Rule:
    """One entry rule over the derived per-bar features. Thresholds are basis points."""

    name: str
    min_contracts_change_1h_bps: int | None = None
    pre_move_band_bps: tuple[int, int] | None = None
    min_oi_usd: int = 0
    # The two controls from #459 v3 tables A and B, replayed on the same corpus for contrast.
    min_usd_change_5m_bps: int | None = None
    min_pulse_bps: int | None = None

    def admits(self, bar: Bar) -> bool:
        if bar.oi_usd < self.min_oi_usd:
            return False
        if self.min_contracts_change_1h_bps is not None and (
            bar.c60_bps is None or bar.c60_bps < self.min_contracts_change_1h_bps
        ):
            return False
        if self.pre_move_band_bps is not None:
            low, high = self.pre_move_band_bps
            if bar.pre1h_bps is None or not (low <= bar.pre1h_bps <= high):
                return False
        if self.min_usd_change_5m_bps is not None and (
            bar.usd5m_bps is None or bar.usd5m_bps < self.min_usd_change_5m_bps
        ):
            return False
        return not (self.min_pulse_bps is not None and (bar.pulse_bps is None or bar.pulse_bps < self.min_pulse_bps))


# ---------------------------------------------------------------- the pre-registration

# `oi_usd >= 5M` is the deployed admission floor, not a tuned parameter: it is what the live Candidate
# Gate already refuses below, so a rule that fires under it could never reach a Case.
PRE_REGISTERED: Final = Rule(
    name="C60>=5% & pre1h in [0,6%] & oi>=$5M",
    min_contracts_change_1h_bps=500,
    pre_move_band_bps=(0, 600),
    min_oi_usd=5_000_000,
)
STOP_LOSS_BPS: Final = 200
COST_BPS: Final = 20
DEDUPE_MS: Final = 24 * HOUR_MS
PASS_MIN_EVENTS: Final = 200
PASS_MAX_P: Final = 0.01

# The 36 symbols the hypothesis was found on (#459 v3): 21 that had recently carried a provider frame,
# plus 20 liquid controls. Scoring excludes every one of them, whether or not it produced an event --
# the selection happened at the symbol level, so the holdout has to be at the symbol level too.
DISCOVERY_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        "0G", "AGT", "ARB", "BLESS", "CC", "COLLECT", "CRV", "CYS", "ETH", "FORM", "HEMI",
        "HYPE", "LIT", "MAGMA", "MIRA", "SKR", "SWARMS", "TWT", "UAI", "UNI", "ZORA",
        "BTC", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "SUI", "PEPE", "WIF", "APT",
        "OP", "NEAR", "TAO", "ONDO", "ENA", "TRUMP", "WLD", "SEI", "AAVE",
    }
)  # fmt: skip

# Reported, never selected from.
ROBUSTNESS: Final[tuple[Rule, ...]] = (
    Rule("C60>=3%", min_contracts_change_1h_bps=300, min_oi_usd=5_000_000),
    Rule("C60>=5%", min_contracts_change_1h_bps=500, min_oi_usd=5_000_000),
    Rule("C60>=8%", min_contracts_change_1h_bps=800, min_oi_usd=5_000_000),
    Rule("C60>=5% & pre1h [0,3%]", min_contracts_change_1h_bps=500, pre_move_band_bps=(0, 300), min_oi_usd=5_000_000),
    Rule("C60>=5% & pre1h [3,6%]", min_contracts_change_1h_bps=500, pre_move_band_bps=(300, 600), min_oi_usd=5_000_000),
    Rule(
        "C60>=5% & pre1h [6,10%]", min_contracts_change_1h_bps=500, pre_move_band_bps=(600, 1_000), min_oi_usd=5_000_000
    ),
    Rule(
        "C60>=5% & pre1h [-inf,0)",
        min_contracts_change_1h_bps=500,
        pre_move_band_bps=(-1_000_000, -1),
        min_oi_usd=5_000_000,
    ),
    Rule("C60>=3% & pre1h [0,6%]", min_contracts_change_1h_bps=300, pre_move_band_bps=(0, 600), min_oi_usd=5_000_000),
    Rule("C60>=8% & pre1h [0,6%]", min_contracts_change_1h_bps=800, pre_move_band_bps=(0, 600), min_oi_usd=5_000_000),
)
CONTROLS: Final[tuple[Rule, ...]] = (
    Rule("VENDOR_LIKE usd 5m >=3%", min_usd_change_5m_bps=300, min_oi_usd=5_000_000),
    Rule("PULSE_OI_BREAKOUT >=8% vs 6-bar mean", min_pulse_bps=800, min_oi_usd=5_000_000),
)


@dataclass(slots=True)
class Bar:
    """One five-minute bar of one symbol, with every feature and outcome the rules can read."""

    symbol: str
    open_ms: int
    oi_usd: int
    c60_bps: int | None
    pre1h_bps: int | None
    usd5m_bps: int | None
    pulse_bps: int | None
    # Outcomes, in basis points, net of `COST_BPS` and after the stop rule.
    net_1h_bps: float | None
    net_4h_bps: float | None
    gross_4h_bps: float | None
    stopped: bool
    mae_bps: float | None
    # The same holding period with neither stop nor cost, which is the convention the 36-symbol probe
    # measured under. Carried so the replay can say *which* of the two changes moved the number.
    hold_4h_bps: float | None


def _bps(numerator: float, denominator: float) -> int | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator - 1.0) * 10_000)


def _exit_return_bps(
    closes: Sequence[float], opens: Sequence[float], lows: Sequence[float], i: int, bars: int
) -> tuple[float, bool, float]:
    """Gross return, whether the stop fired, and the worst excursion, over `bars` bars after `i`."""

    entry = closes[i]
    stop_price = entry * (1.0 - STOP_LOSS_BPS / 10_000.0)
    worst = entry
    for j in range(i + 1, i + bars + 1):
        worst = min(worst, lows[j])
        if lows[j] <= stop_price:
            # A bar that gaps through the stop fills at its open, which is worse than the stop price.
            fill = min(opens[j], stop_price)
            return (fill / entry - 1.0) * 10_000.0, True, (worst / entry - 1.0) * 10_000.0
    return (closes[i + bars] / entry - 1.0) * 10_000.0, False, (worst / entry - 1.0) * 10_000.0


def iter_bars(corpus_dir: Path, manifest: dict[str, Any], *, progress_every: int = 100) -> Iterator[Bar]:
    """Yield every scoreable bar in the corpus, one symbol at a time. No rule applied yet.

    A generator on purpose. The corpus holds ~4.35 M scoreable bars, and materialising them as objects
    costs gigabytes for a result that is a few hundred events and one column of returns; the caller keeps
    what it needs and lets the rest go.
    """

    records = manifest["symbols"]
    for index, record in enumerate(records, start=1):
        symbol = str(record["symbol"])
        base = symbol.removesuffix("USDT")
        if not record.get("oi_points") or not record.get("candle_points"):
            continue
        oi_rows = read_payload(corpus_dir, str(record["oi_sha256"]))
        candle_rows = read_payload(corpus_dir, str(record["candle_sha256"]))
        candles = {int(row[0]): (float(row[1]), float(row[2]), float(row[3]), float(row[4])) for row in candle_rows}
        contracts: dict[int, float] = {}
        usd: dict[int, float] = {}
        for row in oi_rows:
            stamp = int(row["timestamp"]) // FIVE_MIN_MS * FIVE_MIN_MS
            contracts[stamp] = float(row["sumOpenInterest"])
            usd[stamp] = float(row["sumOpenInterestValue"])

        stamps = sorted(stamp for stamp in contracts if stamp in candles)
        # A gap in the provider's series must not silently shift the lookback: every feature indexes by
        # timestamp arithmetic, not by position, so a missing bar makes its dependants `None`.
        closes = {stamp: candles[stamp][3] for stamp in stamps}
        for position, stamp in enumerate(stamps):
            forward = [stamp + FIVE_MIN_MS * step for step in range(1, HOLD_BARS + 1)]
            if any(step not in candles for step in forward):
                continue
            if position < WARMUP_BARS:
                continue
            hour_ago = stamp - HOUR_MS
            five_ago = stamp - FIVE_MIN_MS
            pulse_window = [stamp - FIVE_MIN_MS * step for step in range(1, 7)]
            c60 = _bps(contracts[stamp], contracts[hour_ago]) if hour_ago in contracts else None
            pre1h = _bps(closes[stamp], closes[hour_ago]) if hour_ago in closes else None
            usd5m = _bps(usd[stamp], usd[five_ago]) if five_ago in usd else None
            pulse = None
            if all(step in contracts for step in pulse_window):
                mean = statistics.fmean(contracts[step] for step in pulse_window)
                pulse = _bps(contracts[stamp], mean)

            ordered = [stamp, *forward]
            opens = [candles[step][0] for step in ordered]
            lows = [candles[step][2] for step in ordered]
            closes_seq = [candles[step][3] for step in ordered]
            gross_4h, stopped, mae = _exit_return_bps(closes_seq, opens, lows, 0, HOLD_BARS)
            gross_1h, _stopped_1h, _mae_1h = _exit_return_bps(closes_seq, opens, lows, 0, BARS_PER_HOUR)
            hold_4h = (closes_seq[HOLD_BARS] / closes_seq[0] - 1.0) * 10_000.0
            yield Bar(
                symbol=base,
                open_ms=stamp,
                oi_usd=int(usd[stamp]),
                c60_bps=c60,
                pre1h_bps=pre1h,
                usd5m_bps=usd5m,
                pulse_bps=pulse,
                net_1h_bps=gross_1h - COST_BPS,
                net_4h_bps=gross_4h - COST_BPS,
                gross_4h_bps=gross_4h,
                stopped=stopped,
                mae_bps=mae,
                hold_4h_bps=hold_4h,
            )
        if index % progress_every == 0:
            print(f"[oi-replay] {index}/{len(records)} symbols", flush=True)


def dedupe(events: list[Bar], *, gap_ms: int = DEDUPE_MS) -> list[Bar]:
    """One event per symbol per `gap_ms`, keeping the first. The rule fires on runs, not on ticks."""

    last: dict[str, int] = {}
    kept: list[Bar] = []
    for bar in sorted(events, key=lambda b: (b.symbol, b.open_ms)):
        if bar.symbol in last and bar.open_ms - last[bar.symbol] < gap_ms:
            continue
        last[bar.symbol] = bar.open_ms
        kept.append(bar)
    return sorted(kept, key=lambda b: b.open_ms)


@dataclass(slots=True)
class Score:
    rule: str
    events: int
    symbols: int
    per_day: float
    mean_net_4h_bps: float | None
    median_net_4h_bps: float | None
    mean_net_1h_bps: float | None
    win_rate: float | None
    stopped_share: float | None
    median_mae_bps: float | None
    top10_symbol_share: float | None
    mean_hold_4h_bps: float | None = None
    median_hold_4h_bps: float | None = None
    hold_win_rate: float | None = None
    permutation_p: float | None = None
    baseline_mean_net_4h_bps: float | None = None
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "events": self.events,
            "symbols": self.symbols,
            "per_day": round(self.per_day, 2),
            "mean_net_4h_bps": None if self.mean_net_4h_bps is None else round(self.mean_net_4h_bps, 1),
            "median_net_4h_bps": None if self.median_net_4h_bps is None else round(self.median_net_4h_bps, 1),
            "mean_net_1h_bps": None if self.mean_net_1h_bps is None else round(self.mean_net_1h_bps, 1),
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "stopped_share": None if self.stopped_share is None else round(self.stopped_share, 4),
            "median_mae_bps": None if self.median_mae_bps is None else round(self.median_mae_bps, 1),
            "top10_symbol_share": None if self.top10_symbol_share is None else round(self.top10_symbol_share, 4),
            "mean_hold_4h_bps": None if self.mean_hold_4h_bps is None else round(self.mean_hold_4h_bps, 1),
            "median_hold_4h_bps": None if self.median_hold_4h_bps is None else round(self.median_hold_4h_bps, 1),
            "hold_win_rate": None if self.hold_win_rate is None else round(self.hold_win_rate, 4),
            "permutation_p": self.permutation_p,
            "baseline_mean_net_4h_bps": (
                None if self.baseline_mean_net_4h_bps is None else round(self.baseline_mean_net_4h_bps, 1)
            ),
            "note": self.note,
        }


def _median_or_none(values: list[float]) -> float | None:
    """A median of exactly zero is a real answer; `x or None` would report it as "not measured"."""

    return statistics.median(values) if values else None


def score(rule_name: str, events: Sequence[Bar], *, window_days: float, note: str = "") -> Score:
    if not events:
        return Score(rule_name, 0, 0, 0.0, None, None, None, None, None, None, None, note=note)
    hold4 = [bar.hold_4h_bps for bar in events if bar.hold_4h_bps is not None]
    net4 = [bar.net_4h_bps for bar in events if bar.net_4h_bps is not None]
    net1 = [bar.net_1h_bps for bar in events if bar.net_1h_bps is not None]
    counts: dict[str, int] = {}
    for bar in events:
        counts[bar.symbol] = counts.get(bar.symbol, 0) + 1
    top10 = sum(sorted(counts.values(), reverse=True)[:10])
    return Score(
        rule=rule_name,
        events=len(events),
        symbols=len(counts),
        per_day=len(events) / window_days if window_days else 0.0,
        mean_net_4h_bps=statistics.fmean(net4) if net4 else None,
        median_net_4h_bps=statistics.median(net4) if net4 else None,
        mean_net_1h_bps=statistics.fmean(net1) if net1 else None,
        win_rate=sum(1 for value in net4 if value > 0) / len(net4) if net4 else None,
        stopped_share=sum(1 for bar in events if bar.stopped) / len(events),
        median_mae_bps=_median_or_none([bar.mae_bps for bar in events if bar.mae_bps is not None]),
        top10_symbol_share=top10 / len(events),
        mean_hold_4h_bps=statistics.fmean(hold4) if hold4 else None,
        median_hold_4h_bps=_median_or_none(hold4),
        hold_win_rate=sum(1 for value in hold4 if value > 0) / len(hold4) if hold4 else None,
    )


def permutation_p(
    observed_mean: float, population: Sequence[float], count: int, *, trials: int = PERMUTATION_TRIALS
) -> float:
    """P(random entries in the same universe do at least as well), at the observed event count."""

    if count == 0 or len(population) < count:
        return 1.0
    rng = random.Random(PERMUTATION_SEED)  # noqa: S311 - a placebo draw, not a secret
    hits = 0
    for _ in range(trials):
        sample = rng.sample(population, count)
        if statistics.fmean(sample) >= observed_mean:
            hits += 1
    # Add-one so a p of exactly zero is never reported from a finite number of draws.
    return (hits + 1) / (trials + 1)


@dataclass(slots=True)
class ReplayReport:
    manifest_sha256: str
    window: dict[str, int]
    scores: list[dict[str, Any]] = field(default_factory=list)


def run_replay(corpus_dir: Path, *, trials: int = PERMUTATION_TRIALS, now_ms: int) -> dict[str, Any]:
    manifest = json.loads((corpus_dir / "manifest.json").read_text())
    window = manifest["window"]
    window_days = (window["end_ms"] - window["start_ms"]) / 86_400_000

    started = time.monotonic()
    # One pass. Every rule is evaluated on each bar as it goes by, so the corpus is decompressed once
    # and only the matches and the outcome column survive the loop.
    all_rules: tuple[Rule, ...] = (PRE_REGISTERED, *ROBUSTNESS, *CONTROLS)
    holdout_hits: dict[str, list[Bar]] = {rule.name: [] for rule in all_rules}
    discovery_hits: list[Bar] = []
    holdout_population: list[float] = []
    holdout_hold_population: list[float] = []
    holdout_stopped = 0
    total_bars = 0
    holdout_bars = 0
    for bar in iter_bars(corpus_dir, manifest):
        total_bars += 1
        in_holdout = bar.symbol not in DISCOVERY_SYMBOLS
        if in_holdout:
            holdout_bars += 1
            if bar.net_4h_bps is not None:
                holdout_population.append(bar.net_4h_bps)
            if bar.hold_4h_bps is not None:
                holdout_hold_population.append(bar.hold_4h_bps)
            holdout_stopped += int(bar.stopped)
            for rule in all_rules:
                if rule.admits(bar):
                    holdout_hits[rule.name].append(bar)
        elif PRE_REGISTERED.admits(bar):
            discovery_hits.append(bar)
    print(
        f"[oi-replay] {total_bars} scoreable bars ({holdout_bars} holdout) in {(time.monotonic() - started) / 60:.1f}m",
        flush=True,
    )
    baseline_mean = statistics.fmean(holdout_population) if holdout_population else None
    baseline_hold_mean = statistics.fmean(holdout_hold_population) if holdout_hold_population else None
    baseline_stop_rate = holdout_stopped / holdout_bars if holdout_bars else None

    def events_for(rule: Rule) -> list[Bar]:
        return dedupe(holdout_hits[rule.name])

    results: list[Score] = []

    primary_events = events_for(PRE_REGISTERED)
    primary = score(f"PRIMARY holdout · {PRE_REGISTERED.name}", primary_events, window_days=window_days)
    if primary.mean_net_4h_bps is not None:
        primary.permutation_p = permutation_p(
            primary.mean_net_4h_bps, holdout_population, primary.events, trials=trials
        )
    primary.baseline_mean_net_4h_bps = baseline_mean
    primary.note = "pre-registered rule, symbols outside the discovery set"
    results.append(primary)

    # The probe measured this rule with neither the stop nor the cost. Reporting the same events under
    # that convention is what separates "the holdout killed it" from "the exit rule killed it" -- and
    # its own permutation p is against the same-convention null, so the stop cannot flatter either side.
    probe_view = score(
        f"PRIMARY holdout · probe convention (no stop, no cost) · {PRE_REGISTERED.name}",
        primary_events,
        window_days=window_days,
    )
    if probe_view.mean_hold_4h_bps is not None:
        probe_view.permutation_p = permutation_p(
            probe_view.mean_hold_4h_bps, holdout_hold_population, probe_view.events, trials=trials
        )
    probe_view.baseline_mean_net_4h_bps = baseline_hold_mean
    probe_view.note = "same events, measured the way the 36-symbol probe measured them"
    results.append(probe_view)

    discovery_events = dedupe(discovery_hits)
    discovery_score = score(f"DISCOVERY set · {PRE_REGISTERED.name}", discovery_events, window_days=window_days)
    discovery_score.note = "the 36 symbols the hypothesis was found on; not evidence, shown for contrast"
    results.append(discovery_score)

    midpoint = window["start_ms"] + (window["end_ms"] - window["start_ms"]) // 2
    for label, selected in (
        ("first half", [bar for bar in primary_events if bar.open_ms < midpoint]),
        ("second half", [bar for bar in primary_events if bar.open_ms >= midpoint]),
    ):
        half = score(f"PRIMARY holdout · {label}", selected, window_days=window_days / 2)
        half.note = "time split of the primary result; reported, not selected on"
        results.append(half)

    for rule in ROBUSTNESS:
        entry = score(f"robustness · {rule.name}", events_for(rule), window_days=window_days)
        entry.note = "reported, never selected from"
        results.append(entry)

    for rule in CONTROLS:
        entry = score(f"control · {rule.name}", events_for(rule), window_days=window_days)
        entry.note = "contrast: the vendor-style and Pulse rules on the same corpus"
        results.append(entry)

    verdict = _verdict(primary)
    report = {
        "artifact": "SOURCE_FEATURE_DISCOVERY_REPLAY_V1",
        "issue": 459,
        "stage": "A",
        "corpus_manifest_sha256": manifest["manifest_sha256"],
        "window": window,
        "window_days": round(window_days, 2),
        "universe_symbols": manifest["coverage"]["symbols_with_open_interest"],
        "scoreable_bars": total_bars,
        "holdout_bars": holdout_bars,
        "discovery_symbols_excluded": sorted(DISCOVERY_SYMBOLS),
        "pre_registration": {
            "rule": PRE_REGISTERED.name,
            "entry": "close of the 5m bar the open-interest reading opens",
            "hold_bars": HOLD_BARS,
            "stop_loss_bps": STOP_LOSS_BPS,
            "cost_bps": COST_BPS,
            "dedupe_hours": DEDUPE_MS // HOUR_MS,
            "pass_conditions": {
                "min_events": PASS_MIN_EVENTS,
                "mean_net_4h_bps": "> 0",
                "win_rate": "> 0.5",
                "max_permutation_p": PASS_MAX_P,
            },
            "permutation_trials": trials,
            "permutation_seed": PERMUTATION_SEED,
        },
        "baseline_mean_net_4h_bps": None if baseline_mean is None else round(baseline_mean, 1),
        "baseline_mean_hold_4h_bps": None if baseline_hold_mean is None else round(baseline_hold_mean, 1),
        "baseline_stop_rate": None if baseline_stop_rate is None else round(baseline_stop_rate, 4),
        "verdict": verdict,
        "scores": [entry.as_json() for entry in results],
        "produced_at_ms": now_ms,
    }
    return report


def _verdict(primary: Score) -> dict[str, Any]:
    """`CANDIDATE_LOCKED` only when every pre-registered condition holds; otherwise `NO_CANDIDATE`."""

    checks = {
        "events_at_least_200": primary.events >= PASS_MIN_EVENTS,
        "mean_net_4h_positive": bool(primary.mean_net_4h_bps is not None and primary.mean_net_4h_bps > 0),
        "win_rate_above_half": bool(primary.win_rate is not None and primary.win_rate > 0.5),
        "permutation_p_below_1pct": bool(primary.permutation_p is not None and primary.permutation_p < PASS_MAX_P),
    }
    return {
        "decision": "CANDIDATE_LOCKED" if all(checks.values()) else "NO_CANDIDATE",
        "checks": checks,
    }


def render_table(report: dict[str, Any]) -> str:
    header = (
        f"{'rule':<52}{'N':>6}{'sym':>5}{'/day':>7}{'mean4H':>9}{'med4H':>8}{'win':>7}"
        f"{'stop':>7}{'holdMean':>10}{'holdWin':>9}{'p':>8}"
    )
    lines = [header, "-" * len(header)]
    lines.extend(
        f"{row['rule'][:51]:<52}"
        f"{row['events']:>6}"
        f"{row['symbols']:>5}"
        f"{row['per_day']:>7.2f}"
        f"{_cell(row['mean_net_4h_bps']):>9}"
        f"{_cell(row['median_net_4h_bps']):>8}"
        f"{_pct(row['win_rate']):>7}"
        f"{_pct(row['stopped_share']):>7}"
        f"{_cell(row['mean_hold_4h_bps']):>10}"
        f"{_pct(row['hold_win_rate']):>9}"
        f"{_cell(row['permutation_p']):>8}"
        for row in report["scores"]
    )
    return "\n".join(lines)


def _cell(value: float | None) -> str:
    return "—" if value is None else (f"{value:.3f}" if abs(value) < 1 else f"{value:.0f}")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


__all__ = [
    "COST_BPS",
    "DISCOVERY_SYMBOLS",
    "PRE_REGISTERED",
    "STOP_LOSS_BPS",
    "Bar",
    "Rule",
    "Score",
    "dedupe",
    "iter_bars",
    "permutation_p",
    "render_table",
    "run_replay",
    "score",
]
