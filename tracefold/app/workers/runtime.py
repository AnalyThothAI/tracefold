from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import psycopg
from psycopg_pool import PoolTimeout

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

WORKERS_RUNTIME_STALE_AFTER_MS = 15_000
WORKERS_RUNTIME_VERSION = "2"

# One capability: what an operator loses when it faults, and the unit a status reader reports on.
# They live beside the runtime row because they are published through it, and because a status route
# must be able to name one without importing the Trading or News composition that runs it.
NEWS_INGESTION = "news_ingestion"
NEWS_EDITORIAL = "news_editorial"
NEWS_CLAIM_RECALL = "news_claim_recall"
NEWS_DELIVERY = "news_delivery"
NEWS_INSTRUMENTS = "news_instruments"
NEWS_QUOTES = "news_quotes"
# The market notification loop's own key. Named for what it does rather than for the package that
# owns it, because that is what an operator reading `/api/status` is looking for: market alerts are a
# capability of the product, not of News's internal layout (#553 PR-2).
MARKET_NOTIFICATIONS = "market_notifications"
# The Robinhood Chain wallet tape's own key (#572 PR-1). Named for the stream an operator loses when it
# faults -- the followed wallets' on-chain fills -- rather than for the provider behind it, because a
# second chain or a second roster site would not be a second capability.
CHAIN_TAPE = "chain_tape"
# The published follow list's own key (#649 §5.1). It is a capability rather than a detail of the
# tape because an operator who has lost it has lost *who is watched*, while collection, detection and
# pricing all keep working against the last version that was published.
WALLET_ROSTER = "wallet_roster"
WALLET_NET_BUY = "wallet_net_buy"

CapabilityStateName = Literal["running", "faulted", "unavailable", "disabled"]

# What a capability may never confine. Each of these says the shared PostgreSQL layer or a shared
# native permit failed -- not that one business capability's program is wrong -- so recording it as a
# capability fault would hide a process-wide fault behind a green readiness. They stay root fatal,
# which is also the shared DB layer's existing behavior for an error it did not retry.
SHARED_RESOURCE_FAILURES: tuple[type[BaseException], ...] = (
    psycopg.Error,
    PoolTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

LifecycleState = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
]
FatalCode = Literal[
    "startup_failed",
    "child_failed",
    "control_failed",
    "singleton_lost",
    "resource_operation_overrun",
    "graceful_deadline_exceeded",
    "cleanup_failed",
]


@dataclass(frozen=True, slots=True)
class CapabilityState:
    state: CapabilityStateName
    reason: str | None = None


class CapabilityStates:
    """What each Workers capability is doing, kept apart from basic process readiness.

    `ready` answers "does this process still own PostgreSQL and its singleton"; this answers "which
    business capabilities are actually working". Folding the two together is what let one faulted
    lane switch off healthy fact APIs, so they stay two questions with two answers (#553 §6).
    """

    __slots__ = ("_states",)

    def __init__(self) -> None:
        self._states: dict[str, CapabilityState] = {}

    def declare(self, capability: str, state: CapabilityStateName, *, reason: str | None = None) -> None:
        self._states[capability] = CapabilityState(state=state, reason=reason)

    def running(self, capability: str) -> None:
        self.declare(capability, "running")

    def faulted(self, capability: str, reason: str) -> None:
        self.declare(capability, "faulted", reason=reason)

    def unavailable(self, capability: str, reason: str) -> None:
        self.declare(capability, "unavailable", reason=reason)

    def disabled(self, capability: str, reason: str) -> None:
        self.declare(capability, "disabled", reason=reason)

    def get(self, capability: str) -> CapabilityState | None:
        return self._states.get(capability)

    def payload(self) -> dict[str, dict[str, Any]]:
        return {
            name: {"state": current.state, "reason": current.reason} for name, current in sorted(self._states.items())
        }


def workers_runtime_status(
    row: Mapping[str, Any] | None,
    *,
    now_ms: int,
    query_failed: bool = False,
) -> dict[str, Any]:
    if query_failed:
        return _unavailable_runtime("runtime_status_query_failed")
    if row is None:
        return _unavailable_runtime("runtime_missing")
    lifecycle = str(row["lifecycle_state"])
    heartbeat_at_ms = int(row["heartbeat_at_ms"])
    stale = (
        lifecycle in {"starting", "running", "stopping"}
        and int(now_ms) - heartbeat_at_ms > WORKERS_RUNTIME_STALE_AFTER_MS
    )
    state = "stale" if stale else lifecycle
    reason = (
        "runtime_heartbeat_stale"
        if stale
        else {
            "starting": "runtime_starting",
            "running": None,
            "stopping": "runtime_stopping",
            "stopped": "runtime_stopped",
            "failed": "runtime_failed",
        }[lifecycle]
    )
    return {
        "runtime_id": str(row["runtime_id"]),
        "runtime_version": str(row["runtime_version"]),
        "state": state,
        "started_at_ms": int(row["started_at_ms"]),
        "heartbeat_at_ms": heartbeat_at_ms,
        "heartbeat_stale_after_ms": WORKERS_RUNTIME_STALE_AFTER_MS,
        "fatal_code": cast(str | None, row.get("fatal_code")),
        "unavailable_reason": reason,
        # A stale row describes a process that stopped answering, so its last report is not evidence
        # of anything now: a SIGKILLed Workers would otherwise leave a lane reading `running` in
        # PostgreSQL forever. Terminal rows keep their report -- it says what died with the process.
        "capabilities": {} if stale else _capabilities(row.get("capabilities")),
    }


def _unavailable_runtime(reason: str) -> dict[str, Any]:
    return {
        "runtime_id": None,
        "runtime_version": None,
        "state": "unavailable",
        "started_at_ms": None,
        "heartbeat_at_ms": None,
        "heartbeat_stale_after_ms": WORKERS_RUNTIME_STALE_AFTER_MS,
        "fatal_code": None,
        "unavailable_reason": reason,
        "capabilities": {},
    }


def _capabilities(value: object) -> dict[str, dict[str, Any]]:
    """Read back one runtime's capability report. An unreadable report is reported as absent."""

    raw = json.loads(value) if isinstance(value, (str, bytes)) else value
    if not isinstance(raw, Mapping):
        return {}
    report: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("state"), str):
            continue
        reason = entry.get("reason")
        report[str(name)] = {
            "state": str(entry["state"]),
            "reason": str(reason) if isinstance(reason, str) else None,
        }
    return report


__all__ = [
    "CHAIN_TAPE",
    "MARKET_NOTIFICATIONS",
    "NEWS_DELIVERY",
    "NEWS_EDITORIAL",
    "NEWS_INGESTION",
    "NEWS_INSTRUMENTS",
    "NEWS_QUOTES",
    "SHARED_RESOURCE_FAILURES",
    "WALLET_NET_BUY",
    "WORKERS_RUNTIME_STALE_AFTER_MS",
    "WORKERS_RUNTIME_VERSION",
    "CapabilityState",
    "CapabilityStates",
    "FatalCode",
    "LifecycleState",
    "workers_runtime_status",
]
