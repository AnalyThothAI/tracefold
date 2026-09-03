"""The two pure stage derivations `/api/trading/executions` renders (#528 PR-1)."""

from __future__ import annotations

import pytest

from tracefold.trading import command_stage, execution_stage, signal_disposition

_NOW_NS = 1_900_000_000_000_000_000


def _stage(**overrides: str | None) -> str:
    facts: dict[str, str | None] = {
        "disposition_reason": None,
        "order_status": None,
        "fill_quantity": None,
        "stop_trigger_price": None,
        "position_status": None,
    }
    facts.update(overrides)
    return execution_stage(**facts)  # type: ignore[arg-type]


def test_a_signal_with_no_observation_yet_is_pending_and_carries_no_verdict() -> None:
    assert _stage() == "pending"
    assert signal_disposition(None) is None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("accepted", "ordered"),
        ("recovered", "ordered"),
        ("replayed_query_first", "ordered"),
        ("unknown_query_first", "ordered"),
        ("expired", "expired"),
        ("entries_paused", "rejected"),
        ("instrument_unmapped", "rejected"),
        ("daily_loss_limit", "rejected"),
    ],
)
def test_a_disposition_alone_decides_between_ordered_expired_and_rejected(reason: str, expected: str) -> None:
    assert _stage(disposition_reason=reason) == expected


@pytest.mark.parametrize(
    ("reason", "verdict"),
    [("accepted", "accepted"), ("recovered", "accepted"), ("expired", "rejected"), ("entries_paused", "rejected")],
)
def test_the_two_word_verdict_splits_the_one_word_the_runtime_stores(reason: str, verdict: str) -> None:
    assert signal_disposition(reason) == verdict


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


def test_a_command_with_no_disposition_is_recorded_until_its_own_ttl_closes_it() -> None:
    assert (
        command_stage(disposition=None, disposition_reason=None, expires_at_ns=_NOW_NS + 1, now_ns=_NOW_NS)
        == "recorded"
    )
    assert command_stage(disposition=None, disposition_reason=None, expires_at_ns=_NOW_NS, now_ns=_NOW_NS) == "expired"


def test_only_the_runtimes_own_flat_proof_completes_a_flatten() -> None:
    accepted = command_stage(
        disposition="accepted",
        disposition_reason="flatten_pending",
        expires_at_ns=_NOW_NS + 1,
        now_ns=_NOW_NS,
    )
    completed = command_stage(
        disposition="completed",
        disposition_reason="binance_account_flat",
        expires_at_ns=_NOW_NS + 1,
        now_ns=_NOW_NS,
    )
    assert (accepted, completed) == ("accepted", "completed")


def test_a_refused_command_separates_its_own_expiry_from_every_other_refusal() -> None:
    assert (
        command_stage(disposition="rejected", disposition_reason="expired", expires_at_ns=_NOW_NS + 1, now_ns=_NOW_NS)
        == "expired"
    )
    assert (
        command_stage(
            disposition="rejected",
            disposition_reason="account_slot_mismatch",
            expires_at_ns=_NOW_NS + 1,
            now_ns=_NOW_NS,
        )
        == "rejected"
    )
