"""Focused storage evidence for #288's Event kind and full Strategy provenance."""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.source_contracts import (
    EVENT_KINDS,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
    SourceContractReason,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_900_000_000_000


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    reset_postgres_schema(connection)
    yield connection
    connection.close()


def _item(news: Any, item_id: str, *, source_artifact_id: str = "artifact:shared") -> None:
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
        provider_metadata={"strategies": [{"id": item_id, "name": item_id}]},
        strategy_ids=(item_id,),
        ingest_mode="live",
        trace_id="trace",
        now_ms=NOW,
        source_artifact_id=source_artifact_id,
    )


def _event(
    news: Any,
    event_id: str,
    item_id: str,
    event_kind: EventKind,
    *,
    source_contract_reason: SourceContractReason | None = None,
) -> None:
    news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        family="general",
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
        admission="unsupported_market_contract" if event_kind == "unsupported_market" else "candidate",
        queue_priority="normal",
        provider_score=90,
        engine_type="news",
        asset_class="none",
        grounded_assets=(),
        watchlist_hits=(),
        macro_lexicon=False,
        storyline_key=f"story:{event_id}",
        context_line="",
        ingest_mode="live",
        trace_id="trace",
        band_keys=("same-band",),
        now_ms=NOW,
        source_contract_reason=source_contract_reason,
    )
    news.append_evidence_snapshot(event_id=event_id, now_ms=NOW)


def _verdict(news: Any, event_id: str, *, error_code: str | None = None) -> None:
    evidence = news.latest_evidence_snapshot(event_id)
    news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=f"policy:{event_id}",
        model_decision=None,
        rule_baseline_decision="drop",
        final_decision="drop",
        override_rule=None,
        throttled_by=None,
        verdict={},
        editorial={},
        scored_judgment_sha256="a" * 64,
        runtime_manifest_sha="b" * 64,
        model=None,
        program_version="news_test_v1",
        program_sha256="c" * 64,
        degraded=False,
        error_code=error_code,
        trace={},
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=NOW,
    )


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
                provider_metadata=metadata,
                strategy_ids=(strategy_id,),
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
    assert news.status_snapshot(now_ms=NOW + 1)["pipeline"]["telemetry_received_24h"] == 1


def test_exact_artifact_and_band_dedupe_never_cross_event_kind(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "news-item")
        _item(news, "oi-item")
        _event(news, "news-event", "news-item", "news")
        _event(news, "oi-event", "oi-item", "oi")

    assert (
        news.find_exact_event(family="general", event_kind="news", fingerprint="same-fingerprint", now_ms=NOW)[
            "event_id"
        ]
        == "news-event"
    )
    assert (
        news.find_exact_event(family="general", event_kind="oi", fingerprint="same-fingerprint", now_ms=NOW)["event_id"]
        == "oi-event"
    )
    assert (
        news.find_artifact_event(
            source_artifact_id="artifact:shared",
            family="general",
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
            family="general",
            event_kind="oi",
            fingerprint="same-fingerprint",
            item_id="new-item",
            opened_after_ms=NOW - 1,
        )["event_id"]
        == "oi-event"
    )
    assert [
        row["event_id"]
        for row in news.find_band_candidates(family="general", event_kind="news", band_keys=("same-band",), now_ms=NOW)
    ] == ["news-event"]
    assert [
        row["event_id"]
        for row in news.find_band_candidates(family="general", event_kind="oi", band_keys=("same-band",), now_ms=NOW)
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
                reason: SourceContractReason | None = None
                if event_kind == "unsupported_market":
                    reason = "unsupported_market_contract"
                elif event_kind == "oi" and suffix == "a":
                    reason = "source_contract_drift"
                _event(
                    news,
                    f"{event_kind}-event-{suffix}",
                    f"{event_kind}-item-{suffix}",
                    event_kind,
                    source_contract_reason=reason,
                )
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
                provider_metadata={
                    "strategies": [
                        {
                            "id": "1019",
                            "name": "OI Event Monitor",
                            "source_type": "market",
                            "engine_type": "market",
                        }
                    ]
                },
                strategy_ids=("1019",),
                ingest_mode="live",
                trace_id="trace",
                now_ms=NOW,
            )
        _verdict(news, "news-event-a")
        _verdict(news, "oi-event-a", error_code="oi_parse_failed")
        _verdict(news, "liquidation-event-a")

    def page(
        *channels: EventKind,
        limit: int = 20,
        cursor: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        return news.list_feed(
            family=None,
            admission=None,
            decision=None,
            symbol=None,
            q=None,
            limit=limit,
            cursor=cursor,
            outcome=outcome,
            channels=channels,
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
        if event_kind == "oi":
            assert detail["source_contract_reason"] == "source_contract_drift"
        elif event_kind == "unsupported_market":
            assert detail["source_contract_reason"] == "unsupported_market_contract"
        else:
            assert detail["source_contract_reason"] is None

    all_kinds = page(*EVENT_KINDS)
    assert {row["event_kind"] for row in all_kinds["events"]} == set(EVENT_KINDS)
    assert all_kinds["counts"]["total"] == len(EVENT_KINDS) * 2
    assert all_kinds["counts"]["total"] == sum(all_kinds["counts"][group] for group in ("pushed", "held", "pending"))
    unsupported_held = page("unsupported_market", outcome="held")
    assert {row["event_id"] for row in unsupported_held["events"]} == {
        "unsupported_market-event-a",
        "unsupported_market-event-b",
    }

    status = news.status_snapshot(now_ms=NOW + 1)["pipeline"]
    assert status["telemetry_received_24h"] == 2
    assert status["source_classifier_version"] == SOURCE_CONTRACT_CLASSIFIER_VERSION
    assert status["source_contracts_24h"] == {
        "news_v1": {"received": 2, "parsed": 2, "parse_failed": 0, "unsupported": 0, "verdict": 1},
        "listing_v1": {"received": 2, "parsed": 2, "parse_failed": 0, "unsupported": 0, "verdict": 0},
        "oi_v1": {"received": 2, "parsed": 1, "parse_failed": 1, "unsupported": 0, "verdict": 1},
        "liquidation_v1": {"received": 2, "parsed": 2, "parse_failed": 0, "unsupported": 0, "verdict": 1},
        "unsupported_market": {
            "received": 2,
            "parsed": 0,
            "parse_failed": 0,
            "unsupported": 2,
            "verdict": 0,
        },
    }


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
        family=None,
        admission=None,
        decision=None,
        symbol=None,
        q=None,
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
