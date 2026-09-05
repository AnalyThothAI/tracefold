"""Focused storage evidence for #288's Event kind and full Strategy provenance."""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.news.oi_signals import OiSignal, measurement_definition, oi_source_contract
from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION
from tracefold.news.source_contracts import (
    EVENT_KINDS,
    MARKET_PROVIDER,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
)

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _item(
    news: Any,
    item_id: str,
    *,
    source_artifact_id: str = "artifact:shared",
    market_kind: str | None = None,
) -> None:
    news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=item_id,
        title="Same durable fact appears under two source contracts",
        raw_first_line="",
        description="",
        canonical_url=f"https://example.test/{item_id}",
        reporting_origin="OpenNews",
        published_at_ms=NOW,
        observed_at_ms=NOW,
        provider_metadata_json=canonical_json({"strategies": [{"id": item_id, "name": item_id}]}),
        strategy_ids_json=canonical_json([item_id]),
        ingest_mode="live",
        trace_id="trace",
        now_ms=NOW,
        source_artifact_id=source_artifact_id,
        market_kind=market_kind,
        market_source_strategy_id="1019" if market_kind else None,
        market_parse_status="parsed" if market_kind else None,
        market_parse_error=None,
    )


def _event(news: Any, event_id: str, item_id: str, event_kind: EventKind) -> None:
    news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        dedupe_family="general",
        event_kind=event_kind,
        comparison_fingerprint="same-fingerprint",
        comparison_title="same comparison title",
        leader_title="Same durable fact appears under two source contracts",
        focus_fact_id=f"fact:{event_id}",
        focus_fact_text="Same durable fact appears under two source contracts",
        focus_fact_context="",
        focus_fact_method="whole_item",
        focus_span_start=0,
        focus_span_end=54,
        opened_at_ms=NOW,
        expires_at_ms=NOW + 3_600_000,
        admission="candidate",
        queue_priority="normal",
        provider_score=90,
        engine_type="news",
        asset_class="none",
        grounded_assets=(),
        grounded_assets_json="[]",
        watchlist_hits=(),
        watchlist_hits_json="[]",
        macro_lexicon=False,
        storyline_key=f"story:{event_id}",
        context_line="",
        ingest_mode="live",
        trace_id="trace",
        band_keys=("same-band",),
        now_ms=NOW,
    )
    news.append_evidence_snapshot(event_id=event_id, now_ms=NOW)


def _verdict(news: Any, event_id: str, *, error_code: str | None = None) -> None:
    """One model verdict for one editorial Event.

    The market branches this helper used to carry left with the judges they built (#553): a market
    observation is a stored fact, so there is no verdict for it to write.
    """

    evidence = news.latest_evidence_snapshot(event_id)
    judgment = scored_judgment(
        TriageVerdict(
            novelty="new_fact",
            assets=[],
            direction="neutral",
            scope="single_name",
            magnitude=0,
            confidence=1.0,
            headline_zh="测试新闻判断",
        )
    )
    runtime_manifest_sha = "b" * 64
    program_sha256 = "c" * 64
    trace: dict[str, Any] = {
        "editorial_sha256": judgment.editorial.editorial_sha256,
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": "model",
        "judgment_sha256": judgment.scored_judgment_sha256,
        "verdict_sha256": canonical_sha(judgment.verdict.model_dump(mode="json")),
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": SEMANTIC_PROGRAM_VERSION,
        "program_sha256": program_sha256,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
    }
    news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=TRIAGE_POLICY_VERSION,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin="model",
        rule_baseline_decision="drop",
        final_decision="drop",
        override_rule=None,
        throttled_by=None,
        verdict=judgment.verdict.model_dump(mode="json"),
        model_editorial=judgment.editorial.model_dump(mode="json"),
        judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model="test",
        program_version=SEMANTIC_PROGRAM_VERSION,
        program_sha256=program_sha256,
        degraded=False,
        error_code=error_code,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=NOW,
    )


def _insert_oi(news: Any, *, event_id: str, item_id: str, signal: OiSignal, **over: Any) -> None:
    source = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert source is not None
    fields: dict[str, Any] = {
        "event_id": event_id,
        "metric_version": OI_METRIC_VERSION,
        "symbol": signal.symbol,
        "raw_instrument": signal.raw_instrument,
        "direction": signal.direction,
        "oi_change_bps": signal.oi_change_bps,
        "oi_value_usd": signal.oi_value_usd,
        "whale_long_profit_bps": signal.whale_long_profit_bps,
        "whale_oi_ratio_bps": signal.whale_oi_ratio_bps,
        "observed_at_ms": NOW,
        "received_at_ms": NOW,
        "now_ms": NOW,
        "provider": MARKET_PROVIDER,
        "source_strategy_id": source.strategy_id,
        "source_contract_version": source.contract_version,
        "measurement_window_ms": source.measurement_window_ms,
        "measurement_definition": measurement_definition(source),
        "source_item_id": item_id,
        "source_venue": "binance",
        "historical": False,
    }
    fields.update(over)
    news.insert_oi_signal(**fields)


def test_oi_trade_projection_reads_the_ledger_without_an_event_or_a_verdict(conn) -> None:
    """#553. The projection is one ledger and one Item column, and Triage never runs.

    This is the acceptance case in one test: an OI fact written at admission is readable by Trading
    with no News Event, no verdict and no reader history anywhere in the database.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    signal = OiSignal(
        symbol="BTC",
        raw_instrument="XYZ-BTC",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
    )
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [Instrument("binance.perp", "BTCUSDT", "BTC", "crypto", "USDT")],
            now_ms=NOW - 1,
        )
        _item(news, "projection-oi-item")
        _insert_oi(news, event_id="projection-oi-event", item_id="projection-oi-item", signal=signal)

    def projected() -> list[dict[str, Any]]:
        return news.trade_candidate_oi_rows(
            metric_version=OI_METRIC_VERSION,
            after_created_at_ms=NOW - 1,
            until_created_at_ms=NOW,
        )

    assert conn.execute("SELECT count(*) AS n FROM news_events").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM news_verdicts").fetchone()["n"] == 0
    assert [(row["symbol"], row["ingest_mode"], row["venue"]) for row in projected()] == [("BTC", "live", "binance")]
    # `trade_evidence_catalog_rows` is gone with the fourth copy of the source-venue table it carried
    # inside a SQL `CASE`. Nothing in `tracefold/` ever called it, and the copy had already drifted:
    # it mapped no `hl.xyz` at all (#537 PR-3).
    assert not hasattr(news, "trade_evidence_catalog_rows")
    source_rows = news.trade_fixed_window_oi_sources(
        metric_version=OI_METRIC_VERSION,
        start_observed_at_ms=NOW,
        end_observed_at_ms=NOW + 1,
        drain_cutoff_ms=NOW,
        limit=20,
    )
    assert [(row["event_id"], row["source_venue"]) for row in source_rows] == [("projection-oi-event", "binance")]
    # Ingest provenance is published, never filtered here (#510): the Signal lane refuses a recovery
    # frame by name, and a read that dropped it would make "no rows" and "no eligible rows" the same
    # absence again. The mode is the Item's, because the Item is what the parser read.
    conn.execute("UPDATE news_items SET first_ingest_mode = 'recovery' WHERE item_id = 'projection-oi-item'")
    assert [row["ingest_mode"] for row in projected()] == ["recovery"]
    conn.execute("UPDATE news_items SET first_ingest_mode = 'live' WHERE item_id = 'projection-oi-item'")
    conn.execute(
        "UPDATE news_oi_signals SET oi_value_usd = oi_value_usd + 1 WHERE source_item_id = 'projection-oi-item'"
    )
    assert [row["oi_value_usd"] for row in projected()] == [32_170_001]


def test_one_item_is_one_observation_and_a_replay_of_it_adds_no_row(conn) -> None:
    """#553 §3.1. The observation key is `(source_item_id, metric_version)`.

    A provider replay of the same record is the same observation whatever else has happened to it, and
    the second write is a no-op rather than a second measurement.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    signal = OiSignal(
        symbol="BTC",
        raw_instrument="BTC",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
    )
    with repos.transaction():
        _item(news, "replay-oi-item")
        _insert_oi(news, event_id="replay-oi-event", item_id="replay-oi-item", signal=signal)
        _insert_oi(
            news,
            event_id="replay-oi-event",
            item_id="replay-oi-item",
            signal=signal,
            oi_value_usd=99,
        )

    rows = conn.execute(
        "SELECT event_id, oi_value_usd FROM news_oi_signals WHERE source_item_id = 'replay-oi-item'"
    ).fetchall()
    assert [(row["event_id"], row["oi_value_usd"]) for row in rows] == [("replay-oi-event", 32_170_000)]


def test_a_historical_rebuild_is_readable_and_stays_out_of_the_live_trigger_set(conn) -> None:
    """#553 §3.3. A reconstructed fact is readable evidence and never a new trade trigger."""

    repos = repositories_for_connection(conn)
    news = repos.news
    signal = OiSignal(
        symbol="BTC",
        raw_instrument="BTC",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
    )
    with repos.transaction():
        _item(news, "historical-oi-item", market_kind="oi")
        _insert_oi(news, event_id="historical-oi-event", item_id="historical-oi-item", signal=signal, historical=True)

    stored = conn.execute(
        "SELECT historical, available_at_ms, observed_at_ms FROM news_oi_signals"
        " WHERE source_item_id = 'historical-oi-item'"
    ).fetchone()
    assert stored["historical"] is True
    # The original provider stamp is untouched; only the first-available instant is the rebuild's.
    assert stored["observed_at_ms"] == NOW
    # It is not a trigger: no scan could have seen it at the time, so authoring a Case from it would
    # invent a decision, and a replay of the frozen window would then disagree with the live lane.
    assert (
        news.trade_candidate_oi_rows(
            metric_version=OI_METRIC_VERSION,
            after_created_at_ms=NOW - 1,
            until_created_at_ms=NOW,
        )
        == []
    )
    assert (
        news.trade_evidence_oi_rows(
            metric_version=OI_METRIC_VERSION,
            start_observed_at_ms=NOW - 1,
            end_observed_at_ms=NOW + 1,
            known_at_or_before_ms=NOW,
            available_at_or_before_ms=NOW,
        )
        == []
    )
    # And it is readable, which is the whole reason it was reconstructed.
    live = news.market_groups(
        kinds=("oi",),
        from_ms=NOW - 1,
        to_ms=NOW + 1,
        cursor_received_at_ms=1 << 62,
        cursor_item_id="",
        limit=10,
    )
    assert [group["latest"]["historical"] for group in live] == [True]


def test_item_redelivery_unions_full_strategy_tuples_and_preserves_first_metadata(conn) -> None:
    news = repositories_for_connection(conn).news
    base = {
        "score": 91,
        "source": "first-source",
        "strategies": [
            {"id": "2083", "name": "Large-scale liquidation", "source_type": "market", "engine_type": "market"}
        ],
    }
    rebound = {
        "score": 1,
        "source": "second-source",
        "strategies": [{"id": "1019", "name": "OI Event Monitor", "source_type": "market", "engine_type": "market"}],
    }
    with repositories_for_connection(conn).transaction():
        for metadata, strategy_id in ((base, "2083"), (rebound, "1019"), (base, "2083")):
            news.upsert_item(
                item_id="same-item",
                source_id="opennews",
                source_item_key="same-record",
                title="same title",
                raw_first_line="",
                description="",
                canonical_url=None,
                reporting_origin="OpenNews",
                published_at_ms=NOW,
                observed_at_ms=NOW,
                provider_metadata_json=canonical_json(metadata),
                strategy_ids_json=canonical_json([strategy_id]),
                ingest_mode="live",
                trace_id="trace",
                now_ms=NOW,
            )
    row = conn.execute("SELECT provider_metadata, provenance FROM news_items WHERE item_id='same-item'").fetchone()
    assert row["provider_metadata"] == {
        "score": 91,
        "source": "first-source",
        "strategies": [
            {"id": "2083", "name": "Large-scale liquidation", "source_type": "market", "engine_type": "market"},
            {"id": "1019", "name": "OI Event Monitor", "source_type": "market", "engine_type": "market"},
        ],
    }
    assert row["provenance"] == ["1019", "2083"]


def test_exact_artifact_and_band_dedupe_never_cross_event_kind(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "news-item")
        _item(news, "oi-item")
        _event(news, "news-event", "news-item", "news")
        _event(news, "oi-event", "oi-item", "oi")

    assert (
        news.find_exact_event(dedupe_family="general", event_kind="news", fingerprint="same-fingerprint", now_ms=NOW)[
            "event_id"
        ]
        == "news-event"
    )
    assert (
        news.find_exact_event(dedupe_family="general", event_kind="oi", fingerprint="same-fingerprint", now_ms=NOW)[
            "event_id"
        ]
        == "oi-event"
    )
    assert (
        news.find_artifact_event(
            source_artifact_id="artifact:shared",
            dedupe_family="general",
            event_kind="news",
            fingerprint="same-fingerprint",
            item_id="new-item",
            opened_after_ms=NOW - 1,
        )["event_id"]
        == "news-event"
    )
    assert (
        news.find_artifact_event(
            source_artifact_id="artifact:shared",
            dedupe_family="general",
            event_kind="oi",
            fingerprint="same-fingerprint",
            item_id="new-item",
            opened_after_ms=NOW - 1,
        )["event_id"]
        == "oi-event"
    )
    assert [
        row["event_id"]
        for row in news.find_band_candidates(
            dedupe_family="general", event_kind="news", band_keys=("same-band",), now_ms=NOW
        )
    ] == ["news-event"]
    assert [
        row["event_id"]
        for row in news.find_band_candidates(
            dedupe_family="general", event_kind="oi", band_keys=("same-band",), now_ms=NOW
        )
    ] == ["oi-event"]
    assert news.event_card("oi-event")["event_kind"] == "oi"
    assert news.event_admission("oi-event") == {
        "admission": "candidate",
        "event_kind": "oi",
        "storyline_key": "story:oi-event",
    }


def test_feed_detail_filters_counts_and_status_project_the_closed_event_kinds(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for event_kind in EVENT_KINDS:
            for suffix in ("a", "b"):
                _item(news, f"{event_kind}-item-{suffix}", source_artifact_id=f"artifact:{event_kind}:{suffix}")
                _event(news, f"{event_kind}-event-{suffix}", f"{event_kind}-item-{suffix}", event_kind)
        for suffix in ("a", "b"):
            news.upsert_item(
                item_id=f"oi-before-gate-{suffix}",
                source_id="opennews",
                source_item_key=f"oi-before-gate-{suffix}",
                title="OI frame rejected before Event admission",
                raw_first_line="",
                description="",
                canonical_url=None,
                reporting_origin="OpenNews",
                published_at_ms=NOW,
                observed_at_ms=NOW,
                provider_metadata_json=canonical_json(
                    {
                        "strategies": [
                            {
                                "id": "1019",
                                "name": "OI Event Monitor",
                                "source_type": "market",
                                "engine_type": "market",
                            }
                        ]
                    }
                ),
                strategy_ids_json=canonical_json(["1019"]),
                ingest_mode="live",
                trace_id="trace",
                now_ms=NOW,
            )
        _verdict(news, "news-event-a")

    def page(
        *channels: EventKind,
        limit: int = 20,
        cursor: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        return news.list_feed(
            event_family=None,
            change_state=None,
            assertion_status=None,
            source_authority=None,
            subject_code=None,
            admission=None,
            final_decision=None,
            search=None,
            limit=limit,
            cursor=cursor,
            outcome=outcome,
            event_kind=channels,
            now_ms=NOW + 1,
        )

    for event_kind in EVENT_KINDS:
        first = page(event_kind, limit=1)
        assert [(row["event_id"], row["event_kind"]) for row in first["events"]] == [
            (f"{event_kind}-event-b", event_kind)
        ]
        assert first["counts"]["total"] == 2
        assert first["next_cursor"] is not None
        second = page(event_kind, limit=1, cursor=first["next_cursor"])
        assert [(row["event_id"], row["event_kind"]) for row in second["events"]] == [
            (f"{event_kind}-event-a", event_kind)
        ]
        assert second["counts"] is None
        assert second["next_cursor"] is None
        detail = news.event_detail(f"{event_kind}-event-a")["event"]
        assert detail["event_kind"] == event_kind

    all_kinds = page(*EVENT_KINDS)
    assert {row["event_kind"] for row in all_kinds["events"]} == set(EVENT_KINDS)
    assert all_kinds["counts"]["total"] == len(EVENT_KINDS) * 2
    assert all_kinds["counts"]["total"] == sum(all_kinds["counts"][group] for group in ("pushed", "held", "pending"))
    status = news.status_snapshot(now_ms=NOW + 1)["pipeline"]
    assert status["source_classifier_version"] == SOURCE_CONTRACT_CLASSIFIER_VERSION
    # The funnel is the editorial one. Market intake is a market question and `market_sources`
    # answers it from the facts themselves (#553), so nothing here reports a lane it cannot see.
    assert status["source_contracts_24h"] == {
        "news_v1": {"received": 2, "parsed": 2, "verdict": 1},
        "listing_v1": {"received": 2, "parsed": 2, "verdict": 0},
    }
    assert {"telemetry_received_24h", "telemetry_parsed_24h", "oi"}.isdisjoint(status)


def test_terminal_delivery_without_a_verdict_is_held_in_both_row_and_tab_partition(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "terminal-item")
        _event(news, "terminal-event", "terminal-item", "news")
        assert news.begin_delivery(event_id="terminal-event", kind="first", card={}, now_ms=NOW) == "new"
        assert news.settle_delivery(
            event_id="terminal-event",
            kind="first",
            state="terminal",
            receipt=None,
            error_code="delivery_unavailable",
            now_ms=NOW,
        )

    common = dict(
        event_family=None,
        change_state=None,
        assertion_status=None,
        source_authority=None,
        subject_code=None,
        admission=None,
        final_decision=None,
        event_kind=None,
        search=None,
        limit=20,
        cursor=None,
        now_ms=NOW + 1,
    )
    all_rows = news.list_feed(**common)
    held = news.list_feed(**common, outcome="held")
    row = next(event for event in all_rows["events"] if event["event_id"] == "terminal-event")
    assert row["outcome"]["kind"] == "delivery_failed"
    assert {event["event_id"] for event in held["events"]} == {"terminal-event"}
    assert all_rows["counts"] == {"total": 1, "pushed": 0, "held": 1, "pending": 0}


def test_oi_frame_whose_provider_clock_ran_ahead_stores_on_the_first_attempt(conn) -> None:
    """#544. `observed_at_ms > available_at_ms` is two recorded clocks, not a malformed frame."""

    repos = repositories_for_connection(conn)
    news = repos.news
    signal = OiSignal(
        symbol="BTC",
        raw_instrument="BTC",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
    )
    with repos.transaction():
        _item(news, "ahead-oi-item")
        _insert_oi(
            news,
            event_id="ahead-oi-event",
            item_id="ahead-oi-item",
            signal=signal,
            observed_at_ms=NOW + 250,
        )

    stored = conn.execute(
        "SELECT observed_at_ms, received_at_ms, available_at_ms FROM news_oi_signals"
        " WHERE source_item_id = 'ahead-oi-item'"
    ).fetchone()
    assert stored["observed_at_ms"] - stored["available_at_ms"] == 250
    assert stored["received_at_ms"] == NOW


def test_liquidation_whose_venue_clock_ran_ahead_stores_like_any_other(conn) -> None:
    """#544 and #553. The forced trade happened; the venue stamped it 250 ms ahead of this host."""

    repos = repositories_for_connection(conn)
    news = repos.news
    ahead = parse_liquidation(
        "BTC Large Short Liquidation 1M at $100000",
        item_id="ahead-liq-item",
        fact_id="fact:ahead-liq",
        source_strategy_id="2083",
        provider_source="okx",
        event_at_ms=NOW + 250,
        received_at_ms=NOW,
    )
    assert ahead is not None
    with repos.transaction():
        _item(news, "ahead-liq-item")
        news.insert_market_liquidation(fact=ahead, ingest_mode="live", now_ms=NOW)

    stored = conn.execute(
        "SELECT source_venue, source_strategy_id, raw_instrument, event_at_ms, received_at_ms"
        " FROM news_market_liquidations WHERE item_id = 'ahead-liq-item'"
    ).fetchone()
    assert stored["event_at_ms"] - stored["received_at_ms"] == 250
    assert (stored["source_venue"], stored["source_strategy_id"], stored["raw_instrument"]) == ("okx", "2083", "BTC")


def test_a_purged_item_takes_its_typed_market_facts_with_it(conn) -> None:
    """#553 §3.4. The liquidation ledger had no foreign key, so a purge left unreachable orphans."""

    repos = repositories_for_connection(conn)
    news = repos.news
    fact = parse_liquidation(
        "BTC Large Short Liquidation 1M at $100000",
        item_id="cascade-liq-item",
        fact_id="fact:cascade",
        source_strategy_id="2083",
        provider_source="binance",
        event_at_ms=NOW,
        received_at_ms=NOW,
    )
    assert fact is not None
    with repos.transaction():
        _item(news, "cascade-liq-item")
        news.insert_market_liquidation(fact=fact, ingest_mode="live", now_ms=NOW)

    conn.execute("DELETE FROM news_items WHERE item_id = 'cascade-liq-item'")
    remaining = conn.execute(
        "SELECT count(*) AS n FROM news_market_liquidations WHERE item_id = 'cascade-liq-item'"
    ).fetchone()
    assert remaining["n"] == 0
