"""Feishu card rendering: code facts are the body; AI copy is sanitized and labelled."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import DELIVERY_CARD_VERSION

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[\w]{1,32}")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")

_DIRECTION_LABEL = {"bullish": "利多", "bearish": "利空", "neutral": "中性", "unclear": "不明"}
_DIRECTION_COLOR = {"bullish": "green", "bearish": "red", "neutral": "grey", "unclear": "grey"}
_MAGNITUDE_LABEL = {0: "无影响", 1: "小", 2: "明显", 3: "重大"}


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


def _assets_text(assets: Sequence[str]) -> str:
    shown = [a.upper().replace("XYZ-", "") for a in assets][:6]
    return " ".join(f"`{a}`" for a in shown) if shown else "—"


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
    title_zh = sanitize_ai_text(verdict.get("title_zh"), limit=160)
    headline = sanitize_ai_text(verdict.get("headline_zh"), limit=60, fallback=title_zh or original_title)
    rationale = sanitize_ai_text(verdict.get("rationale"), limit=160)
    header_title = f"{'⚡ ' if decision == 'escalate' else ''}{headline}"
    facts_lines = [
        f"**原标题**：{original_title}",
        f"**标的**：{_assets_text(grounded_assets)}　**方向**：{_DIRECTION_LABEL.get(direction, direction)}"
        f"　**强度**：{_MAGNITUDE_LABEL.get(magnitude, magnitude)}",
        f"**类型**：{verdict.get('event_type') or '-'}　**范围**：{verdict.get('scope') or '-'}"
        f"　**来源**：{event.get('reporting_origin') or '-'}　**成员**：{event.get('member_count') or 1}"
        f"　**Provider 分**：{event.get('provider_score_max') if event.get('provider_score_max') is not None else '-'}",
    ]
    if title_zh and title_zh != original_title:
        facts_lines.insert(0, f"**标题**：{title_zh}")
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(facts_lines)}]
    if rationale:
        elements.append({"tag": "markdown", "content": f"AI 初判：{rationale}"})
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
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"Tracefold News · {DELIVERY_CARD_VERSION} · {event.get('event_id', '')[:12]}",
                }
            ],
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
    original_title = str(event.get("leader_title") or "")
    agrees = bool(analyst_verdict.get("agrees_with_triage"))
    revised_direction = str(analyst_verdict.get("revised_direction") or "unclear")
    thesis = sanitize_ai_text(analyst_verdict.get("thesis_zh"), limit=800)
    risks = sanitize_ai_text(analyst_verdict.get("risks_zh"), limit=400)
    reactions = analyst_verdict.get("market_reaction") or []
    reaction_lines = [
        f"`{r.get('symbol')}` {r.get('window_min')}m 价格 {_pct(r.get('price_change_pct'))}"
        f" OI {_pct(r.get('oi_change_pct'))}"
        for r in reactions
        if isinstance(r, Mapping)
    ]
    verdict_line = (
        f"**深度补充**：与初判一致（{_DIRECTION_LABEL.get(revised_direction, revised_direction)}）"
        if agrees
        else (
            "**深度补充：修正初判** "
            f"{_DIRECTION_LABEL.get(str(triage_verdict.get('direction') or 'unclear'), '')}"
            f" → {_DIRECTION_LABEL.get(revised_direction, revised_direction)}"
        )
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**原标题**：{original_title}\n{verdict_line}"
                f"　**强度**：{_MAGNITUDE_LABEL.get(int(analyst_verdict.get('revised_magnitude') or 0), '-')}"
                f"　**新颖性**：{analyst_verdict.get('novelty_assessment') or '-'}"
            ),
        },
    ]
    if reaction_lines:
        elements.append({"tag": "markdown", "content": "**市场反应**（工具数据）：\n" + "\n".join(reaction_lines)})
    if thesis:
        elements.append({"tag": "markdown", "content": f"**分析**：{thesis}"})
    if risks:
        elements.append({"tag": "markdown", "content": f"**风险**：{risks}"})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"Tracefold News · Analyst · {event.get('event_id', '')[:12]}"}
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (
                    "🔍 深度补充："
                    + sanitize_ai_text(triage_verdict.get("headline_zh"), limit=50, fallback=original_title)
                )[:100],
            },
            "template": "blue" if agrees else "orange",
        },
        "elements": elements,
    }


def _pct(value: object) -> str:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return "n/a"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


__all__ = ["render_first_card", "render_followup_card", "sanitize_ai_text"]
