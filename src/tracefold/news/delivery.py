"""Feishu card rendering: a short human brief. Code facts are the body; AI copy is sanitized and never labelled
"AI" — the reader gets the Chinese headline, the original wire line, one sentence on why it matters now, and the
direction/magnitude/asset facts in plain words. Pipeline internals (event type enums, scope, member counts,
provider scores, verdict rules) stay in the console and `tracefold news why`, not on the card."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[\w]{1,32}")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_DIRECTION_LABEL = {"bullish": "利多", "bearish": "利空", "neutral": "中性", "unclear": "方向待定"}
_DIRECTION_COLOR = {"bullish": "green", "bearish": "red", "neutral": "grey", "unclear": "grey"}
_MAGNITUDE_LABEL = {0: "影响很小", 1: "影响有限", 2: "影响明显", 3: "影响重大"}
_SCOPE_LABEL = {"macro": "宏观", "sector": "板块", "single_name": "个别标的"}
_MAX_ASSETS = 4


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


def _facts_line(*, direction: str, magnitude: int, scope: str, assets: Sequence[str], source: str, members: int) -> str:
    parts = [_DIRECTION_LABEL.get(direction, direction), _MAGNITUDE_LABEL.get(magnitude, str(magnitude))]
    if scope in _SCOPE_LABEL:
        parts.append(_SCOPE_LABEL[scope])
    if assets:
        parts.append(" ".join(f"`{a}`" for a in assets))
    origin = source or "-"
    parts.append(f"{origin}（{members} 条报道）" if members > 1 else origin)
    return " · ".join(parts)


def render_first_card(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    decision: str,
    grounded_assets: Sequence[str],
) -> dict[str, Any]:
    original_title = str(event.get("leader_title") or "")
    link = str(event.get("leader_url") or "")
    direction = str(verdict.get("direction") or "unclear")
    magnitude = int(verdict.get("magnitude") or 0)
    title_zh = sanitize_ai_text(verdict.get("title_zh"), limit=120)
    headline = sanitize_ai_text(verdict.get("headline_zh"), limit=60)
    why = sanitize_ai_text(verdict.get("why_zh"), limit=120)
    header_text = title_zh or headline or original_title
    header_title = f"{'⚡ ' if decision == 'escalate' else ''}{header_text}"
    lines: list[str] = []
    if original_title and original_title != header_text:
        lines.append(original_title)
    if why:
        lines.append(why)
    lines.append(
        _facts_line(
            direction=direction,
            magnitude=magnitude,
            scope=str(verdict.get("scope") or ""),
            assets=card_assets(verdict, grounded_assets),
            source=str(event.get("reporting_origin") or ""),
            members=int(event.get("member_count") or 1),
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
            "template": _DIRECTION_COLOR.get(direction, "grey"),
        },
        "elements": elements,
    }


def render_followup_card(
    *,
    event: Mapping[str, Any],
    triage_verdict: Mapping[str, Any],
    analyst_verdict: Mapping[str, Any],
) -> dict[str, Any]:
    """The follow-up only ships when the Analyst changed or added something; it renders that delta and the thesis."""

    original_title = str(event.get("leader_title") or "")
    title_zh = sanitize_ai_text(triage_verdict.get("title_zh"), limit=90, fallback=original_title)
    agrees = bool(analyst_verdict.get("agrees_with_triage"))
    triage_direction = str(triage_verdict.get("direction") or "unclear")
    revised_direction = str(analyst_verdict.get("revised_direction") or "unclear")
    triage_magnitude = int(triage_verdict.get("magnitude") or 0)
    revised_magnitude = int(analyst_verdict.get("revised_magnitude") or 0)
    thesis = sanitize_ai_text(analyst_verdict.get("thesis_zh"), limit=600)
    lines: list[str] = []
    if not agrees or revised_direction != triage_direction:
        lines.append(
            f"方向修正：{_DIRECTION_LABEL.get(triage_direction, triage_direction)}"
            f" → {_DIRECTION_LABEL.get(revised_direction, revised_direction)}"
        )
    if revised_magnitude != triage_magnitude:
        lines.append(
            f"强度修正：{_MAGNITUDE_LABEL.get(triage_magnitude, triage_magnitude)}"
            f" → {_MAGNITUDE_LABEL.get(revised_magnitude, revised_magnitude)}"
        )
    if thesis:
        lines.append(thesis)
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines) or title_zh}]
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"Tracefold · 补充 · {event.get('event_id', '')[:8]}"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"补充：{title_zh}"[:100]},
            "template": "blue" if agrees else "orange",
        },
        "elements": elements,
    }


__all__ = ["card_assets", "render_first_card", "render_followup_card", "sanitize_ai_text"]
