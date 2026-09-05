"""Offline replay of the v1 rules at raw-record granularity, through the real loop (#553 PR-2).

Every record here goes through the real wire template, the real parser and the real repository, and
every card is decided by `MarketNotificationLoop.advance()` against real PostgreSQL. Nothing about
the lifecycle is re-implemented: the counts below are read back out of `news_market_deliveries` and
`news_items` afterwards, so a rule change that stopped merging would move them.

The corpus is shaped from the sample the Issue measured (§9: 405 OI, 111 liquidation and 108
smart-money live records in one 72 h window, plus the unparsable drift the wallet Strategy really
produces), generated from a seeded arrival pattern so the same replay produces the same report on
every run. Arrival times are drawn across the whole window by construction, so the corpus really does
span 72 h.

**The totals are not a product target.** They describe one shaped corpus. What is asserted is the
property the rules are for -- a card speaks for more than one record, and it speaks for it soon -- with
bounds rather than with this run's exact numbers.

The sender's unreliability is *injected*, not measured: one send in twenty-five is refused with an
explicit rate limit and one in fifty times out. Both rates are chosen so the `failed` and `unknown`
columns exercise something rather than being structurally zero, and neither is a claim about Feishu.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_notifications import (
    SEND_ATTEMPTS_MAX,
    TICK_SECONDS,
    MarketNotificationLoop,
)
from tracefold.news.oi_signals import measurement_definition, oi_source_contract, parse_oi_signal
from tracefold.news.smart_money import parse_smart_money
from tracefold.news.source_contracts import MARKET_PROVIDER

pytestmark = pytest.mark.integration

T0 = 1_787_000_000_000
WINDOW_MS = 72 * 3_600_000
TICK_MS = int(TICK_SECONDS * 1000)

OI_SYMBOLS = ("TAC", "WIF", "PENGU", "ENA", "SUI", "AVAX", "OP", "TIA")
OI_RECORDS = 405
LIQUIDATION_SYMBOLS = ("BTC", "ETH", "DOGE", "SOL")
LIQUIDATION_VENUES = ("binance", "hyperliquid", "okx", "bybit")
LIQUIDATION_RECORDS = 111
WALLETS = (("Machi Big Brother", "0x4d3a"), ("James Wynn", "0x8bd1"), ("qwatio", None))
WALLET_SYMBOLS = ("ETH", "BTC", "HYPE")
WALLET_RECORDS = 108


@dataclass(frozen=True, slots=True)
class _Record:
    """One provider frame as it arrived: the wire line, the Strategy, and the host receive stamp."""

    item_id: str
    strategy_id: str
    kind: str
    title: str
    venue: str | None
    address: str | None
    at_ms: int


def _arrivals(rng: random.Random, count: int) -> list[int]:
    """`count` arrival stamps drawn across the whole window, oldest first.

    Uniform rather than a fixed gap: a fixed gap would put the corpus's span at the mercy of the gap
    size, which is how a "72 h" replay quietly becomes a 42 h one.
    """

    return sorted(T0 + rng.randrange(WINDOW_MS) for _ in range(count))


def _oi_corpus(rng: random.Random) -> list[_Record]:
    """OI telemetry as it actually arrives: per symbol, a drifting magnitude in a stable direction.

    The correlation is the whole reason the 2x follow-up rule suppresses anything; a corpus of
    independent draws would replay a rule this one does not have.
    """

    records: list[_Record] = []
    per_symbol = [OI_RECORDS // len(OI_SYMBOLS)] * len(OI_SYMBOLS)
    for position in range(OI_RECORDS % len(OI_SYMBOLS)):
        per_symbol[position] += 1
    for symbol, count in zip(OI_SYMBOLS, per_symbol, strict=True):
        direction = "Rise" if rng.random() < 0.6 else "Fall"
        magnitude = rng.uniform(1.5, 4.0)
        for at_ms in _arrivals(rng, count):
            if rng.random() < 0.12:
                direction = "Fall" if direction == "Rise" else "Rise"
                magnitude = rng.uniform(1.2, 3.5)
            else:
                magnitude = max(0.4, magnitude * rng.uniform(0.72, 1.45))
            records.append(
                _Record(
                    item_id=f"oi-{len(records)}",
                    strategy_id="1019",
                    kind="oi",
                    title=(
                        f"{symbol} OI {direction} {magnitude:.2f}%, "
                        f"OI Value {rng.choice([3.2, 8.83, 11.03, 42.6])}M, "
                        f"Whale Long Profit {rng.choice([55.1, 84.91, 88.4])}%, "
                        f"Whale/OI Ratio {rng.choice([32.46, 143.9])}%"
                    ),
                    venue="binance",
                    address=None,
                    at_ms=at_ms,
                )
            )
    return records


def _liquidation_corpus(rng: random.Random) -> list[_Record]:
    """Clustered: a liquidation cascade is many reports inside one minute, then nothing for hours."""

    records: list[_Record] = []
    while len(records) < LIQUIDATION_RECORDS:
        symbol = rng.choice(LIQUIDATION_SYMBOLS)
        venue = rng.choice(LIQUIDATION_VENUES)
        side = rng.choice(["Long", "Short"])
        at_ms = T0 + rng.randrange(WINDOW_MS)
        for _ in range(min(rng.choice([1, 1, 2, 4, 7]), LIQUIDATION_RECORDS - len(records))):
            records.append(
                _Record(
                    item_id=f"liq-{len(records)}",
                    strategy_id="2083",
                    kind="liquidation",
                    title=(
                        f"{symbol} Large {side} Liquidation {round(rng.uniform(120, 980), 2)}K "
                        f"at ${round(rng.uniform(0.2, 118_000), 4)}"
                    ),
                    venue=venue,
                    address=None,
                    at_ms=at_ms,
                )
            )
            at_ms += rng.randint(1_000, 14_000)
    return sorted(records, key=lambda record: record.at_ms)


def _smart_money_corpus(rng: random.Random) -> list[_Record]:
    """Bursty per account, and with the drift this Strategy really produces.

    Bursty because the Issue's own counterexample is: one account reported closing a short and opening
    one 49.241 s later. An account filling a position in three prints inside a minute is one subject
    doing one thing, which is the case the 60 s window exists for. `Withdraw` has no Open/Close to
    compare and stays its own raw card.
    """

    records: list[_Record] = []
    per_wallet = WALLET_RECORDS // len(WALLETS)
    for position, (label, address) in enumerate(WALLETS):
        remaining = per_wallet + (1 if position < WALLET_RECORDS % len(WALLETS) else 0)
        symbol = rng.choice(WALLET_SYMBOLS)
        action = rng.choice(["Open", "Close"])
        side = rng.choice(["Long", "Short"])
        while remaining > 0:
            if rng.random() < 0.3:
                action = "Close" if action == "Open" else "Open"
            if rng.random() < 0.2:
                side = "Short" if side == "Long" else "Long"
            at_ms = T0 + rng.randrange(WINDOW_MS)
            for _ in range(min(rng.choice([1, 2, 2, 3, 5]), remaining)):
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
                        item_id=f"sm-{len(records)}",
                        strategy_id="2026",
                        kind="smart_money",
                        title=title,
                        venue="hyperliquid",
                        address=address,
                        at_ms=at_ms,
                    )
                )
                remaining -= 1
                at_ms += rng.randint(4_000, 49_000)
            symbol = rng.choice(WALLET_SYMBOLS) if rng.random() < 0.35 else symbol
    return sorted(records, key=lambda record: record.at_ms)


def _admit(news: Any, record: _Record) -> None:
    """One record admitted exactly as the entry admits it: Item, then its typed fact if it has one."""

    parse_status, parse_error = "raw", "market_template_unmatched"
    oi = liquidation = wallet = None
    if record.kind == "oi":
        oi = parse_oi_signal(record.title)
    elif record.kind == "liquidation":
        liquidation = parse_liquidation(
            record.title,
            item_id=record.item_id,
            fact_id=record.item_id,
            source_strategy_id=record.strategy_id,
            provider_source=record.venue or "",
            event_at_ms=record.at_ms,
            received_at_ms=record.at_ms,
        )
    else:
        wallet = parse_smart_money(
            record.title,
            item_id=record.item_id,
            fact_id=record.item_id,
            source_strategy_id=record.strategy_id,
            provider_source=record.venue or "",
            related_address=record.address,
            event_at_ms=record.at_ms,
            received_at_ms=record.at_ms,
        )
    if oi is not None or liquidation is not None or wallet is not None:
        parse_status, parse_error = "parsed", None
    news.upsert_item(
        item_id=record.item_id,
        source_id="opennews",
        source_item_key=record.item_id,
        title=record.title,
        raw_first_line=record.title,
        description="",
        canonical_url=None,
        reporting_origin="opennews",
        published_at_ms=record.at_ms,
        observed_at_ms=record.at_ms,
        provider_metadata_json="{}",
        strategy_ids_json="[]",
        ingest_mode="live",
        trace_id="replay",
        now_ms=record.at_ms,
        market_kind=record.kind,
        market_source_strategy_id=record.strategy_id,
        market_parse_status=parse_status,
        market_parse_error=parse_error,
        provider_params_json=json.dumps({"relatedAddress": record.address} if record.address else {}),
    )
    if oi is not None:
        source = oi_source_contract({"strategies": [{"id": record.strategy_id}]})
        news.insert_oi_signal(
            event_id=f"replay-{record.item_id}",
            metric_version="oi_signal_v1",
            symbol=oi.symbol,
            raw_instrument=oi.raw_instrument,
            direction=oi.direction,
            oi_change_bps=oi.oi_change_bps,
            oi_value_usd=oi.oi_value_usd,
            whale_long_profit_bps=oi.whale_long_profit_bps,
            whale_oi_ratio_bps=oi.whale_oi_ratio_bps,
            observed_at_ms=record.at_ms,
            received_at_ms=record.at_ms,
            now_ms=record.at_ms,
            provider=MARKET_PROVIDER,
            source_strategy_id=None if source is None else source.strategy_id,
            source_contract_version=None if source is None else source.contract_version,
            measurement_window_ms=None if source is None else source.measurement_window_ms,
            measurement_definition=measurement_definition(source),
            source_item_id=record.item_id,
            source_venue=record.venue,
        )
    elif liquidation is not None:
        news.insert_market_liquidation(fact=liquidation, ingest_mode="live", now_ms=record.at_ms)
    elif wallet is not None:
        news.insert_market_smart_money(fact=wallet, ingest_mode="live", now_ms=record.at_ms)


class _Clock:
    def __init__(self, at_ms: int) -> None:
        self.at_ms = at_ms

    def __call__(self) -> int:
        return self.at_ms


class _ReplaySender:
    """A provider that is mostly fine. Its unreliability is injected and is declared in the module docstring."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.sent = 0

    @property
    def available(self) -> bool:
        return True

    async def send_prepared_card(self, card: Any, *, operation: str = "") -> dict[str, Any]:
        del card, operation
        roll = self.rng.random()
        if roll < 0.04:
            raise _Refused("news_delivery_feishu_business_rate_limited", commit_phase="not_sent", retryable=True)
        if roll < 0.06:
            raise _Refused("news_delivery_feishu_transport_failed", commit_phase="unknown", retryable=False)
        self.sent += 1
        return {"provider": "replay", "message_id": self.sent}


class _Refused(RuntimeError):
    def __init__(self, code: str, *, commit_phase: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.commit_phase = commit_phase
        self.retryable = retryable


class _Db:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        del name, timeout_seconds
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        del name, timeout_seconds
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            return fn(repos)


@dataclass
class _Kind:
    """One family's line of the report, every number read back out of the ledger."""

    records: int = 0
    first: int = 0
    followup: int = 0
    action_change: int = 0
    raw: int = 0
    merged: int = 0
    still_merging: int = 0
    sent: int = 0
    failed: int = 0
    unknown: int = 0
    first_latency_ms: list[int] = field(default_factory=list)
    followup_latency_ms: list[int] = field(default_factory=list)

    @property
    def cards(self) -> int:
        return self.first + self.followup + self.action_change + self.raw


def _tick_at_or_after(now_ms: int, event_ms: int) -> int:
    """The first tick boundary at or after `event_ms`.

    Idle stretches are jumped rather than ticked through -- 72 h at two seconds is 129,600 turns and
    129,598 of them have nothing to do -- but the quantum is kept, so a record still waits for the
    next boundary exactly as it would in production.
    """

    if event_ms <= now_ms:
        return now_ms
    return now_ms + -(-(event_ms - now_ms) // TICK_MS) * TICK_MS


@pytest.fixture(scope="module")
def corpus() -> list[_Record]:
    rng = random.Random(553)
    return sorted(
        _oi_corpus(rng) + _liquidation_corpus(rng) + _smart_money_corpus(rng),
        key=lambda record: record.at_ms,
    )


def test_the_corpus_is_the_shape_the_report_claims(corpus: list[_Record]) -> None:
    """Guard the measurement itself: a corpus that quietly shrank would report a quieter market."""

    assert len(corpus) == OI_RECORDS + LIQUIDATION_RECORDS + WALLET_RECORDS == 624
    kinds = Counter(record.kind for record in corpus)
    assert kinds == {"oi": OI_RECORDS, "liquidation": LIQUIDATION_RECORDS, "smart_money": WALLET_RECORDS}
    # And it really spans the window the report is about, rather than a fraction of it.
    assert corpus[-1].at_ms - corpus[0].at_ms >= 71 * 3_600_000


@pytest.fixture(scope="module")
def replay(postgres_module_clone_dsn: str) -> Iterator[dict[str, _Kind]]:
    rng = random.Random(553)
    corpus = sorted(
        _oi_corpus(rng) + _liquidation_corpus(rng) + _smart_money_corpus(rng),
        key=lambda record: record.at_ms,
    )
    # The sender's rolls must not depend on how the corpus consumed the generator above.
    rng = random.Random(5530)
    connection = connect_postgres_test(read_only=False)
    try:
        clock = _Clock(corpus[0].at_ms)
        loop = MarketNotificationLoop(db=_Db(connection), sender=_ReplaySender(rng), clock=clock)
        asyncio.run(loop.start())
        cursor = 0
        while True:
            repos = repositories_for_connection(connection)
            with repos.transaction():
                while cursor < len(corpus) and corpus[cursor].at_ms <= clock.at_ms:
                    _admit(repos.news, corpus[cursor])
                    cursor += 1
            asyncio.run(loop.advance())
            next_record = corpus[cursor].at_ms if cursor < len(corpus) else None
            next_due = connection.execute(
                "SELECT min(next_attempt_at_ms) AS due FROM news_market_deliveries"
                " WHERE state = ANY (ARRAY['pending', 'unavailable'])"
            ).fetchone()["due"]
            upcoming = [value for value in (next_record, next_due) if value is not None]
            if not upcoming:
                break
            clock.at_ms = _tick_at_or_after(clock.at_ms + TICK_MS, min(upcoming))
        yield _report(connection, corpus)
    finally:
        connection.close()


def _report(connection: Any, corpus: list[_Record]) -> dict[str, _Kind]:
    arrivals = {record.item_id: record.at_ms for record in corpus}
    families = {
        str(row["group_key"]): str(row["family"])
        for row in connection.execute("SELECT group_key, family FROM news_market_tracks").fetchall()
    }
    report: dict[str, _Kind] = {name: _Kind() for name in ("oi", "liquidation", "smart_money", "raw")}
    for row in connection.execute(
        "SELECT market_notify_group_key AS group_key, market_notify_delivery_key AS delivery_key"
        "  FROM news_items WHERE market_notify_state = 'processed'"
    ).fetchall():
        entry = report[families[str(row["group_key"])]]
        entry.records += 1
        if row["delivery_key"] is None:
            entry.still_merging += 1
    for row in connection.execute(
        "SELECT group_key, trigger_reason, trigger_item_id, state, covered_count, first_attempt_at_ms"
        "  FROM news_market_deliveries"
    ).fetchall():
        entry = report[families[str(row["group_key"])]]
        setattr(entry, str(row["trigger_reason"]), getattr(entry, str(row["trigger_reason"])) + 1)
        entry.merged += max(0, int(row["covered_count"]) - 1)
        if str(row["state"]) in {"sent", "failed", "unknown"}:
            setattr(entry, str(row["state"]), getattr(entry, str(row["state"])) + 1)
        latency = int(row["first_attempt_at_ms"]) - arrivals[str(row["trigger_item_id"])]
        bucket = entry.first_latency_ms if str(row["trigger_reason"]) in {"first", "raw"} else entry.followup_latency_ms
        bucket.append(latency)
    return report


def _p50(values: list[int]) -> int:
    return 0 if not values else sorted(values)[len(values) // 2]


def test_the_replay_reports_every_kind_at_raw_record_granularity(replay: dict[str, _Kind], capsys) -> None:
    """The report itself, printed so a reviewer reads the numbers the PR body quotes."""

    lines = [
        "kind records first followup action_change raw merged still_merging "
        "sent failed unknown cards first_p50_ms followup_p50_ms"
    ]
    for kind in ("oi", "liquidation", "smart_money", "raw"):
        entry = replay[kind]
        lines.append(
            f"{kind} {entry.records} {entry.first} {entry.followup} {entry.action_change} {entry.raw} "
            f"{entry.merged} {entry.still_merging} {entry.sent} {entry.failed} {entry.unknown} "
            f"{entry.cards} {_p50(entry.first_latency_ms)} {_p50(entry.followup_latency_ms)}"
        )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert all(replay[kind].records > 0 for kind in ("oi", "liquidation", "smart_money", "raw"))


def test_every_record_is_accounted_for_exactly_once(replay: dict[str, _Kind]) -> None:
    """No record is silently uncounted: it is on a card, or it is still merging into the next one.

    The per-kind totals are counted off *processed* Items, so an ungrouped backlog would be invisible
    to them. The corpus total below is what makes that impossible: every record the replay admitted
    is in exactly one kind's line.
    """

    assert sum(entry.records for entry in replay.values()) == 624
    for kind, entry in replay.items():
        covered = entry.cards + entry.merged
        assert covered + entry.still_merging == entry.records, kind
    # The number the PR body quotes, pinned so the prose cannot drift from the run.
    assert sum(entry.cards for entry in replay.values()) == 246


def test_the_rules_reduce_records_to_fewer_cards(replay: dict[str, _Kind]) -> None:
    """The property the rules exist for, with bounds rather than this run's exact numbers."""

    for kind in ("oi", "liquidation", "smart_money"):
        entry = replay[kind]
        assert entry.merged > 0, kind
        assert entry.cards < entry.records, kind
        # A card speaks for more than one record on average, and never for an implausible crowd.
        assert 1.0 < entry.records / entry.cards < 25.0, kind
    # Unparsable drift is one card per record by construction: it has no group to merge into.
    assert replay["raw"].cards == replay["raw"].records
    assert replay["raw"].merged == 0


def test_a_first_card_waits_only_for_the_loop_tick(replay: dict[str, _Kind]) -> None:
    for kind in ("oi", "liquidation", "smart_money", "raw"):
        latencies = replay[kind].first_latency_ms
        assert latencies, kind
        assert _p50(latencies) <= TICK_MS, kind


def test_a_liquidation_followup_waits_out_its_window_and_no_longer(replay: dict[str, _Kind]) -> None:
    followups = replay["liquidation"].followup_latency_ms
    assert followups, "the corpus must contain at least one follow-up to measure"
    # Anchored on the previous attempt, so a follow-up is at most one window plus one tick after the
    # record that opened it -- never further away for having received more reports.
    assert max(followups) <= 60_000 + TICK_MS


def test_the_injected_provider_failures_reach_the_ledger_as_two_different_states(
    replay: dict[str, _Kind],
) -> None:
    """A refusal is retried and mostly recovers; an unreadable answer is `unknown` and stays there."""

    unknown = sum(entry.unknown for entry in replay.values())
    sent = sum(entry.sent for entry in replay.values())
    failed = sum(entry.failed for entry in replay.values())
    assert unknown > 0
    assert sent > unknown
    # Three real attempts against a 4 % refusal rate makes an outright failure rare but possible.
    assert failed <= sent // SEND_ATTEMPTS_MAX
