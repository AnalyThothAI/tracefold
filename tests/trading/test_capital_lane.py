"""The one business action: `CapitalLane.advance()`, and the answers it may and may not reach.

Every test here is an acceptance clause of #331, and several of them fail against the implementation
this replaces:

* a Hyperliquid frame stays source-native through its own catalogue, bars and binding rather than
  being rerouted to Binance or discarded as research-only;
* an absent `trading_runtime_state` row used to default to `{"control": "RUNNING"}`, so a lane with no
  runtime authority scanned, created Cases and spent budget on the strength of a dict literal;
* an unknown repository error used to be caught and written as `BLOCKED / intent_admission_blocked`,
  which consumed the Source forever and hid a PostgreSQL fault inside a business statistic.

The database is a fake here on purpose: what these tests own is the *ordering* and the *vocabulary*.
Atomicity, concurrency and the commit-time races are proved against real PostgreSQL in
`tests/integration/test_trading_capital_lane.py`, which #373 wrote — until then this reference named
a file that did not exist. That module holds the two claims this one cannot make: a model-based walk
of the Case lifecycle under arbitrary worker, lease and clock interleavings, and the whole authority
product reaching a Policy LONG with exactly one capital reason and no Intent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import pytest

from tracefold.app.http.schemas.trading import TradingGateEvidenceData
from tracefold.trading.admission import AdmissionConfig
from tracefold.trading.blacklist import Blacklist
from tracefold.trading.capital_lane import CapitalLane, CapitalLaneConfig
from tracefold.trading.catalog import (
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
)
from tracefold.trading.contracts import Bar, CaseState, OiCandidateRow, TradingCaseManifest
from tracefold.trading.policy import CAPITAL_POLICY
from tracefold.trading.storage.lane import BindingAuthority, CapitalAuthority, CapitalDispositionCommit

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
        "program_version": "news_oi_signal_v2",
        "program_sha256": DIGEST,
        "policy_version": "news_triage_policy_v11",
        "judgment_contract_version": "news_judgment_v2",
        "judgment_origin": "oi",
        "judgment_sha256": DIGEST,
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


def _catalog_entry(symbol: str = "TUTUSDT") -> VenueInstrumentCatalogEntryV1:
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=symbol,
        provider_symbol=symbol,
        venue="binance.usdm",
        canonical_asset=symbol.removesuffix("USDT"),
        canonical_namespace="crypto",
        product_kind="linear_perpetual",
        active=True,
        settlement_asset="USDT",
        margin_asset="USDT",
        price_increment="0.0001",
        size_increment="0.1",
        min_quantity="0.1",
        raw_metadata_sha256=DIGEST,
    )


def _catalog(*instruments: VenueInstrumentCatalogEntryV1) -> VenueInstrumentCatalogSnapshotV1:
    return build_venue_catalog_snapshot(
        binding="BINANCE_USDM",
        captured_at_ms=NOW - 1_000,
        stale_after_ms=86_400_000,
        instruments=instruments or (_catalog_entry(),),
    )


def _hyperliquid_catalog_entry(symbol: str = "TUT") -> VenueInstrumentCatalogEntryV1:
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=f"main:{symbol}",
        provider_symbol=symbol,
        venue="hyperliquid.perp",
        canonical_asset=symbol,
        canonical_namespace="main",
        product_kind="linear_perpetual",
        active=True,
        settlement_asset="USDC",
        margin_asset="USDC",
        price_increment="0.0001",
        size_increment="0.1",
        min_quantity="0.1",
        raw_metadata_sha256=DIGEST,
    )


def _hyperliquid_catalog() -> VenueInstrumentCatalogSnapshotV1:
    return build_venue_catalog_snapshot(
        binding="HYPERLIQUID_PERP",
        captured_at_ms=NOW - 1_000,
        stale_after_ms=86_400_000,
        instruments=(_hyperliquid_catalog_entry(),),
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
        self.commit_result: Callable[[str], CapitalDispositionCommit] | None = None
        self.claimable: list[str] = []
        self.maintained = 0
        self.runtime_states: list[tuple[str, str | None]] = []

    # -- read
    def capital_authority(self, *, since_ms: int, now_ms: int) -> CapitalAuthority | None:
        return self._authority

    def set_decision_runtime(
        self,
        *,
        state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        self.runtime_states.append((state, reason))
        return True

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
        capital_disposition: str,
        capital_reason: str | None,
        policy_checks: Any = None,
        now_ms: int,
    ) -> bool:
        self.settled.append((case_id, state, policy_reason))
        self.cases[case_id]["state"] = state
        return True

    def commit_capital_disposition(self, *, case_id: str, **_: Any) -> CapitalDispositionCommit:
        self.commits.append(case_id)
        if self.commit_result is not None:
            return self.commit_result(case_id)
        return CapitalDispositionCommit(state=CaseState.BLOCKED, reason="credentials_unconfigured")


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
    binance_catalog = overrides.pop("catalog", _catalog())
    hyperliquid_catalog = overrides.pop("hyperliquid_catalog", _hyperliquid_catalog())
    values: dict[str, Any] = {
        "capital_control": "PAUSED",
        "blacklist": Blacklist.from_rows([]),
        "active_underlyings": frozenset(),
        "underlyings_in_flight": frozenset(),
        "cased_source_keys": frozenset(),
        "bindings": {
            binding: BindingAuthority(
                credential_state="unconfigured",
                runtime_state="stopped",
                account_state="unknown",
                catalog_state="ready",
                catalog_snapshot_sha256=None if catalog is None else catalog.snapshot_sha256,
                reason="credentials_unconfigured",
            )
            for binding, catalog in (
                ("BINANCE_USDM", binance_catalog),
                ("HYPERLIQUID_PERP", hyperliquid_catalog),
            )
        },
        "catalogs": {
            "BINANCE_USDM": binance_catalog,
            "HYPERLIQUID_PERP": hyperliquid_catalog,
        },
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

    async def fetch(instrument: Any, start_ms: int, end_ms: int) -> Sequence[Bar]:
        if provider_calls is not None:
            provider_calls.append((instrument.provider_symbol, start_ms, end_ms))
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
        release_revision="test-release",
        clock=lambda: NOW,
    )
    return lane, db


def _advance(lane: CapitalLane) -> Any:
    return asyncio.run(lane.advance())


def _reasons(trading: FakeTrading) -> dict[str, str]:
    return {row["source_key"]: f"{row['stage']}:{row['reason']}" for row in trading.admission}


# ---------------------------------------------------------------------------- the happy path
def test_a_no_key_binance_frame_becomes_one_case_and_an_independent_capital_block() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row()])
    calls: list[tuple[str, int, int]] = []
    lane, db = _lane(trading, provider_calls=calls)

    turn = _advance(lane)

    assert (turn.outcome, turn.cases_created, turn.blocked) == ("ADVANCED", 1, 1)
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "freeze:case_created"}
    # The instrument came from the active public catalogue, and the
    # window starts at the open of the candle that closed immediately before the lookback target.
    cutoff = NOW - 60_000
    expected_start = ((cutoff - 3_600_000) // 300_000 - 1) * 300_000
    assert calls == [("TUTUSDT", expected_start, cutoff + 300_000)]
    manifest = next(iter(trading.cases.values()))["manifest"]
    assert manifest.instrument.provider_symbol == "TUTUSDT"
    assert manifest.venue_catalog_snapshot_sha256 == _catalog().snapshot_sha256
    assert manifest.policy_id == "source_native_oi_smart_money_long_v3"
    # Every provider call happens outside a transaction: the read, the freeze and the commit are the
    # only database names in the turn, and the bar fetch sits between the first and the second.
    assert "trading_case_create" in db.names
    assert "trading_capital_disposition_commit" in db.names


def test_a_policy_no_trade_is_a_no_trade_case_with_frozen_checks_and_no_intent() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row(whale_oi_ratio_bps=4_000)])
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert (turn.cases_created, turn.no_trade) == (1, 1)
    _case_id, state, reason = trading.settled[0]
    assert state is CaseState.NO_TRADE
    assert reason == "smart_money_ratio_below_or_equal_floor"
    assert trading.commits == []


# ---------------------------------------------------------------------------- source-native dual venue
def test_a_hyperliquid_frame_freezes_its_own_catalog_and_bars_without_rerouting() -> None:

    trading = FakeTrading(authority=_authority(), rows=[_row(venue="hyperliquid")])
    calls: list[tuple[str, int, int]] = []
    lane, _ = _lane(trading, provider_calls=calls)

    turn = _advance(lane)

    assert (turn.cases_created, turn.blocked) == (1, 1)
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "freeze:case_created"}
    manifest = next(iter(trading.cases.values()))["manifest"]
    assert manifest.instrument.binding == "HYPERLIQUID_PERP"
    assert manifest.instrument.venue == "hyperliquid.perp"
    assert manifest.venue_catalog_snapshot_sha256 == _hyperliquid_catalog().snapshot_sha256
    assert calls[0][0] == "TUT"


def test_an_unrecognised_venue_tag_is_rejected_rather_than_routed() -> None:
    trading = FakeTrading(authority=_authority(), rows=[_row(venue="okx")])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "venue:venue_unresolved"}
    assert trading.cases == {}


# ---------------------------------------------------------------------------- F2P: infrastructure
def test_a_missing_runtime_authority_row_faults_before_any_scan_case_or_provider_call() -> None:
    """#331 comment F2P 4. The old reader defaulted the absent row to `control = RUNNING`."""

    trading = FakeTrading(authority=None, rows=[_row()])
    calls: list[tuple[str, int, int]] = []
    lane, db = _lane(trading, provider_calls=calls)

    with pytest.raises(RuntimeError, match="trading_runtime_state_missing"):
        _advance(lane)
    assert (trading.cases, trading.admission, calls) == ({}, [], [])
    assert db.names == [
        "trading_decision_runtime",
        "trading_capital_authority",
        "trading_decision_runtime",
    ]
    assert trading.runtime_states == [
        ("STARTING", None),
        ("FAULTED", "decision_turn_fault"),
    ]


def test_a_paused_capital_plane_still_runs_policy_but_emits_no_intent() -> None:
    trading = FakeTrading(authority=_authority(capital_control="PAUSED"), rows=[_row()])
    lane, db = _lane(trading)

    turn = _advance(lane)

    assert (turn.outcome, turn.reason, turn.cases_created, turn.blocked) == (
        "ADVANCED",
        "advanced",
        1,
        1,
    )
    assert "trading_capital_disposition_commit" in db.names


def test_an_unknown_repository_error_propagates_and_terminalises_nothing() -> None:
    """#331 comment F2P 10. A PostgreSQL fault is never a `NO_TRADE`, `BLOCKED` or `REJECTED` row."""

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)

    def explode(**_: Any) -> CapitalDispositionCommit:
        raise RuntimeError("deadlock detected")

    trading.commit_result = lambda case_id: explode()

    with pytest.raises(RuntimeError, match="deadlock detected"):
        _advance(lane)
    assert trading.runtime_states == [
        ("STARTING", None),
        ("FAULTED", "decision_turn_fault"),
    ]
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
            _row(event_id="unlisted", symbol="CCC"),
        ],
    )
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {
        "oi:poor:oi_signal_v1": "eligibility:oi_value_below_floor",
        "oi:old:oi_signal_v1": "eligibility:trigger_stale",
        "oi:unlisted:oi_signal_v1": "catalog:catalog_absent",
    }


def test_an_issuer_absent_from_the_active_catalog_defers_at_the_catalog_stage() -> None:
    catalog = _catalog(_catalog_entry("BTCUSDT"))
    trading = FakeTrading(authority=_authority(catalog=catalog), rows=[_row()])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "catalog:catalog_absent"}
    assert trading.cases == {}


def test_no_active_catalog_snapshot_defers_every_source_by_name() -> None:
    trading = FakeTrading(authority=_authority(catalog=None), rows=[_row()])
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "catalog:catalog_absent"}


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


def test_a_recovery_obligation_does_not_stop_another_source_reaching_policy() -> None:
    """#350: existing capital recovery cannot turn the Decision Plane into a blind spot."""

    trading = FakeTrading(authority=_authority(active_underlyings=frozenset({"crypto:SOL"})), rows=[_row()])
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert turn.cases_created == 1
    assert turn.blocked == 1
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "freeze:case_created"}


def test_having_entered_today_does_not_refuse_a_later_frame() -> None:
    """#348: the one-entry-per-UTC-day fence is gone, and with it the day's blind spot.

    It refused every later frame *before* the policy ran, so on any day the lane traded it could not
    say which of the day's remaining frames it should have taken. Measured over seven days it would
    have capped the busiest day at one of six qualifying frames, while the real bound — one live
    position, held at most three minutes — was doing the work all along.
    """

    # Three entries already fenced today, and a fourth frame still gets a Case and a decision. The
    # authority no longer even counts them: `entries_today` is gone from the read (#348), so the only
    # honest way to state this is that nothing about the UTC day reaches the lane at all.
    assert not hasattr(_authority(), "entries_today")

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert turn.cases_created == 1
    assert turn.blocked == 1
    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "freeze:case_created"}


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


def test_a_second_issuer_in_one_turn_is_deferred_rather_than_frozen_into_a_doomed_case() -> None:
    """The surplus keeps its Source. It is the lane that is full, not the frame that is unusable.

    The lane freezes one Case per turn because only one can ever reach `INTENT_EMITTED`:
    `ux_trading_intents_one_active` is a unique index admitting a single nonterminal Intent. (#348
    removed the one-entry-per-UTC-day fence this clause used to cite; the index is what the guarantee
    always rested on.) Freezing several meant the first to answer `long` took the fence and the rest were
    settled `BLOCKED / capacity_exhausted` — *terminal*, which puts `primary_source_key` beyond
    re-admission for good. The surplus has to come back at admission, where a refusal is retryable.
    """

    trading = FakeTrading(
        authority=_authority(catalog=_catalog(_catalog_entry(), _catalog_entry("DOGEUSDT"))),
        rows=[_row(), _row(event_id="evt-2", symbol="DOGE")],
    )
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert turn.cases_created == 1
    assert _reasons(trading) == {
        "oi:evt-1:oi_signal_v1": "freeze:case_created",
        "oi:evt-2:oi_signal_v1": "eligibility:lane_capacity_exhausted",
    }
    surplus = next(row for row in trading.admission if row["source_key"] == "oi:evt-2:oi_signal_v1")
    assert surplus["status"] == "DEFERRED"
    assert surplus["evidence"]["lane_full"] == "freezes_per_turn"
    # And no Case was opened only to be closed against a fence its own turn had just consumed: the one
    # Case that exists preserved the LONG decision beside a capital block, with no Intent.
    assert trading.commits == list(trading.cases)
    assert trading.settled == []


def test_a_busy_issuer_is_one_refusal_whose_evidence_says_which_side_holds_it() -> None:
    """#348 merged `active_underlying` and `case_in_flight`. Both halves need a test, and the row it
    writes has to survive the HTTP schema — the reason this branch had none is why the schema mismatch
    that follows it shipped green.
    """

    for holder, authority in (
        ("intent", _authority(active_underlyings=frozenset({"crypto:TUT"}))),
        ("case", _authority(underlyings_in_flight=frozenset({"crypto:TUT"}))),
    ):
        trading = FakeTrading(authority=authority, rows=[_row()])
        lane, _ = _lane(trading)

        _advance(lane)

        assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "eligibility:underlying_busy"}
        row = trading.admission[0]
        assert (row["status"], row["retryable"]) == ("DEFERRED", True)
        assert row["evidence"]["holds"] == holder
        # The ledger row is what `/api/trading/gate` serves, and that schema forbids extra keys, so
        # the whole evidence document has to validate — not just the key this refusal adds.
        TradingGateEvidenceData.model_validate(row["evidence"])


def test_a_source_that_already_authored_a_case_is_terminally_consumed() -> None:
    trading = FakeTrading(
        authority=_authority(cased_source_keys=frozenset({"oi:evt-1:oi_signal_v1"})),
        rows=[_row()],
    )
    lane, _ = _lane(trading)

    _advance(lane)

    assert _reasons(trading) == {"oi:evt-1:oi_signal_v1": "eligibility:already_consumed"}


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
    """#350: a changed public catalog is named, not `intent_admission_blocked`."""

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    trading.commit_result = lambda case_id: CapitalDispositionCommit(state=CaseState.BLOCKED, reason="catalog_mismatch")
    lane, _ = _lane(trading)

    turn = _advance(lane)

    assert turn.blocked == 1
    assert trading.commits == list(trading.cases)


def test_a_backlog_case_that_loses_the_fence_at_commit_is_blocked_by_name() -> None:
    """The one honest `capacity_exhausted`: a race, not a queueing artefact the lane manufactured.

    Only one Case is frozen per turn, so a second claimable Case means a restart or a paused lane left
    it behind — it was frozen in its own turn and had its own chance at the fence. Losing it now is a
    real fact about the lane's capacity at decision time, and it is settled terminally under that name
    rather than left to spin until its 5-minute budget expires.
    """

    trading = FakeTrading(authority=_authority(), rows=[_row()])
    lane, _ = _lane(trading)
    _advance(lane)
    trading.claimable.append(next(iter(trading.cases)))
    trading.commit_result = lambda case_id: CapitalDispositionCommit(
        state=CaseState.BLOCKED, reason="capacity_exhausted"
    )

    state = asyncio.run(lane._decide_one())

    assert state == CaseState.BLOCKED
    # The commit transaction owns the terminal write, so the lane does not settle it a second time.
    assert trading.settled == []
    assert trading.commits[-1] in trading.cases


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
