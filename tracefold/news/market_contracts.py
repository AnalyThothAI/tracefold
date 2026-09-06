"""The market read surface's own bounds and its two status vocabularies.

A value module for the same reason `oi_contracts.py` is one: the HTTP route validates a window
against these numbers, the storage statements bound their own scans with them, and the News package
surface re-exports them for App composition. One definition, three readers, no import of a
persistence or runtime owner.

`notification_status` is deliberately not a member of `parse_status`. A raw card that was delivered
and a parsed card that was not are both ordinary outcomes, and one combined column would have to
misreport one of them. Both vocabularies are values, so both are here: the read model answers with
this one and the notification loop writes it, and neither owns it. The read model reaching into the
loop module for the words it prints made a page read pull in an asyncio loop, three provider ports and
the send entry to learn six strings (#589 L-F13).
"""

from typing import Final

# One page of collapsed groups.
MARKET_PAGE_MAX: Final = 100
# The default window a reader gets without asking, and the widest span one request may cover. The
# span bounds what a single page may scan; it says nothing about how far back the data goes, and any
# window inside the retention is readable.
MARKET_WINDOW_DEFAULT_MS: Final = 72 * 60 * 60_000
MARKET_WINDOW_MAX_MS: Final = 168 * 60 * 60_000
# What one page may read inside that window. Collapsing consecutive observations is a property of the
# whole window rather than of a page -- otherwise one group would appear twice, with two different
# counts, either side of a page boundary -- so the window is read and the groups are paged out of it.
# At the measured 208 market observations a day a full 168 h window is about 1 500 rows.
MARKET_WINDOW_ROW_CAP: Final = 5_000
# One group's expanded timeline on the detail page.
MARKET_TIMELINE_MAX: Final = 200
# How far back an OI card looks for News about its own instrument, and how many already-pushed
# headlines it may print (#582 §3.3). Here rather than in either owner because both read them: the
# two statements in `storage/decisions.py` bound their windows with the first and their LIMIT with
# the second, and the card's own line prints the window it was queried with -- so the `48h` a reader
# sees cannot drift from the window that produced the numbers beside it.
MARKET_NEWS_WINDOW_MS: Final = 48 * 60 * 60_000
MARKET_NEWS_PUSHED_MAX: Final = 3

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
# answer that does not require reading the loop.
REASON_UNPROCESSED: Final = "awaiting_market_loop"
REASON_HISTORICAL: Final = "historical_not_alerted"
REASON_MERGING: Final = "merging_into_prepared_card"
REASON_OI_BELOW_THRESHOLD: Final = "oi_change_below_followup_threshold"
REASON_OI_ZERO_UNCHANGED: Final = "oi_anchor_zero_and_unchanged"
REASON_LIQUIDATION_WINDOW: Final = "liquidation_followup_window_open"
REASON_SMART_MONEY_ROUND: Final = "smart_money_round_open"
REASON_SENDER_UNAVAILABLE: Final = "market_sender_unavailable"
REASON_SEND_INTERRUPTED: Final = "market_send_interrupted"
# The one reason that is final without a send: the round this observation was held in ended before a
# card spoke for it, and the card that opened the next round covers that round only. Saying `merging`
# here would promise a card that is never coming (#562 PR-F).
REASON_ROUND_CLOSED: Final = "alert_round_ended_before_a_card"
# The other final answer without a send, and the whole of what happens to an unstructured record: it
# is recorded, it is readable, and no rule is holding it because no rule ever will (#582 §3.2). The
# read model tells it apart by having no track row at all, which is what `track_reason is None` says.
REASON_UNSTRUCTURED: Final = "unstructured_record_not_alerted"

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
    "round_started_at_ms",
)


def notification_status(
    *,
    notify_state: str,
    delivery_state: str | None,
    delivery_error: str | None,
    track_reason: str | None,
    round_closed: bool,
) -> tuple[str, str]:
    """What the page says about one observation: an independent status and its reason.

    Never folded into `parse_status`. A record whose template was never proved and a parsed record no
    card spoke for are both ordinary outcomes, and the two pairs answer two different questions (§6).

    `track_reason is None` is the read model's LEFT JOIN finding no track row at all, which happens
    for exactly one thing: an unstructured record, grouped and marked processed and deliberately
    given no alerting state (#582 §3.2). It is not `pending_reason = ''` -- that is a real track whose
    group is holding nothing at this instant -- and the two must not be conflated.

    `round_closed` is the read model's own comparison -- no card claimed this observation, and the
    alert round it belonged to started before it. There is no attempt to report and no rule still
    holding it: it is a recorded fact no card ever spoke for, which is a fourth answer rather than a
    variety of `merging` (#562 PR-F).
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
    if track_reason is None:
        return "not_alerted", REASON_UNSTRUCTURED
    if round_closed:
        return "uncovered", REASON_ROUND_CLOSED
    return "merging", str(track_reason or REASON_MERGING)


__all__ = [
    "MARKET_NEWS_PUSHED_MAX",
    "MARKET_NEWS_WINDOW_MS",
    "MARKET_PAGE_MAX",
    "MARKET_TIMELINE_MAX",
    "MARKET_TRACK_FIELDS",
    "MARKET_WINDOW_DEFAULT_MS",
    "MARKET_WINDOW_MAX_MS",
    "MARKET_WINDOW_ROW_CAP",
    "NOTIFY_STATES",
    "NOTIFY_STATE_HISTORICAL",
    "NOTIFY_STATE_PENDING",
    "NOTIFY_STATE_PROCESSED",
    "REASON_HISTORICAL",
    "REASON_LIQUIDATION_WINDOW",
    "REASON_MERGING",
    "REASON_OI_BELOW_THRESHOLD",
    "REASON_OI_ZERO_UNCHANGED",
    "REASON_ROUND_CLOSED",
    "REASON_SENDER_UNAVAILABLE",
    "REASON_SEND_INTERRUPTED",
    "REASON_SMART_MONEY_ROUND",
    "REASON_UNPROCESSED",
    "REASON_UNSTRUCTURED",
    "notification_status",
]
