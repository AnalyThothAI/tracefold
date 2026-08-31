"""Dormant composition seam for the new OI Runtime.

433-C exposes only a profile-gated disabled-readiness entry. It still has no TradingNode activation;
433-E owns paper/live construction and canonical deployment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tracefold.app.repository_session import RepositorySession
from tracefold.integrations.nautilus.oi_runtime.audit_sink import (
    AuditSink,
    ObservationFactory,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.trading import ExecutionObservationV1, TradeSignalV1
from tracefold.trading.storage.execution_stream import (
    EXECUTION_STREAM_NOTIFY_CHANNEL,
    materialize_execution_observation,
    materialize_trade_signals,
    prepare_execution_observations,
)


@dataclass(frozen=True, slots=True)
class OiRuntimeReadiness:
    mode: Literal["disabled"]
    runtime_profile_id: str
    runtime_release: str
    ready: Literal[False]
    reason: Literal["disabled"]


def run_nautilus(profile: OiRuntimeProfile) -> OiRuntimeReadiness:
    """Return the only reachable 433-B state without constructing a node."""

    if profile.mode != "disabled":
        raise RuntimeError("oi_runtime_activation_not_available_before_433e")
    return OiRuntimeReadiness(
        mode="disabled",
        runtime_profile_id=profile.profile_id,
        runtime_release=profile.runtime_release,
        ready=False,
        reason="disabled",
    )


def load_unresolved_trade_signals(
    repos: RepositorySession,
    runtime_profile_id: str,
    execution_strategy: str,
    limit: int,
) -> tuple[TradeSignalV1, ...]:
    """Materialize Trading-owned rows at the App composition boundary."""

    rows = repos.trading.unresolved_trade_signals(
        runtime_profile_id=runtime_profile_id,
        execution_strategy=execution_strategy,
        limit=limit,
    )
    return materialize_trade_signals(rows)


def execution_stream_channel() -> str:
    """Return the Trading-owned LISTEN wake channel to the PostgreSQL adapter."""

    return EXECUTION_STREAM_NOTIFY_CHANNEL


def flush_audit_once(
    *,
    repos: RepositorySession,
    audit: AuditSink,
    signals: ExecutionSignalClient,
) -> int:
    """Background-only durable append; no Strategy callback can reach this function."""

    def writer(values: Sequence[ExecutionObservationV1]) -> None:
        prepared = prepare_execution_observations(values)
        with repos.transaction():
            repos.trading.append_execution_observations(prepared)

    flushed = audit.flush_once(writer)
    for value in flushed:
        if value.normalized_kind == "signal_disposition" and value.signal_id is not None:
            signals.mark_durable(value.signal_id)
    return len(flushed)


def load_or_record_day_start(
    *,
    repos: RepositorySession,
    factory: ObservationFactory,
    utc_day: str,
    equity_usd: Decimal,
    recorded_at_ns: int,
) -> DayStartBaseline:
    """Recover the immutable daily baseline before considering new exposure."""

    event_id = factory.day_start_event_id(utc_day)
    stored = repos.trading.execution_observation(event_id)
    if stored is not None:
        return day_start_baseline_from_observation(materialize_execution_observation(stored))
    baseline, observation = factory.day_start_baseline(
        utc_day=utc_day,
        equity_usd=equity_usd,
        recorded_at_ns=recorded_at_ns,
    )
    prepared = prepare_execution_observations((observation,))
    with repos.transaction():
        repos.trading.append_execution_observations(prepared)
    return baseline


__all__ = ["OiRuntimeReadiness", "run_nautilus"]
