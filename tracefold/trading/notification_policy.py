"""Which execution observation is worth an operator card, and how often — stated once.

Three copies of this predicate drifted apart before #472: the SQL read, the partial index it rides,
and the worker's renderer. The SQL asked `reconciliation` for a `state` key no observation has ever
carried, and asked `readiness` for the control stage the Runtime writes only when it accepts
`/flatten`, so those branches were unreachable for their whole life while the Runtime wrote
`account_flat` and `lifecycle` instead. The vocabulary lives here now: the read carries it into SQL
as parameters and the renderer gates on it, so a Runtime that renames a summary key fails a test
instead of silently emptying the queue.

Frequency is part of the same statement. `reconciliation` arrives every ~30 seconds and `readiness`
once per Runtime start, so both are *coalesced*: only the newest pending observation of that kind is
a candidate, and at most one is sent per `NOTIFICATION_THROTTLE_MS`. Coalescing rather than deferring
is deliberate — a suppressed observation is superseded, never queued, so a card never reports a state
the account has already left. The remaining kinds are rare by construction and are sent one for one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.trading.execution_contracts import ObservationKind

# One card per coalesced kind per half hour. The bound exists for the pathological runs — a crash
# loop restarting the Runtime, or an account that stays unflat for hours — not for the steady state,
# where the notable branches of these kinds do not fire at all.
NOTIFICATION_THROTTLE_MS = 30 * 60_000


@dataclass(frozen=True, slots=True)
class SummaryMatch:
    """One `summary ->> key IN values` test, written in the text `->>` itself produces."""

    key: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KindNotification:
    """When one observation kind is worth a card, and whether its rate needs bounding.

    An empty `matches` means every observation of the kind is notable; the kinds that carry one are
    the kinds whose summaries mix routine and notable states under a single `normalized_kind`.
    """

    matches: tuple[SummaryMatch, ...] = ()
    coalesced: bool = False


# Kinds absent from this mapping are never notified. `protection` and `position` are deliberate
# omissions: what an operator must see about them — the actual fill — is written as `fill` whatever
# the leg, so notifying them too would report one event twice. `risk` is omitted because its only
# fact is the once-a-day `day_start_equity`, which the console already reports.
NOTIFICATION_POLICY: Mapping[ObservationKind, KindNotification] = {
    # The Signal card itself, and the operator's own commands answered. Both are rare and both are
    # addressed to a person who is waiting for them.
    "signal_disposition": KindNotification(),
    "control_disposition": KindNotification(),
    # Money moved, and the audit ledger admitted it lost events. Neither is ever routine.
    "fill": KindNotification(),
    "audit_gap": KindNotification(),
    # Every terminal order status, with no enumeration to keep in step: an order exists only when the
    # Runtime is acting, and listing a subset is exactly the drift that emptied this queue.
    "order": KindNotification(),
    # `runtime_accepted` is `/flatten` being taken up; `started` is the Runtime coming back, which is
    # what tells an operator a deploy or a crash happened underneath their position.
    "readiness": KindNotification(
        matches=(
            SummaryMatch("control_stage", ("runtime_accepted",)),
            SummaryMatch("lifecycle", ("started",)),
        ),
        coalesced=True,
    ),
    # Reconciliation runs on a timer and is flat almost always; exposure is the state worth a card.
    "reconciliation": KindNotification(
        matches=(SummaryMatch("account_flat", ("false",)),),
        coalesced=True,
    ),
}

NOTIFIABLE_KINDS: tuple[str, ...] = tuple(NOTIFICATION_POLICY)
COALESCED_KINDS: tuple[str, ...] = tuple(kind for kind, policy in NOTIFICATION_POLICY.items() if policy.coalesced)


def summary_text(value: Any) -> str | None:
    """What PostgreSQL's `->>` renders for a JSON value, so both readings share one vocabulary."""

    if value is None:
        return None
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def is_notifiable(kind: str, summary: Mapping[str, Any] | None) -> bool:
    """Whether this observation is worth a card, ignoring how recently its kind sent one."""

    policy = NOTIFICATION_POLICY.get(kind)  # type: ignore[call-overload]
    if policy is None:
        return False
    if not policy.matches:
        return True
    values: Mapping[str, Any] = summary if isinstance(summary, Mapping) else {}
    return any(summary_text(values.get(match.key)) in match.values for match in policy.matches)


def notifiable_policy_rows() -> tuple[list[str], list[str], list[str]]:
    """The policy as three parallel arrays: one `(kind, key, value)` triple per way to be notable.

    A kind that is always notable contributes one triple with an empty key, which is how the reading
    side spells "no summary test". `EXISTS` over the triples is the same disjunction `is_notifiable`
    evaluates in Python, so the two readings cannot disagree about a kind without disagreeing here.
    """

    kinds: list[str] = []
    keys: list[str] = []
    values: list[str] = []
    for kind, policy in NOTIFICATION_POLICY.items():
        if not policy.matches:
            kinds.append(kind)
            keys.append("")
            values.append("")
            continue
        for match in policy.matches:
            for value in match.values:
                kinds.append(kind)
                keys.append(match.key)
                values.append(value)
    return kinds, keys, values


__all__ = [
    "COALESCED_KINDS",
    "NOTIFIABLE_KINDS",
    "NOTIFICATION_POLICY",
    "NOTIFICATION_THROTTLE_MS",
    "KindNotification",
    "SummaryMatch",
    "is_notifiable",
    "notifiable_policy_rows",
    "summary_text",
]
