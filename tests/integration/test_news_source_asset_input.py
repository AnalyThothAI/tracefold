"""Stored provider coins accompany only their own pending semantic sources, including optional reads."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.news_update_pg import EVENT, seed_event, sql, store
from tracefold.news.updates.projection import reading_views

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def set_coins(event_id: str, coins: list[dict[str, str]]) -> None:
    sql(
        "UPDATE news_items SET provider_metadata = %s::jsonb WHERE item_id = %s",
        (
            json.dumps({"coins": coins}),
            f"it-{event_id}",
        ),
    )


def test_storage_loaded_candidates_bind_reanalysis_listing_and_source_identity() -> None:
    pg, db, clock = store()
    seed_event()
    empty = asyncio.run(pg.semantic.input_for(EVENT))
    set_coins(EVENT, [{"symbol": "xyz-ORCLUSDT", "market_type": "equity", "grade": "1"}])
    source = asyncio.run(pg.semantic.input_for(EVENT))
    assert source.evidence == empty.evidence and source.input_sha != empty.input_sha
    candidate = source.asset_candidates[source.evidence[0].ref][0]
    assert (candidate.symbol, candidate.market_type, candidate.grade) == ("xyz-ORCLUSDT", "equity", "1")
    listing = asyncio.run(
        db.read(
            "source_candidates",
            lambda repos: repos.news.semantic_work.reanalysis_scope_list(
                event_id=EVENT,
                now_ms=clock(),
                input=repos.news.semantic_input,
            ),
        )
    )
    assert listing["scopes"][0]["read_ref"] == reading_views(source)[0].read_ref
    assert listing["scopes"][0]["read_ref"] != reading_views(empty)[0].read_ref


def test_optional_read_candidates_come_from_loaded_record_instead_of_event_leader() -> None:
    pg, db, clock = store()
    seed_event()
    seed_event("optional", text="Oracle reports quarterly earnings.", fingerprint="optional")
    set_coins(EVENT, [{"symbol": "BTC", "market_type": "crypto", "grade": "3"}])
    set_coins("optional", [{"symbol": "ORCL", "market_type": "equity", "grade": "1"}])
    target = asyncio.run(pg.semantic.input_for("optional")).evidence[0]
    asyncio.run(
        db.tx(
            "attach_source",
            lambda repos: repos.news.semantic_work.attach_extra_evidence(
                event_id=EVENT,
                lineage_id=f"lineage-{EVENT}",
                evidence_json=json.dumps([target.model_dump(mode="json")]),
                focus_claim_refs=(),
                now_ms=clock(),
            ),
        ),
    )
    source = asyncio.run(pg.semantic.input_for(EVENT))
    assert source.evidence == (target,)
    assert set(source.asset_candidates) == {target.ref}
    assert [row.symbol for row in source.asset_candidates[target.ref]] == ["ORCL"]
