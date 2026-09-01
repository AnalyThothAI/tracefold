"""#459 Stage A: the corpus seal and the replay's measurement conventions.

The replay's answer is a number an issue gets closed on, so what these pin is not "it runs" but the
four ways it could quietly lie: reading a feature that was not observable at entry, letting a gap in
the provider's series shift a lookback, filling a stop better than the market would, and reporting a
corpus payload that no longer hashes to what the manifest sealed.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.trading.research.oi_corpus import (
    FIVE_MIN_MS,
    CorpusError,
    CorpusWindow,
    read_payload,
    seal,
    window_now,
)
from tracefold.trading.research.oi_replay import (
    BARS_PER_HOUR,
    COST_BPS,
    HOLD_BARS,
    STOP_LOSS_BPS,
    Bar,
    Rule,
    _exit_return_bps,
    dedupe,
    iter_bars,
    permutation_p,
    score,
)

START = 1_785_000_000_000 // FIVE_MIN_MS * FIVE_MIN_MS


def _bar(**over: Any) -> Bar:
    base: dict[str, Any] = {
        "symbol": "TEST",
        "open_ms": START,
        "oi_usd": 10_000_000,
        "c60_bps": 600,
        "pre1h_bps": 300,
        "usd5m_bps": 100,
        "pulse_bps": 100,
        "net_1h_bps": 10.0,
        "net_4h_bps": 20.0,
        "gross_4h_bps": 40.0,
        "stopped": False,
        "mae_bps": -50.0,
        "hold_4h_bps": 40.0,
    }
    base.update(over)
    return Bar(**base)


# ---------------------------------------------------------------- the exit rule


def test_a_position_that_never_trades_down_exits_at_the_holding_period_close() -> None:
    closes = [100.0] + [101.0] * HOLD_BARS
    opens = list(closes)
    lows = [99.5] * (HOLD_BARS + 1)
    gross, stopped, mae = _exit_return_bps(closes, opens, lows, 0, HOLD_BARS)

    assert stopped is False
    assert gross == pytest.approx(100.0)  # +1%
    assert mae == pytest.approx(-50.0)


def test_the_stop_fires_on_a_low_and_not_on_a_close() -> None:
    """A bar that dips through the stop and recovers is still a stopped position.

    Measuring the exit on closes only would report the round trip as flat, which is the single most
    flattering mistake available to a long-only backtest on five-minute bars.
    """

    closes = [100.0] * (HOLD_BARS + 1)
    opens = list(closes)
    lows = [100.0] * (HOLD_BARS + 1)
    lows[3] = 97.9  # below the 98.0 stop, then the close recovers

    gross, stopped, _ = _exit_return_bps(closes, opens, lows, 0, HOLD_BARS)
    assert stopped is True
    assert gross == pytest.approx(-STOP_LOSS_BPS)


def test_a_gap_through_the_stop_fills_at_the_open_not_at_the_stop_price() -> None:
    closes = [100.0] * (HOLD_BARS + 1)
    opens = [100.0] * (HOLD_BARS + 1)
    lows = [100.0] * (HOLD_BARS + 1)
    opens[5] = 95.0
    lows[5] = 94.0

    gross, stopped, _ = _exit_return_bps(closes, opens, lows, 0, HOLD_BARS)
    assert stopped is True
    assert gross == pytest.approx(-500.0), "a 5% gap must not be reported as a 2% loss"


# ---------------------------------------------------------------- the rule vocabulary


def test_the_price_band_is_inclusive_at_both_ends_and_refuses_an_unmeasured_bar() -> None:
    rule = Rule("band", min_contracts_change_1h_bps=500, pre_move_band_bps=(0, 600))
    assert rule.admits(_bar(pre1h_bps=0))
    assert rule.admits(_bar(pre1h_bps=600))
    assert not rule.admits(_bar(pre1h_bps=601))
    assert not rule.admits(_bar(pre1h_bps=-1))
    # A feature the corpus could not compute is a refusal, never a pass-through.
    assert not rule.admits(_bar(pre1h_bps=None))
    assert not rule.admits(_bar(c60_bps=None))


def test_the_liquidity_floor_refuses_below_the_deployed_admission_gate() -> None:
    rule = Rule("floor", min_oi_usd=5_000_000)
    assert rule.admits(_bar(oi_usd=5_000_000))
    assert not rule.admits(_bar(oi_usd=4_999_999))


def test_one_event_per_symbol_per_day_keeps_the_first() -> None:
    """The rule fires on runs. Counting every bar of one run would report one move as thirty."""

    hour = 3_600_000
    events = [
        _bar(symbol="AAA", open_ms=START),
        _bar(symbol="AAA", open_ms=START + 6 * hour),
        _bar(symbol="AAA", open_ms=START + 25 * hour),
        _bar(symbol="BBB", open_ms=START + hour),
    ]
    kept = dedupe(events)
    # The 6 h repeat is dropped; the one a day later opens a new window. Order is chronological.
    assert [(bar.symbol, bar.open_ms) for bar in kept] == [
        ("AAA", START),
        ("BBB", START + hour),
        ("AAA", START + 25 * hour),
    ]


# ---------------------------------------------------------------- scoring and the null


def test_score_counts_a_win_after_costs_not_before() -> None:
    events = [_bar(net_4h_bps=1.0), _bar(net_4h_bps=-1.0), _bar(net_4h_bps=-1.0)]
    result = score("t", events, window_days=1.0)
    assert result.events == 3
    assert result.win_rate == pytest.approx(1 / 3)
    assert result.mean_net_4h_bps == pytest.approx(-1 / 3)


def test_the_permutation_null_is_the_same_universe_and_never_reports_exactly_zero() -> None:
    population = [float(value) for value in range(1_000)]
    # An observed mean at the top of the population: no random draw of the same size beats it, and the
    # add-one keeps the reported p above zero rather than claiming a certainty 2000 draws cannot buy.
    assert permutation_p(10_000.0, population, 50, trials=200) == pytest.approx(1 / 201)
    # An observed mean at the bottom: every draw beats it.
    assert permutation_p(-10_000.0, population, 50, trials=200) == pytest.approx(1.0)


def test_the_permutation_is_reproducible_from_its_seed() -> None:
    population = [float(value % 37) for value in range(5_000)]
    first = permutation_p(18.0, population, 100, trials=300)
    assert first == permutation_p(18.0, population, 100, trials=300)


# ---------------------------------------------------------------- the sealed corpus round trip


def _write_corpus(tmp_path: Path, *, bars: int, contracts: list[float], closes: list[float]) -> Path:
    """A one-symbol corpus written through the real sealing path, then read back by the replay."""

    from tracefold.trading.research import oi_corpus

    corpus = tmp_path / "corpus"
    (corpus / "raw").mkdir(parents=True)
    window = CorpusWindow(start_ms=START, end_ms=START + bars * FIVE_MIN_MS)
    (corpus / "window.json").write_text(json.dumps(window.as_json()))
    (corpus / "universe.json").write_text(json.dumps({"symbols": ["TESTUSDT"], "captured_at_ms": 0}))

    oi_rows = [
        {
            "symbol": "TESTUSDT",
            "sumOpenInterest": f"{contracts[index]}",
            "sumOpenInterestValue": f"{contracts[index] * closes[index]}",
            "timestamp": START + index * FIVE_MIN_MS,
        }
        for index in range(bars)
    ]
    candle_rows = [
        [START + index * FIVE_MIN_MS, closes[index], closes[index], closes[index], closes[index], "1", 0, "1"]
        for index in range(bars)
    ]
    oi_sha, oi_bytes = oi_corpus.write_payload(corpus / "raw", oi_rows)
    candle_sha, candle_bytes = oi_corpus.write_payload(corpus / "raw", candle_rows)
    (corpus / "progress.jsonl").write_text(
        json.dumps(
            {
                "symbol": "TESTUSDT",
                "oi_sha256": oi_sha,
                "oi_points": len(oi_rows),
                "oi_first_ms": oi_rows[0]["timestamp"],
                "oi_last_ms": oi_rows[-1]["timestamp"],
                "candle_sha256": candle_sha,
                "candle_points": len(candle_rows),
                "candle_first_ms": candle_rows[0][0],
                "candle_last_ms": candle_rows[-1][0],
                "stored_bytes": oi_bytes + candle_bytes,
                "pulled_at_ms": 0,
            },
            sort_keys=True,
        )
        + "\n"
    )
    seal(corpus, now_ms=START)
    return corpus


def test_a_sealed_payload_that_no_longer_hashes_to_its_digest_is_refused(tmp_path: Path) -> None:
    """The corpus cannot be re-pulled -- Binance keeps 30 days -- so a silent edit would be permanent."""

    bars = BARS_PER_HOUR + HOLD_BARS + 5
    corpus = _write_corpus(tmp_path, bars=bars, contracts=[100.0] * bars, closes=[10.0] * bars)
    manifest = json.loads((corpus / "manifest.json").read_text())
    digest = str(manifest["symbols"][0]["oi_sha256"])
    assert read_payload(corpus, digest)

    target = corpus / "raw" / f"{digest}.json.gz"
    target.write_bytes(gzip.compress(b'[{"tampered": true}]', mtime=0))
    with pytest.raises(CorpusError, match=digest):
        read_payload(corpus, digest)


def test_sealing_the_same_corpus_twice_gives_it_the_same_identity(tmp_path: Path) -> None:
    """`manifest_sha256` names the corpus, not the moment it was sealed.

    A receipt quotes this digest as the data it ran on. If a re-seal changed it, every earlier receipt
    would read as though it had been produced from something else.
    """

    bars = BARS_PER_HOUR + HOLD_BARS + 5
    corpus = _write_corpus(tmp_path, bars=bars, contracts=[100.0] * bars, closes=[10.0] * bars)
    first = json.loads((corpus / "manifest.json").read_text())
    second = seal(corpus, now_ms=first["sealed_at_ms"] + 86_400_000)

    assert second["manifest_sha256"] == first["manifest_sha256"]
    assert second["sealed_at_ms"] != first["sealed_at_ms"]


def test_the_replay_reads_only_features_that_precede_its_own_entry(tmp_path: Path) -> None:
    """`c60` is the hour *before* the entry bar, never the hour around it.

    The series steps up once, at the entry bar. If the lookback were centred, or read the next reading,
    the bar before the step would already show the rise -- which is how a backtest buys a move it could
    not have seen.
    """

    step_at = BARS_PER_HOUR + 1
    # Long enough that a bar a full hour past the step still has its four-hour forward window.
    bars = step_at + BARS_PER_HOUR + HOLD_BARS + 2
    contracts = [100.0 if index < step_at else 110.0 for index in range(bars)]
    corpus = _write_corpus(tmp_path, bars=bars, contracts=contracts, closes=[10.0] * bars)
    manifest = json.loads((corpus / "manifest.json").read_text())

    by_index = {bar.open_ms: bar for bar in iter_bars(corpus, manifest)}
    before = by_index[START + (step_at - 1) * FIVE_MIN_MS]
    at_step = by_index[START + step_at * FIVE_MIN_MS]
    an_hour_after = by_index[START + (step_at + BARS_PER_HOUR) * FIVE_MIN_MS]

    assert before.c60_bps == 0, "the bar before the step must not know about it"
    assert at_step.c60_bps == 1_000, "+10% against the reading an hour earlier"
    assert an_hour_after.c60_bps == 0, "the step has left the lookback window"


def test_a_hole_in_the_provider_series_makes_its_dependants_unmeasured(tmp_path: Path) -> None:
    """Indexing by position rather than by timestamp would shift the lookback across a gap.

    A missing five-minute reading would then make `c60` compare 55 minutes, silently, on exactly the
    symbols whose data is worst -- so the feature is `None` and the rule refuses the bar instead.
    """

    from tracefold.trading.research import oi_corpus

    bars = BARS_PER_HOUR * 3 + HOLD_BARS
    corpus = _write_corpus(tmp_path, bars=bars, contracts=[100.0] * bars, closes=[10.0] * bars)
    manifest = json.loads((corpus / "manifest.json").read_text())

    # Drop one reading an hour before a bar that would otherwise be measurable.
    digest = str(manifest["symbols"][0]["oi_sha256"])
    rows = [row for row in read_payload(corpus, digest) if int(row["timestamp"]) != START + BARS_PER_HOUR * FIVE_MIN_MS]
    new_sha, _ = oi_corpus.write_payload(corpus / "raw", rows)
    manifest["symbols"][0]["oi_sha256"] = new_sha
    manifest["symbols"][0]["oi_points"] = len(rows)
    (corpus / "manifest.json").write_text(json.dumps(manifest))

    by_index = {bar.open_ms: bar for bar in iter_bars(corpus, manifest)}
    orphaned = by_index[START + BARS_PER_HOUR * 2 * FIVE_MIN_MS]
    assert orphaned.c60_bps is None
    assert not Rule("r", min_contracts_change_1h_bps=0).admits(orphaned)


def test_costs_are_charged_once_on_the_round_trip(tmp_path: Path) -> None:
    bars = BARS_PER_HOUR + HOLD_BARS + 2
    closes = [10.0] * bars
    # A clean +1% four hours after the first measurable bar.
    entry_index = BARS_PER_HOUR
    for index in range(entry_index + 1, bars):
        closes[index] = 10.1
    corpus = _write_corpus(tmp_path, bars=bars, contracts=[100.0] * bars, closes=closes)
    manifest = json.loads((corpus / "manifest.json").read_text())

    entry = next(bar for bar in iter_bars(corpus, manifest) if bar.open_ms == START + entry_index * FIVE_MIN_MS)
    assert entry.hold_4h_bps == pytest.approx(100.0)
    assert entry.net_4h_bps == pytest.approx(100.0 - COST_BPS)


def test_the_corpus_window_is_aligned_to_the_five_minute_grid() -> None:
    window = window_now(days=1, now_ms=START + 4 * 60_000 + 17)
    assert window.end_ms % FIVE_MIN_MS == 0
    assert window.end_ms <= START + 4 * 60_000 + 17
    assert window.end_ms - window.start_ms == 86_400_000
    # The candle window is wider on both sides: `pre1h` needs history, the forward return needs future.
    assert window.candle_start_ms < window.start_ms
    assert window.candle_end_ms > window.end_ms
