"""Exercise the v5 repository against a disposable schema-0301 database."""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.repository import NewsRepository
from tracefold.platform.postgres.postgres_migrations import upgrade_head

NOW_MS = 1_800_000_000_000
EVENT_ID = "rollback-schema0301-drill"
ITEM_ID = "rollback-schema0301-item"
FOCUS_ID = "f" * 64


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def drill(database_url: str) -> None:
    upgrade_head(database_url)
    artifact = load_stable_program_artifact()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        repository = NewsRepository(conn)
        inserted_item = repository.upsert_item(
            item_id=ITEM_ID,
            source_id="rollback-drill",
            source_item_key="schema0301",
            title="Rollback schema 0301 drill",
            raw_first_line="Rollback schema 0301 drill",
            description="Independent Program v5 image validation",
            canonical_url=None,
            reporting_origin="rollback-drill",
            published_at_ms=NOW_MS,
            observed_at_ms=NOW_MS,
            provider_metadata={"score": 95},
            strategy_ids=("rollback-drill",),
            ingest_mode="live",
            trace_id="rollback-drill-trace",
            now_ms=NOW_MS,
        )
        _require(inserted_item, "news_rollback_drill_item_not_inserted")
        repository.insert_event(
            event_id=EVENT_ID,
            leader_item_id=ITEM_ID,
            family="general",
            comparison_fingerprint="a" * 64,
            comparison_title="rollback schema 0301 drill",
            leader_title="Rollback schema 0301 drill",
            focus_fact_id=FOCUS_ID,
            focus_fact_text="Rollback schema 0301 drill",
            focus_fact_context="Independent Program v5 image validation",
            focus_fact_method="whole_item",
            focus_span_start=0,
            focus_span_end=30,
            opened_at_ms=NOW_MS,
            expires_at_ms=NOW_MS + 3_600_000,
            admission="candidate",
            priority="high",
            provider_score=95,
            engine_type="news",
            asset_class="crypto",
            grounded_assets=("BTC",),
            watchlist_hits=(),
            macro_lexicon=False,
            storyline_key="asset:BTC",
            context_line="",
            ingest_mode="live",
            trace_id="rollback-drill-trace",
            band_keys=(),
            now_ms=NOW_MS,
        )
        evidence = repository.append_evidence_snapshot(event_id=EVENT_ID, now_ms=NOW_MS)
        card = repository.event_card(EVENT_ID)
        _require(card is not None and card["priority"] == "high", "news_rollback_drill_card_priority_mismatch")
        _require(
            dict(evidence["snapshot"])["schema_version"] == "news_event_evidence_v1",
            "news_rollback_drill_evidence_schema_mismatch",
        )

        inserted_verdict = repository.insert_verdict(
            event_id=EVENT_ID,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            model_decision="drop",
            rule_baseline_decision="drop",
            final_decision="drop",
            override_rule="model_hold",
            throttled_by=None,
            verdict={"decision": "drop", "headline_zh": "回滚演练"},
            model="rollback-drill",
            program_version=artifact.program_version,
            program_sha256=artifact.program_sha256,
            degraded=False,
            error_code=None,
            trace={"rollback_drill": True},
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=FOCUS_ID,
            now_ms=NOW_MS,
        )
        _require(inserted_verdict, "news_rollback_drill_verdict_not_inserted")
        stored = conn.execute(
            "SELECT e.queue_priority, v.policy_version, v.editorial, "
            "v.scored_judgment_sha256, v.runtime_manifest_sha "
            "FROM news_events e JOIN news_verdicts v ON v.event_id = e.event_id "
            "WHERE e.event_id = %s",
            (EVENT_ID,),
        ).fetchone()
        _require(stored is not None, "news_rollback_drill_verdict_missing")
        _require(stored["queue_priority"] == "high", "news_rollback_drill_queue_priority_mismatch")
        _require(stored["policy_version"] == "news_triage_policy_v9", "news_rollback_drill_policy_mismatch")
        _require(stored["editorial"] is None, "news_rollback_drill_editorial_not_null")
        _require(stored["scored_judgment_sha256"] is None, "news_rollback_drill_scored_sha_not_null")
        _require(stored["runtime_manifest_sha"] is None, "news_rollback_drill_manifest_sha_not_null")

        feed = repository.list_feed(
            family=None,
            admission=None,
            priority="high",
            decision=None,
            symbol=None,
            q=None,
            sort="priority",
            limit=10,
            cursor=None,
            now_ms=NOW_MS,
        )
        _require(feed["events"][0]["priority"] == "high", "news_rollback_drill_feed_priority_mismatch")
        detail = repository.event_detail(EVENT_ID)
        _require(
            detail is not None and detail["event"]["priority"] == "high",
            "news_rollback_drill_detail_priority_mismatch",
        )
        conn.rollback()


if __name__ == "__main__":
    dsn = os.environ.get("TRACEFOLD_ROLLBACK_DRILL_DSN")
    if not dsn:
        raise RuntimeError("TRACEFOLD_ROLLBACK_DRILL_DSN_required")
    drill(dsn)
    print("news_program_v5_schema0301_rollback_drill_ok")
