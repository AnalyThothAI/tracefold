"""Focused storage evidence for #288's Event kind and full Strategy provenance."""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.liquidations import PROGRAM_VERSION as LIQUIDATION_PROGRAM_VERSION
from tracefold.news.liquidations import TRIAGE_POLICY_VERSION as LIQUIDATION_TRIAGE_POLICY_VERSION
from tracefold.news.liquidations import judge as judge_liquidation
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.liquidations import trace as liquidation_metadata
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.news.oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from tracefold.news.oi_signals import OiPolicy, OiSignal, evaluate_oi, oi_judgment_trace, oi_parse_failure
from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION
from tracefold.news.source_contracts import (
    EVENT_KINDS,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
    SourceContractReason,
)

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
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
    event_kind = news.event_admission(event_id)["event_kind"]
    if event_kind == "news":
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
        origin = "model"
        judgment_sha256 = judgment.scored_judgment_sha256
        model_editorial = judgment.editorial.model_dump(mode="json")
        model = "test"
        program_version = SEMANTIC_PROGRAM_VERSION
        policy_version = TRIAGE_POLICY_VERSION
        trace: dict[str, Any] = {"editorial_sha256": judgment.editorial.editorial_sha256}
    elif event_kind == "oi":
        judgment, lane_trace = oi_parse_failure("invalid OI frame", provider_source="opennews")
        origin = "oi"
        judgment_sha256 = judgment.judgment_sha256
        model_editorial = None
        model = None
        program_version = OI_PROGRAM_VERSION
        policy_version = TRIAGE_POLICY_VERSION
        trace = {"oi_signal": lane_trace, "judgment": judgment.judgment_atom}
        error_code = "oi_parse_failed"
    else:
        fact = parse_liquidation(
            "BTC Large Short Liquidation 1M at $100000",
            item_id=f"{event_id}-item",
            fact_id=f"fact:{event_id}",
            provider_source="binance",
            event_at_ms=NOW,
            received_at_ms=NOW,
        )
        assert fact is not None
        judgment = judge_liquidation(fact)
        origin = "liquidation"
        judgment_sha256 = judgment.judgment_sha256
        model_editorial = None
        model = None
        program_version = LIQUIDATION_PROGRAM_VERSION
        policy_version = LIQUIDATION_TRIAGE_POLICY_VERSION
        trace = {"liquidation": liquidation_metadata(fact), "judgment": judgment.judgment_atom}
    runtime_manifest_sha = "b" * 64
    trace.update(
        {
            "judgment_contract_version": judgment.judgment_contract_version,
            "judgment_origin": origin,
            "judgment_sha256": judgment_sha256,
            "verdict_sha256": canonical_sha(judgment.verdict.model_dump(mode="json")),
            "runtime_manifest_sha": runtime_manifest_sha,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        }
    )
    news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=policy_version,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin=origin,
        rule_baseline_decision=judgment.decision.rule_baseline if origin != "model" else "drop",
        final_decision=judgment.decision.final if origin != "model" else "drop",
        override_rule=judgment.decision.override_rule if origin != "model" else None,
        throttled_by=judgment.decision.throttled_by if origin != "model" else None,
        verdict=judgment.verdict.model_dump(mode="json"),
        model_editorial=model_editorial,
        judgment_sha256=judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model=model,
        program_version=program_version,
        program_sha256="c" * 64,
        degraded=False,
        error_code=error_code,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=NOW,
    )


def test_oi_trade_projection_requires_one_canonical_signal_rank_and_source_identity(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    policy = OiPolicy()
    signal = OiSignal(
        symbol="BTC",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
    )
    judgment = evaluate_oi(signal, earlier_eligible_count=0, policy=policy)
    with repos.transaction():
        news.register_agent_runtime_manifest(
            manifest_sha="d" * 64,
            stable_bundle_sha="f" * 64,
            envelope_sha256="e" * 64,
            artifact_schema_version="news_program_artifact_v1",
            program_version=SEMANTIC_PROGRAM_VERSION,
            program_sha256="c" * 64,
            candidate_shas=(),
            image_digest="sha256:test",
            runtime_revision="projection-test",
            now_ms=NOW,
        )
        _item(news, "projection-oi-item")
        _event(news, "projection-oi-event", "projection-oi-item", "oi")
        evidence = news.latest_evidence_snapshot("projection-oi-event")
        news.insert_oi_signal(
            event_id="projection-oi-event",
            metric_version=OI_METRIC_VERSION,
            symbol=signal.symbol,
            direction=signal.direction,
            oi_change_bps=signal.oi_change_bps,
            oi_value_usd=signal.oi_value_usd,
            whale_long_profit_bps=signal.whale_long_profit_bps,
            whale_oi_ratio_bps=signal.whale_oi_ratio_bps,
            observed_at_ms=NOW,
            rank_in_window=judgment.rank_in_window,
            now_ms=NOW,
        )
        judgment_sha256 = judgment.judgment_sha256
        runtime_manifest_sha = "b" * 64
        trace = {
            "judgment_contract_version": judgment.judgment_contract_version,
            "judgment_origin": "oi",
            "judgment_sha256": judgment_sha256,
            "verdict_sha256": canonical_sha(judgment.verdict.model_dump(mode="json")),
            "runtime_manifest_sha": runtime_manifest_sha,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
            "policy": policy.as_dict(),
            "oi_signal": oi_judgment_trace(judgment, policy=policy),
            "judgment": judgment.judgment_atom,
        }
        news.insert_verdict(
            event_id="projection-oi-event",
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=judgment.judgment_contract_version,
            judgment_origin="oi",
            rule_baseline_decision=judgment.decision.rule_baseline,
            final_decision=judgment.decision.final,
            override_rule=judgment.decision.override_rule,
            throttled_by=judgment.decision.throttled_by,
            verdict=judgment.verdict.model_dump(mode="json"),
            model_editorial=None,
            judgment_sha256=judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model=None,
            program_version=OI_PROGRAM_VERSION,
            program_sha256="c" * 64,
            degraded=False,
            error_code=None,
            trace=trace,
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=NOW,
        )

    def projected() -> list[dict[str, Any]]:
        return news.trade_candidate_oi_rows(
            metric_version=OI_METRIC_VERSION,
            after_created_at_ms=NOW - 1,
            until_created_at_ms=NOW,
        )

    assert [(row["symbol"], row["rank_in_window"], row["source_rule"]) for row in projected()] == [
        ("BTC", 1, "opening_move_with_whale_concentration")
    ]
    conn.execute("UPDATE news_oi_signals SET rank_in_window = 2 WHERE event_id = 'projection-oi-event'")
    assert projected() == []
    conn.execute(
        "UPDATE news_oi_signals SET rank_in_window = 1, oi_value_usd = oi_value_usd + 1 "
        "WHERE event_id = 'projection-oi-event'"
    )
    assert projected() == []
    conn.execute(
        "UPDATE news_oi_signals SET oi_value_usd = oi_value_usd - 1, "
        "source_strategy_id = '1019', source_contract_version = 'opennews_oi_source_v1', "
        "measurement_window_ms = 300000 WHERE event_id = 'projection-oi-event'"
    )
    assert projected() == []


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
