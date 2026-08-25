"""One trigger, at most one prior counterpart: the point-in-time plan for one scan turn.

The shape this module enforces (#211):

    a new OI frame at T      -> the newest eligible News in [T - news_lookback, T]  -> oi_only | news_oi
    a new News verdict at T  -> the newest eligible OI    in [T - oi_lookback,   T]  -> news_only | news_oi

Three rules carry the whole design, and each of them replaces a specific defect:

* **the trigger owns the cutoff.** Freshness gates the trigger only. A counterpart is not required to
  be fresh — it is required to be at or before the trigger and inside its *own* lookback. Re-checking
  the counterpart against the trigger's 5 m budget is what made the configured 60 m / 30 m windows
  unreachable and `news_oi` all but impossible.
* **nothing later than the trigger may enter.** A counterpart written after the frame is the future,
  and a manifest that can see the future is a backtest that proves nothing.
* **choose the counterpart from the whole candidate set, not from a pre-selected newest row.**
  Reducing each lane to its newest row first meant one newer-but-illegal counterpart — in the future,
  or outside lookback — hid an older row that was perfectly legal.
"""

from __future__ import annotations

from collections.abc import Container, Sequence
from dataclasses import dataclass

from ..contracts import CaseKind, NewsTradeCandidate, OiTradeCandidate, underlying_key
from .eligibility import DEFAULT_ELIGIBILITY, EligibilityPolicy, Funnel, is_fresh_trigger


def attach_news(
    candidate: OiTradeCandidate,
    news: Sequence[NewsTradeCandidate],
    *,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
) -> NewsTradeCandidate | None:
    """The newest eligible News for the same underlying, strictly at or before the OI trigger.

    Point-in-time only: a News verdict written after the frame is the future, and attaching it would
    make every replay of that case unreproducible.
    """

    key = underlying_key(candidate.base_symbol)
    window_start = candidate.observed_at_ms - policy.news_lookback_ms
    matches = [
        item
        for item in news
        if underlying_key(item.base_symbol) == key
        and window_start <= item.verdict_created_at_ms <= candidate.observed_at_ms
    ]
    return max(matches, key=lambda item: item.verdict_created_at_ms) if matches else None


def attach_oi(
    candidate: NewsTradeCandidate,
    signals: Sequence[OiTradeCandidate],
    *,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
) -> OiTradeCandidate | None:
    """The newest qualifying OI frame for the same underlying, strictly at or before the verdict."""

    key = underlying_key(candidate.base_symbol)
    window_start = candidate.verdict_created_at_ms - policy.oi_lookback_ms
    matches = [
        item
        for item in signals
        if underlying_key(item.base_symbol) == key
        and window_start <= item.observed_at_ms <= candidate.verdict_created_at_ms
    ]
    return max(matches, key=lambda item: item.observed_at_ms) if matches else None


@dataclass(frozen=True, slots=True)
class _Plan:
    """One underlying's worth of work, already reduced to one trigger and at most one counterpart."""

    kind: CaseKind
    base_symbol: str
    # The trigger's cutoff. Nothing later than this may enter the manifest.
    observed_at_ms: int
    # The two upstream stages the case row records so latency can be reported without a second store:
    # when the provider fact was observed, and when the verdict that names it became durable (#211).
    source_observed_at_ms: int
    trigger_persisted_at_ms: int
    oi: OiTradeCandidate | None
    news: NewsTradeCandidate | None
    source_key: str
    supplemental: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Trigger:
    """One fresh eligible row offered as the reason a case would exist now."""

    payload: OiTradeCandidate | NewsTradeCandidate

    @property
    def at_ms(self) -> int:
        """The trigger's own cutoff: no counterpart later than this may enter its manifest."""

        payload = self.payload
        return payload.observed_at_ms if isinstance(payload, OiTradeCandidate) else payload.verdict_created_at_ms

    @property
    def rank(self) -> tuple[int, int, str]:
        """Latest wins; an OI frame wins a tie; the source key settles whatever is left.

        The tie rule has to be written down rather than left to whichever row the scan returned
        first, or two identical scans could coalesce to different cases. OI wins because this stage
        is OI-first: its side is deterministic and costs no model call.
        """

        return (self.at_ms, 1 if isinstance(self.payload, OiTradeCandidate) else 0, self.payload.source_key)


def plan_triggers(
    *,
    oi: Sequence[OiTradeCandidate],
    news: Sequence[NewsTradeCandidate],
    now_ms: int,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
    active_underlyings: Container[str] = (),
    underlyings_in_flight: Container[str] = (),
    cased_source_keys: Container[str] = (),
    funnel: Funnel | None = None,
) -> list[_Plan]:
    """Every eligible row in, at most one plan per underlying out. Pure; no clock beyond `now_ms`.

    `oi` and `news` are the *context* sets — everything the projection returned that eligibility
    accepted, however old. The fresh subset of each is what may trigger, and the full sets are what a
    trigger may attach from.
    """

    def count(stage: str, amount: int = 1) -> None:
        if funnel is not None and amount > 0:
            funnel.count(stage, amount)

    def _offer(candidate: OiTradeCandidate | NewsTradeCandidate, at_ms: int, lane: str) -> None:
        if not is_fresh_trigger(at_ms, now_ms=now_ms, policy=policy):
            # Not a rejection: still perfectly good context for a trigger inside the other lookback.
            count(f"{lane}_context_only")
            return
        if candidate.source_key in cased_source_keys:
            # It already produced a case. Without this it would keep winning the coalescing below for
            # the rest of its freshness window and keep being refused at the freeze, so the older
            # trigger it beat would never get a turn — the recovery this scan's whole cursor-free
            # design promises. The unique constraint, not this read, is still the authority.
            count(f"{lane}_already_cased")
            return
        triggers.setdefault(underlying_key(candidate.base_symbol), []).append(_Trigger(payload=candidate))

    triggers: dict[str, list[_Trigger]] = {}
    for signal in oi:
        _offer(signal, signal.observed_at_ms, "oi")
    for item in news:
        _offer(item, item.verdict_created_at_ms, "news")

    plans: list[_Plan] = []
    for key in sorted(triggers):
        if key in active_underlyings:
            count("plan_reject:active_underlying", len(triggers[key]))
            continue
        if key in underlyings_in_flight:
            # A pending or running case for this underlying is already one frozen thesis. A second
            # one would spend another model call to answer the same question about the same issuer.
            count("plan_reject:case_in_flight", len(triggers[key]))
            continue
        ordered = sorted(triggers[key], key=lambda trigger: trigger.rank, reverse=True)
        payload = ordered[0].payload
        plan = (
            _oi_trigger_plan(payload, news, policy=policy)
            if isinstance(payload, OiTradeCandidate)
            else _news_trigger_plan(payload, oi, policy=policy)
        )
        # Only a trigger that ends up in neither slot of the winner's manifest was actually dropped.
        # With one fresh frame and one fresh verdict for the same issuer — the ordinary `news_oi`
        # shape — the loser is inside the winner's lookback by construction and gets attached, so
        # counting it as superseded would report the same row as both a rejection and a survivor.
        # A dropped trigger is coalesced for *this turn*, not retired: nothing durable records it, and
        # the winner drops out of the running as soon as it has produced a case, so a loser still
        # inside `max_age` gets its turn on a later scan. The one exception is a winner that keeps
        # being refused at the freeze for a reason only the instrument catalogue knows
        # (`no_perp_at_signal_venue`); it never writes a case and therefore keeps winning.
        kept = {plan.source_key, *plan.supplemental}
        count(
            "plan_reject:superseded_by_newer_trigger",
            sum(1 for trigger in ordered if trigger.payload.source_key not in kept),
        )
        plans.append(plan)
    for plan in plans:
        count(f"plan_kind:{plan.kind}")
    return sorted(plans, key=lambda plan: plan.observed_at_ms, reverse=True)


def _oi_trigger_plan(
    signal: OiTradeCandidate,
    news: Sequence[NewsTradeCandidate],
    *,
    policy: EligibilityPolicy,
) -> _Plan:
    attached = attach_news(signal, news, policy=policy)
    return _Plan(
        kind="news_oi" if attached is not None else "oi_only",
        base_symbol=signal.base_symbol,
        observed_at_ms=signal.observed_at_ms,
        source_observed_at_ms=signal.observed_at_ms,
        trigger_persisted_at_ms=signal.verdict_created_at_ms,
        oi=signal,
        news=attached,
        source_key=signal.source_key,
        supplemental=(attached.source_key,) if attached is not None else (),
    )


def _news_trigger_plan(
    item: NewsTradeCandidate,
    oi: Sequence[OiTradeCandidate],
    *,
    policy: EligibilityPolicy,
) -> _Plan:
    attached = attach_oi(item, oi, policy=policy)
    return _Plan(
        kind="news_oi" if attached is not None else "news_only",
        base_symbol=item.base_symbol,
        observed_at_ms=item.verdict_created_at_ms,
        # The Event's own open time, not the verdict's: for a News trigger the cutoff *is* the verdict,
        # so the two upstream stages would otherwise collapse and report zero ingest latency. Clamped
        # because `opened_at_ms` is not guaranteed to precede the verdict — a re-opened family, a
        # corrected leader item or provider clock skew can invert them — and a latency report saying
        # work finished before it started is indistinguishable from a real number.
        source_observed_at_ms=min(item.opened_at_ms, item.verdict_created_at_ms),
        trigger_persisted_at_ms=item.verdict_created_at_ms,
        oi=attached,
        news=item,
        source_key=item.source_key,
        supplemental=(attached.source_key,) if attached is not None else (),
    )


__all__ = ["attach_news", "attach_oi", "plan_triggers"]
