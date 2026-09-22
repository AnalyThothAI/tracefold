"""The Trading watchdog: read durable facts, alert the operator through the configured push provider.

It exists because every failure the #680 audit found was silent. The Signal lane stopped 35 times and
stayed stopped for up to 31 hours, 391 live OI frames were never answered, the execution Runtime
restarted 110 times a day, and 21 Signals in a row were refused for three and a half days -- and
nothing told anyone (#680 RC11).

**Alert-only.** It reads the Signal lane's capability as this process reports it, the News OI ledger,
the admission ledger and three Runtime facts, and it writes nothing but its own alert ledger
(`platform_watchdog_alerts`). It never pauses, blocks, retries or repairs anything: a watchdog that
could stop trading would be one more thing to fail closed at the wrong moment.

**One message per episode.** A condition is alerted when it starts, again at most every
`REALERT_AFTER_MS` while it lasts, and once more when it has stayed clear for `RESOLVE_AFTER_MS`. A
condition that flaps back inside that hold is the same episode, so a Runtime that restarts every four
minutes is one alert rather than one per restart. The ledger is durable, so a Workers restart neither
re-pages an active condition nor loses the recovery message for one that cleared while it was down.

**Through the existing entry.** Cards leave through the News Deliverer's `InitialSendEntry` -- the one
place a card leaves this process, with its one pacer -- and so through whichever provider the
operator configured, Feishu or Telegram. There is no second channel, sender or credential. With no
working push sender the watchdog is not constructed and its capability says why.

Thresholds are code constants: each is a property of the failure it detects, not an operator tuning
knob, and the one switch is `trading.watchdog_enabled`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

from loguru import logger

from tracefold.app.trading_config import signal_lane_config
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import TRADING_SIGNAL_LANE, TRADING_WATCHDOG, CapabilityState, CapabilityStates
from tracefold.app.workers.watchdog_storage import WatchdogAlertRepository, WatchdogAlertState
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources
from tracefold.news import ReaderCard
from tracefold.news.feishu_card import feishu_card
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.news.reader_card import ReaderCardFacts, ReaderCardHeader, ReaderCardNote
from tracefold.platform.config.models import Settings
from tracefold.platform.resource import ResourceAdmissionTimeout
from tracefold.trading.contracts import OiCandidateRow, oi_source_key
from tracefold.trading.signal_lane import ANSWER_HORIZON_MS
from tracefold.trading.storage.health import OverduePlan, RuntimeLiveness

TRADING_WATCHDOG_TASK_NAME = "trading-watchdog"
WATCHDOG_POLL_SECONDS = 60.0

# A live frame the lane has had this long to answer and has not. The lane answers a fresh frame within
# a turn and sweeps every missed one within a minute of `max_age`, so ten minutes of silence means it is
# not running.
FRAME_ANSWER_BUDGET_MS: Final = 10 * 60_000
RUNTIME_HEARTBEAT_STALE_MS: Final = 60_000
# Starts in the last hour. A deploy is one; the audit's Runtime managed 110 a day.
RUNTIME_STARTS_PER_HOUR_MAX: Final = 3
REFUSAL_STREAK_MIN: Final = 5
PLAN_OVERDUE_GRACE_MS: Final = 15 * 60_000
REALERT_AFTER_MS: Final = 4 * 3_600_000
RESOLVE_AFTER_MS: Final = 10 * 60_000
_DISPOSITION_WINDOW: Final = 50
_OVERDUE_PLANS_MAX: Final = 10
_FRAMES_NAMED_MAX: Final = 5
_READ_TIMEOUT_SECONDS: Final = 10.0
_WRITE_TIMEOUT_SECONDS: Final = 3.0
_HOUR_NS: Final = 3_600_000_000_000

SIGNAL_LANE_FAULTED: Final = "signal_lane_faulted"
OI_FRAMES_UNANSWERED: Final = "oi_frames_unanswered"
RUNTIME_HEARTBEAT_STALE: Final = "runtime_heartbeat_stale"
RUNTIME_RESTART_LOOP: Final = "runtime_restart_loop"
SIGNAL_REFUSAL_STREAK: Final = "signal_refusal_streak"
PLAN_OVERDUE: Final = "plan_overdue"
CONDITION_TITLES: Final[dict[str, str]] = {
    SIGNAL_LANE_FAULTED: "Signal lane 已停止",
    OI_FRAMES_UNANSWERED: "OI 帧没有准入答复",
    RUNTIME_HEARTBEAT_STALE: "执行 Runtime 心跳中断",
    RUNTIME_RESTART_LOOP: "执行 Runtime 频繁重启",
    SIGNAL_REFUSAL_STREAK: "Signal 连续被拒",
    PLAN_OVERDUE: "持仓超过最长持有时间",
}

AlertKind = Literal["onset", "repeat", "resolved"]
_HEADINGS: Final[dict[str, str]] = {
    "onset": "Tracefold 告警",
    "repeat": "Tracefold 告警（持续）",
    "resolved": "Tracefold 已恢复",
}


@dataclass(frozen=True, slots=True)
class WatchdogReads:
    """What one pass read from PostgreSQL."""

    unanswered_frames: tuple[OiCandidateRow, ...] = ()
    runtime: RuntimeLiveness | None = None
    dispositions: tuple[str, ...] = ()
    overdue_plans: tuple[OverduePlan, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchdogFacts:
    """Everything one pass judges, as of `now_ms`. Pure data: `findings` reads nothing else."""

    now_ms: int
    lane: CapabilityState | None
    reads: WatchdogReads
    # Whether a Runtime is configured at all. `execution.mode: disabled` has no Runtime to be silent.
    runtime_expected: bool
    account_slot: str
    runtime_starts_last_hour: int


@dataclass(frozen=True, slots=True)
class Finding:
    condition_key: str
    lines: tuple[str, ...]

    @property
    def title(self) -> str:
        return CONDITION_TITLES[self.condition_key]


def findings(facts: WatchdogFacts) -> dict[str, Finding]:
    """Every watched condition that holds now, keyed by condition. Absent means clear."""

    found: dict[str, Finding] = {}
    now_ms = facts.now_ms
    lane = facts.lane
    if lane is not None and lane.state == "faulted":
        found[SIGNAL_LANE_FAULTED] = Finding(
            SIGNAL_LANE_FAULTED,
            (
                f"{TRADING_SIGNAL_LANE}: faulted · {lane.reason or 'unknown'}",
                "新的 OI 帧不会再被准入；修复原因后重启 Workers。",
            ),
        )
    frames = sorted(facts.reads.unanswered_frames, key=lambda row: (int(row["available_at_ms"] or 0), row["event_id"]))
    if frames:
        oldest = int(frames[0]["available_at_ms"] or 0)
        named = " · ".join(str(row["symbol"]) for row in frames[:_FRAMES_NAMED_MAX])
        more = f" 等 {len(frames)} 个" if len(frames) > _FRAMES_NAMED_MAX else ""
        found[OI_FRAMES_UNANSWERED] = Finding(
            OI_FRAMES_UNANSWERED,
            (
                f"{len(frames)} 个 live OI 帧入账超过 {FRAME_ANSWER_BUDGET_MS // 60_000} 分钟仍没有准入记录",
                f"最早 {_utc(oldest)}（{_duration(now_ms - oldest)}前）：{named}{more}",
            ),
        )
    if facts.runtime_expected:
        runtime = facts.reads.runtime
        if runtime is None:
            found[RUNTIME_HEARTBEAT_STALE] = Finding(
                RUNTIME_HEARTBEAT_STALE,
                (f"{facts.account_slot}：没有执行 Runtime 状态行，Runtime 从未启动或已被移除。",),
            )
        else:
            heartbeat_ms = runtime["heartbeat_at_ns"] // 1_000_000
            if now_ms - heartbeat_ms > RUNTIME_HEARTBEAT_STALE_MS:
                found[RUNTIME_HEARTBEAT_STALE] = Finding(
                    RUNTIME_HEARTBEAT_STALE,
                    (
                        f"{facts.account_slot}：最后心跳 {_utc(heartbeat_ms)}（{_duration(now_ms - heartbeat_ms)}前）",
                        "Runtime 不在运行时，新 Signal 会在 TTL 内过期。",
                    ),
                )
        if facts.runtime_starts_last_hour > RUNTIME_STARTS_PER_HOUR_MAX:
            found[RUNTIME_RESTART_LOOP] = Finding(
                RUNTIME_RESTART_LOOP,
                (
                    f"{facts.account_slot}：近 1 小时启动 {facts.runtime_starts_last_hour} 次"
                    f"（上限 {RUNTIME_STARTS_PER_HOUR_MAX}）",
                    "每次重启都是一次冷重建；查看 nautilus.log 的退出原因。",
                ),
            )
    # Only a configured Runtime writes dispositions. After `execution.mode: disabled` the last streak is
    # history, and repeating it every few hours would page about a Runtime nobody is running.
    streak = _refusal_streak(facts.reads.dispositions) if facts.runtime_expected else []
    if len(streak) >= REFUSAL_STREAK_MIN:
        bound = "≥" if len(streak) == len(facts.reads.dispositions) == _DISPOSITION_WINDOW else ""
        reasons = " · ".join(f"{reason or 'unknown'} ×{count}" for reason, count in Counter(streak).most_common())
        found[SIGNAL_REFUSAL_STREAK] = Finding(
            SIGNAL_REFUSAL_STREAK,
            (f"最近 {bound}{len(streak)} 个 Signal 的处置都不是 accepted", f"拒因：{reasons}"),
        )
    if facts.reads.overdue_plans:
        found[PLAN_OVERDUE] = Finding(
            PLAN_OVERDUE,
            tuple(
                f"{plan['market_key']} {plan['status']}：开仓 {_utc(plan['opened_at_ns'] // 1_000_000)}，"
                f"已持有 {_duration(now_ms - plan['opened_at_ns'] // 1_000_000)}，"
                f"最长 {_duration(plan['max_holding_ns'] // 1_000_000)}"
                for plan in facts.reads.overdue_plans
            ),
        )
    return found


def _refusal_streak(dispositions: Sequence[str]) -> list[str]:
    """The newest run of dispositions that are not `accepted`, newest first."""

    streak: list[str] = []
    for disposition in dispositions:
        if disposition == "accepted":
            break
        streak.append(disposition)
    return streak


@dataclass(frozen=True, slots=True)
class AlertStep:
    """One condition's move this pass: the message to send, if any, and the ledger row either way.

    `delivered` is written when there is no message or the provider accepted it; `undelivered` when the
    send failed, and `None` there means "write nothing, try the same step next pass".
    """

    condition_key: str
    kind: AlertKind | None
    lines: tuple[str, ...]
    delivered: WatchdogAlertState
    undelivered: WatchdogAlertState | None

    @property
    def title(self) -> str:
        return CONDITION_TITLES.get(self.condition_key, self.condition_key)

    @property
    def detail(self) -> str:
        return "\n".join(self.lines)


def plan_alerts(
    found: Mapping[str, Finding],
    states: Mapping[str, WatchdogAlertState],
    *,
    now_ms: int,
) -> list[AlertStep]:
    """The ledger transitions and messages this pass owes, in condition order. Pure."""

    steps: list[AlertStep] = []
    for key in sorted(set(found) | set(states)):
        finding = found.get(key)
        state = states.get(key)
        if finding is not None:
            if state is None or not state.active:
                opened = WatchdogAlertState(condition_key=key, active=True, opened_at_ms=now_ms)
                steps.append(
                    AlertStep(
                        condition_key=key,
                        kind="onset",
                        lines=finding.lines,
                        delivered=replace(opened, notified_at_ms=now_ms),
                        undelivered=opened,
                    )
                )
                continue
            flapped_back = state.clear_since_ms is not None
            if state.notified_at_ms is None or now_ms - state.notified_at_ms >= REALERT_AFTER_MS:
                steps.append(
                    AlertStep(
                        condition_key=key,
                        kind="onset" if state.notified_at_ms is None else "repeat",
                        lines=finding.lines,
                        delivered=replace(state, notified_at_ms=now_ms, clear_since_ms=None),
                        undelivered=replace(state, clear_since_ms=None) if flapped_back else None,
                    )
                )
            elif flapped_back:
                steps.append(_silent(key, finding.lines, replace(state, clear_since_ms=None)))
            continue
        if state is None or not state.active:
            continue
        if state.clear_since_ms is None:
            steps.append(_silent(key, ("clear",), replace(state, clear_since_ms=now_ms)))
            continue
        if now_ms - state.clear_since_ms < RESOLVE_AFTER_MS:
            continue
        closed = replace(state, active=False, clear_since_ms=None)
        if state.notified_at_ms is None:
            # Nobody was told it started, so nobody is told it ended.
            steps.append(_silent(key, ("resolved",), closed))
            continue
        steps.append(
            AlertStep(
                condition_key=key,
                kind="resolved",
                lines=(
                    f"{_utc(state.clear_since_ms)} 起恢复正常，已稳定 {_duration(now_ms - state.clear_since_ms)}",
                    f"问题始于 {_utc(state.opened_at_ms)}，持续 {_duration(state.clear_since_ms - state.opened_at_ms)}",
                ),
                delivered=closed,
                undelivered=None,
            )
        )
    return steps


def _silent(key: str, lines: tuple[str, ...], state: WatchdogAlertState) -> AlertStep:
    return AlertStep(condition_key=key, kind=None, lines=lines, delivered=state, undelivered=None)


def alert_card(step: AlertStep) -> ReaderCard:
    """One step's message as the card model every channel serializes for itself.

    Every time is in the lines, in UTC and with its date, so the card carries no event time of its
    own: the reader card's clock is UTC+8 and minutes only, and one card should not speak two clocks.
    """

    if step.kind is None:
        raise ValueError("trading_watchdog_silent_step_has_no_card")
    return ReaderCard(
        header=ReaderCardHeader(family="news", subject=f"{_HEADINGS[step.kind]} · {step.title}"),
        lead=step.detail,
        facts=ReaderCardFacts(source=("Tracefold", "watchdog")),
        note=ReaderCardNote(id="watchdog"),
    )


class AlertSender(Protocol):
    """The Deliverer's send entry, as the watchdog uses it. `market_notifications` takes the same one."""

    @property
    def available(self) -> bool: ...

    async def send_prepared_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        operation: str,
    ) -> Mapping[str, Any]: ...


class WatchdogDatabase(Protocol):
    async def reads(self, *, now_ms: int, runtime_expected: bool) -> WatchdogReads: ...

    async def alert_states(self) -> dict[str, WatchdogAlertState]: ...

    async def save_alert(self, state: WatchdogAlertState, *, detail: str, now_ms: int) -> None: ...


class TradingWatchdog:
    """One pass reads, judges, and moves each condition's ledger row. `advance()` is the whole API."""

    def __init__(
        self,
        *,
        db: WatchdogDatabase,
        sender: AlertSender,
        capabilities: CapabilityStates,
        runtime_expected: bool,
        account_slot: str,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._db = db
        self._sender = sender
        self._capabilities = capabilities
        self._runtime_expected = runtime_expected
        self._account_slot = account_slot
        self._clock = clock or _now_ms
        # Every Runtime generation this process has seen start within the last hour. The Runtime row
        # holds only the current one, so the count is sampled; a process restart forgets it, which can
        # only under-count (#680 RC11).
        self._runtime_starts: set[int] = set()

    async def advance(self) -> None:
        now_ms = int(self._clock())
        reads = await self._db.reads(now_ms=now_ms, runtime_expected=self._runtime_expected)
        facts = WatchdogFacts(
            now_ms=now_ms,
            lane=self._capabilities.get(TRADING_SIGNAL_LANE),
            reads=reads,
            runtime_expected=self._runtime_expected,
            account_slot=self._account_slot,
            runtime_starts_last_hour=self._count_start(reads.runtime, now_ms=now_ms),
        )
        found = findings(facts)
        for step in plan_alerts(found, await self._db.alert_states(), now_ms=now_ms):
            state = step.delivered if await self._deliver(step) else step.undelivered
            if state is not None:
                await self._db.save_alert(state, detail=step.detail, now_ms=now_ms)

    def _count_start(self, runtime: RuntimeLiveness | None, *, now_ms: int) -> int:
        if runtime is not None:
            self._runtime_starts.add(runtime["started_at_ns"])
        cutoff_ns = now_ms * 1_000_000 - _HOUR_NS
        self._runtime_starts = {started for started in self._runtime_starts if started >= cutoff_ns}
        return len(self._runtime_starts)

    async def _deliver(self, step: AlertStep) -> bool:
        if step.kind is None:
            return True
        card = alert_card(step)
        try:
            await self._sender.send_prepared_card(
                card,
                channel_payload=feishu_card(card),
                operation="trading_watchdog_alert",
            )
        except Exception as exc:
            # A send that failed told nobody: the ledger keeps the step owed and the next pass retries
            # it. The provider being down is not a reason for the watchdog itself to stop.
            logger.warning(
                "trading watchdog alert not delivered condition={} kind={} error={}",
                step.condition_key,
                step.kind,
                type(exc).__name__,
            )
            return False
        logger.info("trading watchdog alert delivered condition={} kind={}", step.condition_key, step.kind)
        return True


class WorkerWatchdogDatabase:
    """`WatchdogDatabase` on ordinary business admission, one short session per question.

    The News frames and the Trading facts are two reads rather than one session: App composes the two
    owners' public reads and never a transaction across both.
    """

    def __init__(self, database: WorkerDatabase, *, oi_metric_version: str, account_slot: str) -> None:
        self._database = database
        self._oi_metric_version = oi_metric_version
        self._account_slot = account_slot

    async def reads(self, *, now_ms: int, runtime_expected: bool) -> WatchdogReads:
        frames = await self._database.run_business(
            "trading_watchdog_oi_frames",
            self._frames,
            now_ms,
            operation_timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        return await self._database.run_business(
            "trading_watchdog_trading_facts",
            self._trading_facts,
            now_ms,
            frames,
            runtime_expected,
            operation_timeout_seconds=_READ_TIMEOUT_SECONDS,
        )

    async def alert_states(self) -> dict[str, WatchdogAlertState]:
        return await self._database.run_business(
            "trading_watchdog_alert_states",
            self._states,
            operation_timeout_seconds=_WRITE_TIMEOUT_SECONDS,
        )

    async def save_alert(self, state: WatchdogAlertState, *, detail: str, now_ms: int) -> None:
        await self._database.run_business(
            "trading_watchdog_alert_save",
            self._save,
            state,
            detail,
            now_ms,
            operation_timeout_seconds=_WRITE_TIMEOUT_SECONDS,
        )

    def _frames(self, now_ms: int) -> list[OiCandidateRow]:
        """Every live frame that became durable inside the lane's answer horizon and is old enough to owe one."""

        with self._database.worker_session("trading_watchdog_oi_frames", _READ_TIMEOUT_SECONDS) as repos:
            return list(
                news_oi_sources(
                    repos,
                    self._oi_metric_version,
                    now_ms - ANSWER_HORIZON_MS,
                    now_ms - FRAME_ANSWER_BUDGET_MS,
                )
            )

    def _trading_facts(self, now_ms: int, frames: list[OiCandidateRow], runtime_expected: bool) -> WatchdogReads:
        keys = [oi_source_key(row["event_id"], row["metric_version"]) for row in frames]
        with self._database.worker_session("trading_watchdog_trading_facts", _READ_TIMEOUT_SECONDS) as repos:
            trading = repos.trading
            answered = trading.gate_answers(source_keys=keys)
            return WatchdogReads(
                unanswered_frames=tuple(row for row, key in zip(frames, keys, strict=True) if key not in answered),
                runtime=trading.runtime_liveness(account_slot=self._account_slot) if runtime_expected else None,
                dispositions=(
                    tuple(trading.recent_signal_dispositions(limit=_DISPOSITION_WINDOW)) if runtime_expected else ()
                ),
                overdue_plans=tuple(
                    trading.overdue_open_plans(
                        now_ns=now_ms * 1_000_000,
                        grace_ns=PLAN_OVERDUE_GRACE_MS * 1_000_000,
                        limit=_OVERDUE_PLANS_MAX,
                    )
                ),
            )

    def _states(self) -> dict[str, WatchdogAlertState]:
        with self._database.worker_session("trading_watchdog_alert_states", _WRITE_TIMEOUT_SECONDS) as repos:
            return WatchdogAlertRepository(repos.conn).states()

    def _save(self, state: WatchdogAlertState, detail: str, now_ms: int) -> None:
        with self._database.worker_session("trading_watchdog_alert_save", _WRITE_TIMEOUT_SECONDS) as repos:
            WatchdogAlertRepository(repos.conn).save(state, detail=detail, now_ms=now_ms)


def wire_trading_watchdog(
    *,
    settings: Settings,
    db: WorkerDatabase,
    capabilities: CapabilityStates,
    news_pipeline: NewsPipeline | None,
) -> TradingWatchdog | None:
    """Construct the watchdog, or record why there is none. It needs Trading and a working push sender.

    It alerts through the Deliverer's send entry, which exists only beside a News pipeline; without
    one, or with one whose sender composition could not build, there is nowhere to alert and the
    capability says `unavailable` rather than running a watchdog nobody can hear.
    """

    if not settings.trading.enabled:
        capabilities.disabled(TRADING_WATCHDOG, "trading_disabled")
        return None
    if not settings.trading.watchdog_enabled:
        capabilities.disabled(TRADING_WATCHDOG, "trading_watchdog_disabled")
        return None
    sender: AlertSender | None = None if news_pipeline is None else news_pipeline.deliverer.send_entry
    if sender is None or not sender.available:
        capabilities.unavailable(TRADING_WATCHDOG, "trading_watchdog_push_unavailable")
        return None
    execution = settings.trading.execution
    watchdog = TradingWatchdog(
        db=WorkerWatchdogDatabase(
            db,
            oi_metric_version=signal_lane_config(settings).oi_metric_version,
            account_slot=execution.account_slot,
        ),
        sender=sender,
        capabilities=capabilities,
        runtime_expected=execution.mode != "disabled",
        account_slot=execution.account_slot,
    )
    capabilities.running(TRADING_WATCHDOG)
    return watchdog


async def run_trading_watchdog(
    watchdog: TradingWatchdog,
    *,
    stop_event: asyncio.Event,
    poll_seconds: float = WATCHDOG_POLL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops.

    A pass the database refused is skipped and the next one runs on schedule: the watchdog judges
    durable state, so a skipped pass loses nothing but a minute. A program error is raised and faults
    `trading_watchdog`, exactly like the Signal lane's.
    """

    while not stop_event.is_set():
        try:
            await watchdog.advance()
        except ResourceAdmissionTimeout as exc:
            logger.warning("trading watchdog pass refused by the database; skipped error={}", exc)
        except Exception:
            logger.exception("trading watchdog pass failed")
            raise
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


def _utc(at_ms: int) -> str:
    return datetime.fromtimestamp(max(0, int(at_ms)) / 1000, tz=UTC).strftime("%m-%d %H:%M UTC")


def _duration(ms: int) -> str:
    minutes = max(0, int(ms)) // 60_000
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"
    return f"{hours // 24} 天 {hours % 24} 小时"


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "CONDITION_TITLES",
    "FRAME_ANSWER_BUDGET_MS",
    "OI_FRAMES_UNANSWERED",
    "PLAN_OVERDUE",
    "REALERT_AFTER_MS",
    "REFUSAL_STREAK_MIN",
    "RESOLVE_AFTER_MS",
    "RUNTIME_HEARTBEAT_STALE",
    "RUNTIME_RESTART_LOOP",
    "RUNTIME_STARTS_PER_HOUR_MAX",
    "SIGNAL_LANE_FAULTED",
    "SIGNAL_REFUSAL_STREAK",
    "TRADING_WATCHDOG_TASK_NAME",
    "WATCHDOG_POLL_SECONDS",
    "AlertStep",
    "Finding",
    "TradingWatchdog",
    "WatchdogFacts",
    "WatchdogReads",
    "WorkerWatchdogDatabase",
    "alert_card",
    "findings",
    "plan_alerts",
    "run_trading_watchdog",
    "wire_trading_watchdog",
]
