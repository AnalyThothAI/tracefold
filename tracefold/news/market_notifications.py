"""One market notification loop: three rule branches, a PostgreSQL to-do list, one send entry.

Everything #553 §4 asks for is a direct branch here. There is no Policy object, no Strategy registry,
no per-symbol task or timer, no model: OI, liquidation and smart money are three known shapes with
three written rules, and a fourth branch prints the provider's own line when nothing could be proved.
An abstraction over three branches would need a consumer this repository does not have.

Two durable states, each with one owner (§5.1):

* `news_market_tracks` answers *when is this group worth interrupting a reader again* -- the last
  observation, the anchor the last card actually covered, the current action, the next due time.
* `news_market_deliveries` answers *what happened to one card* -- a stable `delivery_key`, the frozen
  snapshot, the attempts, the receipt or the error.

Neither is a second copy of the facts. The observations a card covers are the Items that carry its
`market_notify_delivery_key`, so "which observations did this card speak for" is answered by the Items
themselves and never drifts from them.

What the loop deliberately does not promise: exactly-once external delivery. A confirmed
`delivery_key` is never executed twice, and a send whose result this process could not read is
`unknown` rather than `sent` or `failed` -- the provider may well have it. Saying so is the honest
form of the guarantee; a retry that assumed "no receipt" meant "not sent" would double-notify.

Clocks: every window and every reset here is measured on the host's own receive/process stamps
(`news_items.observed_at_ms` and this process's wall clock). The provider's event time is carried on
the card because a reader wants it, and is never compared against a host stamp to gate anything.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from . import card_format as fmt
from .delivery_contracts import COMMIT_PHASE_NOT_SENT
from .feishu_card import feishu_card
from .reader_card import (
    ReaderCard,
    ReaderCardAction,
    ReaderCardFacts,
    ReaderCardHeader,
    ReaderCardLink,
    ReaderCardMarket,
    ReaderCardNote,
    ReaderCardTimes,
)

# --- v1 engineering defaults (#553 §4). Not tuned parameters, and not claimed to be optimal. ---

# The work tick. A cadence for the loop, not an SLA on the provider's network or on the sender.
TICK_SECONDS: Final = 2.0
# One turn's intake bound. A backlog above it is carried to the next turn, never dropped.
BACKLOG_BATCH_MAX: Final = 100
# How many due cards one turn may send, one at a time. A bound on how long a turn holds the shared
# send entry, not a discard rule: what is still due is sent by the next turn.
SENDS_PER_TURN_MAX: Final = 20

# An OI group with no live observation for four hours starts a fresh round: the next observation is a
# first card again. This manages alerting only; it is not a claim that the market went quiet.
OI_QUIET_RESET_MS: Final = 4 * 60 * 60_000
# A follow-up needs the absolute change to reach twice the anchor's -- the observation the last card
# covered, never a running high-water mark.
OI_FOLLOWUP_MULTIPLE: Final = 2
# Liquidation and smart-money follow-ups share one window, anchored at the start of the last send
# attempt so a stream of new reports cannot push it further away.
LIQUIDATION_WINDOW_MS: Final = 60_000
SMART_MONEY_WINDOW_MS: Final = 60_000

# A provably-not-sent retryable failure is retried on the same intent: at most three real attempts,
# waiting these gaps in PostgreSQL rather than holding the send entry.
SEND_RETRY_BACKOFF_MS: Final = (5_000, 30_000)
SEND_ATTEMPTS_MAX: Final = 3

# How much of one card stays bounded. The full timeline is always on the detail page.
CARD_METRIC_LINES_MAX: Final = 4

MarketFamily = Literal["oi", "liquidation", "smart_money", "raw"]
TriggerReason = Literal["first", "followup", "action_change", "raw"]
DeliveryState = Literal["pending", "sending", "sent", "failed", "unknown", "unavailable"]
TRIGGER_REASONS: Final[tuple[TriggerReason, ...]] = ("first", "followup", "action_change", "raw")
DELIVERY_STATES: Final[tuple[DeliveryState, ...]] = (
    "pending",
    "sending",
    "sent",
    "failed",
    "unknown",
    "unavailable",
)
# The Item's own processing marker. `pending` is the take predicate, `historical` is the backlog and
# the recovery frames that are readable but never alerted, `processed` is everything the loop has
# grouped -- whether or not a card ever covered it.
NOTIFY_STATE_PENDING: Final = "pending"
NOTIFY_STATE_HISTORICAL: Final = "historical"
NOTIFY_STATE_PROCESSED: Final = "processed"
NOTIFY_STATES: Final[tuple[str, ...]] = (
    NOTIFY_STATE_PENDING,
    NOTIFY_STATE_HISTORICAL,
    NOTIFY_STATE_PROCESSED,
)

# What the page says when no attempt has been made. These are reasons, not verdicts: every one of
# them names the rule that is currently holding the observation, so "why was I not told" has an
# answer that does not require reading this file.
REASON_UNPROCESSED: Final = "awaiting_market_loop"
REASON_HISTORICAL: Final = "historical_not_alerted"
REASON_MERGING: Final = "merging_into_prepared_card"
REASON_OI_BELOW_THRESHOLD: Final = "oi_change_below_followup_threshold"
REASON_OI_ZERO_UNCHANGED: Final = "oi_anchor_zero_and_unchanged"
REASON_LIQUIDATION_WINDOW: Final = "liquidation_followup_window_open"
REASON_SMART_MONEY_WINDOW: Final = "smart_money_followup_window_open"
REASON_SENDER_UNAVAILABLE: Final = "market_sender_unavailable"
REASON_SEND_INTERRUPTED: Final = "market_send_interrupted"

__all__ = [
    "BACKLOG_BATCH_MAX",
    "CARD_METRIC_LINES_MAX",
    "DELIVERY_STATES",
    "DETAIL_BUTTON_LABEL",
    "LIQUIDATION_WINDOW_MS",
    "MARKET_TRACK_FIELDS",
    "NOTIFY_STATES",
    "NOTIFY_STATE_HISTORICAL",
    "NOTIFY_STATE_PENDING",
    "NOTIFY_STATE_PROCESSED",
    "OI_FOLLOWUP_MULTIPLE",
    "OI_QUIET_RESET_MS",
    "REASON_HISTORICAL",
    "REASON_LIQUIDATION_WINDOW",
    "REASON_MERGING",
    "REASON_OI_BELOW_THRESHOLD",
    "REASON_OI_ZERO_UNCHANGED",
    "REASON_SENDER_UNAVAILABLE",
    "REASON_SEND_INTERRUPTED",
    "REASON_SMART_MONEY_WINDOW",
    "REASON_UNPROCESSED",
    "SENDS_PER_TURN_MAX",
    "SEND_ATTEMPTS_MAX",
    "SEND_RETRY_BACKOFF_MS",
    "SMART_MONEY_WINDOW_MS",
    "TICK_SECONDS",
    "TRIGGER_REASONS",
    "ClaimedCard",
    "GroupTurn",
    "IntentPlan",
    "MarketNotificationDatabasePort",
    "MarketNotificationLoop",
    "MarketObservation",
    "MarketTrack",
    "MarketTurn",
    "PreparedCardSender",
    "SendOutcome",
    "action_changes",
    "classify_send_failure",
    "decide_group",
    "delivery_key",
    "group_family",
    "group_identity",
    "market_detail_url",
    "market_reader_card",
    "notification_status",
    "render_market_card",
    "retry_delay_ms",
    "split_by_group",
]


@dataclass(frozen=True, slots=True)
class MarketObservation:
    """One stored provider record, in the fields the rules and the card actually read.

    Exactly the projection `MarketStorage` already serves the read model, so the rule engine and the
    page cannot disagree about what a record says.
    """

    item_id: str
    market_kind: str
    parse_status: str
    title: str
    event_at_ms: int
    received_at_ms: int
    provider: str | None = None
    source_strategy_id: str | None = None
    source_venue: str | None = None
    raw_instrument: str | None = None
    symbol: str | None = None
    measurement_definition: str | None = None
    direction: str | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    liquidated_position_side: str | None = None
    notional_usd: str | None = None
    price: str | None = None
    trader_label: str | None = None
    account_address: str | None = None
    action: str | None = None
    position_side: str | None = None
    pnl_usd: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MarketObservation:
        return cls(
            item_id=str(row["item_id"]),
            market_kind=str(row["market_kind"]),
            parse_status=str(row["parse_status"]),
            title=str(row.get("title") or ""),
            event_at_ms=int(row["event_at_ms"]),
            received_at_ms=int(row["received_at_ms"]),
            provider=_text(row.get("provider")),
            source_strategy_id=_text(row.get("source_strategy_id")),
            source_venue=_text(row.get("source_venue")),
            raw_instrument=_text(row.get("raw_instrument")),
            symbol=_text(row.get("symbol")),
            measurement_definition=_text(row.get("measurement_definition")),
            direction=_text(row.get("direction")),
            oi_change_bps=_integer(row.get("oi_change_bps")),
            oi_value_usd=_integer(row.get("oi_value_usd")),
            liquidated_position_side=_text(row.get("liquidated_position_side")),
            notional_usd=_text(row.get("notional_usd")),
            price=_text(row.get("price")),
            trader_label=_text(row.get("trader_label")),
            account_address=_text(row.get("account_address")),
            action=_text(row.get("action")),
            position_side=_text(row.get("position_side")),
            pnl_usd=_text(row.get("pnl_usd")),
        )


@dataclass(frozen=True, slots=True)
class MarketTrack:
    """One notification group's alerting state. Never a second copy of an observation.

    `anchor_state` is the whole of what the last attempt proved. `sent` means a reader has the card
    and the next follow-up is measured from it; `unknown` means the provider may have it and this
    process could not read the answer, so the snapshot is kept as an anti-duplicate reference but is
    never reported as delivered. An empty string means nothing has been delivered for this group yet,
    which is also where an explicit failure leaves it -- a failed card told no one, so the next
    observation opens a fresh first card rather than a follow-up to a card nobody saw.
    """

    group_key: str
    market_kind: str
    family: str
    provider: str | None = None
    source_venue: str | None = None
    venue_known: bool = False
    raw_instrument: str | None = None
    symbol: str | None = None
    measurement_definition: str | None = None
    liquidated_position_side: str | None = None
    account_key: str | None = None
    account_verified: bool = False
    trader_label: str | None = None
    current_action: str | None = None
    current_position_side: str | None = None
    last_observed_at_ms: int = 0
    last_observed_item_id: str = ""
    anchor_state: str = ""
    anchor_delivery_key: str | None = None
    anchor_attempt_at_ms: int | None = None
    anchor_oi_change_bps: int | None = None
    anchor_direction: str | None = None
    # What the last delivered card ended on, which is where the next card's action timeline starts.
    # Not `current_action`: that is the newest observation, written every turn, and by the time a
    # change card is claimed it already holds the new action -- so counting changes against it would
    # report zero for exactly the card whose subject is the change (§4.4).
    anchor_action: str | None = None
    anchor_position_side: str | None = None
    open_delivery_key: str | None = None
    next_due_at_ms: int | None = None
    pending_reason: str = ""


@dataclass(frozen=True, slots=True)
class IntentPlan:
    """One un-started card this turn wants to exist. At most one per group (§4.5)."""

    reason: TriggerReason
    trigger_item_id: str
    due_at_ms: int


@dataclass(frozen=True, slots=True)
class GroupTurn:
    """What one group's turn decided: its new track, and at most one new intent.

    An intent here can also *replace* the group's un-started one, which is how an action change ends
    a merging segment without waiting out the window it was merging into (§4.4).
    """

    track: MarketTrack
    intent: IntentPlan | None = None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    return None if value is None else int(value)


def group_family(observation: MarketObservation) -> MarketFamily:
    """Which rule branch owns this record, decided by the typed fact it actually has.

    The same order the read model's group key uses, so one record is never in an OI group on the page
    and a raw group in the loop. A record whose template could not be proved is `raw` whatever
    Strategy reported it: an unparsed OI line carries no number to compare.
    """

    if observation.parse_status != "parsed":
        return "raw"
    if observation.oi_change_bps is not None and observation.direction and observation.measurement_definition:
        return "oi"
    if observation.liquidated_position_side:
        return "liquidation"
    if observation.action and observation.position_side and (observation.trader_label or observation.account_address):
        return "smart_money"
    return "raw"


def group_identity(observation: MarketObservation) -> MarketTrack:
    """The group this record belongs to, and the group's own descriptive fields.

    The account identity is the source's stable address when it gave one, and otherwise the source
    label group `provider + strategy_id + trader_label` -- carried with `account_verified = False`,
    because a display label is not a verified on-chain address and a page that showed them alike
    would be claiming something the provider never said (§4.4).

    A record with no trustworthy group field is its own group. Two records that only agree on being
    unreadable are not the same subject, and merging them into one shared `unknown` bucket would
    invent a group (§4.1.6).
    """

    family = group_family(observation)
    venue = observation.source_venue
    if family == "oi":
        key = "|".join(
            (
                "oi",
                observation.provider or "",
                venue or "",
                observation.raw_instrument or "",
                observation.measurement_definition or "",
            )
        )
        return MarketTrack(
            group_key=key,
            market_kind=observation.market_kind,
            family=family,
            provider=observation.provider,
            source_venue=venue,
            venue_known=venue is not None,
            raw_instrument=observation.raw_instrument,
            symbol=observation.symbol,
            measurement_definition=observation.measurement_definition,
        )
    if family == "liquidation":
        key = "|".join(
            (
                "liquidation",
                observation.provider or "",
                venue or "",
                observation.raw_instrument or "",
                observation.liquidated_position_side or "",
            )
        )
        return MarketTrack(
            group_key=key,
            market_kind=observation.market_kind,
            family=family,
            provider=observation.provider,
            source_venue=venue,
            venue_known=venue is not None,
            raw_instrument=observation.raw_instrument,
            symbol=observation.symbol,
            liquidated_position_side=observation.liquidated_position_side,
        )
    if family == "smart_money":
        verified = observation.account_address is not None
        account = observation.account_address or "|".join(
            (
                observation.provider or "",
                observation.source_strategy_id or "",
                observation.trader_label or "",
            )
        )
        # The action and the side are deliberately *not* in the key. They are what a segment of this
        # group is doing, and a key that carried them would make "Close Short became Open Short" two
        # unrelated groups -- exactly the change the reader is owed a card about (§4.4).
        key = "|".join(
            (
                "smart_money",
                "address" if verified else "label",
                account,
                venue or "",
                observation.raw_instrument or "",
            )
        )
        return MarketTrack(
            group_key=key,
            market_kind=observation.market_kind,
            family=family,
            provider=observation.provider,
            source_venue=venue,
            venue_known=venue is not None,
            raw_instrument=observation.raw_instrument,
            symbol=observation.symbol,
            account_key=account,
            account_verified=verified,
            trader_label=observation.trader_label,
        )
    return MarketTrack(
        group_key="|".join(("raw", observation.market_kind, observation.item_id)),
        market_kind=observation.market_kind,
        family="raw",
        provider=observation.provider,
        source_venue=venue,
        venue_known=venue is not None,
        raw_instrument=observation.raw_instrument,
        symbol=observation.symbol,
    )


def split_by_group(observations: Iterable[MarketObservation]) -> list[tuple[MarketTrack, list[MarketObservation]]]:
    """One turn's intake, split into groups, each group's records in host receive order.

    Groups keep the order their oldest record arrived in, so a turn is deterministic and a replay of
    the same intake produces the same cards in the same order.
    """

    grouped: dict[str, tuple[MarketTrack, list[MarketObservation]]] = {}
    for observation in observations:
        identity = group_identity(observation)
        entry = grouped.get(identity.group_key)
        if entry is None:
            grouped[identity.group_key] = (identity, [observation])
        else:
            entry[1].append(observation)
    for _, members in grouped.values():
        members.sort(key=lambda item: (item.received_at_ms, item.item_id))
    return list(grouped.values())


def delivery_key(group_key: str, trigger_item_id: str, reason: str) -> str:
    """One card's stable identity: its group, the record that triggered it, and why.

    Deterministic on purpose. A merge, a retry and a restart all recompute the same key, so a card
    this process already confirmed can never be executed a second time, and a crash between the send
    and the receipt leaves a row that the next process recognises rather than duplicates.
    """

    digest = hashlib.sha256("\x1f".join((group_key, trigger_item_id, reason)).encode()).hexdigest()
    return digest[:32]


def retry_delay_ms(attempts: int) -> int | None:
    """The wait after `attempts` real attempts, or None when the budget is spent.

    Three real attempts, waiting 5 s and then 30 s. The waits are returned rather than slept: they
    become a due time in PostgreSQL, so a retry costs this process nothing and survives its restart.
    """

    if attempts < 1 or attempts >= SEND_ATTEMPTS_MAX:
        return None
    return SEND_RETRY_BACKOFF_MS[attempts - 1]


def decide_group(
    track: MarketTrack | None,
    identity: MarketTrack,
    observations: Sequence[MarketObservation],
    *,
    now_ms: int,
    has_open_intent: bool,
) -> GroupTurn:
    """One group's turn: the new track state, and at most one new un-started card.

    `has_open_intent` is the whole of the "at most one un-started intent" rule (§4.5). While one
    exists, every new observation merges into it -- the loop assigns them its `delivery_key` -- and no
    second "first" card is invented. What the arriving records can still do is pull that card's due
    time forward, which is how an action change ends a merging segment without waiting out a window.
    """

    if not observations:
        return GroupTurn(track=track or identity)
    current = _carry(track, identity)
    if identity.family == "oi":
        return _decide_oi(current, observations, now_ms=now_ms, has_open_intent=has_open_intent)
    if identity.family == "liquidation":
        return _decide_liquidation(current, observations, now_ms=now_ms, has_open_intent=has_open_intent)
    if identity.family == "smart_money":
        return _decide_smart_money(current, observations, now_ms=now_ms, has_open_intent=has_open_intent)
    return _decide_raw(current, observations, now_ms=now_ms, has_open_intent=has_open_intent)


def _carry(track: MarketTrack | None, identity: MarketTrack) -> MarketTrack:
    """Keep the alerting state, refresh the descriptive fields from the newest record."""

    if track is None:
        return identity
    return replace(
        identity,
        last_observed_at_ms=track.last_observed_at_ms,
        last_observed_item_id=track.last_observed_item_id,
        anchor_state=track.anchor_state,
        anchor_delivery_key=track.anchor_delivery_key,
        anchor_attempt_at_ms=track.anchor_attempt_at_ms,
        anchor_oi_change_bps=track.anchor_oi_change_bps,
        anchor_direction=track.anchor_direction,
        anchor_action=track.anchor_action,
        anchor_position_side=track.anchor_position_side,
        current_action=track.current_action,
        current_position_side=track.current_position_side,
        open_delivery_key=track.open_delivery_key,
        next_due_at_ms=track.next_due_at_ms,
        pending_reason=track.pending_reason,
    )


def _observed(track: MarketTrack, observation: MarketObservation) -> MarketTrack:
    return replace(
        track,
        last_observed_at_ms=observation.received_at_ms,
        last_observed_item_id=observation.item_id,
    )


def _decide_oi(
    track: MarketTrack,
    observations: Sequence[MarketObservation],
    *,
    now_ms: int,
    has_open_intent: bool,
) -> GroupTurn:
    """§4.2. A first card per round, then a follow-up when the change doubles or turns.

    The anchor is the observation the last delivered card covered, never a running maximum: after a
    6 % card, `9 %` is not twice 6 % and `13 %` is, which is why `6 → 9 → 13` is two cards and
    `6 → 6.1 → 6.2` is one. Records that arrive before the first card has started sending merge into
    it, so the same three numbers arriving together are one card covering all three -- there is no
    second card to "restore" the example's shape, because nobody was told the first number yet.
    """

    intent: IntentPlan | None = None
    reason = ""
    for observation in observations:
        quiet_reset = (
            track.last_observed_at_ms > 0
            and observation.received_at_ms - track.last_observed_at_ms >= OI_QUIET_RESET_MS
        )
        track = _observed(track, observation)
        if has_open_intent or intent is not None:
            reason = REASON_MERGING
            continue
        if track.anchor_state == "" or quiet_reset:
            intent = IntentPlan("first", observation.item_id, now_ms)
            continue
        hold = _oi_hold(track, observation)
        if hold is None:
            intent = IntentPlan("followup", observation.item_id, now_ms)
        else:
            reason = hold
    return GroupTurn(track=replace(track, pending_reason=reason if intent is None else REASON_MERGING), intent=intent)


def _oi_hold(track: MarketTrack, observation: MarketObservation) -> str | None:
    """The reason this observation earns no follow-up, or None when it does."""

    anchor = track.anchor_oi_change_bps
    if anchor is None:
        return None
    if observation.direction != track.anchor_direction:
        return None
    change = abs(int(observation.oi_change_bps or 0))
    if abs(anchor) == 0:
        return None if change != 0 else REASON_OI_ZERO_UNCHANGED
    return None if change >= OI_FOLLOWUP_MULTIPLE * abs(anchor) else REASON_OI_BELOW_THRESHOLD


def _decide_liquidation(
    track: MarketTrack,
    observations: Sequence[MarketObservation],
    *,
    now_ms: int,
    has_open_intent: bool,
) -> GroupTurn:
    """§4.3. First report immediately, then at most one follow-up per 60 s window.

    The window is anchored at the *start of the last send attempt*, not at the newest record, so a
    steady stream of reports cannot push the follow-up further away for ever. The follow-up card is
    only created when a record arrived to put on it, which is the whole of "no empty card".
    """

    for observation in observations:
        track = _observed(track, observation)
    if has_open_intent:
        return GroupTurn(track=replace(track, pending_reason=REASON_MERGING))
    trigger = observations[0].item_id
    window_end = _window_end(track, LIQUIDATION_WINDOW_MS)
    # A card nobody received leaves the anchor empty, so the next one is a first card again even
    # though an attempt was made: the reason names what the reader is about to be told, not what this
    # process tried to do.
    reason: TriggerReason = "first" if track.anchor_state == "" else "followup"
    if window_end is None or now_ms >= window_end:
        return GroupTurn(
            track=replace(track, pending_reason=REASON_MERGING, next_due_at_ms=now_ms),
            intent=IntentPlan(reason, trigger, now_ms),
        )
    return GroupTurn(
        track=replace(track, pending_reason=REASON_LIQUIDATION_WINDOW, next_due_at_ms=window_end),
        intent=IntentPlan(reason, trigger, window_end),
    )


def _decide_smart_money(
    track: MarketTrack,
    observations: Sequence[MarketObservation],
    *,
    now_ms: int,
    has_open_intent: bool,
) -> GroupTurn:
    """§4.4. One account's stream, merged while it keeps doing the same thing.

    A change of action or side ends the merging segment at once: the reader is told that the account
    that was closing shorts is now opening them, without waiting out the 60 s window the previous
    reports were merging into. Accounts never suppress one another -- they are different groups.
    """

    changed = False
    change_trigger = observations[0].item_id
    for observation in observations:
        track = _observed(track, observation)
        if track.current_action is not None and (
            observation.action != track.current_action or observation.position_side != track.current_position_side
        ):
            if not changed:
                change_trigger = observation.item_id
            changed = True
        track = replace(track, current_action=observation.action, current_position_side=observation.position_side)
    if has_open_intent and not changed:
        return GroupTurn(track=replace(track, pending_reason=REASON_MERGING))
    if changed:
        # The un-notified activity of the old segment and the new action belong on one ordered
        # summary. When a card was already merging that segment this *replaces* it rather than
        # arriving beside it: still one un-started card per group, and it says what it actually is.
        # The loop discards the card it replaces -- never attempted, so it told nobody.
        return GroupTurn(
            track=replace(track, pending_reason=REASON_MERGING, next_due_at_ms=now_ms),
            intent=IntentPlan("action_change", change_trigger, now_ms),
        )
    window_end = _window_end(track, SMART_MONEY_WINDOW_MS)
    trigger = observations[0].item_id
    reason: TriggerReason = "first" if track.anchor_state == "" else "followup"
    if window_end is None or now_ms >= window_end:
        return GroupTurn(
            track=replace(track, pending_reason=REASON_MERGING, next_due_at_ms=now_ms),
            intent=IntentPlan(reason, trigger, now_ms),
        )
    return GroupTurn(
        track=replace(track, pending_reason=REASON_SMART_MONEY_WINDOW, next_due_at_ms=window_end),
        intent=IntentPlan(reason, trigger, window_end),
    )


def _decide_raw(
    track: MarketTrack,
    observations: Sequence[MarketObservation],
    *,
    now_ms: int,
    has_open_intent: bool,
) -> GroupTurn:
    """A record whose template could not be proved gets its own card, outside every suppression rule.

    A `Withdraw`, a new provider action word or a drifted format is exactly the thing a reader most
    needs to see, and the Open/Close window would swallow it. The group is the record itself, so one
    record is one card and two unreadable records never merge into one (§4.4, §4.1.6).
    """

    for observation in observations:
        track = _observed(track, observation)
    if has_open_intent or track.anchor_state != "":
        return GroupTurn(track=replace(track, pending_reason=REASON_MERGING))
    return GroupTurn(
        track=replace(track, pending_reason=REASON_MERGING, next_due_at_ms=now_ms),
        intent=IntentPlan("raw", observations[0].item_id, now_ms),
    )


def _window_end(track: MarketTrack, window_ms: int) -> int | None:
    """The follow-up window, or None when there is nothing to follow up on.

    An attempt that ended `failed` told nobody, so the next report is a first card and §4.3's
    "first report immediately" applies to it -- a window anchored on an attempt whose card never
    arrived would delay the reader for a send they never saw. An `unknown` attempt does anchor a
    window: the whole point of `unknown` is that it may well have been delivered.
    """

    if track.anchor_attempt_at_ms is None or track.anchor_state == "":
        return None
    return int(track.anchor_attempt_at_ms) + window_ms


def notification_status(
    *,
    notify_state: str,
    delivery_state: str | None,
    delivery_error: str | None,
    track_reason: str | None,
) -> tuple[str, str]:
    """What the page says about one observation: an independent status and its reason.

    Never folded into `parse_status`. A raw card that was delivered and a parsed card that was not are
    both ordinary outcomes, and the two pairs answer two different questions (§6).
    """

    if delivery_state:
        # A settled card carries its own error or nothing at all. Borrowing the track's live holding
        # reason here would print `liquidation_followup_window_open` beside a card that was delivered
        # ten minutes ago -- the reason the *group* is currently merging, attached to an observation
        # whose own outcome is already known.
        return delivery_state, str(delivery_error or "")
    if notify_state == NOTIFY_STATE_PENDING:
        return "unprocessed", REASON_UNPROCESSED
    if notify_state == NOTIFY_STATE_HISTORICAL:
        return "historical", REASON_HISTORICAL
    return "merging", str(track_reason or REASON_MERGING)


# --- the card (§5.2): one bounded summary, and a link to everything it summarises ---
#
# Facts only. The words a family is written in, the order of its lines and the characters a figure
# takes live in `reader_card` / `card_format`, which the News first card fills the same way (#562
# PR-A), so a reader cannot be shown the same number two ways on two cards.

DETAIL_BUTTON_LABEL: Final = "打开明细"
_REASON_TITLE: Final[dict[str, str]] = {
    "first": "",
    "followup": "跟进",
    "action_change": "动作变化",
    "raw": "原文",
}


def market_detail_url(console_base_url: str | None, item_id: str) -> str | None:
    """The console link for one observation, or None when no absolute console URL is known.

    The card is opened in Feishu or Telegram, not in the console's own origin, so a relative path is
    not a link there at all: the first market card sent in production carried `/news/market/<id>` and
    no client could follow it. The operator names the origin in `api.public_url` -- `api.host`/`port`
    is a bind address, not an address a reader can open -- and a deployment that has not named one
    gets a card with no button rather than a dead one, its note line carrying the item id (#553).
    """

    base = _absolute_http_url(console_base_url)
    if base is None or not item_id:
        return None
    return f"{base}/news/market/{item_id}"


def _absolute_http_url(value: str | None) -> str | None:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate


def market_reader_card(
    *,
    track: MarketTrack,
    reason: str,
    observations: Sequence[MarketObservation],
    detail_url: str | None = None,
    action_changes: int = 0,
) -> ReaderCard:
    """One intent's bounded summary, in facts.

    v1 sends exactly one card per intent -- no page cursor, no per-page receipt, no paging retry --
    and the observations it could not fit are on the page it links to, not deleted. Without an
    absolute console URL there is no button: the note line prints the item id instead, which is what
    an operator needs to reach the same page, and Telegram's own adapter already refuses a link it
    cannot open.
    """

    first, latest = observations[0], observations[-1]
    subject = track.symbol or track.raw_instrument or latest.symbol or latest.raw_instrument or ""
    link = _absolute_http_url(detail_url)
    return ReaderCard(
        header=ReaderCardHeader(
            family=track.family,  # type: ignore[arg-type]
            subject=subject,
            qualifier=_REASON_TITLE.get(reason, ""),
        ),
        lead=latest.title,
        facts=ReaderCardFacts(
            tickers=(subject,) if subject else (),
            source=(latest.provider or "", track.market_kind),
            report_count=len(observations),
        ),
        market=_reader_market(track=track, observations=observations, action_changes=action_changes),
        link=ReaderCardLink(url=link, label=DETAIL_BUTTON_LABEL) if link is not None else None,
        note=ReaderCardNote(id=track.group_key, detail_id=latest.item_id),
        times=ReaderCardTimes(event_at_ms=latest.event_at_ms, span_from_ms=first.event_at_ms),
    )


def _reader_market(
    *,
    track: MarketTrack,
    observations: Sequence[MarketObservation],
    action_changes: int,
) -> ReaderCardMarket:
    first, latest = observations[0], observations[-1]
    return ReaderCardMarket(
        kind=track.market_kind,
        venue=track.source_venue,
        measurement=track.measurement_definition,
        direction=latest.direction,
        oi_change_bps=latest.oi_change_bps,
        oi_value_usd=latest.oi_value_usd,
        side=track.liquidated_position_side,
        notional=max((fmt.decimal_text(item.notional_usd) for item in observations), default=""),
        account=track.trader_label or track.account_key or "",
        account_verified=track.account_verified,
        actions=tuple(
            ReaderCardAction(action=item.action, side=item.position_side, notional=item.notional_usd)
            for item in observations[-CARD_METRIC_LINES_MAX:]
        ),
        action_changes=action_changes,
        # Where the described timeline starts is what the last delivered card ended on when there is
        # one, not the first observation this card covers.
        opened_action=(
            ReaderCardAction(action=track.anchor_action, side=track.anchor_position_side)
            if track.anchor_action is not None
            else ReaderCardAction(action=first.action, side=first.position_side)
        ),
        latest_action=ReaderCardAction(action=latest.action, side=latest.position_side),
    )


def render_market_card(
    *,
    track: MarketTrack,
    reason: str,
    observations: Sequence[MarketObservation],
    detail_url: str | None = None,
    action_changes: int = 0,
) -> dict[str, Any]:
    """The intent's `ReaderCard` in the wire shape the delivery ledger freezes and Feishu accepts.

    The same structure ordinary News sends, from the same value object and the same serializer, so
    both configured adapters render it with no market branch of their own.
    """

    return feishu_card(
        market_reader_card(
            track=track,
            reason=reason,
            observations=observations,
            detail_url=detail_url,
            action_changes=action_changes,
        )
    )


# --- the loop (§4.1.2, §5.3) ---------------------------------------------------------------------
#
# Ports, not imports. This module is a News *value* module: it may not reach a storage class, a
# pipeline consumer or an HTTP client, and the architecture test that says so is the reason the rules
# above can be read and tested without a database at all. What the loop needs is named here and
# supplied by composition.


class MarketNotificationDatabasePort(Protocol):
    """The narrow News database lane, in the two shapes this loop uses."""

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = 3.0) -> T: ...

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = 3.0) -> T: ...


class PreparedCardSender(Protocol):
    """The existing delivery owner's "send one prepared card" entry.

    One entry, shared with ordinary News, so both queue for the same initial-send guard and the same
    pacing. The market loop has no sender of its own, no thread of its own and no rate limit of its
    own: a second one would be a second answer to "how often may this process interrupt a reader".
    """

    @property
    def available(self) -> bool: ...

    async def send_prepared_card(self, card: Mapping[str, Any], *, operation: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """What one attempt proved, in the only three shapes that differ operationally."""

    state: str
    receipt: Mapping[str, Any] | None = None
    error: str | None = None
    retry_in_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ClaimedCard:
    """One frozen card, out of the database and ready for the send entry."""

    delivery_key: str
    group_key: str
    market_kind: str
    trigger_reason: str
    attempts: int
    card: Mapping[str, Any]
    covered_count: int
    anchor_oi_change_bps: int | None
    anchor_direction: str | None
    anchor_action: str | None
    anchor_position_side: str | None


@dataclass(frozen=True, slots=True)
class MarketTurn:
    """What one turn did. Counted rather than logged, so a test can assert on a turn."""

    observations: int = 0
    groups: int = 0
    intents: int = 0
    sent: int = 0
    failed: int = 0
    unknown: int = 0
    retried: int = 0
    held_unavailable: int = 0
    swept: int = 0


def classify_send_failure(exc: BaseException, *, attempts: int) -> SendOutcome:
    """Three outcomes, and the difference between them is what the adapter could prove.

    `commit_phase = "not_sent"` is the adapter saying the request never reached the provider or the
    provider answered with a refusal: a connect failure, an explicit rejection, a rate limit. Only
    those are retried, and only while the attempt budget lasts.

    Everything else is `unknown`, and that is the honest answer rather than a pessimistic one. A read
    timeout means the request was written and the answer was not read -- the provider may well have
    delivered it -- and a 5xx means the provider's own tier answered, not that it did nothing. Calling
    either "not sent" and retrying would double-notify a reader (§5.2).
    """

    code = str(getattr(exc, "code", "") or "") or f"market_send_failed:{type(exc).__name__}"
    if str(getattr(exc, "commit_phase", "") or "") != COMMIT_PHASE_NOT_SENT:
        return SendOutcome(state="unknown", error=code)
    if bool(getattr(exc, "retryable", False)):
        delay = retry_delay_ms(attempts)
        if delay is not None:
            return SendOutcome(state="pending", error=code, retry_in_ms=delay)
    return SendOutcome(state="failed", error=code)


class MarketNotificationLoop:
    """One loop, one tick, one card at a time.

    Every wait lives in PostgreSQL as a due time. There is no timer per symbol, no task per group and
    no in-memory queue: a process that dies between two ticks loses nothing, and the process that
    replaces it reads the same to-do list. That is what lets the whole feature exist without a broker
    queue of its own (§2, §5.1).
    """

    def __init__(
        self,
        *,
        db: MarketNotificationDatabasePort,
        sender: PreparedCardSender,
        console_base_url: str | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.sender = sender
        # The public origin of the operator console, when the deployment has one: `api.public_url`,
        # passed by the Workers wiring. `market_detail_url` decides what unset means for the card.
        self.console_base_url = console_base_url
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def start(self) -> int:
        """Adopt what the previous process left behind. Run once, after Workers ownership is held.

        A row still reading `sending` was being sent by a process that no longer exists, and nothing
        can now discover whether the provider received it. It becomes `unknown`: never re-sent, kept
        as the anti-duplicate anchor, and never reported as delivered. `pending` continues untouched
        and `sent`/`failed` are not rewritten -- they already said what happened.
        """

        stamp = self._clock()
        return int(await self.db.tx("news_market_notify_sweep", lambda repos: self._sweep(repos, stamp)))

    async def advance(self) -> MarketTurn:
        """One turn: group what arrived, then send what is due.

        The intake bound is a bound on one turn, never a discard rule -- what does not fit is still
        `pending` and is the first thing the next turn reads. Sends happen after grouping so a burst
        that arrives together becomes one card rather than a card per record.
        """

        stamp = self._clock()
        backlog = await self.db.read(
            "news_market_notify_backlog",
            lambda repos: [
                MarketObservation.from_row(row)
                for row in repos.news.market_notification_backlog(limit=BACKLOG_BATCH_MAX)
            ],
        )
        grouped = split_by_group(backlog)
        intents = 0
        for identity, observations in grouped:
            # One transaction per group: the processing marker, the track and the intent commit
            # together or not at all. A notification failure rolls back none of the facts, because
            # none of the facts are written here (§4.1.3).
            intents += int(
                await self.db.tx("news_market_notify_group", _group_turn(self, identity, observations, stamp))
            )
        turn = await self._drain_due()
        return replace(turn, observations=len(backlog), groups=len(grouped), intents=intents)

    # --- one group's turn ---

    def _process_group(
        self, repos: Any, identity: MarketTrack, observations: Sequence[MarketObservation], now_ms: int
    ) -> int:
        news = repos.news
        row = news.market_track(group_key=identity.group_key, for_update=True)
        track = _track_from_row(row) if row is not None else None
        # The un-started intent is read from the deliveries themselves rather than from the track's
        # copy of its key: the unique index is what actually enforces "at most one", so it is also
        # what should answer whether one exists.
        open_key = news.market_group_open_delivery(group_key=identity.group_key)
        turn = decide_group(track, identity, observations, now_ms=now_ms, has_open_intent=open_key is not None)
        news.market_mark_processed(
            item_ids=[observation.item_id for observation in observations], group_key=identity.group_key
        )
        created = 0
        if turn.intent is not None:
            key = delivery_key(identity.group_key, turn.intent.trigger_item_id, turn.intent.reason)
            if open_key is not None and open_key != key:
                # The card this one replaces has never been attempted, so it told nobody and is
                # discarded rather than settled. Its observations lose their pointer with it -- the
                # foreign key is `ON DELETE SET NULL` -- and are adopted by the new card below.
                news.market_discard_delivery(delivery_key=open_key)
                open_key = None
            if news.market_open_delivery(
                delivery_key=key,
                group_key=identity.group_key,
                market_kind=identity.market_kind,
                trigger_reason=turn.intent.reason,
                trigger_item_id=turn.intent.trigger_item_id,
                due_at_ms=turn.intent.due_at_ms,
                now_ms=now_ms,
            ):
                created = 1
            open_key = news.market_group_open_delivery(group_key=identity.group_key) or open_key
        state = replace(turn.track, open_delivery_key=open_key)
        news.market_save_track(track=_track_as_row(state), now_ms=now_ms)
        if open_key is not None:
            # Every observation this group still owes a card for joins the un-started one. This is
            # the merge: no second "first" card, and the covered set is the Items themselves.
            news.market_adopt_unclaimed(group_key=identity.group_key, delivery_key=open_key)
        return created

    # --- sending ---

    async def _drain_due(self) -> MarketTurn:
        turn = MarketTurn()
        for _ in range(SENDS_PER_TURN_MAX):
            stamp = self._clock()
            if not self.sender.available:
                held = int(await self.db.tx("news_market_notify_hold", _hold_unavailable(stamp)))
                return replace(turn, held_unavailable=held)
            claimed = await self.db.tx("news_market_notify_claim", _claim_due(self, stamp))
            if claimed is None:
                return turn
            # The PostgreSQL connection is released before the external call: the send entry queues
            # fairly with ordinary News, and a slow provider never holds a database slot.
            outcome = await self._send(claimed)
            settled = self._clock()
            await self.db.tx("news_market_notify_settle", _settle_send(self, claimed, outcome, settled))
            turn = replace(
                turn,
                sent=turn.sent + (1 if outcome.state == "sent" else 0),
                failed=turn.failed + (1 if outcome.state == "failed" else 0),
                unknown=turn.unknown + (1 if outcome.state == "unknown" else 0),
                retried=turn.retried + (1 if outcome.retry_in_ms is not None else 0),
            )
        return turn

    def _claim(self, repos: Any, now_ms: int) -> ClaimedCard | None:
        news = repos.news
        # A sender exists again, so cards held for its absence become due. They were never attempted,
        # so no attempt was consumed and each group still has exactly one merged card.
        news.market_release_unavailable(now_ms=now_ms)
        row = news.market_due_delivery(now_ms=now_ms)
        if row is None:
            return None
        key = str(row["delivery_key"])
        observations = [
            MarketObservation.from_row(item) for item in news.market_delivery_observations(delivery_key=key)
        ]
        if not observations:
            # Nothing to put on it. An empty card is never sent, and an intent that has never been
            # attempted is not evidence of anything, so it is discarded rather than settled (§4.3).
            news.market_discard_delivery(delivery_key=key)
            return None
        track_row = news.market_track(group_key=str(row["group_key"]))
        track = _track_from_row(track_row) if track_row is not None else group_identity(observations[-1])
        reason = str(row["trigger_reason"])
        card = (
            dict(row["card"])
            if int(row["attempts"] or 0) > 0 and row["card"]
            else render_market_card(
                track=track,
                reason=reason,
                observations=observations,
                detail_url=market_detail_url(self.console_base_url, observations[-1].item_id),
                action_changes=action_changes(observations, since=(track.anchor_action, track.anchor_position_side)),
            )
        )
        news.market_begin_send(
            delivery_key=key,
            card=card,
            covered_count=len(observations),
            covered_from_ms=observations[0].received_at_ms,
            covered_to_ms=observations[-1].received_at_ms,
            now_ms=now_ms,
        )
        news.market_set_track_attempt(group_key=str(row["group_key"]), delivery_key=key, attempt_at_ms=now_ms)
        latest = observations[-1]
        return ClaimedCard(
            delivery_key=key,
            group_key=str(row["group_key"]),
            market_kind=str(row["market_kind"]),
            trigger_reason=reason,
            attempts=int(row["attempts"] or 0) + 1,
            card=card,
            covered_count=len(observations),
            anchor_oi_change_bps=latest.oi_change_bps,
            anchor_direction=latest.direction,
            anchor_action=latest.action,
            anchor_position_side=latest.position_side,
        )

    async def _send(self, claimed: ClaimedCard) -> SendOutcome:
        """Every failure of the send is a delivery state, never a fault of this loop."""

        try:
            receipt = await self.sender.send_prepared_card(claimed.card, operation="news_market_delivery_send")
        except Exception as exc:
            return classify_send_failure(exc, attempts=claimed.attempts)
        return SendOutcome(state="sent", receipt=dict(receipt))

    def _settle(self, repos: Any, claimed: ClaimedCard, outcome: SendOutcome, now_ms: int) -> None:
        news = repos.news
        news.market_settle_delivery(
            delivery_key=claimed.delivery_key,
            state=outcome.state,
            receipt=outcome.receipt,
            error=outcome.error,
            next_attempt_at_ms=None if outcome.retry_in_ms is None else now_ms + outcome.retry_in_ms,
            now_ms=now_ms,
        )
        if outcome.state not in {"sent", "unknown"}:
            # A failure told nobody, so it moves no anchor: the next observation of this group opens a
            # first card rather than a follow-up to a card that was never delivered.
            return
        news.market_set_track_anchor(
            group_key=claimed.group_key,
            anchor_state=outcome.state,
            anchor_delivery_key=claimed.delivery_key,
            anchor_oi_change_bps=claimed.anchor_oi_change_bps,
            anchor_direction=claimed.anchor_direction,
            anchor_action=claimed.anchor_action,
            anchor_position_side=claimed.anchor_position_side,
            pending_reason="",
            now_ms=now_ms,
        )

    def _sweep(self, repos: Any, now_ms: int) -> int:
        news = repos.news
        swept = news.market_sweep_interrupted_sends(reason=REASON_SEND_INTERRUPTED, now_ms=now_ms)
        for row in swept:
            key = str(row["delivery_key"])
            observations = [
                MarketObservation.from_row(item) for item in news.market_delivery_observations(delivery_key=key)
            ]
            latest = observations[-1] if observations else None
            news.market_set_track_anchor(
                group_key=str(row["group_key"]),
                anchor_state="unknown",
                anchor_delivery_key=key,
                anchor_oi_change_bps=None if latest is None else latest.oi_change_bps,
                anchor_direction=None if latest is None else latest.direction,
                anchor_action=None if latest is None else latest.action,
                anchor_position_side=None if latest is None else latest.position_side,
                pending_reason=REASON_SEND_INTERRUPTED,
                now_ms=now_ms,
            )
        return len(swept)


# The database ports take one synchronous callable over the repositories. These build them: bound
# closures rather than lambdas with default arguments, so the types survive.


def _group_turn(
    loop: MarketNotificationLoop,
    identity: MarketTrack,
    observations: Sequence[MarketObservation],
    now_ms: int,
) -> Callable[[Any], int]:
    def run(repos: Any) -> int:
        return loop._process_group(repos, identity, observations, now_ms)

    return run


def _hold_unavailable(now_ms: int) -> Callable[[Any], int]:
    def run(repos: Any) -> int:
        return int(repos.news.market_hold_unavailable(reason=REASON_SENDER_UNAVAILABLE, now_ms=now_ms))

    return run


def _claim_due(loop: MarketNotificationLoop, now_ms: int) -> Callable[[Any], ClaimedCard | None]:
    def run(repos: Any) -> ClaimedCard | None:
        return loop._claim(repos, now_ms)

    return run


def _settle_send(
    loop: MarketNotificationLoop, claimed: ClaimedCard, outcome: SendOutcome, now_ms: int
) -> Callable[[Any], None]:
    def run(repos: Any) -> None:
        loop._settle(repos, claimed, outcome, now_ms)

    return run


def action_changes(
    observations: Sequence[MarketObservation], *, since: tuple[str | None, str | None] = (None, None)
) -> int:
    """How many times the reported action changed across what this card speaks for.

    `since` is what the last delivered card ended on. Without it a change card whose previous segment
    was entirely covered by the card before it would count zero changes -- the segment starts *at* the
    change -- and the card would omit the count §4.4 requires it to show.
    """

    changes = 0
    previous_action, previous_side = since
    for observation in observations:
        if previous_action is not None and (
            observation.action != previous_action or observation.position_side != previous_side
        ):
            changes += 1
        previous_action, previous_side = observation.action, observation.position_side
    return changes


# The track's own columns, named once. The upsert statement, the row reader and the row writer all
# build from this tuple, so a column added to `MarketTrack` cannot reach one of them and miss another.
MARKET_TRACK_FIELDS: Final[tuple[str, ...]] = (
    "group_key",
    "market_kind",
    "family",
    "provider",
    "source_venue",
    "venue_known",
    "raw_instrument",
    "symbol",
    "measurement_definition",
    "liquidated_position_side",
    "account_key",
    "account_verified",
    "trader_label",
    "current_action",
    "current_position_side",
    "last_observed_at_ms",
    "last_observed_item_id",
    "anchor_state",
    "anchor_delivery_key",
    "anchor_attempt_at_ms",
    "anchor_oi_change_bps",
    "anchor_direction",
    "anchor_action",
    "anchor_position_side",
    "open_delivery_key",
    "next_due_at_ms",
    "pending_reason",
)


def _track_from_row(row: Mapping[str, Any]) -> MarketTrack:
    values = {field: row[field] for field in MARKET_TRACK_FIELDS}
    values["venue_known"] = bool(values["venue_known"])
    values["account_verified"] = bool(values["account_verified"])
    values["last_observed_at_ms"] = int(values["last_observed_at_ms"] or 0)
    values["last_observed_item_id"] = str(values["last_observed_item_id"] or "")
    values["anchor_state"] = str(values["anchor_state"] or "")
    values["pending_reason"] = str(values["pending_reason"] or "")
    return MarketTrack(**values)


def _track_as_row(track: MarketTrack) -> dict[str, Any]:
    return {field: getattr(track, field) for field in MARKET_TRACK_FIELDS}
