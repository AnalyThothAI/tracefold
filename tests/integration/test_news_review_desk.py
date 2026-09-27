from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_legacy import (
    LEGACY_JUDGMENT_CONTRACT_VERSION,
    LEGACY_PROGRAM_VERSION,
    LEGACY_TRIAGE_POLICY_VERSION,
    legacy_judgment,
    legacy_taxonomy,
)
from tests.support.news_legacy_storage import legacy_news
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.review.desk import (
    DecisionFeedbackSubmission,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)

pytestmark = pytest.mark.integration

NOW = 1_787_287_000_000
PRINCIPAL = Principal(subject="operator")
ACTIVE_BUNDLE = "1" * 64
APPOINTED_AT_MS = NOW - 24 * 3_600_000


def _appoint_legacy_agent(conn, bundle_sha: str, *, now_ms: int) -> None:
    """The last Agent appointment the retired runtime recorded; the market view defaults to its cohort."""

    payload = {"stable_sha": bundle_sha, "runtime_manifest_sha": "a" * 64, "registered_at_ms": now_ms}
    document = json.dumps({"kind": "active_agent", "payload": payload}, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'active_agent', NULL, %s::jsonb, 'worker_startup', %s)",
        (hashlib.sha256(document.encode()).hexdigest(), json.dumps(payload), now_ms),
    )


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    with repositories_for_connection(connection).transaction():
        _appoint_legacy_agent(connection, ACTIVE_BUNDLE, now_ms=APPOINTED_AT_MS)
    yield connection
    connection.close()


def _open_event(
    conn,
    *,
    delivered: bool = True,
    hit_id: int = 112001,
    title: str = "Micron says DRAM contract prices rose again in August",
    source: str = "Reuters",
    bundle_sha: str = ACTIVE_BUNDLE,
    program_sha256: str = "b" * 64,
    final_decision: str = "push",
    throttled_by: str | None = None,
) -> str:
    repos = repositories_for_connection(conn)
    wire = {
        "id": hit_id,
        "text": title,
        "link": f"https://example.test/{hit_id}",
        "source": source,
        "newsType": "news",
        "engineType": "news",
        "ts": "2026-08-21T08:00:00+08:00",
        "aiRating": {"score": 82, "signal": "long", "status": "done"},
        "coins": [],
        "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
    }
    event = parse_opennews_message({"method": "strategy.triggered", "params": wire})
    assert event is not None
    with repos.transaction():
        opened = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=NOW - 3_600_000,
            trace_id="review-test",
            watchlist_symbols=frozenset(),
            now_ms=NOW - 3_600_000,
        )
        evidence = repos.news.latest_evidence_snapshot(opened.event_id)
        assert evidence is not None
        verdict = TriageVerdict.model_validate(
            {
                "novelty": "new_fact",
                "restates": -1,
                "assets": [],
                "direction": "bullish",
                "scope": "sector",
                "fact_kind": "new_quantity",
                "evidence_ref": "c1",
                "confidence": 0.7,
                "headline_zh": "DRAM 合约价继续上涨",
                "why_zh": "存储厂商议价能力改善，但持续性仍需后续数据确认。",
            }
        )
        judgment = legacy_judgment(
            verdict,
            source_authority="reputable_secondary",
            taxonomy=legacy_taxonomy(
                event_family="regulatory_legal",
                change_state="reported",
                assertion_status="claimed",
            ),
        )
        editorial = judgment.editorial
        assert legacy_news(repos.news).insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version=LEGACY_TRIAGE_POLICY_VERSION,
            judgment_contract_version=LEGACY_JUDGMENT_CONTRACT_VERSION,
            judgment_origin="model",
            rule_baseline_decision="drop",
            final_decision=final_decision,
            override_rule="fact_kind_new_quantity",
            throttled_by=throttled_by,
            verdict=verdict.model_dump(mode="json"),
            model_editorial=editorial.document,
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha="a" * 64,
            model="test-model",
            program_version=LEGACY_PROGRAM_VERSION,
            program_sha256=program_sha256,
            degraded=False,
            error_code=None,
            trace={
                "input_sha256": "a" * 64,
                "prompt_sha256": "b" * 64,
                "schema_sha256": "c" * 64,
                "gate_policy_version": "v4",
                "judgment_contract_version": LEGACY_JUDGMENT_CONTRACT_VERSION,
                "judgment_origin": "model",
                "judgment_sha256": judgment.scored_judgment_sha256,
                "verdict_sha256": judgment.verdict_sha256,
                "editorial_sha256": editorial.editorial_sha256,
                "runtime_manifest_sha": "a" * 64,
                "program_version": LEGACY_PROGRAM_VERSION,
                "program_sha256": program_sha256,
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "told": [],
                "told_count": 0,
                "agent_assignment": {"bundle_sha": bundle_sha},
            },
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=NOW - 3_500_000,
        )
        if delivered:
            assert (
                legacy_news(repos.news).begin_delivery(
                    event_id=opened.event_id,
                    kind="first",
                    card={"header": {"title": {"content": "DRAM 合约价继续上涨"}}},
                    now_ms=NOW - 3_400_000,
                )
                == "new"
            )
            assert legacy_news(repos.news).settle_delivery(
                event_id=opened.event_id,
                kind="first",
                state="sent",
                receipt={"ok": True},
                error_code=None,
                now_ms=NOW - 3_300_000,
            )
    return opened.event_id


def test_legacy_event_review_is_read_only(conn) -> None:
    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    virtual = desk._event_task(event_id)
    assert virtual is not None
    ref = TaskRef(task_id=virtual.task_id, task_version=virtual.task_version)
    task = desk.evidence(ref, principal=PRINCIPAL)
    assert task["evidence"]["focus_fact"]["text"].startswith("Micron")
    assert task["agent"]["verdict"] is not None
    source = desk.evidence(ref, principal=PRINCIPAL, source_only=True)
    assert source["schema"]
    with pytest.raises(ValueError, match="news_review_legacy_task_read_only"):
        desk.submit(
            ref,
            EventRubricSubmission(dimensions={"factual_fidelity": "pass"}),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert conn.execute("SELECT count(*) AS n FROM news_reviews").fetchone()["n"] == 0


def test_external_miss_creates_snapshot_and_short_feedback(conn) -> None:
    db_now = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    desk = ReviewDesk(conn, now_ms=db_now + 1)
    submission = ExternalMissSubmission(
        source_url="https://example.test/missed",
        title="A material source item the receiver never ingested",
        body="Primary source body",
        occurred_at_ms=db_now - 10_000,
        feedback=DecisionFeedbackSubmission(should_push="should_push", note="Reader should see this"),
    )
    key = str(uuid.uuid4())
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=key)
    with repositories_for_connection(conn).transaction():
        again = desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=key)
    assert again["idempotent"] is True and again["receipt"]["review_id"] == receipt["receipt"]["review_id"]
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM news_external_miss_snapshots) AS snapshots, "
        "(SELECT count(*) FROM news_notification_external_feedback) AS feedback, "
        "(SELECT count(*) FROM news_reviews) AS legacy_reviews"
    ).fetchone()
    assert counts == {"snapshots": 1, "feedback": 1, "legacy_reviews": 0}
    assert (
        conn.execute("SELECT provenance FROM news_external_miss_snapshots").fetchone()["provenance"]
        == "operator_reported"
    )
    with pytest.raises(ValueError, match="news_review_idempotency_conflict"):
        desk.submit(None, submission.model_copy(update={"title": "Changed"}), principal=PRINCIPAL, idempotency_key=key)


def test_external_miss_rejects_future_source_time(conn) -> None:
    db_now = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    submission = ExternalMissSubmission(
        source_url="https://example.test/future",
        title="Future source",
        occurred_at_ms=db_now + 60_000,
        feedback=DecisionFeedbackSubmission(should_push="uncertain"),
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_external_miss_future"),
    ):
        ReviewDesk(conn, now_ms=NOW).submit(None, submission, principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))
