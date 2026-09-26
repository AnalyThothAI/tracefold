"""Adopted EventUpdates, notify plans and frozen cards for delivery tests (#706).

Everything is built through the core itself -- `assemble_update`, `NotificationPlan`, `freeze_card` -- so a
test card is exactly the value the Deliverer receives in production, not a hand-written lookalike.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.news.updates.contracts import (
    Asset,
    Change,
    Citation,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import digest
from tracefold.news.updates.notification import (
    CardCopy,
    CardLine,
    ClaimDecision,
    FrozenCard,
    NotificationPlan,
    freeze_card,
)
from tracefold.news.updates.semantics import assemble_update

# 2026-08-18 14:40 UTC, which the reader's clock (UTC+8) prints as 22:40.
STAMP = 1_787_064_000_000
READER_REVISION = f"reader_v1:{STAMP}:{'0' * 64}"
EVENT_ID = "event-nvda"


def source(
    text: str,
    *,
    publisher: str = "opennews",
    origin: str | None = "Reuters",
    url: str | None = "https://www.reuters.com/a",
    published_at_ms: int | None = STAMP,
    available_at_ms: int = STAMP,
) -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id=publisher,
            artifact_id=f"{publisher}:{digest(text)[:12]}",
            artifact_revision="1",
            origin_id=origin,
            published_at_ms=published_at_ms,
            first_available_at_ms=available_at_ms,
            url=url,
        ),
    )


def asset(symbol: str, market_type: str = "equity", role: str = "primary") -> Asset:
    return Asset.model_validate({"symbol": symbol, "market_type": market_type, "role": role})


def draft(
    slot: str,
    item: Evidence,
    *,
    assets: Sequence[Asset] = (),
    mode: str = "decision",
    kind: str = "state_change",
    statement: str | None = None,
) -> DraftClaim:
    return DraftClaim(
        slot=slot,
        statement=statement or item.text,
        fields=ClaimFields.model_validate(
            {"subject": "Company", "action": slot, "mode": mode, "content_kind": kind, "assets": tuple(assets)}
        ),
        citations=(Citation(evidence_ref=item.ref, quote=item.text),),
    )


def adopted(
    *rows: tuple[DraftClaim, Evidence],
    extra: Sequence[Evidence] = (),
    event_id: str = EVENT_ID,
    supports: Sequence[SupportDraft] = (),
    changes: Sequence[tuple[str, str]] = (),
) -> EventUpdate:
    """One adopted head. `changes` replaces the change kinds of the named slots: `(slot, kind)`."""

    items = {item.ref: item for _draft, item in rows} | {item.ref: item for item in extra}
    frozen = FrozenInput(event_id=event_id, revision=1, lineage_id="lineage", evidence=tuple(items.values()))
    extraction = Extraction(claims=tuple(row for row, _item in rows), supports=tuple(supports))
    update = assemble_update(frozen, extraction, None, adopted_at_ms=STAMP)
    assert update is not None
    if not changes:
        return update
    slots = {row.slot: claim for (row, _item), claim in zip(rows, update.claims, strict=True)}
    rewritten = [
        Change(kind=kind, current_ref=slots[slot].ref, previous_ref="cl:earlier", previous_content_ref="rev:earlier")
        if kind in {"correction", "parameter_change", "phase_change", "scope_change", "restatement"}
        else Change(kind=kind, current_ref=slots[slot].ref)
        for slot, kind in changes
    ]
    return EventUpdate.model_validate({**update.model_dump(mode="json"), "changes": [*rewritten]})


def plan_for(
    update: EventUpdate,
    *,
    key: bool = False,
    selected: Sequence[str] | None = None,
    reader_revision: str = READER_REVISION,
    channel: str = "news",
) -> NotificationPlan:
    chosen = {claim.ref for claim in update.claims} if selected is None else set(selected)
    return NotificationPlan(
        action="notify",
        reason="uncovered_claims",
        update_ref=update.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="notify", reason="actionable_content")
            if claim.ref in chosen
            else ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="mode_commentary")
            for claim in update.claims
        ),
        key=key,
        channel=channel,
        reader_revision=reader_revision,
    )


def copy_for(plan: NotificationPlan, headline: str = "英伟达向数据中心投资千亿美元", **lines: str) -> CardCopy:
    refs = plan.selected_claim_refs
    return CardCopy(
        headline_zh=headline,
        lines=tuple(
            CardLine(claim_ref=ref, text_zh=lines.get(ref, f"第{index + 1}条：英伟达宣布投资"))
            for index, ref in enumerate(refs)
        ),
    )


def frozen_card(
    plan: NotificationPlan, update: EventUpdate, headline: str = "英伟达向数据中心投资千亿美元"
) -> FrozenCard:
    return freeze_card(plan, update, copy_for(plan, headline))


def nvda_update(**kwargs: Any) -> EventUpdate:
    item = source("Nvidia to invest $100bn in OpenAI data centres.")
    return adopted((draft("a", item, assets=(asset("NVDA"), asset("OPENAI", "unknown", "mentioned"))), item), **kwargs)


__all__ = [
    "EVENT_ID",
    "READER_REVISION",
    "STAMP",
    "adopted",
    "asset",
    "copy_for",
    "draft",
    "frozen_card",
    "nvda_update",
    "plan_for",
    "source",
]
