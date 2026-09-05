"""The one place a `ReaderCard` becomes Feishu's interactive-card JSON (#562 §3).

Feishu's wire shape is this module's whole subject: the envelope, the header template names, the
markdown block, the action button and the note element. It reads a `ReaderCard` and nothing else --
no Event, no verdict, no observation, no track -- so the two renderers can stop knowing what a
`wide_screen_mode` is, and the frozen snapshot in `news_deliveries.card` / `news_market_deliveries.card`
keeps exactly the structure it has today.

It lives in `news/` rather than in `integrations/feishu.py` because both the News delivery path and
the market loop freeze this JSON before the adapter is reached, and an adapter that business modules
import is not an adapter. `FeishuNewsPushSender` still receives a finished card and posts it.

`template` is Feishu's colour vocabulary, mapped from the card's own `family + tone`: the model's
judgment colours a News card, and a market card is coloured by its family because it carries no
judgment to colour it with. Telegram maps the same two fields to its own icons (#562 PR-C).
"""

from __future__ import annotations

from typing import Any, Final

from .reader_card import ReaderCard

_FAMILY_TEMPLATE: Final[dict[str, str]] = {
    "news": "grey",
    "oi": "blue",
    "liquidation": "red",
    "smart_money": "turquoise",
    "raw": "grey",
}
_TONE_TEMPLATE: Final[dict[str, str]] = {"bullish": "green", "bearish": "red"}


def feishu_card(card: ReaderCard) -> dict[str, Any]:
    """One reader card as the JSON Feishu accepts, and as the delivery ledgers store it."""

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(card.body_lines())}]
    if card.link is not None:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": card.link.label},
                        "type": "default",
                        "url": card.link.url,
                    }
                ],
            }
        )
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": card.note_text()}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": card.title()},
            "template": _TONE_TEMPLATE.get(card.header.tone) or _FAMILY_TEMPLATE.get(card.header.family, "grey"),
        },
        "elements": elements,
    }


__all__ = ["feishu_card"]
