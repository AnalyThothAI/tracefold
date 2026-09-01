"""#458 PR-B: the Signal card, its four-hour outcome, and the channel that carries them.

The card is the only place a human sees what the Signal lane decided, during a period when the
strategy is explicitly unproven (#459 returned `NO_CANDIDATE`). So what these pin is not that a
message is sent but that it cannot mislead: a provider figure is never printed as venue truth, a
missing Case is never rendered as a Signal with no reasons, and the outcome is measured from the
first price a taker could actually have had.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tracefold.app.workers.trading_notifications import (
    RESULT_HOLD_MS,
    RESULT_SETTLE_MS,
    TradingNotificationWorker,
    trading_notification_text,
    trading_result_text,
)
from tracefold.integrations.feishu import FeishuDeliveryError, FeishuTradingNotifier

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/0f5c1f52-4d0e-4f77-9d33-6c6e4a7f0f11"
FIVE_MIN_MS = 300_000
ENTRY_MS = 1_788_000_000_000 // FIVE_MIN_MS * FIVE_MIN_MS


def _observation(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": 41,
        "event_id": "e" * 64,
        "runtime_profile_id": "binance_usdm_primary",
        "signal_id": "5" * 64,
        "command_id": None,
        "normalized_kind": "signal_disposition",
        "summary": {"disposition": "accepted"},
        "market_key": "crypto:perp:SKR:USDT",
        "direction": "long",
        "case_id": "c" * 64,
        "policy_decision": "long",
        "policy_reason": "smart_money_long",
        "policy_checks": {
            "policy_id": "trading_alpha_policy_v4",
            "policy_version": "trading_alpha_policy_v4",
            "decision": "long",
            "rule": "smart_money_long",
            "checks": [
                {
                    "check": "oi_change_bps",
                    "operator": ">=",
                    "threshold": "500",
                    "measured": "631",
                    "passed": True,
                },
                {
                    "check": "pre_move_bps",
                    "operator": "<=",
                    "threshold": "600",
                    "measured": "210",
                    "passed": True,
                },
            ],
        },
        "manifest": {
            "primary_trigger": {"venue": "binance.usdm"},
            "contexts": {
                "oi": {
                    "oi_change_bps": 631,
                    "whale_oi_ratio_bps": 8021,
                    "whale_long_profit_bps": 9390,
                    "oi_value_usd": 7_900_000,
                },
                "market": {"pre_move_bps": 210, "pre_move_lookback_ms": 3_600_000},
            },
        },
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- the card body


def test_a_signal_card_states_the_case_checks_it_was_decided_on() -> None:
    """The judgment lines are `policy_checks` read back, threshold and measurement together.

    A card that printed only the outcome would leave the reader comparing it against today's
    configuration, which is exactly the "conflict on a row that passed" #331 removed elsewhere.
    """

    text = trading_notification_text(_observation())
    assert text is not None
    assert "Signal accepted" in text
    assert "policy: trading_alpha_policy_v4 long smart_money_long" in text
    assert "PASS oi_change_bps >= 500 · measured 631" in text
    assert "PASS pre_move_bps <= 600 · measured 210" in text
    assert "market: crypto:perp:SKR:USDT long" in text


def test_a_failed_check_is_printed_as_failed_beside_the_ones_that_passed() -> None:
    row = _observation()
    row["policy_checks"]["checks"][1]["passed"] = False
    row["policy_checks"]["decision"] = "no_trade"
    row["policy_checks"]["rule"] = "move_above_band_chasing"

    text = trading_notification_text(row)
    assert text is not None
    assert "PASS oi_change_bps" in text
    assert "FAIL pre_move_bps <= 600 · measured 210" in text


def test_every_provider_number_is_labelled_as_the_providers() -> None:
    """#459 measured the vendor's five-minute "OI change" as substantially price, not position.

    Printing it beside a venue price without saying whose caliber it is invites precisely the reading
    that measurement disproved, so the label travels with the four numbers or they do not appear.
    """

    text = trading_notification_text(_observation())
    assert text is not None
    vendor_line = next(line for line in text.splitlines() if "OI change +6.31%" in line)
    assert "OpenNews 1019" in vendor_line
    assert "not venue truth" in vendor_line
    assert "whale/OI +80.21%" in vendor_line
    assert "OI value $7.9M" in vendor_line
    # The one venue-truth number on the card says so, and says over what window.
    assert "venue price: +2.10% over 60m before the trigger" in text


def test_a_signal_whose_case_cannot_be_read_says_so_rather_than_looking_reasonless() -> None:
    """A silently reason-free Signal card is indistinguishable from a Signal with no reasons."""

    text = trading_notification_text(_observation(policy_checks=None, manifest=None))
    assert text is not None
    assert "case: unavailable" in text
    assert "policy:" not in text


def test_a_non_signal_observation_carries_no_case_lines() -> None:
    text = trading_notification_text(
        _observation(normalized_kind="fill", summary={}, policy_checks=None, manifest=None)
    )
    assert text is not None
    assert "Fill observed" in text
    assert "case: unavailable" not in text


# ---------------------------------------------------------------- the four-hour outcome


def _bars(count: int, *, start_ms: int = ENTRY_MS, price: float = 100.0, step: float = 0.0) -> list[tuple[int, str]]:
    return [(start_ms + index * FIVE_MIN_MS, f"{price + index * step:.4f}") for index in range(count)]


def test_entry_is_the_close_of_the_bar_the_signal_fell_inside() -> None:
    """The first *close* at or after the Signal, which is normally the bar it landed in the middle of.

    #459 found this is not a detail: measuring the same sample from a close that preceded the trigger
    turned a negative result positive. So the bar that closed before the Signal must never be the
    entry, and the bar the Signal is inside must be -- its close is a price a taker could have had.
    """

    signal_at_ms = ENTRY_MS + 2 * 60_000  # two minutes into the bar that opens at ENTRY_MS
    prices = {
        ENTRY_MS - FIVE_MIN_MS: "50.0000",  # closes at ENTRY_MS, before the Signal: never the entry
        ENTRY_MS: "100.0000",  # closes at ENTRY_MS + 5m, the first close after the Signal
        ENTRY_MS + 12 * FIVE_MIN_MS: "101.0000",  # closes one hour after the entry close
        ENTRY_MS + 48 * FIVE_MIN_MS: "98.0000",  # closes four hours after the entry close
    }
    bars = [
        (open_ms, prices.get(open_ms, "100.0000"))
        for open_ms in (ENTRY_MS - FIVE_MIN_MS + index * FIVE_MIN_MS for index in range(61))
    ]

    text = trading_result_text(
        {"signal_id": "5" * 64, "market_key": "crypto:perp:SKR:USDT", "direction": "long"},
        bars,
        entry_at_ms=signal_at_ms,
    )
    assert text is not None
    assert "entry: 100.0000" in text
    assert "50.0000" not in text
    assert "1H: +1.00%" in text
    assert "4H: -2.00%" in text


def test_an_outcome_with_no_close_after_the_signal_is_not_reported() -> None:
    """Reporting a result nobody could have entered is worse than reporting none."""

    signal_at_ms = ENTRY_MS + 2 * 60_000
    assert (
        trading_result_text(
            {"signal_id": "5" * 64, "market_key": "crypto:perp:SKR:USDT", "direction": "long"},
            # This bar closes exactly at ENTRY_MS, two minutes before the Signal.
            [(ENTRY_MS - FIVE_MIN_MS, "50.0000")],
            entry_at_ms=signal_at_ms,
        )
        is None
    )


def test_a_horizon_the_venue_has_not_reached_reads_pending_rather_than_zero() -> None:
    text = trading_result_text(
        {"signal_id": "5" * 64, "market_key": "crypto:perp:SKR:USDT", "direction": "long"},
        _bars(20, price=100.0),
        entry_at_ms=ENTRY_MS,
    )
    assert text is not None
    assert "1H: +0.00%" in text
    assert "4H: pending" in text


# ---------------------------------------------------------------- the Feishu channel


def _transport(captured: list[dict[str, Any]], *, code: int = 0) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(200, json={"code": code, "msg": "ok"})

    return httpx.MockTransport(handle)


def test_the_feishu_notifier_sends_a_card_and_answers_no_message_id() -> None:
    """A custom-bot webhook has no message id and no edit endpoint; `None` is the honest answer.

    Returning a placeholder would put a number in the receipt that nothing can address, and the
    four-hour outcome would look editable when it is a second message.
    """

    captured: list[dict[str, Any]] = []
    notifier = FeishuTradingNotifier(webhook_url=WEBHOOK, transport=_transport(captured))
    try:
        notifier.prepare()
        assert notifier.send("Tracefold execution: Signal accepted") is None
    finally:
        notifier.close()

    assert len(captured) == 1
    assert "Signal accepted" in captured[0]["body"]
    assert '"msg_type":"interactive"' in captured[0]["body"]


def test_the_feishu_target_identity_comes_from_the_hook_id_and_never_the_secret() -> None:
    """The receipt says where a message went. It must not carry anything that could send another."""

    with_secret = FeishuTradingNotifier(webhook_url=WEBHOOK, signing_secret="s" * 24, transport=_transport([]))
    without_secret = FeishuTradingNotifier(webhook_url=WEBHOOK, transport=_transport([]))
    try:
        assert with_secret.target_sha256 == without_secret.target_sha256
        assert len(with_secret.target_sha256) == 64
    finally:
        with_secret.close()
        without_secret.close()


def test_a_rejected_feishu_call_raises_the_sanitized_delivery_error() -> None:
    notifier = FeishuTradingNotifier(webhook_url=WEBHOOK, transport=_transport([], code=19001))
    try:
        notifier.prepare()
        with pytest.raises(FeishuDeliveryError):
            notifier.send("Tracefold execution: Signal accepted")
    finally:
        notifier.close()


# ---------------------------------------------------------------- the worker's two passes


class _FakeNotifier:
    target_sha256 = "b" * 64

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.prepared = 0

    def prepare(self) -> None:
        self.prepared += 1

    def send(self, text: str) -> int | None:
        self.sent.append(text)
        return None

    def close(self) -> None:
        return None


class _FakeFinite:
    async def run(self, _name: str, function: Any, *args: Any, **_kwargs: Any) -> Any:
        return function(*args)


class _FakeTradingRepos:
    def __init__(self, owner: _FakeDb) -> None:
        self.trading = owner


class _FakeDb:
    """Enough of the trading database for the two passes; every call is recorded in order."""

    def __init__(self, *, observation: dict[str, Any] | None, due: dict[str, Any] | None) -> None:
        self._observation = observation
        self._due = due
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def read(self, _name: str, function: Any, **_kwargs: Any) -> Any:
        return function(_FakeTradingRepos(self))

    async def tx(self, _name: str, function: Any, **_kwargs: Any) -> Any:
        return function(_FakeTradingRepos(self))

    def next_execution_notification(self, target_sha256: str) -> dict[str, Any] | None:
        self.calls.append(("next", {"target": target_sha256}))
        row, self._observation = self._observation, None
        return row

    def append_execution_notification_delivery(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("append", kwargs))
        return dict(kwargs)

    def next_execution_notification_result(self, target_sha256: str, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("result_due", {"target": target_sha256, **kwargs}))
        row, self._due = self._due, None
        return row

    def mark_execution_notification_result(self, **kwargs: Any) -> bool:
        self.calls.append(("result_mark", kwargs))
        return True


async def _bar_reader(_market_key: str, _venue: str, _start_ms: int, _end_ms: int) -> list[tuple[int, str]]:
    bars = _bars(60, price=100.0)
    bars[48] = (ENTRY_MS + 48 * FIVE_MIN_MS, "103.0000")
    return bars


def test_a_webhook_delivery_receipt_records_no_message_id() -> None:
    db = _FakeDb(observation=_observation(), due=None)
    worker = TradingNotificationWorker(db=db, finite=_FakeFinite(), sender=_FakeNotifier())

    assert asyncio.run(worker.advance_once()) == "sent"
    append = next(kwargs for name, kwargs in db.calls if name == "append")
    assert append["message_id"] is None
    assert append["observation_seq"] == 41


def test_the_outcome_pass_sends_a_second_message_and_marks_the_same_receipt() -> None:
    """One Signal, two messages, one receipt. `result_delivered_at_ns` is what M4 counts."""

    now_ns = (ENTRY_MS + RESULT_HOLD_MS + RESULT_SETTLE_MS + 60_000) * 1_000_000
    db = _FakeDb(
        observation=None,
        due={
            "observation_seq": 41,
            "delivered_at_ns": ENTRY_MS * 1_000_000,
            "signal_id": "5" * 64,
            "case_id": "c" * 64,
            "market_key": "crypto:perp:SKR:USDT",
            "direction": "long",
            "signal_observed_at_ns": ENTRY_MS * 1_000_000,
            "manifest": {"primary_trigger": {"venue": "binance.usdm"}},
        },
    )
    sender = _FakeNotifier()
    worker = TradingNotificationWorker(
        db=db, finite=_FakeFinite(), sender=sender, bars=_bar_reader, clock_ns=lambda: now_ns
    )

    assert asyncio.run(worker.advance_result_once()) == "sent"
    assert len(sender.sent) == 1
    assert "Tracefold Signal result" in sender.sent[0]
    assert "4H: +3.00%" in sender.sent[0]
    mark = next(kwargs for name, kwargs in db.calls if name == "result_mark")
    assert mark["observation_seq"] == 41
    # The due bound is the caller's clock minus the holding period and the settling margin, so a card
    # sent four hours ago minus one minute is not yet due.
    due_call = next(kwargs for name, kwargs in db.calls if name == "result_due")
    assert due_call["due_at_or_before_ns"] == now_ns - (RESULT_HOLD_MS + RESULT_SETTLE_MS) * 1_000_000


def test_a_venue_read_that_fails_leaves_the_receipt_unmarked_for_the_next_turn() -> None:
    async def broken(*_args: Any) -> list[tuple[int, str]]:
        raise RuntimeError("venue down")

    db = _FakeDb(
        observation=None,
        due={
            "observation_seq": 41,
            "delivered_at_ns": ENTRY_MS * 1_000_000,
            "signal_id": "5" * 64,
            "case_id": "c" * 64,
            "market_key": "crypto:perp:SKR:USDT",
            "direction": "long",
            "signal_observed_at_ns": ENTRY_MS * 1_000_000,
            "manifest": {"primary_trigger": {"venue": "binance.usdm"}},
        },
    )
    worker = TradingNotificationWorker(db=db, finite=_FakeFinite(), sender=_FakeNotifier(), bars=broken)

    assert asyncio.run(worker.advance_result_once()) == "delivery_unavailable"
    assert [name for name, _ in db.calls] == ["result_due"]
