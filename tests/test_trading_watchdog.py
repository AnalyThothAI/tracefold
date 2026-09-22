"""The Trading watchdog: conditions, one message per episode, and the two audit incidents replayed (#680).

Hermetic. The facts come from a timeline instead of PostgreSQL and the provider is a recording fake;
`tests/integration/test_trading_watchdog_storage.py` holds the SQL and the alert ledger to the real
database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.workers.runtime import CapabilityStates
from tracefold.app.workers.watchdog_storage import WatchdogAlertState
from tracefold.app.workers.wiring import watchdog as wd
from tracefold.integrations.telegram import _telegram_message
from tracefold.news import ReaderCard, ReaderDeliveryPresentation
from tracefold.news.feishu_card import feishu_card
from tracefold.platform.config.models import Settings
from tracefold.trading.contracts import OiCandidateRow
from tracefold.trading.storage.health import OverduePlan, RuntimeLiveness

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
SLOT = "binance_usdm_primary"


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def _frame(event_id: str, *, at_ms: int, symbol: str = "BCH") -> OiCandidateRow:
    return OiCandidateRow(  # type: ignore[typeddict-item]
        event_id=event_id,
        metric_version="oi_signal_v1",
        source_item_id=f"item-{event_id}",
        symbol=symbol,
        direction="rise",
        oi_change_bps=720,
        oi_value_usd=32_000_000,
        whale_long_profit_bps=8_000,
        whale_oi_ratio_bps=10_000,
        observed_at_ms=at_ms,
        available_at_ms=at_ms,
        ingest_mode="live",
        source_strategy_id="1019",
        source_contract_version="opennews_oi_source_v1",
        measurement_window_ms=300_000,
        venue="binance",
    )


def _facts(now_ms: int, **overrides: Any) -> wd.WatchdogFacts:
    values: dict[str, Any] = {
        "now_ms": now_ms,
        "lane": CapabilityStates().get("trading_signal_lane"),
        "reads": wd.WatchdogReads(),
        "runtime_expected": False,
        "account_slot": SLOT,
        "runtime_starts_last_hour": 0,
    }
    values.update(overrides)
    return wd.WatchdogFacts(**values)


class RecordingSender:
    """The send entry as the watchdog sees it: accepted cards are recorded, a failure raises."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, ReaderCard, Mapping[str, Any]]] = []
        self.failing = False
        self.clock: Callable[[], int] = lambda: 0

    @property
    def available(self) -> bool:
        return True

    async def send_prepared_card(
        self, card: ReaderCard, *, channel_payload: Mapping[str, Any], operation: str
    ) -> Mapping[str, Any]:
        assert operation == "trading_watchdog_alert"
        if self.failing:
            raise RuntimeError("news_delivery_feishu_transport_failed")
        self.sent.append((self.clock(), card.header.subject, card, channel_payload))
        return {"provider": "feishu", "code": 0}

    def subjects(self) -> list[str]:
        return [subject for _, subject, _, _ in self.sent]


class TimelineDatabase:
    """`WatchdogDatabase` over a function of time, and an in-memory alert ledger that outlives watchdogs."""

    def __init__(self, reads: Callable[[int], wd.WatchdogReads]) -> None:
        self._reads = reads
        self.ledger: dict[str, WatchdogAlertState] = {}
        self.details: dict[str, str] = {}

    async def reads(self, *, now_ms: int, runtime_expected: bool) -> wd.WatchdogReads:
        return self._reads(now_ms)

    async def alert_states(self) -> dict[str, WatchdogAlertState]:
        return dict(self.ledger)

    async def save_alert(self, state: WatchdogAlertState, *, detail: str, now_ms: int) -> None:
        self.ledger[state.condition_key] = state
        self.details[state.condition_key] = detail


def _watchdog(
    db: TimelineDatabase,
    sender: RecordingSender,
    clock: list[int],
    *,
    capabilities: CapabilityStates | None = None,
    runtime_expected: bool = False,
) -> wd.TradingWatchdog:
    sender.clock = lambda: clock[0]
    return wd.TradingWatchdog(
        db=db,
        sender=sender,
        capabilities=capabilities or CapabilityStates(),
        runtime_expected=runtime_expected,
        account_slot=SLOT,
        clock=lambda: clock[0],
    )


def _run(watchdog: wd.TradingWatchdog, clock: list[int], *, start_ms: int, end_ms: int, before: Any = None) -> None:
    """Pass every `WATCHDOG_POLL_SECONDS`, the way `run_trading_watchdog` ticks, over [start, end]."""

    step_ms = int(wd.WATCHDOG_POLL_SECONDS * 1000)
    at = start_ms
    while at <= end_ms:
        clock[0] = at
        if before is not None:
            before(at)
        asyncio.run(watchdog.advance())
        at += step_ms


# -- conditions ---------------------------------------------------------------------------------------


def test_each_condition_is_named_by_the_fact_that_holds_it() -> None:
    now = _ms("2026-09-22 18:40")
    lane = CapabilityStates()
    lane.faulted("trading_signal_lane", "trading_signal_lane:RuntimeError")
    reads = wd.WatchdogReads(
        unanswered_frames=(_frame("a", at_ms=now - 40 * MINUTE_MS), _frame("b", at_ms=now - 12 * MINUTE_MS)),
        runtime=RuntimeLiveness(heartbeat_at_ns=(now - 90_000) * 1_000_000, started_at_ns=(now - HOUR_MS) * 1_000_000),
        dispositions=("unexpected_exposure",) * 4 + ("instrument_busy", "accepted", "unexpected_exposure"),
        overdue_plans=(
            OverduePlan(
                market_key="crypto:perp:UNI:USDT",
                status="closing",
                opened_at_ns=(now - 5 * HOUR_MS) * 1_000_000,
                max_holding_ns=4 * HOUR_MS * 1_000_000,
            ),
        ),
    )

    found = wd.findings(
        _facts(
            now,
            lane=lane.get("trading_signal_lane"),
            reads=reads,
            runtime_expected=True,
            runtime_starts_last_hour=4,
        )
    )

    assert set(found) == set(wd.CONDITION_TITLES)
    assert "trading_signal_lane:RuntimeError" in found[wd.SIGNAL_LANE_FAULTED].lines[0]
    assert found[wd.OI_FRAMES_UNANSWERED].lines[0].startswith("2 个 live OI 帧")
    assert "09-22 18:00 UTC" in found[wd.OI_FRAMES_UNANSWERED].lines[1]
    assert "1 分钟前" in found[wd.RUNTIME_HEARTBEAT_STALE].lines[0]
    assert "启动 4 次" in found[wd.RUNTIME_RESTART_LOOP].lines[0]
    # The histogram names every refusal in the streak and stops at the accepted one.
    assert found[wd.SIGNAL_REFUSAL_STREAK].lines == (
        "最近 5 个 Signal 的处置都不是 accepted",
        "拒因：unexpected_exposure ×4 · instrument_busy ×1",
    )
    assert "crypto:perp:UNI:USDT closing" in found[wd.PLAN_OVERDUE].lines[0]


def test_a_healthy_deployment_holds_no_condition() -> None:
    now = _ms("2026-09-22 18:40")
    lane = CapabilityStates()
    lane.running("trading_signal_lane")
    reads = wd.WatchdogReads(
        runtime=RuntimeLiveness(heartbeat_at_ns=(now - 5_000) * 1_000_000, started_at_ns=(now - HOUR_MS) * 1_000_000),
        dispositions=("position_limit",) * 4 + ("accepted",) + ("unexpected_exposure",) * 10,
    )

    found = wd.findings(
        _facts(
            now, lane=lane.get("trading_signal_lane"), reads=reads, runtime_expected=True, runtime_starts_last_hour=3
        )
    )

    assert found == {}


def test_runtime_conditions_need_a_configured_runtime() -> None:
    """`execution.mode: disabled` has no Runtime, so a missing row is not a silent one.

    Nor is the last refusal streak it wrote: with nobody running the Runtime no disposition will ever
    end it, and repeating it every four hours would page about history.
    """

    now = _ms("2026-09-22 18:40")
    history = wd.WatchdogReads(dispositions=("unexpected_exposure",) * 21)

    assert wd.findings(_facts(now, reads=history, runtime_expected=False, runtime_starts_last_hour=9)) == {}
    missing = wd.findings(_facts(now, runtime_expected=True))
    assert set(missing) == {wd.RUNTIME_HEARTBEAT_STALE}
    assert "没有执行 Runtime 状态行" in missing[wd.RUNTIME_HEARTBEAT_STALE].lines[0]


def _beating(now_ms: int) -> RuntimeLiveness:
    """A Runtime row that is beating now and started long ago: no Runtime condition holds."""

    return RuntimeLiveness(heartbeat_at_ns=now_ms * 1_000_000, started_at_ns=1_000_000)


def test_a_full_window_of_refusals_says_it_may_be_longer() -> None:
    reads = wd.WatchdogReads(runtime=_beating(0), dispositions=("spread_limit",) * 50)
    found = wd.findings(_facts(0, reads=reads, runtime_expected=True))

    assert found[wd.SIGNAL_REFUSAL_STREAK].lines[0] == "最近 ≥50 个 Signal 的处置都不是 accepted"


# -- one message per episode ----------------------------------------------------------------------------


def _step_kinds(steps: list[wd.AlertStep]) -> list[tuple[str, str | None]]:
    return [(step.condition_key, step.kind) for step in steps]


def test_onset_then_silence_then_a_repeat_after_the_realert_interval() -> None:
    finding = {wd.PLAN_OVERDUE: wd.Finding(wd.PLAN_OVERDUE, ("x",))}
    t0 = 1_000 * HOUR_MS

    onset = wd.plan_alerts(finding, {}, now_ms=t0)
    assert _step_kinds(onset) == [(wd.PLAN_OVERDUE, "onset")]
    told = onset[0].delivered
    assert (told.active, told.opened_at_ms, told.notified_at_ms) == (True, t0, t0)

    assert wd.plan_alerts(finding, {wd.PLAN_OVERDUE: told}, now_ms=t0 + wd.REALERT_AFTER_MS - 1) == []
    repeat = wd.plan_alerts(finding, {wd.PLAN_OVERDUE: told}, now_ms=t0 + wd.REALERT_AFTER_MS)
    assert _step_kinds(repeat) == [(wd.PLAN_OVERDUE, "repeat")]
    assert repeat[0].delivered.opened_at_ms == t0


def test_a_condition_resolves_only_after_it_has_stayed_clear() -> None:
    t0 = 1_000 * HOUR_MS
    told = WatchdogAlertState(wd.PLAN_OVERDUE, active=True, opened_at_ms=t0, notified_at_ms=t0)

    first_clear = wd.plan_alerts({}, {wd.PLAN_OVERDUE: told}, now_ms=t0 + HOUR_MS)
    assert _step_kinds(first_clear) == [(wd.PLAN_OVERDUE, None)]
    holding = first_clear[0].delivered
    assert holding.clear_since_ms == t0 + HOUR_MS and holding.active

    assert wd.plan_alerts({}, {wd.PLAN_OVERDUE: holding}, now_ms=t0 + HOUR_MS + wd.RESOLVE_AFTER_MS - 1) == []
    resolved = wd.plan_alerts({}, {wd.PLAN_OVERDUE: holding}, now_ms=t0 + HOUR_MS + wd.RESOLVE_AFTER_MS)
    assert _step_kinds(resolved) == [(wd.PLAN_OVERDUE, "resolved")]
    assert resolved[0].delivered.active is False
    assert resolved[0].undelivered is None


def test_a_flap_inside_the_hold_is_the_same_episode() -> None:
    t0 = 1_000 * HOUR_MS
    holding = WatchdogAlertState(
        wd.RUNTIME_HEARTBEAT_STALE, active=True, opened_at_ms=t0, notified_at_ms=t0, clear_since_ms=t0 + 2 * MINUTE_MS
    )
    finding = {wd.RUNTIME_HEARTBEAT_STALE: wd.Finding(wd.RUNTIME_HEARTBEAT_STALE, ("x",))}

    back = wd.plan_alerts(finding, {wd.RUNTIME_HEARTBEAT_STALE: holding}, now_ms=t0 + 5 * MINUTE_MS)

    assert _step_kinds(back) == [(wd.RUNTIME_HEARTBEAT_STALE, None)]
    assert back[0].delivered == replace(holding, clear_since_ms=None)


def test_an_episode_nobody_was_told_about_closes_without_a_recovery_message() -> None:
    t0 = 1_000 * HOUR_MS
    untold = WatchdogAlertState(
        wd.SIGNAL_LANE_FAULTED, active=True, opened_at_ms=t0, notified_at_ms=None, clear_since_ms=t0
    )

    steps = wd.plan_alerts({}, {wd.SIGNAL_LANE_FAULTED: untold}, now_ms=t0 + wd.RESOLVE_AFTER_MS)

    assert _step_kinds(steps) == [(wd.SIGNAL_LANE_FAULTED, None)]
    assert steps[0].delivered.active is False


def test_a_failed_send_is_owed_and_retried_on_the_next_pass() -> None:
    now = _ms("2026-09-22 18:40")
    clock = [now]
    sender = RecordingSender()
    sender.failing = True
    db = TimelineDatabase(lambda at: wd.WatchdogReads(runtime=_beating(at), dispositions=("unmapped",) * 6))
    watchdog = _watchdog(db, sender, clock, runtime_expected=True)

    asyncio.run(watchdog.advance())
    assert sender.sent == []
    owed = db.ledger[wd.SIGNAL_REFUSAL_STREAK]
    assert owed.active and owed.notified_at_ms is None

    sender.failing = False
    clock[0] = now + MINUTE_MS
    asyncio.run(watchdog.advance())
    assert sender.subjects() == ["Tracefold 告警 · Signal 连续被拒"]
    assert db.ledger[wd.SIGNAL_REFUSAL_STREAK].opened_at_ms == now
    assert db.ledger[wd.SIGNAL_REFUSAL_STREAK].notified_at_ms == now + MINUTE_MS


def test_the_ledger_outlives_a_workers_restart() -> None:
    """A new process neither re-pages an active condition nor loses the recovery message (#680 RC11)."""

    now = _ms("2026-09-22 18:40")
    clock = [now]
    sender = RecordingSender()
    overdue = [True]

    def reads(_at: int) -> wd.WatchdogReads:
        plan = OverduePlan(market_key="crypto:perp:UNI:USDT", status="open", opened_at_ns=1, max_holding_ns=1)
        return wd.WatchdogReads(overdue_plans=(plan,) if overdue[0] else ())

    db = TimelineDatabase(reads)
    asyncio.run(_watchdog(db, sender, clock).advance())

    clock[0] = now + 20 * MINUTE_MS
    asyncio.run(_watchdog(db, sender, clock).advance())  # a restarted process, same ledger
    assert sender.subjects() == ["Tracefold 告警 · 持仓超过最长持有时间"]

    overdue[0] = False
    clock[0] = now + 30 * MINUTE_MS
    asyncio.run(_watchdog(db, sender, clock).advance())
    clock[0] = now + 45 * MINUTE_MS
    asyncio.run(_watchdog(db, sender, clock).advance())  # restarted again inside the hold
    assert sender.subjects()[-1] == "Tracefold 已恢复 · 持仓超过最长持有时间"
    assert len(sender.sent) == 2


def test_the_restart_count_is_the_generations_seen_start_within_the_hour() -> None:
    now = _ms("2026-09-22 18:40")
    clock = [now]
    starts: list[int] = []
    sender = RecordingSender()

    def reads(_at: int) -> wd.WatchdogReads:
        return wd.WatchdogReads(runtime=RuntimeLiveness(heartbeat_at_ns=clock[0] * 1_000_000, started_at_ns=starts[-1]))

    watchdog = _watchdog(TimelineDatabase(reads), sender, clock, runtime_expected=True)
    for minutes in (0, 5, 10, 15):
        clock[0] = now + minutes * MINUTE_MS
        starts.append((clock[0] - 1_000) * 1_000_000)
        asyncio.run(watchdog.advance())

    # Four generations seen start inside one hour: the fourth pass is the first over the limit.
    assert sender.subjects() == ["Tracefold 告警 · 执行 Runtime 频繁重启"]
    assert sender.sent[0][0] == now + 15 * MINUTE_MS
    assert "启动 4 次" in sender.sent[0][2].lead


# -- the two incidents the audit found, replayed ----------------------------------------------------------


def test_replay_09_10_lane_outage_alerts_within_minutes_and_resolves_once() -> None:
    """09-10 18:33 -> 09-12 02:25: the lane faulted and 129 live frames got no ledger row.

    The watchdog must page within 30 minutes (#680 §8), not page again more often than the re-alert
    interval, and say once that it is over after the restart.
    """

    fault_at = _ms("2026-09-10 18:33")
    restart_at = _ms("2026-09-12 02:25")
    frames = [_frame(f"f{index}", at_ms=fault_at + index * 13 * MINUTE_MS) for index in range(145)]
    capabilities = CapabilityStates()
    capabilities.running("trading_signal_lane")

    def reads(now_ms: int) -> wd.WatchdogReads:
        # While the lane is down nothing is answered; the first sweep after the restart answers every
        # frame inside the horizon. The read's own window is the watchdog's: 12 h back, 10 min old.
        lane_down = fault_at <= now_ms < restart_at
        unanswered = tuple(
            frame
            for frame in frames
            if now_ms - wd.ANSWER_HORIZON_MS < frame["available_at_ms"] <= now_ms - wd.FRAME_ANSWER_BUDGET_MS
            and (lane_down or frame["available_at_ms"] < restart_at - wd.ANSWER_HORIZON_MS)
        )
        return wd.WatchdogReads(unanswered_frames=unanswered)

    def lane_state(now_ms: int) -> None:
        if fault_at <= now_ms < restart_at:
            capabilities.faulted("trading_signal_lane", "trading_signal_lane:ResourceAdmissionTimeout")
        else:
            capabilities.running("trading_signal_lane")

    clock = [fault_at - 10 * MINUTE_MS]
    sender = RecordingSender()
    watchdog = _watchdog(TimelineDatabase(reads), sender, clock, capabilities=capabilities)

    _run(watchdog, clock, start_ms=fault_at - 10 * MINUTE_MS, end_ms=restart_at + HOUR_MS, before=lane_state)

    by_subject: dict[str, list[int]] = {}
    for at, subject, _card, _payload in sender.sent:
        by_subject.setdefault(subject, []).append(at)
    lane_onset = by_subject["Tracefold 告警 · Signal lane 已停止"]
    frames_onset = by_subject["Tracefold 告警 · OI 帧没有准入答复"]
    assert lane_onset[0] - fault_at <= 30 * MINUTE_MS
    assert frames_onset[0] - fault_at <= 30 * MINUTE_MS
    outage_hours = (restart_at - fault_at) / HOUR_MS
    repeats = by_subject["Tracefold 告警（持续） · Signal lane 已停止"]
    assert len(repeats) == int(outage_hours // (wd.REALERT_AFTER_MS / HOUR_MS))
    assert all(later - earlier >= wd.REALERT_AFTER_MS for earlier, later in pairwise(repeats))
    resolved = by_subject["Tracefold 已恢复 · Signal lane 已停止"]
    assert len(resolved) == 1 and 0 < resolved[0] - restart_at <= wd.RESOLVE_AFTER_MS + MINUTE_MS
    assert len(by_subject["Tracefold 已恢复 · OI 帧没有准入答复"]) == 1


def test_replay_09_18_refusal_streak_alerts_at_the_fifth_refusal_and_resolves_on_the_next_accept() -> None:
    """09-18 03:36 -> 09-21 15:56: 21 Signals refused `unexpected_exposure` and one `instrument_busy`.

    Nobody noticed for three and a half days. The streak pages the pass after its fifth refusal is
    written, repeats at most every four hours, names the refusals, and clears on the next accepted one.
    """

    first = _ms("2026-09-18 03:36")
    last = _ms("2026-09-21 15:56")
    accepted_again = _ms("2026-09-22 00:39")
    refusals = ["unexpected_exposure"] * 21
    refusals.insert(9, "instrument_busy")
    spacing = (last - first) // (len(refusals) - 1)
    timeline = [(first - 6 * HOUR_MS, "accepted")]
    timeline += [(first + index * spacing, reason) for index, reason in enumerate(refusals)]
    timeline += [(accepted_again, "accepted")]

    def reads(now_ms: int) -> wd.WatchdogReads:
        written = [reason for at, reason in timeline if at <= now_ms]
        return wd.WatchdogReads(runtime=_beating(now_ms), dispositions=tuple(reversed(written[-50:])))

    clock = [first - HOUR_MS]
    sender = RecordingSender()
    watchdog = _watchdog(TimelineDatabase(reads), sender, clock, runtime_expected=True)

    _run(watchdog, clock, start_ms=first - HOUR_MS, end_ms=accepted_again + HOUR_MS)

    fifth_at = timeline[5][0]
    onset = [(at, card) for at, subject, card, _ in sender.sent if subject == "Tracefold 告警 · Signal 连续被拒"]
    assert len(onset) == 1
    assert 0 <= onset[0][0] - fifth_at <= MINUTE_MS
    assert "拒因：unexpected_exposure ×5" in onset[0][1].lead
    repeats = [at for at, subject, _, _ in sender.sent if subject == "Tracefold 告警（持续） · Signal 连续被拒"]
    assert repeats and all(later - earlier >= wd.REALERT_AFTER_MS for earlier, later in pairwise(repeats))
    latest_repeat = [card for _, subject, card, _ in sender.sent if subject.startswith("Tracefold 告警（持续）")][-1]
    assert "unexpected_exposure ×21 · instrument_busy ×1" in latest_repeat.lead
    resolved = [at for at, subject, _, _ in sender.sent if subject == "Tracefold 已恢复 · Signal 连续被拒"]
    assert len(resolved) == 1 and 0 < resolved[0] - accepted_again <= wd.RESOLVE_AFTER_MS + MINUTE_MS


# -- the message itself -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["onset", "repeat", "resolved"])
def test_the_alert_is_one_card_both_providers_can_send(kind: str) -> None:
    step = wd.AlertStep(
        condition_key=wd.SIGNAL_LANE_FAULTED,
        kind=kind,  # type: ignore[arg-type]
        lines=("trading_signal_lane: faulted · trading_signal_lane:RuntimeError", "第二行"),
        delivered=WatchdogAlertState(wd.SIGNAL_LANE_FAULTED, active=True, opened_at_ms=1),
        undelivered=None,
    )

    card = wd.alert_card(step)
    feishu = feishu_card(card)
    telegram = _telegram_message(card, view=ReaderDeliveryPresentation(), pushed_at_ms=_ms("2026-09-22 18:40"))

    assert feishu["header"]["title"]["content"].endswith("Signal lane 已停止")
    assert "trading_signal_lane:RuntimeError" in feishu["elements"][0]["content"]
    assert "Signal lane 已停止" in telegram and "第二行" in telegram


def test_the_watchdog_is_built_only_beside_trading_and_a_working_sender() -> None:
    class Entry:
        def __init__(self, available: bool) -> None:
            self.available = available

        async def send_prepared_card(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:  # pragma: no cover
            return {}

    def wire(settings: Settings, sender: Any) -> tuple[Any, dict[str, Any]]:
        capabilities = CapabilityStates()
        pipeline = None if sender is None else SimpleNamespace(deliverer=SimpleNamespace(send_entry=sender))
        built = wd.wire_trading_watchdog(
            settings=settings,
            db=object(),  # type: ignore[arg-type]
            capabilities=capabilities,
            news_pipeline=pipeline,  # type: ignore[arg-type]
        )
        return built, capabilities.payload()["trading_watchdog"]

    enabled = Settings.model_validate({"trading": {"enabled": True}})
    assert wire(Settings(), Entry(True)) == (None, {"state": "disabled", "reason": "trading_disabled"})
    muted = Settings.model_validate({"trading": {"enabled": True, "watchdog_enabled": False}})
    assert wire(muted, Entry(True)) == (None, {"state": "disabled", "reason": "trading_watchdog_disabled"})
    unavailable = {"state": "unavailable", "reason": "trading_watchdog_push_unavailable"}
    assert wire(enabled, None) == (None, unavailable)
    assert wire(enabled, Entry(False)) == (None, unavailable)
    built, state = wire(enabled, Entry(True))
    assert isinstance(built, wd.TradingWatchdog)
    assert state == {"state": "running", "reason": None}
