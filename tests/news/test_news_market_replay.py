"""Offline replay of the v1 rules at raw-record granularity (#553 PR-2 acceptance).

Every record here goes through the real wire template and the real parser before it reaches a rule,
so what is replayed is what a provider frame becomes -- not a hand-built fact. The corpus is shaped
from the production sample the Issue measured (§9: 405 OI, 111 liquidation and 108 smart-money live
records in one 72 h window, plus unparsable drift on the wallet Strategy), generated deterministically
from a seeded arrival pattern so the same replay produces the same report on every run.

**The totals here are not a product target.** They describe one shaped corpus. What the report is for
is the ratio a reader actually feels -- how many records became a card, how many were merged into one,
and how long a first card and a follow-up waited -- and that ratio is a property of the rules, which is
why it is asserted rather than printed.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace

import pytest

from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_notifications import (
    MarketObservation,
    decide_group,
    delivery_key,
    split_by_group,
)
from tracefold.news.oi_signals import measurement_definition, oi_source_contract, parse_oi_signal
from tracefold.news.smart_money import parse_smart_money

T0 = 1_787_000_000_000
OI_SYMBOLS = ("TAC", "WIF", "PENGU", "ENA", "SUI", "AVAX", "OP", "TIA")
LIQUIDATION_SYMBOLS = ("BTC", "ETH", "DOGE", "SOL")
LIQUIDATION_VENUES = ("binance", "hyperliquid", "okx", "bybit")
WALLETS = (
    ("Machi Big Brother", "0x4d3a"),
    ("James Wynn", "0x8bd1"),
    ("qwatio", None),
)
WALLET_SYMBOLS = ("ETH", "BTC", "HYPE")


@dataclass(frozen=True, slots=True)
class _Record:
    """One provider frame as it arrived: the wire line, the Strategy, and the host receive stamp."""

    item_id: str
    strategy_id: str
    kind: str
    title: str
    venue: str | None
    at_ms: int


def _oi_corpus(rng: random.Random) -> list[_Record]:
    """405 frames over 72 h, as OI telemetry actually arrives.

    Per symbol rather than uniformly: the provider's trigger fires repeatedly on one instrument while
    it is moving, so consecutive frames for one symbol are a drifting magnitude in a mostly-stable
    direction, not independent draws. That correlation is the whole reason the 2x follow-up rule
    suppresses anything, so a corpus of coin flips would replay a rule this one does not have.
    """

    records: list[_Record] = []
    index = 0
    for symbol in OI_SYMBOLS:
        at_ms = T0 + rng.randint(0, 3_600_000)
        direction = "Rise" if rng.random() < 0.6 else "Fall"
        magnitude = rng.uniform(1.5, 4.0)
        for _ in range(405 // len(OI_SYMBOLS) + (1 if index < 405 % len(OI_SYMBOLS) else 0)):
            if rng.random() < 0.12:
                direction = "Fall" if direction == "Rise" else "Rise"
                magnitude = rng.uniform(1.2, 3.5)
            else:
                magnitude = max(0.4, magnitude * rng.uniform(0.72, 1.45))
            title = (
                f"{symbol} OI {direction} {magnitude:.2f}%, OI Value {rng.choice([3.2, 8.83, 11.03, 42.6])}M, "
                f"Whale Long Profit {rng.choice([55.1, 84.91, 88.4])}%, Whale/OI Ratio {rng.choice([32.46, 143.9])}%"
            )
            records.append(
                _Record(
                    item_id=f"oi-{index}",
                    strategy_id="1019",
                    kind="oi",
                    title=title,
                    venue="binance",
                    at_ms=at_ms,
                )
            )
            index += 1
            at_ms += rng.randint(300_000, 5_400_000)
    return records


def _liquidation_corpus(rng: random.Random) -> list[_Record]:
    """111 reports over 72 h, clustered: a liquidation cascade is many reports in one minute."""

    records: list[_Record] = []
    at_ms = T0
    index = 0
    while index < 111:
        symbol = rng.choice(LIQUIDATION_SYMBOLS)
        venue = rng.choice(LIQUIDATION_VENUES)
        side = rng.choice(["Long", "Short"])
        burst = min(rng.choice([1, 1, 2, 4, 7]), 111 - index)
        for _ in range(burst):
            notional = round(rng.uniform(120, 980), 2)
            title = f"{symbol} Large {side} Liquidation {notional}K at ${round(rng.uniform(0.2, 118_000), 4)}"
            records.append(
                _Record(
                    item_id=f"liq-{index}",
                    strategy_id="2083",
                    kind="liquidation",
                    title=title,
                    venue=venue,
                    at_ms=at_ms,
                )
            )
            index += 1
            at_ms += rng.randint(1_000, 14_000)
        at_ms += rng.randint(600_000, 5_400_000)
    return records


def _smart_money_corpus(rng: random.Random) -> list[_Record]:
    """108 reports over 72 h, per account, bursty, and with the drift this Strategy really produces.

    Bursty because that is what the Issue's own counterexample shows: one account reported closing a
    short and opening one 49.241 s later. An account that fills a position in three prints inside a
    minute is one subject doing one thing, and it is exactly the case the 60 s window exists for.
    `Withdraw` has no Open/Close to compare and is kept as its own raw card.
    """

    records: list[_Record] = []
    index = 0
    per_wallet = 108 // len(WALLETS)
    for wallet_index, (label, _address) in enumerate(WALLETS):
        at_ms = T0 + rng.randint(0, 3_600_000)
        symbol = rng.choice(WALLET_SYMBOLS)
        action = rng.choice(["Open", "Close"])
        side = rng.choice(["Long", "Short"])
        count = per_wallet + (1 if wallet_index < 108 % len(WALLETS) else 0)
        while count > 0:
            burst = min(rng.choice([1, 2, 2, 3, 5]), count)
            if rng.random() < 0.3:
                action = "Close" if action == "Open" else "Open"
            if rng.random() < 0.2:
                side = "Short" if side == "Long" else "Long"
            for position in range(burst):
                if rng.random() < 0.09:
                    title = f"{label} Withdraw ${rng.randint(100, 4_000)},000 USDC"
                else:
                    title = (
                        f"{label} {action} {side} {symbol} "
                        f"${rng.randint(1, 9)},{rng.randint(100, 999)},{rng.randint(100, 999)}.00, "
                        f"Price ${round(rng.uniform(0.3, 4_200), 2)}"
                    )
                records.append(
                    _Record(
                        item_id=f"sm-{index}",
                        strategy_id="2026",
                        kind="smart_money",
                        title=title,
                        venue="hyperliquid",
                        at_ms=at_ms,
                    )
                )
                index += 1
                count -= 1
                # Inside one burst the fills are seconds apart; between bursts, hours.
                at_ms += rng.randint(4_000, 49_000) if position + 1 < burst else rng.randint(600_000, 9_000_000)
            symbol = rng.choice(WALLET_SYMBOLS) if rng.random() < 0.35 else symbol
    return records


def _observation(record: _Record) -> MarketObservation:
    """One record, parsed exactly as admission parses it, or kept as its own raw line."""

    base = MarketObservation(
        item_id=record.item_id,
        market_kind=record.kind,
        parse_status="raw",
        title=record.title,
        event_at_ms=record.at_ms,
        received_at_ms=record.at_ms,
        provider="opennews",
        source_strategy_id=record.strategy_id,
        source_venue=record.venue,
    )
    if record.kind == "oi":
        signal = parse_oi_signal(record.title)
        if signal is None:
            return base
        source = oi_source_contract({"strategies": [{"id": record.strategy_id}]})
        return replace(
            base,
            parse_status="parsed",
            raw_instrument=signal.raw_instrument,
            symbol=signal.symbol,
            measurement_definition=measurement_definition(source),
            direction=signal.direction,
            oi_change_bps=signal.oi_change_bps,
            oi_value_usd=signal.oi_value_usd,
        )
    if record.kind == "liquidation":
        fact = parse_liquidation(
            record.title,
            item_id=record.item_id,
            fact_id=record.item_id,
            source_strategy_id=record.strategy_id,
            provider_source=record.venue or "",
            event_at_ms=record.at_ms,
            received_at_ms=record.at_ms,
        )
        if fact is None:
            return base
        return replace(
            base,
            parse_status="parsed",
            raw_instrument=fact.raw_instrument,
            symbol=fact.symbol,
            liquidated_position_side=fact.liquidated_position_side,
            notional_usd=str(fact.notional_usd),
            price=str(fact.price),
        )
    address = next((value for label, value in WALLETS if record.title.startswith(label)), None)
    fact = parse_smart_money(
        record.title,
        item_id=record.item_id,
        fact_id=record.item_id,
        source_strategy_id=record.strategy_id,
        provider_source=record.venue or "",
        related_address=address,
        event_at_ms=record.at_ms,
        received_at_ms=record.at_ms,
    )
    if fact is None:
        return base
    return replace(
        base,
        parse_status="parsed",
        raw_instrument=fact.raw_instrument,
        symbol=fact.symbol,
        trader_label=fact.trader_label,
        account_address=fact.account_address,
        action=fact.action,
        position_side=fact.position_side,
        notional_usd=str(fact.reported_notional_usd),
        price=str(fact.price),
    )


@dataclass
class _Replay:
    """Per-kind counters and latencies, in the shape the acceptance item asks to be reported."""

    first: int = 0
    followup: int = 0
    action_change: int = 0
    raw: int = 0
    merged: int = 0
    first_latency_ms: list[int] = None  # type: ignore[assignment]
    followup_latency_ms: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.first_latency_ms = []
        self.followup_latency_ms = []

    @property
    def cards(self) -> int:
        return self.first + self.followup + self.action_change + self.raw


def _replay(records: list[_Record], *, tick_ms: int = 2_000) -> dict[str, _Replay]:
    """Drive the rules over the corpus at the loop's own tick, one group at a time.

    The clock advances in 2 s ticks, so a card's latency is a real wait rather than an instant: a
    first card is prepared on the tick after its record was committed, and a follow-up when its
    window closes. Nothing here sends -- the send is proved against the real adapter elsewhere -- so
    every prepared card is counted as delivered, which is what makes the anchors move.
    """

    report: dict[str, _Replay] = defaultdict(_Replay)
    tracks: dict[str, object] = {}
    open_intent: dict[str, tuple[str, str, int, int]] = {}
    uncovered: dict[str, int] = defaultdict(int)
    newest: dict[str, MarketObservation] = {}
    pending: list[MarketObservation] = []
    queue = sorted(records, key=lambda record: record.at_ms)
    cursor = 0
    now = queue[0].at_ms
    last = queue[-1].at_ms + 10 * tick_ms
    while now <= last:
        while cursor < len(queue) and queue[cursor].at_ms <= now:
            pending.append(_observation(queue[cursor]))
            cursor += 1
        for identity, observations in split_by_group(pending):
            key = identity.group_key
            turn = decide_group(
                tracks.get(key),  # type: ignore[arg-type]
                identity,
                observations,
                now_ms=now,
                has_open_intent=key in open_intent,
            )
            tracks[key] = turn.track
            uncovered[key] += len(observations)
            newest[key] = observations[-1]
            if turn.intent is not None:
                open_intent[key] = (
                    delivery_key(key, turn.intent.trigger_item_id, turn.intent.reason),
                    turn.intent.reason,
                    turn.intent.due_at_ms,
                    observations[0].received_at_ms,
                )
        pending = []
        for key, (_delivery, reason, due_at_ms, arrived_at_ms) in list(open_intent.items()):
            if now < due_at_ms:
                continue
            track = tracks[key]
            kind = track.family  # type: ignore[attr-defined]
            entry = report[kind]
            covered = uncovered.pop(key, 1)
            entry.merged += max(0, covered - 1)
            setattr(entry, reason, getattr(entry, reason) + 1)
            latency = now - arrived_at_ms
            (entry.first_latency_ms if reason in {"first", "raw"} else entry.followup_latency_ms).append(latency)
            # The anchor is the observation the card covered, which is the whole of the follow-up
            # rule: taking the previous anchor here would make every later observation escalate.
            covered_latest = newest[key]
            tracks[key] = replace(
                track,  # type: ignore[arg-type]
                anchor_state="sent",
                anchor_delivery_key=_delivery,
                anchor_attempt_at_ms=now,
                anchor_oi_change_bps=covered_latest.oi_change_bps,
                anchor_direction=covered_latest.direction,
                current_action=covered_latest.action,
                current_position_side=covered_latest.position_side,
                open_delivery_key=None,
            )
            del open_intent[key]
        now += tick_ms
    return dict(report)


@pytest.fixture(scope="module")
def replay() -> dict[str, _Replay]:
    rng = random.Random(553)
    corpus = _oi_corpus(rng) + _liquidation_corpus(rng) + _smart_money_corpus(rng)
    return _replay(corpus)


def test_the_replay_report_is_produced_for_every_kind(replay: dict[str, _Replay], capsys) -> None:
    """The report itself. Printed so a reviewer reads the numbers the PR body quotes."""

    lines = ["kind first followup action_change raw merged cards first_p50_ms followup_p50_ms"]
    for kind in ("oi", "liquidation", "smart_money", "raw"):
        entry = replay.get(kind)
        if entry is None:
            continue
        lines.append(
            f"{kind} {entry.first} {entry.followup} {entry.action_change} {entry.raw} "
            f"{entry.merged} {entry.cards} {_p50(entry.first_latency_ms)} {_p50(entry.followup_latency_ms)}"
        )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert {"oi", "liquidation", "smart_money", "raw"} <= set(replay)


def test_every_kind_reduces_its_records_to_fewer_cards(replay: dict[str, _Replay]) -> None:
    """The point of the rules, stated as a property rather than as one corpus's total.

    A card always speaks for at least one record, and across a corpus with bursts and windows it
    speaks for more than one on average. No absolute count is asserted: this corpus is a shape, not
    a forecast.
    """

    for kind in ("oi", "liquidation", "smart_money"):
        entry = replay[kind]
        records = entry.cards + entry.merged
        assert entry.cards >= 1
        assert entry.cards < records, kind
        assert entry.merged > 0, kind


def test_a_first_card_waits_only_for_the_loop_tick(replay: dict[str, _Replay]) -> None:
    """No window is applied to a new group: the first record of one is prepared on the next tick."""

    for kind in ("oi", "liquidation", "smart_money"):
        assert _p50(replay[kind].first_latency_ms) <= 2_000, kind


def test_a_liquidation_followup_waits_out_its_window_and_no_longer(replay: dict[str, _Replay]) -> None:
    followups = replay["liquidation"].followup_latency_ms
    assert followups, "the corpus must contain at least one follow-up to measure"
    # Anchored at the previous attempt, so a follow-up is at most one window plus one tick after the
    # record that opened it -- never further away for having received more reports.
    assert max(followups) <= 60_000 + 2_000


def test_unparsable_wallet_drift_becomes_its_own_card_rather_than_being_dropped(
    replay: dict[str, _Replay],
) -> None:
    """`Withdraw` has no Open/Close to compare, and is exactly what a reader most needs to see."""

    raw = replay["raw"]
    assert raw.raw >= 1
    assert raw.merged == 0


def _p50(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
