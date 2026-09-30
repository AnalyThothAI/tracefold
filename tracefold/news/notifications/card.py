"""Compose exactly the selected claims and freeze the actual reader-visible body."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, Protocol

from ..updates.contracts import Claim, EventUpdate, Source
from ..updates.identity import digest
from .contracts import CardCopy, FrozenCard, NotificationPlan, ReaderRepairContext


class CardComposer(Protocol):
    identity: str

    async def compose(
        self,
        claims: tuple[Claim, ...],
        *,
        sources: Mapping[str, Source],
        earlier: Mapping[str, ReaderRepairContext] | None = None,
    ) -> CardCopy:
        """Chinese copy for exactly the selected claims. The caller bounds the call with asyncio.timeout."""
        ...


def card_copy_material(
    claims: tuple[Claim, ...],
    sources: Mapping[str, Source],
    earlier: Mapping[str, ReaderRepairContext] | None = None,
) -> list[dict[str, object]]:
    """Exactly the claim and provenance fields the Chinese composer receives.

    A claim rendered as an increment or a correction carries the earlier message the reader already has.
    """

    return [
        {
            "claim_ref": claim.ref,
            "statement": claim.statement,
            "fields": claim.fields.model_dump(mode="json"),
            "citations": [
                {
                    "evidence_ref": citation.evidence_ref,
                    "quote": citation.quote,
                    "source": None
                    if (source := sources.get(citation.evidence_ref)) is None
                    else {
                        "publisher_id": source.publisher_id,
                        "attribution": source.attribution,
                        "origin_id": source.origin_id,
                    },
                }
                for citation in claim.citations
            ],
            **(
                {}
                if (context := (earlier or {}).get(claim.ref)) is None
                else {"earlier": {"render": context.render, "delivered_text": context.body}}
            ),
        }
        for claim in claims
    ]


def _has_han(text: str) -> bool:
    return any("㐀" <= char <= "鿿" for char in text)


# Reader copy is plain text that every channel shows exactly as frozen. A link or a control character
# in model copy is not something a channel may strip afterwards, so such copy is refused, not cleaned.
_COPY_LINK_RE: Final = re.compile(r"https?://|www\.", re.IGNORECASE)
_COPY_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _unsafe_copy(text: str) -> bool:
    return bool(_COPY_LINK_RE.search(text) or _COPY_CONTROL_RE.search(text))


def freeze_card(plan: NotificationPlan, update: EventUpdate, copy: CardCopy) -> FrozenCard:
    """Freeze actual reader copy; selected IDs are not proof of what the reader was told.

    Later reader judgments compare this exact delivered body, not the original
    article, the selected-ID set, or an unsent draft. Adapters may reject oversized
    copy, but may not silently truncate the frozen body.
    """
    refs = plan.selected_claim_refs
    if plan.update_ref != update.ref or not set(refs) <= {claim.ref for claim in update.claims}:
        raise ValueError("news_card_update_selection_mismatch")
    if {line.claim_ref for line in copy.lines} != set(refs) or len(copy.lines) != len(refs):
        raise ValueError("news_card_selected_claims_mismatch")
    lines = {line.claim_ref: line.text_zh for line in copy.lines}
    if not _has_han(copy.headline_zh) or any(not _has_han(text) for text in lines.values()):
        raise ValueError("news_card_chinese_copy_required")
    if "\n" in copy.headline_zh or any(_unsafe_copy(text) for text in (copy.headline_zh, *lines.values())):
        raise ValueError("news_card_copy_unsafe")
    body = "\n\n".join([copy.headline_zh, *(lines[ref] for ref in refs)])
    return FrozenCard(
        intent_id=plan.intent_id,
        claim_refs=refs,
        headline_zh=copy.headline_zh,
        body=body,
        payload_sha256=digest(body),
    )
