"""Stored provider coins accompany only their own pending semantic sources, including optional reads."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.news_update_pg import EVENT, TaskBackend, draft, run_agent, seed_event, set_semantic_job, sql, store
from tracefold.news.market_review.pricing import QuoteRequest
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.updates.contracts import Asset, Extraction, FrozenInput
from tracefold.news.updates.judgment import NewsJudgments
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def instrument(symbol: str, market: str, *, venue: str = "hl.xyz", status: str = "trading") -> None:
    sql(
        """INSERT INTO news_market_instruments
           (venue, venue_symbol, base_symbol, instrument_class, status, observed_at_ms)
           VALUES (%s, %s, %s, %s, %s, 100)""",
        (venue, symbol, symbol, market, status),
    )


def test_candidates_freeze_only_traded_canonical_base_markets() -> None:
    pg, _db, _clock = store()
    seed_event(text="NEAR, PENDLE and TSLA report updates as Brent crude rises.")
    symbols = ["NEAR", "xyz-PENDLEUSDT", "TSLA", "CL", "REFONLY", "UNCLASSIFIED", "DEAD"]
    set_coins(EVENT, [{"symbol": symbol, "market_type": "cex", "grade": "C"} for symbol in symbols])
    sql("DELETE FROM news_market_instruments")
    instrument("NEAR", "crypto", venue="binance.perp")
    instrument("NEAR", "crypto", venue="hl.spot")
    instrument("NEAR", "equity", venue="us.listed")
    instrument("PENDLE", "crypto", venue="binance.perp")
    instrument("TSLA", "crypto", venue="hl.spot")
    instrument("TSLA", "equity")
    instrument("CL", "commodity")
    instrument("REFONLY", "equity", venue="us.listed")
    instrument("UNCLASSIFIED", "unknown")
    instrument("DEAD", "equity", status="delisted")
    sql(
        "INSERT INTO news_symbol_aliases(alias, base_symbol, source, updated_at_ms)"
        " VALUES ('PENDLEUSDT', 'PENDLE', 'venue', 100)"
    )
    source = asyncio.run(pg.semantic.input_for(EVENT))
    candidates = source.asset_candidates[source.evidence[0].ref]
    assert [candidate.symbol for candidate in candidates] == symbols
    assert [candidate.listed_markets for candidate in candidates] == [
        ("crypto",),
        ("crypto",),
        ("crypto", "equity"),
        ("commodity",),
        (),
        (),
        (),
    ]
    assert all(candidate.market_type == "unknown" for candidate in candidates)


def test_catalogue_refresh_does_not_reopen_processed_source_reads() -> None:
    pg, _db, _clock = store()
    seed_event(text="NEAR announces a network update.")
    set_coins(EVENT, [{"symbol": "NEAR", "market_type": "cex"}, {"symbol": "CL", "market_type": "cex"}])
    original = asyncio.run(pg.semantic.input_for(EVENT))
    read_ref = reading_views(original)[0].read_ref
    instrument("NEAR", "crypto", venue="binance.perp")
    refreshed = asyncio.run(pg.semantic.input_for(EVENT))
    assert refreshed.input_sha != original.input_sha
    assert reading_views(refreshed)[0].read_ref == read_ref
    assert [candidate.symbol for candidate in refreshed.asset_candidates[refreshed.evidence[0].ref]] == ["NEAR"]
    set_semantic_job(EVENT, processed_read_refs=[read_ref], wanted_revision=2)
    assert asyncio.run(pg.semantic.input_for(EVENT)).evidence == ()


def test_unambiguous_candidate_market_reaches_adopted_facts_and_typed_instrument_resolution() -> None:
    pg, db, clock = store()
    seed_event(text="NEAR announces a network upgrade.")
    set_coins(EVENT, [{"symbol": "NEAR", "market_type": "cex"}, {"symbol": "CL", "market_type": "cex"}])
    instrument("NEAR", "crypto", venue="binance.perp")

    class Extractor:
        identity = "unknown-market-extractor"

        async def extract(self, source: FrozenInput) -> Extraction:
            evidence = source.evidence[0]
            assert [candidate.symbol for candidate in source.asset_candidates[evidence.ref]] == ["NEAR"]
            claim = draft(evidence)
            claim = claim.model_copy(
                update={
                    "fields": claim.fields.model_copy(
                        update={"assets": (Asset(symbol="NEAR", market_type="unknown", role="primary"),)}
                    )
                }
            )
            return Extraction(claims=(claim,))

    analyzer = SemanticAnalyzer(
        Extractor(), NewsJudgments(generated=TaskBackend({"support": "supports"}), cache=PgJudgmentCache(db))
    )
    agent = NewsAgent(pg.semantic, analyzer, program_identity="program-788", clock=clock)
    assert asyncio.run(run_agent(agent, EVENT)) == "adopted"
    head = asyncio.run(pg.semantic.head(EVENT))
    assert head is not None
    assert head.current_claims[0].fields.assets == (Asset(symbol="NEAR", market_type="crypto", role="primary"),)
    understanding = sql("SELECT understanding FROM news_analyses WHERE adopted_at_ms IS NOT NULL")[0]["understanding"]
    assert understanding["claims"][0]["fields"]["assets"][0]["market_type"] == "crypto"
    request = QuoteRequest("NEAR", "crypto")
    refs = asyncio.run(db.read("typed_refs", lambda repos: repos.instruments.asset_refs([request])))
    assert refs[request]["resolution_state"] == "resolved"
    assert refs[request]["venue"] == "binance.perp"


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
