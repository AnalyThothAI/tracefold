"""Feishu card rendering — the reader contract (issue #57): one Event, one card, three lines.

    header  ⚡? headline_zh            (model: one complete headline incl. the decisive fact, Chinese)
    line 1  why_zh                     (model: why it matters now and to whom, Chinese)
    line 2  利多 · 影响明显 · BTC ETH · CoinDesk, 2 条报道 · 14:32
            (code: direction, magnitude, tickers, source, local time)

No original headline, no translated title, no scope/type enums, no provider score, no "AI" label, no follow-up
card. Pipeline internals live in the console and `tracefold news why`.

Degraded Events (the model chain failed and the rule baseline still pushes) get the wire text instead of a
verdict view (issue #65): the header is the original headline, the body is the original description when there
is one, and the facts line carries only tickers / source / time — no direction or magnitude the model never judged,
and no "model unavailable" copy in the reader's face.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .outcome import DIRECTION_ZH, MAGNITUDE_ZH

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[\w]{1,32}")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_DIRECTION_COLOR = {"bullish": "green", "bearish": "red", "neutral": "grey", "unclear": "grey"}
_MAX_ASSETS = 4
_CARD_TZ_OFFSET_S = 8 * 3600  # the reader's clock (Asia/Shanghai); the source timestamp is UTC ms


def sanitize_ai_text(value: object, *, limit: int, fallback: str = "") -> str:
    """Deterministic clean of model text; any surviving URL falls back to the code-owned fallback."""

    raw = str(value or "")
    if _URL_RE.search(raw):
        return fallback[:limit]
    cleaned = _CONTROL_RE.sub(" ", raw)
    cleaned = _HANDLE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    return (cleaned or fallback)[:limit]


def _wire_text(value: object, *, limit: int) -> str:
    """Provider text for a degraded card: control characters, markdown and whitespace cleaned, URLs kept out of the
    header/body (the source button carries the link)."""

    cleaned = _URL_RE.sub("", _CONTROL_RE.sub(" ", str(value or "")))
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    return _SPACE_RE.sub(" ", cleaned).strip()[:limit]


def card_assets(verdict: Mapping[str, Any], grounded_assets: Sequence[str]) -> list[str]:
    """Assets shown on the card: the verdict's primary assets that the Gate grounded (code fact ∩ model claim);
    when the model named no grounded primary, the grounded assets themselves — never provider noise alone."""

    grounded = {str(a).upper().replace("XYZ-", "") for a in grounded_assets}
    primaries = [
        str(a.get("symbol") or "").upper().replace("XYZ-", "")
        for a in (verdict.get("assets") or [])
        if isinstance(a, Mapping) and a.get("role") == "primary"
    ]
    shown = [s for s in dict.fromkeys(primaries) if s in grounded]
    if not shown and len(grounded) <= _MAX_ASSETS:
        shown = sorted(grounded)
    return shown[:_MAX_ASSETS]


def _facts_line(
    *,
    direction: str | None,
    magnitude: int | None,
    assets: Sequence[str],
    source: str,
    members: int,
    at_ms: int | None,
) -> str:
    parts: list[str] = []
    if direction is not None and magnitude is not None:
        parts += [DIRECTION_ZH.get(direction, direction), MAGNITUDE_ZH.get(magnitude, str(magnitude))]
    if assets:
        parts.append(" ".join(assets))
    origin = source or "-"
    parts.append(f"{origin}（{members} 条报道）" if members > 1 else origin)
    if at_ms:
        parts.append(time.strftime("%H:%M", time.gmtime(int(at_ms) / 1000 + _CARD_TZ_OFFSET_S)))
    return " · ".join(parts)


def render_first_card(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    decision: str,
    grounded_assets: Sequence[str],
    degraded: bool = False,
) -> dict[str, Any]:
    original_title = str(event.get("leader_title") or "")
    link = str(event.get("leader_url") or "")
    if degraded:
        header_text = _wire_text(original_title, limit=100)
        why = _wire_text(event.get("leader_description"), limit=140)
        direction: str | None = None
        magnitude: int | None = None
    else:
        direction = str(verdict.get("direction") or "unclear")
        magnitude = int(verdict.get("magnitude") or 0)
        headline = sanitize_ai_text(verdict.get("headline_zh"), limit=60)
        title_zh = sanitize_ai_text(verdict.get("title_zh"), limit=120)
        why = sanitize_ai_text(verdict.get("why_zh"), limit=140)
        header_text = headline or title_zh or original_title
    header_title = f"{'⚡ ' if decision == 'escalate' else ''}{header_text}"
    lines: list[str] = []
    if why:
        lines.append(why)
    lines.append(
        _facts_line(
            direction=direction,
            magnitude=magnitude,
            assets=card_assets(verdict, grounded_assets),
            source=str(event.get("reporting_origin") or ""),
            members=int(event.get("member_count") or 1),
            at_ms=event.get("leader_published_at_ms") or event.get("opened_at_ms"),
        )
    )
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开来源"},
                        "type": "default",
                        "url": link,
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"Tracefold · {event.get('event_id', '')[:8]}"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title[:100]},
            "template": _DIRECTION_COLOR.get(direction or "", "grey"),
        },
        "elements": elements,
    }


__all__ = ["card_assets", "render_first_card", "sanitize_ai_text"]
