from __future__ import annotations

import json
from typing import Any

from tracefold.market.capture.gmgn_token_payload import parse_gmgn_token_payload
from tracefold.market.capture.twitter_event import (
    Author,
    AvatarChange,
    BioChange,
    Content,
    Media,
    Reference,
    Source,
    TwitterEvent,
    UnfollowTarget,
)

TWITTER_CHANNELS = {
    "twitter_monitor_basic",
    "twitter_monitor_token",
    "twitter_monitor_translation",
    "twitter_monitor_express",
    "public_broadcast",
}


def parse_gmgn_frame(frame_data: Any) -> dict[str, Any] | None:
    if not isinstance(frame_data, str):
        return None

    payload = frame_data.lstrip("0123456789")
    if not payload:
        return None

    parsed = json.loads(payload)
    if isinstance(parsed, list) and len(parsed) >= 2:
        parsed = parsed[1]
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("channel") not in TWITTER_CHANNELS:
        return None
    if not isinstance(parsed.get("data"), list):
        return None
    return parsed


def normalize_gmgn_payload(parsed: dict[str, Any], *, received_at_ms: int) -> list[TwitterEvent]:
    channel = parsed.get("channel")
    data = parsed.get("data")
    if not isinstance(channel, str) or not isinstance(data, list):
        return []

    events = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if channel == "public_broadcast":
            event = _normalize_public_broadcast(channel, item, received_at_ms)
        else:
            event = _normalize_twitter_item(channel, item, received_at_ms)
        if event is not None:
            events.append(event)
    return events


def _normalize_twitter_item(channel: str, item: dict[str, Any], received_at_ms: int) -> TwitterEvent | None:
    action = str(item.get("tw") or "")
    if not action:
        return None

    internal_id = _string_or_none(item.get("i"))
    tweet_id = _string_or_none(item.get("ti"))
    event_key = internal_id or tweet_id
    if not event_key:
        return None

    author_data = _dict_or_empty(item.get("u"))
    content_data = _dict_or_empty(item.get("c"))
    timestamp = _normalize_timestamp(item.get("ts"))
    handle = _string_or_none(author_data.get("s"))

    return TwitterEvent(
        event_id=f"gmgn:{channel}:{event_key}",
        source=Source(provider="gmgn", transport="direct_ws", coverage="public_stream", channel=channel),
        action=action,
        original_action=_string_or_none(item.get("stw")),
        tweet_id=tweet_id,
        internal_id=internal_id,
        timestamp=timestamp,
        received_at_ms=received_at_ms,
        author=Author(
            handle=handle,
            name=_string_or_none(author_data.get("n")),
            avatar=_string_or_none(author_data.get("a")),
            followers=_int_or_none(author_data.get("f")),
            tags=[str(tag) for tag in item.get("ut", []) if tag],
        ),
        content=Content(
            text=_string_or_none(content_data.get("t")),
            media=_media_list(content_data.get("m")),
        ),
        reference=_reference(item, action),
        unfollow_target=_unfollow_target(item) if action in {"follow", "unfollow"} else None,
        avatar_change=_avatar_change(item) if action == "photo" else None,
        bio_change=_bio_change(item) if action == "description" else None,
        raw=item,
        token_snapshot=parse_gmgn_token_payload(item),
    )


def _normalize_public_broadcast(channel: str, item: dict[str, Any], received_at_ms: int) -> TwitterEvent | None:
    if item.get("et") != "twitter_watched":
        return None

    event_data = _dict_or_empty(item.get("ed"))
    original = _dict_or_empty(event_data.get("ot"))
    source = _dict_or_empty(event_data.get("st"))
    text = _first_text(original, source)
    tweet_id = _first_tweet_id(original, source)
    raw_id = _string_or_none(event_data.get("id"))
    event_type = _string_or_none(event_data.get("tp")) or "unknown"
    if not text and not tweet_id:
        return None

    event_key = f"{raw_id}:{event_type}" if raw_id else tweet_id
    if not event_key:
        return None

    return TwitterEvent(
        event_id=f"gmgn:{channel}:{event_key}",
        source=Source(provider="gmgn", transport="direct_ws", coverage="public_stream", channel=channel),
        action="public_broadcast",
        original_action=event_type,
        tweet_id=tweet_id,
        internal_id=event_key,
        timestamp=0,
        received_at_ms=received_at_ms,
        author=Author(
            handle=None,
            name="GMGN Public Broadcast",
            avatar=None,
            followers=None,
            tags=["public_broadcast", event_type],
        ),
        content=Content(text=text, media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw=item,
        token_snapshot=None,
    )


def _media_list(raw_media: Any) -> list[Media]:
    if not isinstance(raw_media, list):
        return []
    return [
        Media(type=_string_or_none(item.get("t")), url=_string_or_none(item.get("u")))
        for item in raw_media
        if isinstance(item, dict)
    ]


def _reference(item: dict[str, Any], action: str) -> Reference | None:
    if "su" not in item:
        return None

    user = _dict_or_empty(item.get("su"))
    content = _dict_or_empty(item.get("sc"))
    ref_type = {
        "repost": "retweeted",
        "reply": "replied_to",
        "quote": "quoted",
        "delete_post": "deleted",
    }.get(action, "referenced")

    return Reference(
        tweet_id=_string_or_none(item.get("si")),
        author_handle=_string_or_none(user.get("s")),
        author_name=_string_or_none(user.get("n")),
        author_avatar=_string_or_none(user.get("a")),
        author_followers=_int_or_none(user.get("f")),
        text=_string_or_none(content.get("t")),
        media=_media_list(content.get("m")),
        type=ref_type,
    )


def _unfollow_target(item: dict[str, Any]) -> UnfollowTarget | None:
    follow_data = _dict_or_empty(item.get("f"))
    target = _dict_or_empty(follow_data.get("f"))
    if not target:
        return None
    return UnfollowTarget(
        handle=_string_or_none(target.get("s")),
        name=_string_or_none(target.get("n")),
        bio=_string_or_none(target.get("d")),
        avatar=_string_or_none(target.get("a")),
        followers=_int_or_none(target.get("f")),
    )


def _avatar_change(item: dict[str, Any]) -> AvatarChange | None:
    photo = _dict_or_empty(item.get("p"))
    if not photo:
        return None
    return AvatarChange(before=_string_or_none(photo.get("ba")), after=_string_or_none(photo.get("aa")))


def _bio_change(item: dict[str, Any]) -> BioChange | None:
    bio = _dict_or_empty(item.get("p"))
    if not bio:
        return None
    return BioChange(before=_string_or_none(bio.get("bd")), after=_string_or_none(bio.get("d")))


def _normalize_timestamp(value: Any) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return 0
    return timestamp // 1000 if timestamp > 9_999_999_999 else timestamp


def _first_text(*records: dict[str, Any]) -> str | None:
    for record in records:
        text = _string_or_none(record.get("ak"))
        if text:
            return text
    for record in records:
        keywords = record.get("kw")
        if isinstance(keywords, list):
            text = ", ".join(str(keyword) for keyword in keywords if keyword)
            if text:
                return text
        if isinstance(keywords, str) and keywords:
            return keywords
    return None


def _first_tweet_id(*records: dict[str, Any]) -> str | None:
    for record in records:
        tweet_id = _string_or_none(record.get("ti"))
        if tweet_id:
            return tweet_id
    return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
