"""Prepare local material, optionally read one source URL, then freeze as-of inputs."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..bus import now_ms
from ..evidence import (
    DOCUMENT_CACHE_MS,
    DocumentResult,
    NewsDocumentReader,
    PreparedEvidence,
    assemble_evidence,
    query_for,
    shortlist,
)
from ..models import MarketAsset, market_type_of
from .runtime import NewsDatabasePort


async def prepare_evidence(
    db: NewsDatabasePort,
    card: Mapping[str, Any],
    *,
    catalog: Mapping[str, Sequence[str]],
    reader: NewsDocumentReader | None,
    allow_fetch: bool = True,
    clock: Callable[[], int] = now_ms,
) -> PreparedEvidence:
    started = time.monotonic()
    item = await db.read(
        "news_evidence_current", lambda repos: repos.news.evidence_item(str(card.get("leader_item_id") or ""))
    )
    document = None
    status = "not_needed"
    url = str(item.get("canonical_url") or "")
    text = str(item.get("evidence_text") or "")
    if len(text) < 800 and url and card.get("focus_fact_method") != "explicit_numbered":
        read_at = clock()
        cached = await db.read(
            "news_evidence_document_cache",
            lambda repos: repos.news.evidence_document(url, cutoff=read_at, since=read_at - DOCUMENT_CACHE_MS),
        )
        if cached:
            document, status = DocumentResult.model_validate(cached), "cache_hit"
        elif reader is not None and allow_fetch:
            document = await reader.read(url)
            status = document.status
            if status == "success":
                await db.tx("news_evidence_document_save", lambda repos: repos.news.save_evidence_document(document))
        else:
            status = "disabled" if reader is None else "already_attempted"
    elif not url:
        status = "no_url"
    cutoff = clock()
    assets = tuple(
        MarketAsset(
            str(symbol), market_type_of(classes[0]) if len(classes := catalog.get(str(symbol), ())) == 1 else "unknown"
        )
        for symbol in card.get("grounded_assets") or ()
    )
    query = query_for(card, item, cutoff=cutoff, assets=assets)
    candidates = await db.read("news_evidence_candidates", lambda repos: repos.news.evidence_candidates(query))
    selected = shortlist(candidates)
    material = (
        await db.read(
            "news_evidence_background",
            lambda repos: repos.news.evidence_background_material([row["item_id"] for row in selected]),
        )
        if selected
        else []
    )
    by_id = {row["item_id"]: row for row in material}
    ordered = [{**row, **by_id[row["item_id"]]} for row in selected if row["item_id"] in by_id]
    prepared = assemble_evidence(
        card,
        item,
        query=query,
        candidates=ordered,
        document=document,
        document_status=status,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return prepared.model_copy(update={"candidate_count": len(candidates)})
