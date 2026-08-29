"""The one business action: `CapitalLane.advance()`, and the answers it may and may not reach.

Every test here is an acceptance clause of #331, and several of them fail against the implementation
this replaces:

* a Hyperliquid frame with complete bars used to reach a frozen Case and be refused four stages later
  as `intent_instrument_not_allowed`; it is now `RESEARCH_ONLY` before a Case exists;
* an absent `trading_runtime_state` row used to default to `{"control": "RUNNING"}`, so a lane with no
  runtime authority scanned, created Cases and spent budget on the strength of a dict literal;
* an unknown repository error used to be caught and written as `BLOCKED / intent_admission_blocked`,
  which consumed the Source forever and hid a PostgreSQL fault inside a business statistic.

The database is a fake here on purpose: what these tests own is the *ordering* and the *vocabulary*.
Atomicity, concurrency and the two commit-time races are proved against real PostgreSQL in
`tests/integration/test_trading_capital_lane.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import pytest

from tracefold.trading.admission import AdmissionConfig
from tracefold.trading.blacklist import Blacklist
from tracefold.trading.capabilities import (
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
)
from tracefold.trading.capital_lane import CapitalLane, CapitalLaneConfig
from tracefold.trading.contracts import Bar, CaseState, OiCandidateRow, TradingCaseManifest
from tracefold.trading.policy import CAPITAL_POLICY
from tracefold.trading.storage.lane import CapitalAuthority, DecisionCommit

NOW = 1_787_000_000_000
DIGEST = "a" * 64
EPOCH = "epoch-1"


def _row(**overrides: Any) -> OiCandidateRow:
    values: dict[str, Any] = {
        "event_id": "evt-1",
        "verdict_created_at_ms": NOW - 30_000,
        "final_decision": "push",
        "source_rule": "oi_whale_ratio",
        "source_strategy_id": "opennews_oi_v1",
        "source_contract_version": "oi_source_contract_v1",
        "measurement_window_ms": 300_000,
        "learning_epoch": EPOCH,
        "program_version": "news_oi_signal_v1",
        "program_sha256": DIGEST,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": DIGEST,
        "scored_judgment_sha256": DIGEST,
        "runtime_manifest_sha": DIGEST,
        "metric_version": "oi_signal_v1",
        "symbol": "TUT",
        "direction": "rise",
        "oi_change_bps": 900,
        "oi_value_usd": 40_000_000,
        "whale_long_profit_bps": 3_000,
        "whale_oi_ratio_bps": 6_000,
        "rank_in_window": 1,
        "observed_at_ms": NOW - 60_000,
        "ingest_mode": "live",
        "venue": "binance",
    }
    values.update(overrides)
    return OiCandidateRow(**values)  # type: ignore[typeddict-item]


def _capability(symbol: str = "TUTUSDT") -> ExecutionInstrumentCapabilityV1:
    return ExecutionInstrumentCapabilityV1(
        instrument_id=f"{symbol}-PERP.BINANCE",
        native_symbol=symbol,
        underlying_key=f"crypto:{symbol.removesuffix('USDT')}",
        quote_currency="USDT",
        price_precision=4,
        size_precision=1,
        price_increment="0.0001",
        size_increment="0.1",
        min_quantity="0.1",
    )


def _snapshot(*capabilities: ExecutionInstrumentCapabilityV1) -> ExecutionCapabilitySnapshotV1:
    rows = capabilities or (_capability(),)
    return ExecutionCapabilitySnapshotV1(
        app_revision="rev",
        app_image_digest="sha256:image",
        nautilus_wheel_identity="wheel",
        news_universe_digest=DIGEST,
        provider_universe_digest=DIGEST,
        included={row.instrument_id: row for row in rows},
        excluded={},
    )


def _bars(anchor_at_ms: int = NOW - 60_000, *, closes: Sequence[str] = ("1.00", "1.02")) -> list[Bar]:
    """A lookback end and a mark at the cutoff, one hour apart, both closed before the trigger."""

    start = anchor_at_ms - 3_600_000
    return [
        Bar(open_at_ms=start - 300_000, close_at_ms=start, close=Decimal(closes[0])),
        Bar(open_at_ms=anchor_at_ms - 300_000, close_at_ms=anchor_at_ms, close=Decimal(closes[1])),
    ]


class FakeTrading:
    """The three lane operations plus the Case lifecycle, with every call recorded."""

    def __init__(
        self,
        *,
        authority: CapitalAuthority | None,
        rows: Sequence[OiCandidateRow] = (),
    ) -> None:
        self._authority = authority
        self.rows = list(rows)
        self.admission: list[dict[str, Any]] = []
        self.cases: dict[str, dict[str, Any]] = {}
        self.settled: list[tuple[str, CaseState, str]] = []
        self.commits: list[str] = []
        self.commit_result: Callable[[str], DecisionCommit] | None = None
        self.claimable: list[str] = []
        self.maintained = 0

    # -- read
    def capital_authority(self, *, since_ms: int, day_start_ms: int, now_ms: int) -> CapitalAuthority | None:
        return self._authority

    # -- freeze
    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: dict[str, Any],
        now_ms: int,
    ) -> bool:
        source_key = manifest.primary_trigger.source_key
        if any(case["manifest"].primary_trigger.source_key == source_key for case in self.cases.values()):
            return False
        self.cases[case_id] = {"manifest": manifest, "created_at_ms": now_ms, "state": CaseState.PENDING}
        self.admission.append({**admission, "case_id": case_id})
        self.claimable.append(case_id)
        return True

    # -- admission ledger
    def record_gate_decision(self, **row: Any) -> None:
        self.admission.append(row)

    def expire_stale_gate_decisions(self, *, stale_before_ms: int, now_ms: int) -> int:
        self.maintained += 1
        return 0

    def purge_gate_decisions(self, *, observed_before_ms: int) -> int:
        return 0

    # -- decide
    def claim_case(self, *, run_id: str, lease_ms: int, now_ms: int) -> dict[str, Any] | None:
        if not self.claimable:
            return None
        case_id = self.claimable.pop(0)
        case = self.cases[case_id]
        return {
            "case_id": case_id,
            "manifest": case["manifest"].model_dump(mode="json"),
            "created_at_ms": case["created_at_ms"],
        }

    def settle_case(
        self,
        *,
        case_id: str,
        run_id: str,
        state: CaseState,
        policy_decision: str | None,
        policy_reason: str,
        policy_checks: Any = None,
        now_ms: int,
    ) -> bool:
        self.settled.append((case_id, state, policy_reason))
        self.cases[case_id]["state"] = state
        return True

    def commit_long_decision(self, *, case_id: str, **_: Any) -> DecisionCommit:
        self.commits.append(case_id)
        if self.commit_result is not None:
            return self.commit_result(case_id)
        return DecisionCommit(state=CaseState.INTENT_EMITTED, reason="smart_money_momentum_long", intent_id="i" * 64)


class FakeRepos:
    def __init__(self, trading: FakeTrading) -> None:
        self.trading = trading


class FakeDb:
    """One bounded read and one bounded transaction, and it records which names were used."""

    def __init__(self, trading: FakeTrading) -> None:
        self._repos = FakeRepos(trading)
        self.names: list[str] = []

    async def read(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        self.names.append(name)
        return fn(self._repos)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        self.names.append(name)
        return fn(self._repos)


def _authority(**overrides: Any) -> CapitalAuthority:
    values: dict[str, Any] = {
        "control": "RUNNING",
        "entries_today": 0,
        "blacklist": Blacklist.from_rows([]),
        "active_underlyings": frozenset(),
        "underlyings_in_flight": frozenset(),
        "cased_source_keys": frozenset(),
        "last_close_at_ms": {},
        "capability": _snapshot(),
    }
    values.update(overrides)
    return CapitalAuthority(**values)


def _lane(
    trading: FakeTrading,
    *,
    bars: Any = None,
    provider_calls: list[tuple[str, int, int]] | None = None,
) -> tuple[CapitalLane, FakeDb]:
    db = FakeDb(trading)

    async def fetch(symbol: str, start_ms: int, end_ms: int) -> Sequence[Bar]:
        if provider_calls is not None:
            provider_calls.append((symbol, start_ms, end_ms))
        return _bars() if bars is None else bars

    lane = CapitalLane(
        db=db,  # type: ignore[arg-type]
        config=CapitalLaneConfig(
            admission=AdmissionConfig(min_oi_value_usd=20_000_000),
            policy=CAPITAL_POLICY,
            target_notional_usd=Decimal("10"),
        ),
        bars=fetch,
        oi_projection=lambda repos, metric, after, until: repos.trading.rows,
        news_generation=EPOCH,
        clock=lambda: NOW,
    )
    return lane, db


def _advance(lane: CapitalLane) -> Any:
    return asyncio.run(lane.advance())


def _reasons(trading: FakeTrading) -> dict[str, str]:
    return {row["source_key"]: f"{row['stage']}:{row['reason']}" for row in trading.admission}


# ---------------------------------------------------------------------------- the happy path
def test_a_binance_frame_becomes_one_case_and_one_intent_in_the_fixed_order() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row()])
    calls: list[tuple[str, int, int]] = []
    lane, db = _lane(trading, provider_calls=calls)

    turn = _advance(lane)

    assert (turn.outcome, turn.cases_created, turn.intents_emitted) == ("ADVANCED", 1, 1)
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "freeze:case_created"}
    # The instrument came from the active capability snapshot, not from a second catalogue, and the
    # window starts at the open of the candle that closed immediately before the lookback target.
    cutoff = NOW - 60_000
    expected_start = ((cutoff - 3_600_000) // 300_000 - 1) * 300_000
    assert calls == [("TUTUSDT", expected_start, cutoff + 300_000)]
    manifest = next(iter(trading.cases.values()))["manifest"]
    assert manifest.instrument.provider_symbol == "TUTUSDT"
    assert manifest.execution_capability_snapshot_sha256 == _snapshot().snapshot_sha256
    assert manifest.policy_id == "binance_oi_smart_money_long_v2"
    # Every provider call happens outside a transaction: the read, the freeze and the commit are the
    # only database names in the turn, and the bar fetch sits between the first and the second.
    assert "trading_case_create" in db.names
    assert "trading_intent_commit" in db.names


def test_a_policy_no_trade_is_a_no_trade_case_with_frozen_checks_and_no_intent() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row(whale_oi_ratio_bps=4_000)])
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert (turn.cases_created, turn.no_trade, turn.intents_emitted) == (1, 1, 0)
    _case_id, state, reason = trading.settled[0]
    assert state is CaseState.NO_TRADE
    assert reason == "smart_money_ratio_below_or_equal_floor"
    assert trading.commits == []


# ---------------------------------------------------------------------------- F2P 1: research only
def test_a_hyperliquid_frame_with_complete_bars_is_research_only_and_creates_no_case() -> None:
    """#331 F2P 1. The old lane froze a Case and failed at `intent_instrument_not_allowed`."""

    trading = FakeTrading(authority=_authority(), rows=[_row(venue="hyperliquid")])
    calls: list[tuple[str, int, int]] = []
    lane, _ = _lane(trading, provider_calls=calls)

    turn = _advance(lane)

    assert (turn.cases_created, turn.intents_emitted, turn.research_only) == (0, 0, 1)
    assert trading.cases == {}
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "venue:research_only_venue"}
    assert trading.admission[0]["status"] == "RESEARCH_ONLY"
    # Nothing was priced for it either: a research venue the live lane can fetch bars for is one
    # refactor away from being traded by it.
    assert calls == []


def test_an_unrecognised_venue_tag_is_rejected_rather_than_routed() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row(venue="okx")])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "venue:venue_unresolved"}
    assert trading.cases == {}


# ---------------------------------------------------------------------------- F2P: infrastructure
def test_a_missing_runtime_authority_row_halts_before_any_scan_case_or_provider_call() -> None:
    """#331 comment F2P 4. The old reader defaulted the absent row to `control = RUNNING`."""

    trading = FakeTrading(authority=None, rows=[_row()])
    calls: list[tuple[str, int, int]] = []
    lane, db = _lane(trading, provider_calls=calls)

    turn = _advance(lane)

    assert (turn.outcome, turn.reason) == ("HALTED", "runtime_state_missing")
    assert (trading.cases, trading.admission, calls) == ({}, [], [])
    assert db.names == ["trading_capital_authority"]


def test_a_paused_lane_scans_nothing_and_creates_nothing() -> None:
    trading = FakeTrading(authority=_authority(control="PAUSED"), rows=[_row()])
    lane, db = _lane(trading)

    turn = _advance(lane)

    assert (turn.outcome, turn.reason) == ("HALTED", "control_paused")
    assert db.names == ["trading_capital_authority"]


def test_an_unknown_repository_error_propagates_and_terminalises_nothing() -> None:
    """#331 comment F2P 10. A PostgreSQL fault is never a `NO_TRADE`, `BLOCKED` or `REJECTED` row."""

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)

    def explode(**_: Any) -> DecisionCommit:
        raise RuntimeError("deadlock detected")

    trading.commit_result = lambda case_id: explode()

    with pytest.raises(RuntimeError, match="deadlock detected"):
        _advance(lane)
    assert trading.settled == []
    assert [row["reason"] for row in trading.admission] == ["case_created"]


# ---------------------------------------------------------------------------- admission vocabulary
def test_every_refusal_is_stage_specific_and_none_of_them_is_a_catch_all() -> None:
    """#331 P2P: capability, price, floor and staleness each get their own durable answer."""

    trading = FakeTrading(
        authority=_authority(),
        rows=[
            _row(event_id="poor", symbol="AAA", oi_value_usd=1_000_000),
            _row(event_id="old", symbol="BBB", observed_at_ms=NOW - 3_600_000),
            _row(event_id="deep", symbol="CCC", rank_in_window=9),
        ],
    )
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {
        "oi:poor:oi_signal_v1": "eligibility:oi_value_below_floor",
        "oi:old:oi_signal_v1": "eligibility:trigger_stale",
        "oi:deep:oi_signal_v1": "eligibility:rank_above_limit",
    }


def test_an_issuer_absent_from_the_active_capability_snapshot_defers_at_the_capability_stage() -> None:
    trading = FakeTrading(authority=_authority(capability=_snapshot(_capability("BTCUSDT"))), rows=[_row()])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "capability:capability_absent"}
    assert trading.cases == {}


def test_no_active_capability_snapshot_defers_every_source_by_name() -> None:
    trading = FakeTrading(authority=_authority(capability=None), rows=[_row()])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "capability:capability_absent"}


def test_missing_bars_defer_and_a_gap_at_the_cutoff_rejects() -> None:
    """Two different facts: the provider had nothing, or this frame's own cutoff has no candle."""

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading, bars=[])
    _advance(lane)
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "market_context:market_data_unavailable"}

    stale = [Bar(open_at_ms=NOW - 7_200_000, close_at_ms=NOW - 6_900_000, close=Decimal("1"))]
    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading, bars=stale)
    _advance(lane)
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "market_context:market_data_invalid"}


def test_a_full_lane_answers_every_admitted_frame_rather_than_leaving_a_hole() -> None:
    trading = FakeTrading(authority=_authority(entries_today=1), rows=[_row()])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "eligibility:lane_capacity_exhausted"}
    assert trading.admission[0]["evidence"]["lane_full"] == "daily_entry_fence"


def test_one_issuer_produces_one_thesis_and_the_loser_is_deferred_not_retired() -> None:
    trading = FakeTrading(
        authority=_authority(),
        rows=[_row(event_id="older", observed_at_ms=NOW - 120_000), _row(event_id="newer")],
    )
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert turn.cases_created == 1
    assert _reasons(trading) == {
        "oi:older:oi_signal_v1": "eligibility:superseded_by_newer_trigger",
        "oi:newer:oi_signal_v1": "freeze:case_created",
    }


def test_a_source_that_already_authored_a_case_is_terminally_consumed() -> None:
    trading = FakeTrading(
        authority=_authority(cased_source_keys=frozenset({"oi:evt-1:oi_signal_v1"})),
        rows=[_row()],
    )
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "eligibility:already_consumed"}


def test_a_symbol_inside_its_cooldown_defers_with_the_measured_gap() -> None:
    trading = FakeTrading(
        authority=_authority(last_close_at_ms={"crypto:TUT": NOW - 60_000}),
        rows=[_row()],
    )
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "eligibility:cooldown"}
    assert trading.admission[0]["evidence"]["since_close_ms"] == 60_000


# ---------------------------------------------------------------------------- decision-time guards
def test_a_case_frozen_under_another_news_generation_is_blocked_not_traded() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)
    _advance(lane)
    assert trading.commits == list(trading.cases)

    # A deployment later, the process runs a different News generation than this Case was frozen
    # under. `program_version` and `policy_version` do not move when a prompt or a model slot does,
    # so without this the Case would advance to an Intent under rules it was never reasoned under.
    trading.claimable = list(trading.cases)
    trading.commits.clear()
    lane._news_generation = "epoch-3"
    asyncio.run(lane._decide_one())
    assert trading.settled[-1][1:] == (CaseState.BLOCKED, "source_generation_retired")
    assert trading.commits == []


def test_a_case_older_than_the_decision_budget_is_blocked_rather_than_sized_off_a_stale_mark() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)
    _advance(lane)

    case_id = next(iter(trading.cases))
    trading.cases[case_id]["created_at_ms"] = NOW - 600_000
    trading.claimable = [case_id]
    trading.settled.clear()

    asyncio.run(lane._decide_one())
    assert trading.settled[-1][1:] == (CaseState.BLOCKED, "case_stale")


def test_a_commit_time_denial_is_a_typed_blocked_reason_and_emits_no_intent() -> None:
    """#331 F2P 4/5: blacklist and capability races are named, not `intent_admission_blocked`."""

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    trading.commit_result = lambda case_id: DecisionCommit(state=CaseState.BLOCKED, reason="capability_mismatch")
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert (turn.blocked, turn.intents_emitted) == (1, 0)
    assert trading.commits == list(trading.cases)


# ---------------------------------------------------------------------------- what cannot be reached
def test_the_lane_has_no_news_or_liquidation_input_at_all() -> None:
    """#331 F2P 2. There is no editorial trigger to disable: the lane takes one projection."""

    import inspect

    signature = inspect.signature(CapitalLane.__init__)
    assert "oi_projection" in signature.parameters
    inputs = [name for name in signature.parameters if name != "news_generation"]
    assert not any("news" in name or "liquidation" in name for name in inputs)
    assert not hasattr(CapitalLane, "_liquidation_shadow")
    source = inspect.getsource(CapitalLane)
    assert "program" not in source and "dspy" not in source.lower()
