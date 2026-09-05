"""The v1 market notification rules, driven exactly as #553 §4 writes them.

Every case here is transcribed from the Issue: the numbers, the boundaries and the counterexamples are
its own. The rules are pure functions over one group's observations and its track, so this file needs
no database -- what a *rule* decides and what a *loop* durably does are two different claims, and the
second one is proved against real PostgreSQL in `tests/integration/test_news_market_notifications.py`.

`_Group` below drives `decide_group` the way the loop drives it: read the track, decide, open at most
one un-started intent, and -- when a send is confirmed -- move the anchor to the observation that card
covered. It is deliberately small and explicit rather than a second implementation; the anchor
transition it applies is the same two calls the loop makes (`market_set_track_attempt` then
`market_set_track_anchor`), and the integration suite proves those two against the real tables.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from tracefold.news.market_notifications import (
    LIQUIDATION_WINDOW_MS,
    OI_QUIET_RESET_MS,
    SEND_ATTEMPTS_MAX,
    SEND_RETRY_BACKOFF_MS,
    SMART_MONEY_WINDOW_MS,
    IntentPlan,
    MarketObservation,
    MarketTrack,
    action_changes,
    classify_send_failure,
    decide_group,
    delivery_key,
    group_family,
    group_identity,
    notification_status,
    render_market_card,
    retry_delay_ms,
    split_by_group,
)

T0 = 1_780_000_000_000


def oi(
    *,
    at_ms: int,
    change_bps: int,
    direction: str = "rise",
    item_id: str | None = None,
    venue: str = "binance",
    instrument: str = "WIF",
    definition: str = "oi_signal_v1|opennews_oi_source_v1|300000",
) -> MarketObservation:
    return MarketObservation(
        item_id=item_id or f"oi-{at_ms}-{change_bps}",
        market_kind="oi",
        parse_status="parsed",
        title=f"{instrument} OI {direction} {change_bps / 100}%",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        provider="opennews",
        source_strategy_id="1019",
        source_venue=venue,
        raw_instrument=instrument,
        symbol=instrument,
        measurement_definition=definition,
        direction=direction,
        oi_change_bps=change_bps,
        oi_value_usd=11_030_000,
    )


def liquidation(
    *,
    at_ms: int,
    side: str = "long",
    venue: str | None = "binance",
    instrument: str = "DOGE",
    notional: str = "412530.00",
    item_id: str | None = None,
) -> MarketObservation:
    return MarketObservation(
        item_id=item_id or f"liq-{at_ms}-{side}-{venue}",
        market_kind="liquidation",
        parse_status="parsed",
        title=f"{instrument} {side} liquidation {notional}",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        provider="opennews",
        source_strategy_id="2083",
        source_venue=venue,
        raw_instrument=instrument,
        symbol=instrument,
        liquidated_position_side=side,
        notional_usd=notional,
    )


def wallet(
    *,
    at_ms: int,
    action: str = "open",
    side: str = "long",
    label: str = "Machi Big Brother",
    address: str | None = "0xabc",
    venue: str | None = "hyperliquid",
    instrument: str = "ETH",
    item_id: str | None = None,
) -> MarketObservation:
    return MarketObservation(
        item_id=item_id or f"sm-{at_ms}-{action}-{side}-{label}",
        market_kind="smart_money",
        parse_status="parsed",
        title=f"{label} {action} {side} {instrument}",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        provider="opennews",
        source_strategy_id="2026",
        source_venue=venue,
        raw_instrument=instrument,
        symbol=instrument,
        trader_label=label,
        account_address=address,
        action=action,
        position_side=side,
        notional_usd="1250000.00",
        price="3120.5",
    )


def raw_record(*, at_ms: int, title: str, kind: str = "smart_money", item_id: str | None = None) -> MarketObservation:
    return MarketObservation(
        item_id=item_id or f"raw-{at_ms}",
        market_kind=kind,
        parse_status="raw",
        title=title,
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        provider="opennews",
        source_strategy_id="2026",
        source_venue="hyperliquid",
    )


@dataclass
class _Card:
    """One prepared card, as the loop would have written it into `news_market_deliveries`."""

    delivery_key: str
    reason: str
    covered: tuple[str, ...]


class _Group:
    """One group's track and its un-started intent, driven the way the loop drives them.

    `observe` is one turn of the loop for one group. `deliver` is what the loop does after a send
    settles: `sent` moves the anchor to the observation the card covered, `unknown` moves it without
    claiming delivery, and a failure moves nothing.
    """

    def __init__(self) -> None:
        self.track: MarketTrack | None = None
        self.open_intent: IntentPlan | None = None
        self.open_key: str | None = None
        self.uncovered: list[MarketObservation] = []
        self.covered: dict[str, list[MarketObservation]] = {}
        self.cards: list[_Card] = []

    def observe(self, *observations: MarketObservation, now_ms: int | None = None) -> None:
        identity = group_identity(observations[0])
        stamp = observations[-1].received_at_ms if now_ms is None else now_ms
        turn = decide_group(
            self.track,
            identity,
            list(observations),
            now_ms=stamp,
            has_open_intent=self.open_key is not None,
        )
        self.uncovered.extend(observations)
        if turn.intent is not None:
            # A new intent replaces an un-started one rather than joining it: at most one per group,
            # and the observations of the card it replaces move to this one.
            self.open_intent = turn.intent
            self.open_key = delivery_key(identity.group_key, turn.intent.trigger_item_id, turn.intent.reason)
        self.track = replace(turn.track, open_delivery_key=self.open_key)

    def due_at(self) -> int | None:
        return None if self.open_intent is None else self.open_intent.due_at_ms

    def send(self, *, now_ms: int, outcome: str = "sent") -> _Card | None:
        """Claim the due card, freeze what it covers, and settle it. None when nothing is due."""

        if self.open_intent is None or self.track is None or now_ms < self.open_intent.due_at_ms:
            return None
        assert self.open_key is not None
        card = _Card(
            delivery_key=self.open_key,
            reason=self.open_intent.reason,
            covered=tuple(observation.item_id for observation in self.uncovered),
        )
        covered = list(self.uncovered)
        self.covered[self.open_key] = covered
        self.cards.append(card)
        self.uncovered = []
        # The attempt anchors the follow-up window whatever it proves, and the card stops merging.
        self.track = replace(self.track, anchor_attempt_at_ms=now_ms, open_delivery_key=None, next_due_at_ms=None)
        self.open_intent = None
        self.open_key = None
        if outcome in {"sent", "unknown"}:
            latest = covered[-1]
            anchor_bps = latest.oi_change_bps if latest.oi_change_bps is not None else self.track.anchor_oi_change_bps
            self.track = replace(
                self.track,
                anchor_state=outcome,
                anchor_delivery_key=card.delivery_key,
                anchor_oi_change_bps=anchor_bps,
                anchor_direction=latest.direction or self.track.anchor_direction,
                current_action=latest.action or self.track.current_action,
                current_position_side=latest.position_side or self.track.current_position_side,
            )
        return card


# --- §4.2 open interest -------------------------------------------------------------------------


def test_oi_six_then_nine_then_thirteen_is_a_first_card_and_one_followup() -> None:
    """The Issue's own example. 9 % is not twice 6 %; 13 % is, and the anchor is 6 % throughout."""

    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    first = group.send(now_ms=T0)
    assert first is not None and first.reason == "first"

    group.observe(oi(at_ms=T0 + 60_000, change_bps=900, item_id="oi-nine"))
    assert group.send(now_ms=T0 + 60_000) is None

    group.observe(oi(at_ms=T0 + 120_000, change_bps=1_300, item_id="oi-thirteen"))
    followup = group.send(now_ms=T0 + 120_000)
    assert followup is not None
    assert followup.reason == "followup"
    # The follow-up speaks for both un-notified observations, not only the one that triggered it.
    assert followup.covered == ("oi-nine", "oi-thirteen")
    assert [card.reason for card in group.cards] == ["first", "followup"]


def test_oi_six_then_six_point_one_then_six_point_two_is_one_card() -> None:
    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    assert group.send(now_ms=T0) is not None
    group.observe(oi(at_ms=T0 + 60_000, change_bps=610))
    group.observe(oi(at_ms=T0 + 120_000, change_bps=620))
    assert group.send(now_ms=T0 + 120_000) is None
    assert [card.reason for card in group.cards] == ["first"]


def test_oi_observations_arriving_before_the_first_card_is_sent_become_one_card() -> None:
    """No second card is invented to reproduce the example's shape: nobody was told the first number."""

    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    group.observe(oi(at_ms=T0 + 1_000, change_bps=900))
    group.observe(oi(at_ms=T0 + 2_000, change_bps=1_300))
    card = group.send(now_ms=T0 + 2_000)
    assert card is not None
    assert card.reason == "first"
    assert len(card.covered) == 3
    assert len(group.cards) == 1


@pytest.mark.parametrize(
    ("anchor_bps", "next_bps", "expected"),
    [
        pytest.param(0, 0, False, id="zero_to_zero_is_not_a_change"),
        pytest.param(0, 25, True, id="zero_to_nonzero_is"),
        pytest.param(600, 1_199, False, id="just_under_twice"),
        pytest.param(600, 1_200, True, id="exactly_twice"),
    ],
)
def test_oi_followup_threshold_is_twice_the_anchors_absolute_change(
    anchor_bps: int, next_bps: int, expected: bool
) -> None:
    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=anchor_bps))
    assert group.send(now_ms=T0) is not None
    group.observe(oi(at_ms=T0 + 60_000, change_bps=next_bps))
    assert (group.send(now_ms=T0 + 60_000) is not None) is expected


def test_oi_direction_change_follows_up_whatever_the_magnitude() -> None:
    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600, direction="rise"))
    assert group.send(now_ms=T0) is not None
    group.observe(oi(at_ms=T0 + 60_000, change_bps=120, direction="fall"))
    card = group.send(now_ms=T0 + 60_000)
    assert card is not None
    assert card.reason == "followup"


@pytest.mark.parametrize(
    ("gap_ms", "expected_reason"),
    [
        pytest.param(OI_QUIET_RESET_MS - 1, None, id="just_inside_four_hours_is_the_ordinary_rule"),
        pytest.param(OI_QUIET_RESET_MS, "first", id="at_four_hours_the_next_observation_is_a_first_card"),
    ],
)
def test_oi_four_hour_quiet_reset_boundary(gap_ms: int, expected_reason: str | None) -> None:
    """Measured on the host's receive stamps, and only for the same small change that is otherwise held."""

    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    assert group.send(now_ms=T0) is not None
    group.observe(oi(at_ms=T0 + gap_ms, change_bps=610))
    card = group.send(now_ms=T0 + gap_ms)
    assert (None if card is None else card.reason) == expected_reason


def test_oi_groups_split_on_venue_instrument_and_measurement_definition() -> None:
    keys = {
        group_identity(observation).group_key
        for observation in (
            oi(at_ms=T0, change_bps=600),
            oi(at_ms=T0, change_bps=600, venue="okx"),
            oi(at_ms=T0, change_bps=600, instrument="XYZ-WIF"),
            oi(at_ms=T0, change_bps=600, definition="oi_signal_v1|unproven|unproven"),
        )
    }
    assert len(keys) == 4


def test_a_failed_card_leaves_the_anchor_empty_so_the_next_observation_is_a_first_card_again() -> None:
    """A card that failed told nobody. The next observation opens a first card, not a follow-up."""

    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    assert group.send(now_ms=T0, outcome="failed") is not None
    group.observe(oi(at_ms=T0 + 60_000, change_bps=610))
    card = group.send(now_ms=T0 + 60_000)
    assert card is not None and card.reason == "first"


def test_an_unknown_result_anchors_without_claiming_delivery_and_never_locks_the_group() -> None:
    """The provider may have it, so the snapshot is the anti-duplicate anchor -- and only that."""

    group = _Group()
    group.observe(oi(at_ms=T0, change_bps=600))
    assert group.send(now_ms=T0, outcome="unknown") is not None
    assert group.track is not None and group.track.anchor_state == "unknown"

    # The same magnitude does not re-send the same snapshot ...
    group.observe(oi(at_ms=T0 + 60_000, change_bps=610))
    assert group.send(now_ms=T0 + 60_000) is None
    # ... and a genuine escalation still reaches the reader.
    group.observe(oi(at_ms=T0 + 120_000, change_bps=1_300))
    assert group.send(now_ms=T0 + 120_000) is not None


# --- §4.3 liquidations --------------------------------------------------------------------------


def test_liquidation_first_is_immediate_then_one_followup_at_the_window_close() -> None:
    """t=0 first, t=5 and t=8 merge, and t=60 is exactly one follow-up covering both."""

    group = _Group()
    group.observe(liquidation(at_ms=T0, item_id="liq-0"))
    assert group.send(now_ms=T0) is not None

    group.observe(liquidation(at_ms=T0 + 5_000, item_id="liq-5"))
    group.observe(liquidation(at_ms=T0 + 8_000, item_id="liq-8"))
    assert group.send(now_ms=T0 + 8_000) is None
    assert group.due_at() == T0 + LIQUIDATION_WINDOW_MS

    followup = group.send(now_ms=T0 + LIQUIDATION_WINDOW_MS)
    assert followup is not None
    assert followup.reason == "followup"
    assert followup.covered == ("liq-5", "liq-8")
    assert len(group.cards) == 2


def test_liquidation_sends_no_empty_card_when_nothing_new_arrived() -> None:
    group = _Group()
    group.observe(liquidation(at_ms=T0))
    assert group.send(now_ms=T0) is not None
    assert group.send(now_ms=T0 + LIQUIDATION_WINDOW_MS) is None
    assert group.send(now_ms=T0 + 10 * LIQUIDATION_WINDOW_MS) is None
    assert len(group.cards) == 1


def test_liquidation_after_the_window_the_next_report_is_immediate_and_opens_the_next_window() -> None:
    group = _Group()
    group.observe(liquidation(at_ms=T0))
    assert group.send(now_ms=T0) is not None
    later = T0 + LIQUIDATION_WINDOW_MS + 1_000
    group.observe(liquidation(at_ms=later, item_id="liq-later"))
    card = group.send(now_ms=later)
    assert card is not None
    assert group.track is not None and group.track.anchor_attempt_at_ms == later


def test_liquidation_window_is_anchored_on_the_send_attempt_not_on_the_newest_report() -> None:
    """A steady stream cannot push its own follow-up away for ever."""

    group = _Group()
    group.observe(liquidation(at_ms=T0))
    assert group.send(now_ms=T0) is not None
    for offset in range(5_000, LIQUIDATION_WINDOW_MS, 5_000):
        group.observe(liquidation(at_ms=T0 + offset, item_id=f"liq-{offset}"))
        assert group.due_at() == T0 + LIQUIDATION_WINDOW_MS
    assert group.send(now_ms=T0 + LIQUIDATION_WINDOW_MS) is not None


def test_a_liquidation_card_nobody_received_leaves_the_next_report_immediate() -> None:
    """§4.3 首条立即. A failed card told nobody, so the next report is a first card, not a follow-up.

    The window is anchored on the last send *attempt*, which did happen -- but an attempt whose card
    never arrived cannot make a reader wait, or an outage would be paid for twice: once by the card
    they never got and again by the minute before the next one.
    """

    group = _Group()
    group.observe(liquidation(at_ms=T0, item_id="liq-0"))
    assert group.send(now_ms=T0, outcome="failed") is not None

    group.observe(liquidation(at_ms=T0 + 5_000, item_id="liq-5"))
    card = group.send(now_ms=T0 + 5_000)
    assert card is not None
    assert card.reason == "first"


def test_an_unknown_liquidation_card_does_anchor_its_window() -> None:
    """The other half: `unknown` may well have been delivered, so it is not a free pass."""

    group = _Group()
    group.observe(liquidation(at_ms=T0, item_id="liq-0"))
    assert group.send(now_ms=T0, outcome="unknown") is not None

    group.observe(liquidation(at_ms=T0 + 5_000, item_id="liq-5"))
    assert group.send(now_ms=T0 + 5_000) is None
    followup = group.send(now_ms=T0 + LIQUIDATION_WINDOW_MS)
    assert followup is not None
    assert followup.reason == "followup"


def test_liquidation_sides_and_venues_are_separate_groups_and_never_suppress_each_other() -> None:
    long_side = _Group()
    short_side = _Group()
    okx = _Group()
    unknown_venue = _Group()
    long_side.observe(liquidation(at_ms=T0, side="long"))
    short_side.observe(liquidation(at_ms=T0 + 1_000, side="short"))
    okx.observe(liquidation(at_ms=T0 + 2_000, venue="okx"))
    unknown_venue.observe(liquidation(at_ms=T0 + 3_000, venue=None))
    for index, group in enumerate((long_side, short_side, okx, unknown_venue)):
        assert group.send(now_ms=T0 + index * 1_000) is not None, index

    keys = {
        group_identity(observation).group_key
        for observation in (
            liquidation(at_ms=T0, side="long"),
            liquidation(at_ms=T0, side="short"),
            liquidation(at_ms=T0, venue="okx"),
            liquidation(at_ms=T0, venue=None),
        )
    }
    assert len(keys) == 4
    # An unknown venue is its own group and is labelled as unknown rather than filed under a known one.
    assert group_identity(liquidation(at_ms=T0, venue=None)).venue_known is False


# --- §4.4 smart money ---------------------------------------------------------------------------


def test_smart_money_same_account_and_action_merges_into_one_followup_window() -> None:
    group = _Group()
    group.observe(wallet(at_ms=T0, item_id="sm-0"))
    assert group.send(now_ms=T0) is not None
    group.observe(wallet(at_ms=T0 + 10_000, item_id="sm-10"))
    group.observe(wallet(at_ms=T0 + 20_000, item_id="sm-20"))
    assert group.send(now_ms=T0 + 20_000) is None
    followup = group.send(now_ms=T0 + SMART_MONEY_WINDOW_MS)
    assert followup is not None
    assert followup.covered == ("sm-10", "sm-20")


def test_smart_money_action_change_at_49_241_seconds_ends_the_segment_at_once() -> None:
    """The Issue's own counterexample: the reader is told inside the window, not after it."""

    change_at = T0 + 49_241
    group = _Group()
    group.observe(wallet(at_ms=T0, action="close", side="short", item_id="sm-close-0"))
    assert group.send(now_ms=T0) is not None
    group.observe(wallet(at_ms=T0 + 12_000, action="close", side="short", item_id="sm-close-12"))
    assert group.send(now_ms=T0 + 12_000) is None

    group.observe(wallet(at_ms=change_at, action="open", side="short", item_id="sm-open-49"))
    card = group.send(now_ms=change_at)
    assert card is not None
    assert card.reason == "action_change"
    # The old segment's un-notified activity rides on the same ordered summary as the new action.
    assert card.covered == ("sm-close-12", "sm-open-49")
    assert change_at - T0 < SMART_MONEY_WINDOW_MS


def test_smart_money_change_card_shows_the_change_count_and_the_first_and_last_action() -> None:
    observations = [
        wallet(at_ms=T0, action="close", side="short", item_id="a"),
        wallet(at_ms=T0 + 10_000, action="open", side="short", item_id="b"),
        wallet(at_ms=T0 + 20_000, action="open", side="long", item_id="c"),
    ]
    card = render_market_card(
        track=group_identity(observations[0]),
        reason="action_change",
        observations=observations,
        detail_url="/news/market/c",
        action_changes=2,
    )
    text = card["elements"][0]["content"]
    assert "动作变化 2 次" in text
    assert "首 平空" in text
    assert "末 开多" in text
    # A Close is a reported action, never a claim about the account's whole position.
    assert "不代表账户已全部清仓" in text


def test_the_change_count_starts_from_what_the_last_delivered_card_ended_on() -> None:
    """A segment that begins *at* the change has no transition inside it -- and still shows one.

    Counting only within the covered set reports zero for exactly the card whose subject is the
    change, which is the one §4.4 requires the count on.
    """

    covered = [wallet(at_ms=T0 + 10_000, action="open", side="short", item_id="b")]
    assert action_changes(covered) == 0
    assert action_changes(covered, since=("close", "short")) == 1

    card = render_market_card(
        track=replace(group_identity(covered[0]), anchor_action="close", anchor_position_side="short"),
        reason="action_change",
        observations=covered,
        detail_url="/news/market/b",
        action_changes=action_changes(covered, since=("close", "short")),
    )
    printed = card["elements"][0]["content"]
    assert "动作变化 1 次" in printed
    # "首" is where the timeline starts, which is what the last card ended on -- not the first record
    # of a segment that begins at the change.
    assert "首 平空 → 末 开空" in printed


def test_smart_money_accounts_never_suppress_one_another() -> None:
    machi = _Group()
    other = _Group()
    machi.observe(wallet(at_ms=T0, label="Machi Big Brother", address="0xabc"))
    other.observe(wallet(at_ms=T0 + 1_000, label="James Wynn", address="0xdef"))
    assert machi.send(now_ms=T0) is not None
    assert other.send(now_ms=T0 + 1_000) is not None
    assert (
        group_identity(wallet(at_ms=T0, address="0xabc")).group_key
        != group_identity(wallet(at_ms=T0, address="0xdef")).group_key
    )


def test_smart_money_without_an_address_uses_the_source_label_group_and_says_it_is_not_verified() -> None:
    labelled = group_identity(wallet(at_ms=T0, address=None))
    verified = group_identity(wallet(at_ms=T0, address="0xabc"))
    assert labelled.account_verified is False
    assert verified.account_verified is True
    assert labelled.group_key != verified.group_key
    assert labelled.account_key == "opennews|2026|Machi Big Brother"
    card = render_market_card(
        track=labelled,
        reason="first",
        observations=[wallet(at_ms=T0, address=None)],
        detail_url="/news/market/x",
    )
    assert "来源标签，非已核实地址" in card["elements"][0]["content"]


def test_smart_money_unknown_venue_is_its_own_group_and_is_labelled() -> None:
    unknown = group_identity(wallet(at_ms=T0, venue=None))
    known = group_identity(wallet(at_ms=T0, venue="hyperliquid"))
    assert unknown.group_key != known.group_key
    assert unknown.venue_known is False
    card = render_market_card(
        track=unknown, reason="first", observations=[wallet(at_ms=T0, venue=None)], detail_url="/x"
    )
    assert "场所未知" in card["elements"][0]["content"]


def test_a_reported_address_is_an_account_even_when_the_provider_sent_no_label() -> None:
    """§4.4 puts the stable address first. A missing display label is not a missing identity."""

    unlabelled = replace(wallet(at_ms=T0, address="0xabc"), trader_label=None)
    assert group_family(unlabelled) == "smart_money"
    identity = group_identity(unlabelled)
    assert identity.account_verified is True
    assert identity.account_key == "0xabc"
    # And a record with neither is genuinely unidentifiable, so it stays its own raw card.
    assert group_family(replace(unlabelled, account_address=None)) == "raw"


def test_smart_money_action_and_side_are_not_part_of_the_notification_group() -> None:
    """A change of action must stay inside one group -- it is the thing worth a card."""

    close_short = group_identity(wallet(at_ms=T0, action="close", side="short"))
    open_short = group_identity(wallet(at_ms=T0, action="open", side="short"))
    assert close_short.group_key == open_short.group_key


# --- §4.4 raw and unknown -----------------------------------------------------------------------


def test_a_withdraw_gets_its_own_card_outside_the_open_close_suppression() -> None:
    withdraw = raw_record(at_ms=T0 + 5_000, title="Machi Big Brother Withdraw 1.2M USDC")
    assert group_family(withdraw) == "raw"

    suppressed = _Group()
    suppressed.observe(wallet(at_ms=T0))
    assert suppressed.send(now_ms=T0) is not None

    unstructured = _Group()
    unstructured.observe(withdraw)
    card = unstructured.send(now_ms=T0 + 5_000)
    assert card is not None
    assert card.reason == "raw"


def test_two_unreadable_records_are_two_groups_and_never_share_one_unknown_bucket() -> None:
    first = raw_record(at_ms=T0, title="something new", item_id="raw-a")
    second = raw_record(at_ms=T0 + 1_000, title="something else", item_id="raw-b")
    assert group_identity(first).group_key != group_identity(second).group_key
    assert len(split_by_group([first, second])) == 2


def test_an_unparsed_oi_line_is_raw_rather_than_an_oi_comparison() -> None:
    unparsed = replace(oi(at_ms=T0, change_bps=600), parse_status="raw", oi_change_bps=None, direction=None)
    assert group_family(unparsed) == "raw"


# --- grouping and identity ----------------------------------------------------------------------


def test_split_by_group_keeps_each_group_in_host_receive_order() -> None:
    observations = [
        oi(at_ms=T0 + 2_000, change_bps=700, item_id="oi-b"),
        liquidation(at_ms=T0 + 1_000, item_id="liq-a"),
        oi(at_ms=T0, change_bps=600, item_id="oi-a"),
    ]
    grouped = split_by_group(observations)
    assert [identity.family for identity, _ in grouped] == ["oi", "liquidation"]
    assert [item.item_id for item in grouped[0][1]] == ["oi-a", "oi-b"]


def test_a_delivery_key_is_deterministic_in_the_group_the_trigger_and_the_reason() -> None:
    key = delivery_key("oi|opennews|binance|WIF|d", "item-1", "first")
    assert key == delivery_key("oi|opennews|binance|WIF|d", "item-1", "first")
    assert key != delivery_key("oi|opennews|binance|WIF|d", "item-1", "followup")
    assert key != delivery_key("oi|opennews|binance|WIF|d", "item-2", "first")
    assert len(key) == 32


# --- §5.3 attempts, and the two states a failure can be -----------------------------------------


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(1, SEND_RETRY_BACKOFF_MS[0]), (2, SEND_RETRY_BACKOFF_MS[1]), (SEND_ATTEMPTS_MAX, None)],
)
def test_retry_waits_are_five_then_thirty_seconds_and_then_the_budget_is_spent(
    attempts: int, expected: int | None
) -> None:
    assert retry_delay_ms(attempts) == expected


class _Refused(RuntimeError):
    def __init__(self, code: str, *, commit_phase: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.commit_phase = commit_phase
        self.retryable = retryable


@pytest.mark.parametrize(
    ("commit_phase", "retryable", "attempts", "state", "retries"),
    [
        pytest.param("not_sent", True, 1, "pending", True, id="provably_not_sent_and_retryable"),
        pytest.param("not_sent", True, SEND_ATTEMPTS_MAX, "failed", False, id="attempt_budget_spent"),
        pytest.param("not_sent", False, 1, "failed", False, id="explicit_rejection_is_not_retried"),
        pytest.param("unknown", True, 1, "unknown", False, id="unknown_is_never_auto_resent"),
        pytest.param("", False, 1, "unknown", False, id="an_adapter_that_proved_nothing_is_unknown"),
    ],
)
def test_send_failures_are_classified_by_what_the_adapter_could_prove(
    commit_phase: str, retryable: bool, attempts: int, state: str, retries: bool
) -> None:
    outcome = classify_send_failure(
        _Refused("news_delivery_x", commit_phase=commit_phase, retryable=retryable), attempts=attempts
    )
    assert outcome.state == state
    assert (outcome.retry_in_ms is not None) is retries
    assert outcome.receipt is None


def test_an_exception_with_no_commit_evidence_at_all_is_unknown() -> None:
    outcome = classify_send_failure(TimeoutError("read timeout"), attempts=1)
    assert outcome.state == "unknown"
    assert outcome.error == "market_send_failed:TimeoutError"


# --- §6 the two independent status pairs --------------------------------------------------------


@pytest.mark.parametrize(
    ("notify_state", "delivery_state", "expected"),
    [
        pytest.param("pending", None, ("unprocessed", "awaiting_market_loop"), id="not_yet_grouped"),
        pytest.param("historical", None, ("historical", "historical_not_alerted"), id="pre_enable_or_recovery"),
        pytest.param("processed", None, ("merging", "liquidation_followup_window_open"), id="merging"),
        pytest.param("processed", "sent", ("sent", ""), id="delivered"),
        pytest.param("processed", "unknown", ("unknown", ""), id="result_unreadable"),
        pytest.param("processed", "unavailable", ("unavailable", ""), id="no_sender"),
        pytest.param("processed", "pending", ("pending", ""), id="attempted_and_awaiting_retry"),
    ],
)
def test_notification_status_names_the_rule_holding_an_observation(
    notify_state: str, delivery_state: str | None, expected: tuple[str, str]
) -> None:
    status, reason = notification_status(
        notify_state=notify_state,
        delivery_state=delivery_state,
        delivery_error=None,
        # The track's live reason is always present, because it always is in the read model's join.
        # A settled card must not borrow it.
        track_reason="liquidation_followup_window_open",
    )
    assert (status, reason) == expected


def test_a_settled_card_reports_its_own_error_and_nothing_else() -> None:
    status, reason = notification_status(
        notify_state="processed",
        delivery_state="failed",
        delivery_error="news_delivery_feishu_business_rejected",
        track_reason="liquidation_followup_window_open",
    )
    assert (status, reason) == ("failed", "news_delivery_feishu_business_rejected")


def test_a_raw_record_that_was_delivered_is_both_raw_and_sent() -> None:
    """The two pairs are independent, which is the whole reason they are two pairs."""

    status, _ = notification_status(
        notify_state="processed", delivery_state="sent", delivery_error=None, track_reason=None
    )
    assert status == "sent"
    assert group_family(raw_record(at_ms=T0, title="Withdraw")) == "raw"
