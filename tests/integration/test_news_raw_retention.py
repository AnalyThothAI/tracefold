from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.program.runtime import PROGRAM_VERSION

pytestmark = pytest.mark.integration

DAY_MS = 86_400_000
NOW_MS = 2_000_000_000_000


def _seed_current_event(repos, *, event_id: str, at_ms: int, judged: bool) -> str:
    item_id = f"item-{event_id}"
    fact_id = f"fact-{event_id}"
    repos.news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=f"key-{event_id}",
        title=f"title {event_id}",
        raw_first_line=f"title {event_id}",
        description="",
        canonical_url=None,
        reporting_origin="OpenNews",
        published_at_ms=at_ms,
        observed_at_ms=at_ms,
        provider_metadata={},
        strategy_ids=[],
        ingest_mode="live",
        trace_id=f"trace-{event_id}",
        now_ms=at_ms,
    )
    repos.news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        dedupe_family="general",
        event_kind="news",
        comparison_fingerprint=f"fingerprint-{event_id}",
        comparison_title=f"title {event_id}",
        leader_title=f"title {event_id}",
        focus_fact_id=fact_id,
        focus_fact_text=f"title {event_id}",
        focus_fact_context="",
        focus_fact_method="whole_item",
        focus_span_start=0,
        focus_span_end=len(f"title {event_id}"),
        opened_at_ms=at_ms,
        expires_at_ms=at_ms + DAY_MS,
        admission="candidate",
        queue_priority="normal",
        provider_score=None,
        engine_type="news",
        asset_class="none",
        grounded_assets=[],
        watchlist_hits=[],
        macro_lexicon=False,
        storyline_key=f"story-{event_id}",
        context_line="",
        ingest_mode="live",
        trace_id=f"trace-{event_id}",
        band_keys=[],
        now_ms=at_ms,
    )
    evidence = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=at_ms)
    if judged:
        verdict = TriageVerdict(
            novelty="new_fact",
            assets=[],
            direction="neutral",
            scope="macro",
            magnitude=1,
            confidence=0.7,
            headline_zh="保留证据",
            why_zh="",
        )
        judgment = scored_judgment(verdict)
        runtime_manifest_sha = "b" * 64
        program_sha = "a" * 64
        verdict_payload = verdict.model_dump(mode="json")
        trace = {
            "judgment_contract_version": judgment.judgment_contract_version,
            "judgment_origin": "model",
            "judgment_sha256": judgment.scored_judgment_sha256,
            "verdict_sha256": canonical_sha(verdict_payload),
            "editorial_sha256": judgment.editorial.editorial_sha256,
            "runtime_manifest_sha": runtime_manifest_sha,
            "program_version": PROGRAM_VERSION,
            "program_sha256": program_sha,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        }
        repos.news.insert_verdict(
            event_id=event_id,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=judgment.judgment_contract_version,
            judgment_origin="model",
            rule_baseline_decision="drop",
            final_decision="drop",
            override_rule=None,
            throttled_by=None,
            verdict=verdict_payload,
            model_editorial=judgment.editorial.model_dump(mode="json"),
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model="retention-fixture",
            program_version=PROGRAM_VERSION,
            program_sha256=program_sha,
            degraded=False,
            error_code=None,
            trace=trace,
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=at_ms,
        )
    return str(evidence["evidence_sha256"])


def test_raw_retention_deletes_stable_bounded_batches_and_reports_backlog(
    tmp_path,
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    repos = repositories_for_connection(conn)
    try:
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'raw-' || value, 'opennews', 'key-' || value, 'raw ' || value,
                   value, value, '{}'::jsonb, 'live', value, value
              FROM generate_series(0, 4) AS value
            """,
        )
        conn.commit()

        batches: list[dict[str, object]] = []
        for _ in range(3):
            with repos.transaction():
                batches.append(repos.news.purge_before(cutoff_ms=10, batch_size=2))

        assert [batch["deleted_items"] for batch in batches] == [2, 2, 1]
        assert [batch["backlog_items"] for batch in batches] == [3, 1, 0]
        assert [batch["backlog_capped"] for batch in batches] == [True, False, False]
        assert [batch["oldest_observed_at_ms"] for batch in batches] == [2, 4, None]
        assert conn.execute("SELECT item_id FROM news_items ORDER BY observed_at_ms, item_id").fetchall() == []
    finally:
        conn.close()


def test_raw_retention_keeps_30_day_judged_corpus_and_expires_it_after_365_days(
    tmp_path,
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    repos = repositories_for_connection(conn)
    try:
        with repos.transaction():
            _seed_current_event(repos, event_id="raw-31d", at_ms=NOW_MS - 31 * DAY_MS, judged=False)
            retained_sha = _seed_current_event(
                repos,
                event_id="judged-31d",
                at_ms=NOW_MS - 31 * DAY_MS,
                judged=True,
            )
            _seed_current_event(repos, event_id="judged-366d", at_ms=NOW_MS - 366 * DAY_MS, judged=True)

        with repos.transaction():
            result = repos.news.purge_before(
                cutoff_ms=NOW_MS - 30 * DAY_MS,
                judged_cutoff_ms=NOW_MS - 365 * DAY_MS,
                batch_size=10,
            )

        assert result["deleted_items"] == 2
        assert result["backlog_items"] == 0
        assert conn.execute("SELECT item_id FROM news_items ORDER BY item_id").fetchall() == [
            {"item_id": "item-judged-31d"}
        ]
        retained = conn.execute(
            "SELECT evidence_sha256 FROM news_event_evidence_snapshots WHERE event_id = 'judged-31d'"
        ).fetchone()
        assert retained == {"evidence_sha256": retained_sha}
        assert conn.execute("SELECT count(*) AS n FROM news_verdicts WHERE event_id = 'judged-31d'").fetchone() == {
            "n": 1
        }
        assert conn.execute(
            "SELECT count(*) AS n FROM news_events WHERE event_id IN ('raw-31d', 'judged-366d')"
        ).fetchone() == {"n": 0}
    finally:
        conn.close()
