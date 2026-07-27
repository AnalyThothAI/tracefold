from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Source:
    provider: str
    transport: str
    coverage: str
    channel: str


@dataclass(frozen=True, slots=True)
class Author:
    handle: str | None
    name: str | None
    avatar: str | None
    followers: int | None
    tags: list[str]


@dataclass(frozen=True, slots=True)
class Media:
    type: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class Content:
    text: str | None
    media: list[Media]


@dataclass(frozen=True, slots=True)
class Reference:
    tweet_id: str | None
    author_handle: str | None
    author_name: str | None
    author_avatar: str | None
    author_followers: int | None
    text: str | None
    media: list[Media]
    type: str


@dataclass(frozen=True, slots=True)
class UnfollowTarget:
    handle: str | None
    name: str | None
    bio: str | None
    avatar: str | None
    followers: int | None


@dataclass(frozen=True, slots=True)
class AvatarChange:
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class BioChange:
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class TokenSnapshot:
    address: str
    chain: str
    symbol: str | None
    icon_url: str | None
    trigger_type: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TwitterEvent:
    event_id: str
    source: Source
    action: str
    original_action: str | None
    tweet_id: str | None
    internal_id: str | None
    timestamp: int
    received_at_ms: int
    author: Author
    content: Content
    reference: Reference | None
    unfollow_target: UnfollowTarget | None
    avatar_change: AvatarChange | None
    bio_change: BioChange | None
    raw: dict[str, Any] | None
    token_snapshot: TokenSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
