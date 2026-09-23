"""The two pure stage derivations `/api/trading/executions` renders (#528 PR-1)."""

from __future__ import annotations

import pytest

from tracefold.trading.stages import ACCEPTED_ENTRY_DISPOSITIONS, execution_stage

_NOW_NS = 1_900_000_000_000_000_000


def _stage(**overrides: str | int | None) -> str:
    facts: dict[str, str | int | None] = {
        "disposition_reason": None,
        "order_status": None,
        "fill_quantity": None,
        "stop_trigger_price": None,
        "position_status": None,
        "expires_at_ns": None,
        "now_ns": _NOW_NS,
    }
    facts.update(overrides)
    return execution_stage(**facts)  # type: ignore[arg-type]


def test_a_signal_with_no_observation_yet_is_pending() -> None:
    assert _stage() == "pending"
    assert _stage(expires_at_ns=_NOW_NS + 1) == "pending"


def test_a_signal_that_never_got_a_disposition_expires_when_its_own_ttl_passes() -> None:
    """#604 T3 (audit A4). The bridge that offers Signals to the Runtime anti-joins on
    `expires_at_ns > now`, so a Signal refused for a retryable reason -- which writes no durable
    disposition -- stops being offered the instant it expires and never receives one. `pending`
    forever was the desk reading that hole as work still in flight.

    The TTL is the Signal's own published clock and nothing else changes: the row is `pending` right
    up to it, `expired` once past it, and a manual entry, whose Command carries its own TTL and whose
    refusals are always written down, passes `None` and is untouched.
    """

    assert _stage(expires_at_ns=_NOW_NS) == "pending"
    assert _stage(expires_at_ns=_NOW_NS - 1) == "expired"
    # A Signal that did reach the venue is never re-read as expired by the clock: the venue fact wins.
    assert _stage(expires_at_ns=_NOW_NS - 1, order_status="submitted_or_unknown") == "ordered"
    assert _stage(expires_at_ns=_NOW_NS - 1, disposition_reason="entries_paused") == "rejected"
    # A manual entry has no Signal TTL at all.
    assert _stage(expires_at_ns=None) == "pending"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("accepted", "ordered"),
        # `accepted` is written only once the venue took the order (#680); the old query-first words
        # are history rows that also carry the order facts `stage` reads first.
        ("venue_rejected", "rejected"),
        ("post_stop_cooldown", "rejected"),
        ("expired", "expired"),
        ("entries_paused", "rejected"),
        ("instrument_unmapped", "rejected"),
        ("daily_loss_limit", "rejected"),
    ],
)
def test_a_disposition_alone_decides_between_ordered_expired_and_rejected(reason: str, expected: str) -> None:
    assert _stage(disposition_reason=reason) == expected


@pytest.mark.parametrize(
    ("reason", "verdict", "expected"),
    [
        ("accepted", "accepted", "ordered"),
        ("account_slot_mismatch", "rejected", "rejected"),
        ("expired", "rejected", "expired"),
    ],
)
def test_a_manual_entry_reason_derives_the_same_stage_a_signal_reason_does(
    reason: str,
    verdict: str,
    expected: str,
) -> None:
    """#528 PR-3. A manual entry's `control_disposition` carries the same reason word a Signal's does.

    The read model returns that one column for either entry identity, and `stage` is the only word
    derived from it: the published `accepted` / `rejected` split beside it said what `ordered` and
    `rejected` already say about the same row (#537 PR-5). `dispose_command` writes the stored word
    off `ACCEPTED_ENTRY_DISPOSITIONS`, which is the frozenset `execution_stage` reads.
    """

    assert ("accepted" if reason in ACCEPTED_ENTRY_DISPOSITIONS else "rejected") == verdict
    assert _stage(disposition_reason=reason) == expected


def test_the_newest_venue_fact_wins_over_every_earlier_one() -> None:
    """A closed position is closed however it got there; a stop makes an open one `protected`."""

    assert _stage(disposition_reason="accepted", order_status="submitted_or_unknown") == "ordered"
    assert _stage(disposition_reason="accepted", order_status="filled", fill_quantity="0.049") == "filled"
    assert (
        _stage(
            disposition_reason="accepted",
            order_status="filled",
            fill_quantity="0.049",
            stop_trigger_price="9800",
            position_status="opened",
        )
        == "protected"
    )
    assert (
        _stage(
            disposition_reason="accepted",
            order_status="filled",
            fill_quantity="0.049",
            stop_trigger_price="9800",
            position_status="closed",
        )
        == "closed"
    )


def test_an_entry_order_without_its_disposition_row_is_still_ordered() -> None:
    """The order observation and the disposition are two appends; a read between them is not `pending`."""

    assert _stage(order_status="submitted_or_unknown") == "ordered"


def test_a_plan_decides_the_stage_and_a_refused_entry_plan_is_a_rejection_not_a_closed_trade() -> None:
    assert _stage(plan_status="prepared") == "pending"
    assert _stage(plan_status="prepared", order_status="submitted") == "ordered"
    assert _stage(plan_status="open", fill_quantity="0.049") == "filled"
    assert _stage(plan_status="open", stop_trigger_price="9800") == "protected"
    assert _stage(plan_status="closed", exit_reason="stop_filled") == "closed"
    assert _stage(plan_status="closed", exit_reason="venue_unknown") == "closed"
    # #680. A venue that refused the entry order ended the plan before anything traded.
    assert _stage(plan_status="closed", exit_reason="not_submitted", order_status="rejected") == "rejected"
