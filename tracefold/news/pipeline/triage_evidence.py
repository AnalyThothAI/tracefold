"""Freeze bounded local material once, outside the model and without external reads."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..bus import now_ms
from ..evidence import PreparedEvidence, assemble_evidence, frozen_members, query_for, select_members, shortlist
from ..models import MarketAsset, market_type_of
from .runtime import NewsDatabasePort


async def prepare_evidence(
    db: NewsDatabasePort,
    card: Mapping[str, Any],
    *,
    catalog: Mapping[str, Sequence[str]],
    clock: Callable[[], int] = now_ms,
) -> PreparedEvidence:
    started = time.monotonic()
    cutoff = clock()
    members, exclusions = frozen_members(card)
    metadata = await db.read(
        "news_evidence_member_metadata",
        lambda repos: repos.news.evidence_member_metadata([row["item_id"] for row in members]),
    )
    selected_members = select_members(members, metadata, cutoff=cutoff)
    material = await db.read(
        "news_evidence_current",
        lambda repos: repos.news.evidence_material([row["item_id"] for row in selected_members]),
    )
    by_id = {row["item_id"]: row for row in material}
    current = [{**row, **by_id.get(row["item_id"], {})} for row in selected_members]
    item = current[0]
    assets = tuple(
        MarketAsset(
            str(symbol),
            market_type_of(classes[0])
            if len(classes := catalog.get(str(symbol), ())) == 1
            else market_type_of(card.get("asset_class")),
        )
        for symbol in card.get("grounded_assets") or ()
    )
    query = query_for(card, item, cutoff=cutoff, assets=assets)
    candidates = await db.read("news_evidence_candidates", lambda repos: repos.news.evidence_candidates(query))
    selected = shortlist(candidates, query=query)
    background = (
        (
            await db.read(
                "news_evidence_background",
                lambda repos: repos.news.evidence_material([row["item_id"] for row in selected]),
            )
        )
        if selected
        else []
    )
    by_id = {row["item_id"]: row for row in background}
    ordered = [{**row, **by_id[row["item_id"]]} for row in selected if row["item_id"] in by_id]
    if len(members) > len(selected_members):
        exclusions += ("member_duplicate_or_material_cap",)
    prepared = assemble_evidence(
        card,
        item,
        query=query,
        candidates=ordered,
        members=current[1:],
        exclusions=exclusions,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return prepared.model_copy(update={"candidate_count": len(candidates), "member_candidate_count": len(members)})
