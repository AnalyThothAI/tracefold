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
from tracefold.trading.admission import AdmissionConfig
from tracefold.trading.contracts import Bar, CaseState, OiCandidateRow, TradingCaseManifest
from tracefold.trading.policy import ALPHA_POLICY
from tracefold.trading.signal_lane import SignalLane, SignalLaneConfig
from tracefold.trading.sources import SourceRejected, normalize_oi_source
from tracefold.trading.storage.execution_stream import PreparedTradeSignal
from tracefold.trading.storage.lane import SignalLaneSnapshot

NOW = 1_787_000_000_000
DIGEST = "a" * 64
EPOCH = "epoch-1"


def _row(**overrides: Any) -> OiCandidateRow:
    values: dict[str, Any] = {
        "event_id": "evt-1",
        "source_item_id": "source-1",
        "verdict_created_at_ms": NOW - 30_000,
        "final_decision": "push",
        "source_rule": "oi_whale_ratio",
        "source_strategy_id": "opennews_oi_v1",
        "source_contract_version": "oi_source_contract_v1",
        "measurement_window_ms": 300_000,
        "provider_symbol": "BTC",
        "learning_epoch": EPOCH,
        "program_version": "news_oi_signal_v3",
        "program_sha256": DIGEST,
        "policy_version": "news_triage_policy_v12",
        "judgment_contract_version": "news_judgment_v2",
        "judgment_origin": "oi",
        "judgment_sha256": DIGEST,
        "runtime_manifest_sha": DIGEST,
        "metric_version": "oi_signal_v1",
        "symbol": "BTC",
        "direction": "rise",
        "oi_change_bps": 900,
        "oi_value_usd": 40_000_000,
        "whale_long_profit_bps": 3_000,
        "whale_oi_ratio_bps": 6_000,
        "observed_at_ms": NOW - 60_000,
        "source_available_at_ms": NOW - 30_000,
        "ingest_mode": "live",
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
        self.snapshot = SignalLaneSnapshot(frozenset(), frozenset())
        self.cases: dict[str, dict[str, Any]] = {}
        self.claimable: list[str] = []
        self.signals: list[PreparedTradeSignal] = []
        self.admission: list[dict[str, Any]] = []
        self.runtime_states: list[str] = []

    def signal_lane_snapshot(self, *, since_ms: int) -> SignalLaneSnapshot:
        del since_ms
        return self.snapshot

    def set_decision_runtime(self, *, state: str, **_: Any) -> bool:
        self.runtime_states.append(state)
        return True

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
    expected_symbol: str = "BTC",
) -> SignalLane:
    del settings_noise

    async def bars(candidate: Any, _start: int, _end: int) -> Sequence[Bar]:
        assert candidate.base_symbol == expected_symbol
        return _bars(candidate.observed_at_ms)

    async def projection(_metric: str, _after: int, _until: int) -> Sequence[OiCandidateRow]:
        return trading.rows

    return SignalLane(
        db=FakeDb(trading),  # type: ignore[arg-type]
        config=SignalLaneConfig(admission=AdmissionConfig(), policy=ALPHA_POLICY),
        bars=bars,
        oi_projection=projection,
        news_generation=EPOCH,
        release_revision="test-release",
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


def test_duplicate_source_and_busy_market_do_not_create_duplicate_signal() -> None:
    consumed = FakeTrading((_row(),))
    consumed.snapshot = SignalLaneSnapshot(frozenset({"oi:evt-1:oi_signal_v1"}), frozenset())
    busy = FakeTrading((_row(),))
    busy.snapshot = SignalLaneSnapshot(frozenset(), frozenset({"crypto:BTC"}))

    asyncio.run(_lane(consumed).advance())
    asyncio.run(_lane(busy).advance())

    assert consumed.cases == busy.cases == {}
    assert {row["reason"] for row in consumed.admission} == {"already_consumed"}
    assert {row["reason"] for row in busy.admission} == {"underlying_busy"}


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
    candidate = normalize_oi_source(_row(symbol="UNITREE", provider_symbol="XYZ-UNITREE", venue="hyperliquid"))

    assert not isinstance(candidate, SourceRejected)
    asyncio.run(trading_wiring._source_native_bars(candidate, NOW - 300_000, NOW))

    assert requested == [("xyz:UNITREE", "hl.xyz")]


def test_pre_move_and_four_hour_reads_measure_the_same_book(monkeypatch: pytest.MonkeyPatch) -> None:
    """#460 M2: the two source-native reads translate one venue vocabulary through one function.

    The pre-move read takes a live `OiTradeCandidate`; the four-hour result read takes a `market_key`
    the Case froze hours earlier. They had a `binance.usdm` -> `binance.perp` ladder each, and the
    failure that costs something is not a crash — it is the two quietly asking different books for
    the same Signal, so a 4 h card reports an outcome the entry price was never measured against.
    This drives both through a real Binance klines payload and asserts they come back identical.
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
    candidate = normalize_oi_source(_row(symbol="SOL", provider_symbol="SOL", venue="binance"))
    assert not isinstance(candidate, SourceRejected)

    pre_move = asyncio.run(trading_wiring._source_native_bars(candidate, NOW - 300_000, NOW))
    result = asyncio.run(
        trading_wiring._source_native_result_bars("crypto:perp:SOL:USDT", candidate.venue, NOW - 300_000, NOW)
    )

    # One symbol spelling and one host across both reads: `SOLUSDT` on the USD-M book, never the spot one.
    assert requested == [("SOLUSDT", "fapi.binance.com"), ("SOLUSDT", "fapi.binance.com")]
    assert [(bar.open_at_ms, str(bar.close)) for bar in pre_move] == list(result)
    assert [str(bar.close) for bar in pre_move] == ["100.5", "101.5"]


def test_provider_symbol_mismatch_fails_closed_before_market_data() -> None:
    trading = FakeTrading((_row(provider_symbol="XYZ-OTHER"),))

    asyncio.run(_lane(trading).advance())

    assert trading.cases == {}
    assert trading.admission[0]["reason"] == "source_contract_invalid"
    assert trading.admission[0]["evidence"] == {"rule": "provider_symbol_mismatch"}


def test_invalid_market_key_is_durably_rejected_without_faulting_workers() -> None:
    trading = FakeTrading((_row(symbol="@107", provider_symbol="@107", venue="hyperliquid"),))

    turn = asyncio.run(_lane(trading, expected_symbol="@107").advance())

    assert (turn.cases_created, turn.signals_emitted) == (0, 0)
    assert trading.cases == {}
    assert trading.admission[0]["reason"] == "source_contract_invalid"
    assert trading.admission[0]["evidence"] == {"rule": "market_key_invalid"}
    assert trading.runtime_states == ["STARTING", "RUNNING"]


def test_repository_fault_propagates_and_faults_decision_runtime() -> None:
    class Broken(FakeTrading):
        def commit_signal(self, **_: Any) -> bool:
            raise RuntimeError("database unavailable")

    trading = Broken((_row(),))

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(_lane(trading).advance())

    assert trading.signals == []
    assert trading.runtime_states == ["STARTING", "FAULTED"]
