"""The one business action: SignalLane.advance()."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tracefold.app.trading_config import signal_lane_config
from tracefold.app.workers.wiring import trading as trading_wiring
from tracefold.integrations.venues import fetch_binance_candles
from tracefold.platform.config.models import Settings
from tracefold.trading.admission import ADMISSION_VERSION, AdmissionConfig
from tracefold.trading.contracts import Bar, CaseState, OiCandidateRow, TradingCaseManifest, canonical_sha256
from tracefold.trading.policy import ALPHA_POLICY
from tracefold.trading.signal_lane import SignalLane, SignalLaneConfig
from tracefold.trading.sources import SourceRejected, normalize_oi_source
from tracefold.trading.storage.execution_stream import PreparedTradeSignal
from tracefold.trading.storage.lane import SignalLaneSnapshot

NOW = 1_787_000_000_000


def _row(**overrides: Any) -> OiCandidateRow:
    values: dict[str, Any] = {
        "event_id": "evt-1",
        "metric_version": "oi_signal_v1",
        "source_item_id": "source-1",
        "symbol": "BTC",
        "direction": "rise",
        "oi_change_bps": 900,
        "oi_value_usd": 40_000_000,
        "whale_long_profit_bps": 3_000,
        "whale_oi_ratio_bps": 6_000,
        "observed_at_ms": NOW - 60_000,
        "available_at_ms": NOW - 30_000,
        "ingest_mode": "live",
        "source_strategy_id": "opennews_oi_v1",
        "source_contract_version": "oi_source_contract_v1",
        "measurement_window_ms": 300_000,
        "venue": "binance",
    }
    values.update(overrides)
    return OiCandidateRow(**values)  # type: ignore[typeddict-item]


def _bars(cutoff: int = NOW - 60_000) -> tuple[Bar, ...]:
    return (
        Bar(open_at_ms=cutoff - 3_900_000, close_at_ms=cutoff - 3_600_000, close=Decimal("100")),
        Bar(open_at_ms=cutoff - 300_000, close_at_ms=cutoff, close=Decimal("102")),
    )


class FakeTrading:
    def __init__(self, rows: Sequence[OiCandidateRow]) -> None:
        self.rows = tuple(rows)
        self.snapshot = SignalLaneSnapshot(frozenset())
        self.cases: dict[str, dict[str, Any]] = {}
        self.claimable: list[str] = []
        self.signals: list[PreparedTradeSignal] = []
        self.admission: list[dict[str, Any]] = []

    def signal_lane_snapshot(self, *, since_ms: int) -> SignalLaneSnapshot:
        del since_ms
        return self.snapshot

    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: dict[str, Any],
        now_ms: int,
        **_: Any,
    ) -> bool:
        if manifest.primary_trigger.source_key in {
            value["manifest"].primary_trigger.source_key for value in self.cases.values()
        }:
            return False
        self.cases[case_id] = {"manifest": manifest, "created_at_ms": now_ms, "state": CaseState.PENDING}
        self.claimable.append(case_id)
        self.admission.append(dict(admission))
        return True

    def claim_case(self, **_: Any) -> dict[str, Any] | None:
        if not self.claimable:
            return None
        case_id = self.claimable.pop(0)
        value = self.cases[case_id]
        return {
            "case_id": case_id,
            "manifest": value["manifest"].model_dump(mode="json"),
            "created_at_ms": value["created_at_ms"],
        }

    def settle_case(self, *, case_id: str, state: CaseState, **values: Any) -> bool:
        self.cases[case_id]["state"] = state
        self.cases[case_id]["policy_reason"] = values.get("policy_reason")
        return True

    def commit_signal(self, *, case_id: str, prepared: PreparedTradeSignal, **_: Any) -> bool:
        self.signals.append(prepared)
        self.cases[case_id]["state"] = CaseState.SIGNAL_EMITTED
        return True

    def record_gate_decision(self, **row: Any) -> None:
        self.admission.append(row)

    def expire_stale_gate_decisions(self, **_: Any) -> int:
        return 0

    def purge_gate_decisions(self, **_: Any) -> int:
        return 0


class FakeRepos:
    def __init__(self, trading: FakeTrading) -> None:
        self.trading = trading


class FakeDb:
    def __init__(self, trading: FakeTrading) -> None:
        self.repos = FakeRepos(trading)

    async def read(self, _name: str, fn: Any, **_: Any) -> Any:
        return fn(self.repos)

    async def tx(self, _name: str, fn: Any, **_: Any) -> Any:
        return fn(self.repos)


def _lane(
    trading: FakeTrading,
    *,
    settings_noise: object | None = None,
    expected_symbol: str | None = "BTC",
) -> SignalLane:
    del settings_noise

    async def bars(candidate: Any, _start: int, _end: int) -> Sequence[Bar]:
        assert expected_symbol is None or candidate.base_symbol == expected_symbol
        return _bars(candidate.observed_at_ms)

    async def projection(_metric: str, _after: int, _until: int) -> Sequence[OiCandidateRow]:
        return trading.rows

    return SignalLane(
        db=FakeDb(trading),  # type: ignore[arg-type]
        config=SignalLaneConfig(oi_metric_version="oi_signal_v1", admission=AdmissionConfig(), policy=ALPHA_POLICY),
        bars=bars,
        oi_projection=projection,
        clock=lambda: NOW,
    )


def test_long_writes_one_engine_neutral_signal_and_terminal_case() -> None:
    trading = FakeTrading((_row(),))

    turn = asyncio.run(_lane(trading).advance())

    assert (turn.cases_created, turn.signals_emitted, turn.blocked) == (1, 1, 0)
    signal = trading.signals[0].value
    assert signal.market_key == "crypto:perp:BTC:USDT"
    assert signal.direction == "long"
    assert signal.expires_at_ns - signal.observed_at_ns == 180_000_000_000
    assert trading.cases[signal.case_id]["state"] is CaseState.SIGNAL_EMITTED
    forbidden = {
        "runtime",
        "exchange",
        "account",
        "credentials",
        "quantity",
        "notional",
        "leverage",
        "order_type",
        "stop",
        "reservation",
        "authorization",
    }
    assert forbidden.isdisjoint(signal.model_dump())


def test_signal_expiry_is_capped_at_the_source_freshness_deadline() -> None:
    observed_at_ms = NOW - 290_000
    trading = FakeTrading((_row(observed_at_ms=observed_at_ms),))

    turn = asyncio.run(_lane(trading).advance())

    assert (turn.signals_emitted, turn.blocked) == (1, 0)
    signal = trading.signals[0].value
    assert signal.observed_at_ns == NOW * 1_000_000
    assert signal.expires_at_ns == (observed_at_ms + 300_000) * 1_000_000


def test_source_at_its_freshness_deadline_is_blocked_without_a_signal() -> None:
    trading = FakeTrading((_row(observed_at_ms=NOW - 300_000),))

    turn = asyncio.run(_lane(trading).advance())

    assert (turn.signals_emitted, turn.blocked) == (0, 1)
    assert trading.signals == []
    case = next(iter(trading.cases.values()))
    assert case["state"] is CaseState.BLOCKED
    assert case["policy_reason"] == "source_stale"


def test_execution_configuration_or_credentials_are_not_inputs_to_alpha() -> None:
    first = FakeTrading((_row(),))
    second = FakeTrading((_row(),))

    asyncio.run(_lane(first, settings_noise={"mode": "disabled", "credentials": None}).advance())
    asyncio.run(_lane(second, settings_noise={"mode": "live", "credentials": "unavailable"}).advance())

    left = first.signals[0].value
    right = second.signals[0].value
    assert left.model_dump(exclude={"signal_id", "case_id"}) == right.model_dump(exclude={"signal_id", "case_id"})


@pytest.mark.parametrize(
    ("max_age_seconds", "expected_ttl_ms"),
    ((30, 30_000), (179, 179_000), (180, 180_000), (3_600, 180_000)),
)
def test_signal_ttl_never_exceeds_an_accepted_source_freshness_window(
    max_age_seconds: int,
    expected_ttl_ms: int,
) -> None:
    settings = Settings.model_validate({"trading": {"candidates": {"max_age_seconds": max_age_seconds}}})

    config = signal_lane_config(settings)

    assert config.signal_ttl_ms == expected_ttl_ms
    assert config.signal_ttl_ms <= config.admission.max_age_ms


def test_no_trade_writes_no_signal() -> None:
    trading = FakeTrading((_row(whale_oi_ratio_bps=4_000),))

    turn = asyncio.run(_lane(trading).advance())

    assert (turn.no_trade, turn.signals_emitted) == (1, 0)
    assert trading.signals == []
    assert next(iter(trading.cases.values()))["state"] is CaseState.NO_TRADE


def test_a_source_that_already_has_a_case_does_not_create_a_second_one() -> None:
    """The one idempotency rule the lane still executes, and the only one it needs.

    A second Case for the same *issuer* is refused by `ux_trading_case_in_flight_underlying` inside the
    insert, so the lane no longer restates it as `underlying_busy` — a reason that never once fired in
    the ledger, because the lane decides every Case it freezes in the turn that freezes it (#537 PR-3).
    """

    consumed = FakeTrading((_row(),))
    consumed.snapshot = SignalLaneSnapshot(frozenset({"oi:evt-1:oi_signal_v1"}))

    asyncio.run(_lane(consumed).advance())

    assert consumed.cases == {}
    assert {row["reason"] for row in consumed.admission} == {"already_consumed"}


def test_every_admissible_frame_in_the_turn_is_frozen() -> None:
    """No per-turn freeze budget: the budget existed to protect a route catalogue read that is gone.

    Two issuers in one scan window used to cost two turns, and the loser carried
    `lane_capacity_exhausted` — an admission refusal about the lane's own bookkeeping rather than about
    the frame (#537 PR-3).
    """

    trading = FakeTrading((_row(), _row(event_id="evt-2", symbol="SOL", source_item_id="source-2")))

    turn = asyncio.run(_lane(trading, expected_symbol=None).advance())

    assert turn.cases_created == 2
    assert {value["manifest"].base_symbol for value in trading.cases.values()} == {"BTC", "SOL"}
    assert [row["reason"] for row in trading.admission if row["reason"] != "case_created"] == []


def test_unknown_source_venue_is_rejected_without_provider_or_case() -> None:
    trading = FakeTrading((_row(venue="okx"),))

    asyncio.run(_lane(trading).advance())

    assert trading.cases == {}
    assert trading.admission[0]["reason"] == "venue_unresolved"


def test_hyperliquid_builder_source_uses_provider_native_market_key(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[tuple[str, str]] = []

    async def fetch(
        venue_symbol: str,
        *,
        venue: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[Any, ...]:
        del start_ms, end_ms
        requested.append((venue_symbol, venue))
        return ()

    monkeypatch.setattr(trading_wiring, "fetch_hyperliquid_candles", fetch)
    # The provider's own venue text is the whole of the builder-DEX evidence (#510): the ledger's
    # `symbol` is already canonicalised, so an `XYZ-` title token no longer exists to infer it from.
    candidate = normalize_oi_source(_row(symbol="UNITREE", venue="hl.xyz"))

    assert not isinstance(candidate, SourceRejected)
    asyncio.run(trading_wiring._source_native_bars(candidate, NOW - 300_000, NOW))

    assert requested == [("xyz:UNITREE", "hl.xyz")]


def test_the_pre_move_read_asks_the_usdm_book_in_its_own_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """#460 M2: one function translates the provider's venue vocabulary into the price-plane key.

    There were two source-native reads here until #528 deleted the four-hour outcome card and with it
    `_source_native_result_bars`; the pre-move read is the only caller left. The translation is still
    what matters -- `binance.usdm` has to reach `fapi.binance.com` as `SOLUSDT`, never the spot book.
    """

    rows = [
        [1_787_000_000_000, "100.0", "101.0", "99.5", "100.5", "12", 1_787_000_299_999],
        [1_787_000_300_000, "100.5", "102.0", "100.0", "101.5", "18", 1_787_000_599_999],
    ]
    requested: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((str(request.url.params.get("symbol")), request.url.host))
        return httpx.Response(200, json=rows)

    async def fetch(venue_symbol: str, *, venue: str, start_ms: int, end_ms: int) -> Any:
        return await fetch_binance_candles(
            venue_symbol,
            venue=venue,
            start_ms=start_ms,
            end_ms=end_ms,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(trading_wiring, "fetch_binance_candles", fetch)
    candidate = normalize_oi_source(_row(symbol="SOL", venue="binance"))
    assert not isinstance(candidate, SourceRejected)

    pre_move = asyncio.run(trading_wiring._source_native_bars(candidate, NOW - 300_000, NOW))

    assert requested == [("SOLUSDT", "fapi.binance.com")]
    assert [str(bar.close) for bar in pre_move] == ["100.5", "101.5"]


def test_a_source_without_its_durable_clock_is_rejected_before_market_data() -> None:
    trading = FakeTrading((_row(available_at_ms=None),))

    asyncio.run(_lane(trading).advance())

    assert trading.cases == {}
    assert trading.admission[0]["reason"] == "source_contract_invalid"
    # The rulebook that answered rides in `evidence` beside the rule that refused (#537 PR-3).
    assert trading.admission[0]["evidence"]["rule"] == "available_at_missing"
    assert trading.admission[0]["evidence"]["gate_version"] == ADMISSION_VERSION


def test_the_frozen_case_carries_no_upstream_judgment_program_or_cohort_identity() -> None:
    """#510 PR-4. A News policy or Program bump changes nothing a frozen Case names."""

    trading = FakeTrading((_row(),))

    asyncio.run(_lane(trading).advance())

    manifest = next(iter(trading.cases.values()))["manifest"]
    assert manifest.manifest_version == "trading_manifest_v11"
    source = manifest.contexts.oi
    assert set(source.model_dump()) == {
        "event_id",
        "metric_version",
        "source_item_id",
        "observed_at_ms",
        "available_at_ms",
        "base_symbol",
        "venue",
        "oi_direction",
        "oi_change_bps",
        "oi_value_usd",
        "whale_long_profit_bps",
        "whale_oi_ratio_bps",
        "source_strategy_id",
        "source_contract_version",
        "measurement_window_ms",
    }
    assert manifest.primary_trigger.persisted_at_ms == NOW - 30_000


class _RetiredManifest:
    """A Case frozen under `trading_manifest_v10`, as the claim read returns it."""

    def __init__(self, manifest: TradingCaseManifest) -> None:
        self.primary_trigger = manifest.primary_trigger
        self._raw = manifest.model_dump(mode="json") | {"manifest_version": "trading_manifest_v10"}

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(self._raw)


def test_a_case_frozen_under_a_retired_manifest_version_is_blocked_not_re_decided() -> None:
    """#510 PR-4: a pending v10 Case is refused by the pinned version, with no migration."""

    trading = FakeTrading((_row(),))
    lane = _lane(trading)
    asyncio.run(lane.advance())
    case_id, value = next(iter(trading.cases.items()))
    trading.cases[case_id] = {**value, "manifest": _RetiredManifest(value["manifest"]), "state": CaseState.PENDING}
    trading.claimable.append(case_id)
    trading.signals.clear()

    turn = asyncio.run(lane.advance())

    assert turn.blocked == 1
    assert trading.cases[case_id]["policy_reason"] == "manifest_invalid"
    assert trading.signals == []


def test_invalid_market_key_is_durably_rejected_without_faulting_workers() -> None:
    trading = FakeTrading((_row(symbol="@107", venue="hyperliquid"),))

    turn = asyncio.run(_lane(trading, expected_symbol="@107").advance())

    assert (turn.cases_created, turn.signals_emitted) == (0, 0)
    assert trading.cases == {}
    assert trading.admission[0]["reason"] == "source_contract_invalid"
    # The rulebook that answered rides in `evidence` beside the rule that refused (#537 PR-3).
    assert trading.admission[0]["evidence"]["rule"] == "market_key_invalid"
    assert trading.admission[0]["evidence"]["gate_version"] == ADMISSION_VERSION


def test_repository_fault_propagates_out_of_the_turn() -> None:
    class Broken(FakeTrading):
        def commit_signal(self, **_: Any) -> bool:
            raise RuntimeError("database unavailable")

    trading = Broken((_row(),))

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(_lane(trading).advance())

    assert trading.signals == []


@pytest.mark.parametrize(
    ("age_ms", "expected_reason", "expected_cases"),
    ((AdmissionConfig().max_age_ms - 1, "case_created", 1), (AdmissionConfig().max_age_ms + 1, "trigger_stale", 0)),
    ids=("inside-the-window", "past-the-window"),
)
def test_the_scan_horizon_is_exactly_the_admission_window(
    age_ms: int,
    expected_reason: str,
    expected_cases: int,
) -> None:
    """#537 PR-3 F2P. One window, not three.

    `scan_horizon_ms` was `max_age_ms * 3`, so every turn re-read two windows of frames whose only
    possible answer was the one already stored: the median frame was evaluated 439 times before the
    expiry sweep closed it. A frame one millisecond inside the window is admitted; one millisecond
    outside it is `trigger_stale`, and that is the whole of what the horizon has to reach.
    """

    config = SignalLaneConfig(oi_metric_version="oi_signal_v1", admission=AdmissionConfig(), policy=ALPHA_POLICY)
    assert config.scan_horizon_ms == config.admission.max_age_ms

    trading = FakeTrading((_row(observed_at_ms=NOW - age_ms, available_at_ms=NOW - age_ms),))

    turn = asyncio.run(_lane(trading).advance())

    assert turn.cases_created == expected_cases
    assert [row["reason"] for row in trading.admission] == [expected_reason]


def test_the_v5_policy_digest_no_longer_carries_the_profit_threshold() -> None:
    """#537 PR-3. A rule that passed on 310 of 310 admitted frames is not a rule.

    The measurement stays on the Case — the manifest freezes it and the console renders it — because
    it is data about the frame. What is gone is the key in the identity every Case is decided under.
    """

    assert ALPHA_POLICY.policy_id == "source_native_oi_smart_money_long_v5"
    assert "min_whale_long_profit_bps" not in ALPHA_POLICY.config_snapshot
    assert ALPHA_POLICY.config_digest == canonical_sha256(ALPHA_POLICY.config_snapshot)

    trading = FakeTrading((_row(whale_long_profit_bps=0),))
    turn = asyncio.run(_lane(trading).advance())

    assert turn.signals_emitted == 1
    manifest = next(iter(trading.cases.values()))["manifest"]
    assert manifest.policy_id == "source_native_oi_smart_money_long_v5"
    assert manifest.contexts.oi.whale_long_profit_bps == 0
    assert [check.check for check in ALPHA_POLICY.decide(manifest.contexts).checks] == [
        "source_measurement_window_ms",
        "oi_direction",
        "oi_change_bps",
        "whale_oi_ratio_bps",
        "pre_move_bps",
        "pre_move_bps",
    ]
