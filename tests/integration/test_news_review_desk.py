from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.http.app import create_app
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.learning.evaluator import LEARNING_EPOCH
from tracefold.news.learning.review import (
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExpectedCorrection,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from tracefold.platform.config.models import Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_787_287_000_000
PRINCIPAL = Principal(subject="operator")
ACTIVE_BUNDLE = "1" * 64


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    with repositories_for_connection(connection).transaction():
        repositories_for_connection(connection).news.register_agent_runtime_manifest(
            manifest_sha="a" * 64,
            stable_bundle_sha=ACTIVE_BUNDLE,
            candidate_shas=(),
            image_digest="sha256:review-test",
            runtime_revision="review-test",
            now_ms=NOW - 24 * 3_600_000,
        )
    yield connection
    connection.close()


def _open_event(
    conn,
    *,
    delivered: bool = True,
    hit_id: int = 112001,
    title: str = "Micron says DRAM contract prices rose again in August",
    bundle_sha: str = ACTIVE_BUNDLE,
    program_sha256: str = "d" * 64,
    relevance_overrides: dict[str, object] | None = None,
) -> str:
    repos = repositories_for_connection(conn)
    wire = {
        "id": hit_id,
        "text": title,
        "link": f"https://example.test/{hit_id}",
        "source": "Reuters",
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
                "event_type": "macro",
                "assets": [],
                "direction": "bullish",
                "scope": "sector",
                "magnitude": 2,
                "actionable": True,
                "confidence": 0.7,
                "decision": "push",
                "audience": "us_equity",
                "headline_zh": "DRAM 合约价继续上涨",
                "title_zh": "",
                "why_zh": "存储厂商议价能力改善，但持续性仍需后续数据确认。",
            }
        )
        relevance = {
            "impact_breadth": "sector",
            "tradability": "direct",
            "surprise": "unknown",
            "development_delta": "material_detail",
            "channels": ("earnings_cashflow",),
            "affected_markets": ("single_asset",),
            "reader_value": "realtime",
        }
        relevance.update(relevance_overrides or {})
        editorial = EditorialEnvelope.issue(
            editorial_origin="model",
            relevance=TradeRelevanceV1.model_validate(relevance),
        )
        judgment = ScoredJudgment.issue(verdict=verdict, editorial=editorial)
        assert repos.news.insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version="news_triage_policy_v10",
            model_decision="push",
            rule_baseline_decision="drop",
            final_decision="push",
            override_rule="trade_relevance_realtime",
            throttled_by=None,
            verdict=verdict.model_dump(mode="json"),
            editorial=editorial.model_dump(mode="json"),
            scored_judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha="a" * 64,
            model="test-model",
            program_version="news_semantic_program_test",
            program_sha256=program_sha256,
            degraded=False,
            error_code=None,
            trace={
                "input_sha256": "a" * 64,
                "prompt_sha256": "b" * 64,
                "schema_sha256": "c" * 64,
                "policy": {"push_magnitude": 1},
                "gate_policy_version": "v4",
                "agent_assignment": {"bundle_sha": bundle_sha},
            },
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=NOW - 3_500_000,
        )
        if delivered:
            assert (
                repos.news.begin_delivery(
                    event_id=opened.event_id,
                    kind="first",
                    card={"header": {"title": {"content": "DRAM 合约价继续上涨"}}},
                    now_ms=NOW - 3_400_000,
                )
                == "new"
            )
            assert repos.news.settle_delivery(
                event_id=opened.event_id,
                kind="first",
                state="sent",
                receipt={"ok": True},
                error_code=None,
                now_ms=NOW - 3_300_000,
            )
    return opened.event_id


def _rubric(
    *,
    why: str = "pass",
    should_push: str = "must_push",
    first_bad_owner: str | None = None,
    magnitude: str | None = None,
) -> EventRubricSubmission:
    """One accepted rubric.

    `first_bad_owner` is the operator's own attribution and is what #199's Objective Plan reads to decide
    whether GEPA may try to repair the case. It is deliberately not defaulted: a rubric that leaves it
    unset is exactly the shape ReviewDesk derives an owner for, and the plan must not treat a derived
    owner as a grant.

    `magnitude="fail"` is the *typed* failure — a stated correct value the metric can score a repair
    against. `why="fail"` is a copy complaint with no such value; #199 keeps it as an excluded diagnostic
    rather than a target, so a corpus meant to exercise optimization has to fail a typed dimension.
    """

    dimensions = {
        "factual_fidelity": "pass",
        "headline_fidelity": "pass",
        "why_support": why,
        "why_value": "pass",
        "timeliness": "pass",
    }
    if magnitude is not None:
        dimensions["magnitude"] = magnitude
    failed = why == "fail" or magnitude == "fail"
    return EventRubricSubmission(
        should_push=should_push,  # type: ignore[arg-type]
        dimensions=dimensions,
        novelty={"judgment": "new_fact"},
        first_bad_owner=first_bad_owner,  # type: ignore[arg-type]
        expected=ExpectedCorrection(magnitude=3) if magnitude == "fail" else None,
        evidence_refs=["source:sentence:1", "output:why"] if failed else [],
        expected_correction="Do not claim priced-in without source evidence." if failed else "",
    )


def _insert_learning_dataset(
    conn,
    dataset_sha: str,
    *,
    learning_epoch: str = LEARNING_EPOCH,
    bundle_sha: str = ACTIVE_BUNDLE,
) -> None:
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'dataset', NULL, %s::jsonb, 'test', %s)",
        (
            dataset_sha,
            json.dumps({"learning_epoch": learning_epoch, "agent_cohort": {"bundle_sha": bundle_sha}}),
            NOW,
        ),
    )


def test_review_queue_evidence_submit_idempotency_and_correction(conn) -> None:
    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    queue = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)
    assert queue["status"] == "ready" and len(queue["tasks"]) == 1
    task = queue["tasks"][0]
    assert task["reader_receipt"]["truth"] == "received"
    assert task["reader_receipt"]["rendered_card"]["header"]["title"]["content"] == "DRAM 合约价继续上涨"
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    evidence = desk.evidence(ref, principal=PRINCIPAL)
    assert evidence["evidence"]["focus_fact"]["text"].startswith("Micron")
    assert evidence["agent"]["cohort"] == "news_semantic_program_test/news_triage_policy_v10/test-model"
    assert evidence["agent"]["agent_cohort"]["cohort_sha256"] == task["agent_cohort"]["cohort_sha256"]
    cohort_queue = desk.open(DeskQuery(cohort=ACTIVE_BUNDLE), principal=PRINCIPAL)
    repeated_cohort_queue = desk.open(DeskQuery(cohort=ACTIVE_BUNDLE), principal=PRINCIPAL)
    # Delivered cases use a deterministic 25% sample.  This particular case
    # may be absent, but reopening the queue must not draw a different sample.
    assert cohort_queue["tasks"] == repeated_cohort_queue["tasks"]
    assert all(item["event_id"] == event_id for item in cohort_queue["tasks"])
    with pytest.raises(ValueError, match="news_review_cohort_invalid"):
        desk.open(DeskQuery(cohort="v9/v6/test-model"), principal=PRINCIPAL)

    key = str(uuid.uuid4())
    with repositories_for_connection(conn).transaction():
        first = desk.submit(ref, _rubric(why="fail"), principal=PRINCIPAL, idempotency_key=key)
    with repositories_for_connection(conn).transaction():
        replay = desk.submit(ref, _rubric(why="fail"), principal=PRINCIPAL, idempotency_key=key)
    assert first["idempotent"] is False and replay["idempotent"] is True
    assert first["receipt"]["review_id"] == replay["receipt"]["review_id"]
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_idempotency_conflict"),
    ):
        desk.submit(ref, _rubric(), principal=PRINCIPAL, idempotency_key=key)
    accepted = desk.open(DeskQuery(event=event_id, status="accepted"), principal=PRINCIPAL)["tasks"][0]
    assert accepted["accepted_review"]["first_bad_owner"] == "triage_prompt"

    with repositories_for_connection(conn).transaction():
        corrected = desk.submit(
            ref,
            _rubric(),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert corrected["receipt"]["review_id"] != first["receipt"]["review_id"]
    rows = conn.execute(
        "SELECT review_kind, supersedes_review_id FROM news_reviews "
        "WHERE event_id = %s ORDER BY created_at_ms, review_id",
        (event_id,),
    ).fetchall()
    assert len(rows) == 4
    judgment_rows = [row for row in rows if row["review_kind"] == "judgment"]
    assert judgment_rows[-1]["supersedes_review_id"] == first["receipt"]["review_id"]

    conn.execute("BEGIN")
    conn.execute("SAVEPOINT immutable_review")
    with pytest.raises(RaiseException, match="news_review_append_only"):
        conn.execute("UPDATE news_reviews SET note = 'mutated' WHERE review_id = %s", (first["receipt"]["review_id"],))
    conn.execute("ROLLBACK TO SAVEPOINT immutable_review")
    conn.execute("RELEASE SAVEPOINT immutable_review")
    conn.commit()


def test_coverage_uses_only_the_exact_active_agent_bundle(conn) -> None:
    first_bundle, second_bundle = "1" * 64, "2" * 64
    first_event = _open_event(
        conn,
        hit_id=112011,
        title="Micron DRAM contract prices rise in August",
        bundle_sha=first_bundle,
        relevance_overrides={"impact_breadth": "regional"},
    )
    second_event = _open_event(
        conn,
        hit_id=112012,
        title="Federal Reserve governor announces an immediate resignation",
        bundle_sha=second_bundle,
        relevance_overrides={"impact_breadth": "regional"},
    )
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (LEARNING_EPOCH,),
        ).fetchone()["starts_at_ms"]
    )
    review_now = epoch_start + 3_600_000
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (epoch_start + 1_000, first_event),
    )
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (epoch_start + 2_000, second_event),
    )
    conn.execute(
        "UPDATE news_events SET queue_priority = 'high' WHERE event_id = ANY(%s)",
        ([first_event, second_event],),
    )

    desk = ReviewDesk(conn, now_ms=review_now)
    for event_id in (first_event, second_event):
        task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
        with repositories_for_connection(conn).transaction():
            desk.submit(
                TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
                _rubric(),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )

    coverage = desk.open(DeskQuery(view="coverage"), principal=PRINCIPAL)
    by_cohort = {row["cohort"]: row for row in coverage["cohorts"]}
    assert set(by_cohort) == {first_bundle}
    assert by_cohort[first_bundle]["agent"]["bundle_sha"] == first_bundle
    assert {row["legacy_cohort"] for row in coverage["cohorts"]} == {
        "news_semantic_program_test/news_triage_policy_v10/test-model"
    }
    assert coverage["funnel"]["total"] == 1
    assert coverage["funnel"]["accepted"] == 1
    eligibility = conn.execute(
        "SELECT event_id, bool_and(release_eligible) AS release_eligible FROM news_reviews "
        "WHERE event_id = ANY(%s) GROUP BY event_id",
        ([first_event, second_event],),
    ).fetchall()
    assert {row["event_id"]: row["release_eligible"] for row in eligibility} == {
        first_event: True,
        second_event: False,
    }
    default_queue = ReviewDesk(conn, now_ms=review_now).open(DeskQuery(status="all"), principal=PRINCIPAL)
    assert {task["event_id"] for task in default_queue["tasks"]} == {first_event}
    second_queue = ReviewDesk(conn, now_ms=review_now).open(
        DeskQuery(cohort=second_bundle, status="all"), principal=PRINCIPAL
    )
    assert {task["event_id"] for task in second_queue["tasks"]} == {second_event}


def test_coverage_epoch_excludes_prior_events_reviews_and_external_misses(conn) -> None:
    prior_event = _open_event(
        conn,
        hit_id=112013,
        title="Prior epoch evidence remains visible only through direct audit lookup",
    )
    current_event = _open_event(
        conn,
        hit_id=112014,
        title="Current epoch evidence is eligible for coverage",
    )
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (LEARNING_EPOCH,),
        ).fetchone()["starts_at_ms"]
    )
    review_now = epoch_start + 3_600_000
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (epoch_start - 1, prior_event),
    )
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (epoch_start + 1, current_event),
    )
    desk = ReviewDesk(conn, now_ms=review_now)
    for event_id in (prior_event, current_event):
        task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
        with repositories_for_connection(conn).transaction():
            desk.submit(
                TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
                _rubric(),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )
    with repositories_for_connection(conn).transaction():
        desk.submit(
            None,
            ExternalMissSubmission(
                source_url="https://example.test/current-epoch-miss",
                title="Current epoch external miss",
                occurred_at_ms=epoch_start,
                rubric=_rubric(),
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    with repositories_for_connection(conn).transaction():
        desk.submit(
            None,
            ExternalMissSubmission(
                source_url="https://example.test/prior-epoch-miss",
                title="Prior epoch external miss",
                occurred_at_ms=epoch_start - 2,
                rubric=_rubric(),
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    coverage = desk.open(DeskQuery(view="coverage", hours=24), principal=PRINCIPAL)

    eligibility = conn.execute(
        "SELECT event_id, review_kind, release_eligible FROM news_reviews "
        "WHERE event_id = ANY(%s) ORDER BY event_id, review_kind",
        ([prior_event, current_event],),
    ).fetchall()
    by_event = {
        event_id: {row["review_kind"]: row["release_eligible"] for row in eligibility if row["event_id"] == event_id}
        for event_id in (prior_event, current_event)
    }
    assert by_event[prior_event] == {"acceptance": False, "judgment": False}
    assert by_event[current_event] == {"acceptance": True, "judgment": True}
    external_eligibility = conn.execute(
        "SELECT source.source_url, review.review_kind, review.release_eligible "
        "FROM news_reviews review JOIN news_external_miss_snapshots source "
        "ON source.snapshot_id = review.external_snapshot_id "
        "WHERE source.source_url = ANY(%s) ORDER BY source.source_url, review.review_kind",
        (
            [
                "https://example.test/current-epoch-miss",
                "https://example.test/prior-epoch-miss",
            ],
        ),
    ).fetchall()
    by_source = {
        source_url: {
            row["review_kind"]: row["release_eligible"]
            for row in external_eligibility
            if row["source_url"] == source_url
        }
        for source_url in (
            "https://example.test/current-epoch-miss",
            "https://example.test/prior-epoch-miss",
        )
    }
    assert by_source["https://example.test/current-epoch-miss"] == {
        "acceptance": True,
        "judgment": True,
    }
    assert by_source["https://example.test/prior-epoch-miss"] == {
        "acceptance": False,
        "judgment": False,
    }

    assert coverage["window"]["from_ms"] == epoch_start
    assert coverage["status"] == "ready"
    assert coverage["funnel"] == {
        "received": 1,
        "replayable": 1,
        "reviewed": 1,
        "accepted": 1,
        "holdout_ready": 0,
        "total": 1,
        "external_misses": 1,
    }
    assert sum(row["events"] for row in coverage["strata"]) == 1


def test_market_view_defaults_to_latest_homogeneous_cohort_and_hides_bad_taxonomy(conn) -> None:
    _open_event(conn)
    _open_event(
        conn,
        hit_id=112099,
        title="A second event from another Program artifact",
        bundle_sha="f" * 64,
        program_sha256="e" * 64,
    )
    market = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="market"), principal=PRINCIPAL)
    assert market["status"] == "ready"
    assert market["reaction"]["meta"]["cohort"] == ("news_semantic_program_test/news_triage_policy_v10/test-model")
    assert market["reaction"]["meta"]["cohort_sha256"] == ACTIVE_BUNDLE
    assert market["reaction"]["meta"]["program_sha256"] == "d" * 64
    assert market["reaction"]["coverage"][0]["eligible_n"] == 1
    assert market["reaction"]["event_types"] == []
    assert "不是新闻因果" in market["disclaimer_zh"]
    with pytest.raises(ValueError, match="news_review_market_hours_too_large"):
        ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="market", hours=720), principal=PRINCIPAL)


def test_high_reaction_held_case_is_discovery_only_and_not_release_truth(conn) -> None:
    event_id = _open_event(conn, delivered=False)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": event_id,
                "symbol": "MU",
                "anchor_at_ms": NOW - 3_600_000,
                "return_1h_bps": 450,
                "is_primary": True,
                "state": "partial",
            },
            now_ms=NOW,
        )
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert task["selection"] == {
        "stratum": "high_reaction",
        "stratum_zh": "高波动发现样本（非成绩）",
        "reason": "market_discovery_only",
        "reason_zh": "仅因事后波动进入发现队列",
        "sampling_probability": 1.0,
        "selection_version": "news_review_sampler_v3",
    }
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    with repos.transaction():
        desk.submit(ref, _rubric(), principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))
    rows = conn.execute(
        "SELECT review_kind, release_eligible FROM news_reviews WHERE event_id = %s ORDER BY created_at_ms",
        (event_id,),
    ).fetchall()
    assert {row["review_kind"]: row["release_eligible"] for row in rows} == {
        "judgment": False,
        "acceptance": False,
    }


def test_legacy_reconstructed_evidence_stays_discovery_only_after_review(conn) -> None:
    event_id = _open_event(conn)
    conn.execute(
        """
        INSERT INTO news_event_evidence_snapshots (
          event_id, evidence_version, focus_fact_id, evidence_sha256,
          provenance, release_eligible, snapshot, created_at_ms
        )
        SELECT event_id, evidence_version + 1, focus_fact_id, %s,
               'legacy_reconstructed', false,
               snapshot || '{"provenance":"legacy_reconstructed"}'::jsonb,
               %s
          FROM news_event_evidence_snapshots
         WHERE event_id = %s
         ORDER BY evidence_version DESC
         LIMIT 1
        """,
        ("f" * 64, NOW, event_id),
    )
    conn.commit()

    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert task["evidence_ready"] is False
    evidence = desk.evidence(
        TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
        principal=PRINCIPAL,
    )
    assert evidence["evidence"]["provenance"] == "legacy_reconstructed"
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            _rubric(),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    rows = conn.execute(
        "SELECT review_kind, release_eligible FROM news_reviews "
        "WHERE review_id = %s OR accepts_review_id = %s ORDER BY review_kind",
        (receipt["receipt"]["review_id"], receipt["receipt"]["review_id"]),
    ).fetchall()
    assert {row["review_kind"]: row["release_eligible"] for row in rows} == {
        "acceptance": False,
        "judgment": False,
    }


def test_task_version_conflicts_when_delivery_truth_changes(conn) -> None:
    event_id = _open_event(conn, delivered=False)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=NOW - 1000) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="sent",
            receipt={"ok": True},
            error_code=None,
            now_ms=NOW,
        )
    with repos.transaction(), pytest.raises(ValueError, match="news_review_task_version_conflict"):
        desk.submit(ref, _rubric(), principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))


def test_acceptance_is_bound_to_exact_task_version(conn) -> None:
    event_id = _open_event(conn, delivered=False)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            _rubric(),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]["review_status"] == "accepted"

    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=NOW - 1000) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="sent",
            receipt={"ok": True},
            error_code=None,
            now_ms=NOW,
        )
    changed = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert changed["task_version"] != task["task_version"]
    assert changed["review_status"] == "pending"
    assert changed["accepted_review"] is None


def test_stronger_evidence_creates_a_new_pending_review_task(conn) -> None:
    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    first = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=first["task_id"], task_version=first["task_version"]),
            _rubric(),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        conn.execute(
            "UPDATE news_events SET member_count = member_count + 1, last_member_at_ms = last_member_at_ms + 1 "
            "WHERE event_id = %s",
            (event_id,),
        )
        evidence = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=NOW + 1)
    assert evidence["evidence_version"] == 2

    second = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert second["evidence_version"] == 2
    assert second["verdict_evidence_version"] == 1
    assert second["task_id"] != first["task_id"]
    assert second["review_status"] == "pending"
    assert second["accepted_review"] is None


def test_delivery_terminal_error_code_distinguishes_unknown_from_known_failure(conn) -> None:
    event_id = _open_event(conn, delivered=False)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=NOW - 1000) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="terminal",
            receipt=None,
            error_code="ambiguous_after_crash",
            now_ms=NOW,
        )
    ambiguous = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert ambiguous["reader_receipt"]["truth"] == "unknown"
    assert ambiguous["selection"]["stratum"] == "delivery_ambiguous"

    with repos.transaction():
        conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (event_id,))
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=NOW - 1000) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="terminal",
            receipt=None,
            error_code="delivery_unavailable",
            now_ms=NOW,
        )
    failed = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    assert failed["reader_receipt"]["truth"] == "not_received"
    assert failed["selection"]["stratum"] == "delivery_failed"


def test_external_miss_appends_snapshot_and_accepted_judgment_atomically(conn) -> None:
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (LEARNING_EPOCH,),
        ).fetchone()["starts_at_ms"]
    )
    db_now = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    review_now = db_now + 1
    desk = ReviewDesk(conn, now_ms=review_now)
    submission = ExternalMissSubmission(
        source_url="https://example.test/missed",
        title="A material source item the receiver never ingested",
        body="Primary source body",
        occurred_at_ms=max(epoch_start, db_now - 10_000),
        rubric=_rubric(),
    )
    key = str(uuid.uuid4())
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=key)
    assert receipt["receipt"]["external_snapshot_id"]
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM news_external_miss_snapshots) AS snapshots, "
        "(SELECT count(*) FROM news_reviews) AS reviews"
    ).fetchone()
    assert counts == {"snapshots": 1, "reviews": 2}
    snapshot = conn.execute("SELECT provenance FROM news_external_miss_snapshots").fetchone()
    assert snapshot["provenance"] == "operator_reported"
    coverage = desk.open(DeskQuery(view="coverage"), principal=PRINCIPAL)
    assert coverage["funnel"]["external_misses"] == 1


def test_external_miss_rejects_a_future_source_time(conn) -> None:
    db_now = conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
        "now_ms"
    ]
    submission = ExternalMissSubmission(
        source_url="https://example.test/future",
        title="A source item that has not happened yet",
        body="Primary source body",
        occurred_at_ms=int(db_now) + 60_000,
        rubric=_rubric(),
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_external_miss_future"),
    ):
        ReviewDesk(conn, now_ms=NOW).submit(
            None,
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )


def test_pairwise_queue_hides_arm_identity_and_appends_blind_acceptance(conn) -> None:
    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    run_sha = "a" * 64
    case_id = "b" * 64
    _insert_learning_dataset(conn, "c" * 64)
    stable = {
        "verdict": {"headline_zh": "DRAM 价格上涨", "why_zh": "需求改善。", "magnitude": 2},
        "final_decision": "push",
        "delivered": True,
    }
    candidate = {
        "verdict": {"headline_zh": "DRAM 合约价续涨", "why_zh": "供给偏紧改善厂商议价。", "magnitude": 2},
        "final_decision": "push",
        "delivered": True,
    }
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES (
          %s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, %s, %s, %s,
          %s::jsonb, %s::jsonb, %s::jsonb, %s
        )
        """,
        (
            run_sha,
            case_id,
            "c" * 64,
            event_id,
            source["evidence_version"],
            "d" * 64,
            source["opened_at_ms"],
            source["evidence_sha256"],
            "e" * 64,
            "critical",
            json.dumps(stable),
            json.dumps(candidate),
            json.dumps(
                {
                    "pair_order": "candidate_A",
                    "review_eligible": True,
                    "outcome_revealed": False,
                }
            ),
            NOW,
        ),
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    queue = desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)
    assert queue["status"] == "ready" and len(queue["tasks"]) == 1
    assert queue["disclosure"]["arm_identity_revealed"] is False
    task = queue["tasks"][0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    evidence = desk.evidence(ref, principal=PRINCIPAL)
    assert evidence["output_A"]["headline_zh"] == "DRAM 合约价续涨"
    assert evidence["output_B"]["headline_zh"] == "DRAM 价格上涨"
    serialized = json.dumps(evidence)
    assert "pair_order" not in serialized
    assert "candidate_observation" not in serialized
    assert "stable_observation" not in serialized

    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            ref,
            BlindPairwiseSubmission(preference="A", evidence_refs=["output:A", "output:B"]),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    judgment = conn.execute(
        "SELECT pairwise_case_id, payload FROM news_reviews WHERE review_id = %s",
        (receipt["receipt"]["review_id"],),
    ).fetchone()
    assert judgment["pairwise_case_id"] == f"{run_sha}:{case_id}"
    assert judgment["payload"]["preference"] == "A"
    assert desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["status"] == "insufficient_evidence"
    accepted_queue = desk.open(DeskQuery(mode="pairwise", status="accepted"), principal=PRINCIPAL)
    assert [item["task_id"] for item in accepted_queue["tasks"]] == [task["task_id"]]
    direct = desk.open(DeskQuery(task=task["task_id"]), principal=PRINCIPAL)
    assert direct["mode"] == "pairwise" and direct["tasks"][0]["review_status"] == "accepted"
    # A validation case stays blind after its own acceptance.  The whole run
    # must be accepted and then re-sealed by CandidateEvaluator first.
    after = desk.evidence(ref, principal=PRINCIPAL)
    assert after["reveal"] is None
    assert after["disclosure"]["arm_identity_revealed"] is False


def test_prompt_era_proposal_remains_readable_as_audit_history(conn) -> None:
    candidate_sha = "0" * 64
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'candidate', %s, %s::jsonb, 'test', %s)",
        (
            "f" * 64,
            "e" * 64,
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "manifest": {"target": "prompt", "hypothesis": "historical audit only"},
                }
            ),
            NOW,
        ),
    )

    proposals = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="proposals"), principal=PRINCIPAL)["proposals"]

    # #202 made the two advisory instructions the one candidate kind, so `prompt` is the live label again.
    # What marks this row as history is `evidence_disposition` and its epoch, not the variable's name.
    assert [(item["target"], item["target_zh"]) for item in proposals] == [("prompt", "两段提示词")]


def test_superseded_epoch_proposal_and_receipts_are_visible_but_audit_only(conn) -> None:
    old_dataset_sha, current_dataset_sha = "1" * 64, "2" * 64
    old_candidate_sha, current_candidate_sha = "3" * 64, "4" * 64
    old_report_sha, current_report_sha = "5" * 64, "6" * 64
    conn.execute(
        """
        INSERT INTO news_learning_artifacts (
          artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
        ) VALUES
          (%s, 'dataset', NULL, %s::jsonb, 'test', %s),
          (%s, 'dataset', NULL, %s::jsonb, 'test', %s),
          (%s, 'candidate', NULL, %s::jsonb, 'test', %s),
          (%s, 'candidate', NULL, %s::jsonb, 'test', %s),
          (%s, 'evaluation_report', %s, %s::jsonb, 'test', %s),
          (%s, 'evaluation_report', %s, %s::jsonb, 'test', %s),
          (%s, 'release_evidence', %s, %s::jsonb, 'test', %s),
          (%s, 'release_evidence', %s, %s::jsonb, 'test', %s)
        """,
        (
            old_dataset_sha,
            json.dumps({"learning_epoch": "program_v1", "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
            NOW - 8,
            current_dataset_sha,
            json.dumps({"learning_epoch": LEARNING_EPOCH, "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
            NOW - 7,
            "7" * 64,
            json.dumps(
                {
                    "candidate_sha": old_candidate_sha,
                    "manifest": {
                        "target": "program",
                        "hypothesis": "historical candidate",
                        "development_dataset_sha": old_dataset_sha,
                        "parent_stable_sha": ACTIVE_BUNDLE,
                    },
                }
            ),
            NOW - 6,
            "8" * 64,
            json.dumps(
                {
                    "candidate_sha": current_candidate_sha,
                    "manifest": {
                        "target": "program",
                        "hypothesis": "current candidate",
                        "development_dataset_sha": current_dataset_sha,
                        "parent_stable_sha": ACTIVE_BUNDLE,
                    },
                }
            ),
            NOW - 5,
            old_report_sha,
            old_candidate_sha,
            json.dumps({"recommended_action": "advance", "evidence": {"blockers": [], "failures": []}}),
            NOW - 4,
            current_report_sha,
            current_candidate_sha,
            json.dumps({"recommended_action": "advance", "evidence": {"blockers": [], "failures": []}}),
            NOW - 3,
            "9" * 64,
            old_report_sha,
            json.dumps(
                {
                    "candidate_sha": old_candidate_sha,
                    "report_sha": old_report_sha,
                    "run_sha": "a" * 64,
                    "stage": "canary",
                    "gate_outcome": "pass",
                }
            ),
            NOW - 2,
            "b" * 64,
            current_report_sha,
            json.dumps(
                {
                    "candidate_sha": current_candidate_sha,
                    "report_sha": current_report_sha,
                    "run_sha": "c" * 64,
                    "stage": "canary",
                    "gate_outcome": "pass",
                }
            ),
            NOW - 1,
        ),
    )

    proposals = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="proposals"), principal=PRINCIPAL)["proposals"]
    by_candidate = {item["candidate_sha"]: item for item in proposals}

    historical = by_candidate[old_candidate_sha]
    assert historical["learning_epoch"] == "program_v1"
    assert historical["evidence_disposition"] == "audit_only"
    assert historical["status"] == "audit_only"
    assert historical["timeline"][0]["outcome"] == "pass"
    assert historical["timeline"][0]["evidence_disposition"] == "audit_only"

    current = by_candidate[current_candidate_sha]
    assert current["learning_epoch"] == LEARNING_EPOCH
    assert current["evidence_disposition"] == "current"
    assert current["status"] == "promotion_ready"
    assert current["timeline"][0]["evidence_disposition"] == "current"


def test_current_epoch_proposal_from_inactive_parent_is_audit_only(conn) -> None:
    dataset_sha, candidate_sha = "1" * 64, "2" * 64
    _insert_learning_dataset(conn, dataset_sha)
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'candidate', NULL, %s::jsonb, 'test', %s)",
        (
            "3" * 64,
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "manifest": {
                        "target": "program",
                        "hypothesis": "stale parent candidate",
                        "development_dataset_sha": dataset_sha,
                        "parent_stable_sha": "f" * 64,
                    },
                }
            ),
            NOW,
        ),
    )

    proposal = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="proposals"), principal=PRINCIPAL)["proposals"][0]

    assert proposal["learning_epoch"] == LEARNING_EPOCH
    assert proposal["evidence_disposition"] == "audit_only"
    assert proposal["status"] == "audit_only"


def test_current_epoch_proposal_from_inactive_dataset_bundle_is_audit_only(conn) -> None:
    dataset_sha, candidate_sha = "1" * 64, "2" * 64
    _insert_learning_dataset(conn, dataset_sha, bundle_sha="e" * 64)
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'candidate', NULL, %s::jsonb, 'test', %s)",
        (
            "3" * 64,
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "manifest": {
                        "target": "program",
                        "hypothesis": "stale development cohort candidate",
                        "development_dataset_sha": dataset_sha,
                        "parent_stable_sha": ACTIVE_BUNDLE,
                    },
                }
            ),
            NOW,
        ),
    )

    proposal = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="proposals"), principal=PRINCIPAL)["proposals"][0]

    assert proposal["learning_epoch"] == LEARNING_EPOCH
    assert proposal["evidence_disposition"] == "audit_only"
    assert proposal["status"] == "audit_only"


def test_coverage_holdout_denominator_excludes_superseded_epoch_cases(conn) -> None:
    old_dataset_sha, current_dataset_sha = "1" * 64, "2" * 64
    conn.execute(
        """
        INSERT INTO news_learning_artifacts (
          artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
        ) VALUES
          (%s, 'dataset', NULL, %s::jsonb, 'test', %s),
          (%s, 'dataset', NULL, %s::jsonb, 'test', %s)
        """,
        (
            old_dataset_sha,
            json.dumps({"learning_epoch": "program_v1", "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
            NOW - 2,
            current_dataset_sha,
            json.dumps({"learning_epoch": LEARNING_EPOCH, "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
            NOW - 1,
        ),
    )
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES
          (%s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, 'critical',
           '{}'::jsonb, '{}'::jsonb, %s::jsonb, %s),
          (%s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, 'critical',
           '{}'::jsonb, '{}'::jsonb, %s::jsonb, %s)
        """,
        (
            "3" * 64,
            "4" * 64,
            old_dataset_sha,
            "5" * 64,
            NOW - 2,
            "6" * 64,
            "old-cluster",
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW - 2,
            "7" * 64,
            "8" * 64,
            current_dataset_sha,
            "9" * 64,
            NOW - 1,
            "a" * 64,
            "current-cluster",
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW - 1,
        ),
    )

    coverage = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="coverage"), principal=PRINCIPAL)

    assert coverage["holdout"]["case_n"] == 1
    assert coverage["holdout"]["cluster_n"] == 1


def test_superseded_epoch_pairwise_task_is_visible_only_as_read_only_audit_history(conn) -> None:
    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    old_dataset_sha, current_dataset_sha = "1" * 64, "2" * 64
    old_run_sha, current_run_sha = "3" * 64, "4" * 64
    old_case_id, current_case_id = "5" * 64, "6" * 64
    _insert_learning_dataset(conn, old_dataset_sha, learning_epoch="program_v1")
    _insert_learning_dataset(conn, current_dataset_sha)
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES
          (%s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, %s, 'old-cluster', 'critical',
           '{}'::jsonb, '{}'::jsonb, %s::jsonb, %s),
          (%s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, %s, 'current-cluster', 'critical',
           '{}'::jsonb, '{}'::jsonb, %s::jsonb, %s)
        """,
        (
            old_run_sha,
            old_case_id,
            old_dataset_sha,
            event_id,
            source["evidence_version"],
            "7" * 64,
            source["opened_at_ms"],
            source["evidence_sha256"],
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW - 1,
            current_run_sha,
            current_case_id,
            current_dataset_sha,
            event_id,
            source["evidence_version"],
            "8" * 64,
            source["opened_at_ms"],
            source["evidence_sha256"],
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW,
        ),
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    old_task_id = f"pair.{old_run_sha}.{old_case_id}"
    current_task_id = f"pair.{current_run_sha}.{current_case_id}"

    pending = desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)
    assert [task["task_id"] for task in pending["tasks"]] == [current_task_id]

    all_tasks = desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"]
    by_id = {task["task_id"]: task for task in all_tasks}
    assert by_id[old_task_id]["learning_epoch"] == "program_v1"
    assert by_id[old_task_id]["evidence_disposition"] == "audit_only"
    assert by_id[old_task_id]["review_status"] == "audit_only"
    assert by_id[current_task_id]["learning_epoch"] == LEARNING_EPOCH
    assert by_id[current_task_id]["evidence_disposition"] == "current"
    assert by_id[current_task_id]["review_status"] == "pending"

    direct = desk.open(DeskQuery(task=old_task_id), principal=PRINCIPAL)["tasks"][0]
    assert direct["learning_epoch"] == "program_v1"
    assert direct["evidence_disposition"] == "audit_only"
    assert direct["review_status"] == "audit_only"

    judgment_id, acceptance_id = "9" * 64, "a" * 64
    conn.execute(
        """
        INSERT INTO news_reviews (
          review_id, review_kind, subject_kind, task_id, task_version, pairwise_case_id,
          rubric_version, reader_contract_version, reviewer, payload, accepts_review_id,
          release_eligible, created_at_ms
        ) VALUES
          (%s, 'judgment', 'pairwise', %s, %s, %s,
           'news_review_v2', 'reader_contract_v2', 'historical-reviewer', %s::jsonb, NULL, true, %s),
          (%s, 'acceptance', 'pairwise', %s, %s, %s,
           'news_review_v2', 'reader_contract_v2', 'historical-reviewer', '{}'::jsonb, %s, true, %s)
        """,
        (
            judgment_id,
            old_task_id,
            direct["task_version"],
            f"{old_run_sha}:{old_case_id}",
            json.dumps({"preference": "A"}),
            NOW - 1,
            acceptance_id,
            old_task_id,
            direct["task_version"],
            f"{old_run_sha}:{old_case_id}",
            judgment_id,
            NOW,
        ),
    )
    assert desk.open(DeskQuery(mode="pairwise", status="accepted"), principal=PRINCIPAL)["tasks"] == []
    historical = {
        task["task_id"]: task
        for task in desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"]
    }[old_task_id]
    assert historical["review_status"] == "audit_only"
    assert historical["accepted_review"]["review_id"] == judgment_id

    old_ref = TaskRef(task_id=old_task_id, task_version=direct["task_version"])
    with (
        pytest.raises(ValueError, match="news_review_pairwise_task_audit_only"),
        repositories_for_connection(conn).transaction(),
    ):
        desk.submit(
            old_ref,
            BlindPairwiseSubmission(preference="A", evidence_refs=["output:A", "output:B"]),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM news_reviews WHERE pairwise_case_id = %s",
            (f"{old_run_sha}:{old_case_id}",),
        ).fetchone()["n"]
        == 2
    )


def test_inactive_bundle_pairwise_task_is_audit_only_inside_current_epoch(conn) -> None:
    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    dataset_sha, run_sha, case_id = "1" * 64, "2" * 64, "3" * 64
    _insert_learning_dataset(conn, dataset_sha, bundle_sha="f" * 64)
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES (
          %s, %s, %s, 'validation', 'holdout', 'event', %s, %s, %s, %s, %s, 'inactive-cluster', 'critical',
          '{}'::jsonb, '{}'::jsonb, %s::jsonb, %s
        )
        """,
        (
            run_sha,
            case_id,
            dataset_sha,
            event_id,
            source["evidence_version"],
            "4" * 64,
            source["opened_at_ms"],
            source["evidence_sha256"],
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW,
        ),
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    task_id = f"pair.{run_sha}.{case_id}"

    assert desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["tasks"] == []
    audit_task = desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"][0]
    assert audit_task["task_id"] == task_id
    assert audit_task["learning_epoch"] == LEARNING_EPOCH
    assert audit_task["evidence_disposition"] == "audit_only"
    with (
        pytest.raises(ValueError, match="news_review_pairwise_task_audit_only"),
        repositories_for_connection(conn).transaction(),
    ):
        desk.submit(
            TaskRef(task_id=task_id, task_version=audit_task["task_version"]),
            BlindPairwiseSubmission(preference="A", evidence_refs=["output:A", "output:B"]),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )


def test_development_pair_reveals_arm_mapping_and_exact_candidate_diff_after_acceptance(conn) -> None:
    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    run_sha, case_id, candidate_sha = "1" * 64, "2" * 64, "3" * 64
    _insert_learning_dataset(conn, "4" * 64)
    stable = {"verdict": {"headline_zh": "旧标题", "why_zh": "旧解释。", "magnitude": 1}, "delivered": False}
    candidate = {"verdict": {"headline_zh": "新标题", "why_zh": "有证据的新解释。", "magnitude": 2}, "delivered": True}
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES (
          %s, %s, %s, 'development', 'offline', 'event', %s, %s, %s, %s, %s, %s, %s,
          %s::jsonb, %s::jsonb, %s::jsonb, %s
        )
        """,
        (
            run_sha,
            case_id,
            "4" * 64,
            event_id,
            source["evidence_version"],
            "5" * 64,
            source["opened_at_ms"],
            source["evidence_sha256"],
            "6" * 64,
            "critical",
            json.dumps(stable),
            json.dumps(candidate),
            json.dumps({"pair_order": "candidate_A", "review_eligible": True}),
            NOW,
        ),
    )
    exact_diff = {
        "target": "program",
        "changed_fields": ["program_version", "program_sha256"],
        "unified_diff": "--- stable/program-v1\n+++ candidate/program-v2\n@@ changed\n",
    }
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'candidate', %s, %s::jsonb, 'test', %s), "
        "(%s, 'evaluation_report', %s, %s::jsonb, 'test', %s)",
        (
            "7" * 64,
            "8" * 64,
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "manifest": {"target": "program", "hypothesis": "修复无证据的 priced-in 判断"},
                    "exact_diff": exact_diff,
                }
            ),
            NOW,
            "9" * 64,
            candidate_sha,
            json.dumps({"run_sha": run_sha, "evidence": {"primary": {}}}),
            NOW,
        ),
    )
    proposals = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="proposals"), principal=PRINCIPAL)["proposals"]
    assert [(item["target"], item["target_zh"]) for item in proposals] == [("program", "DSPy Program（历史审计）")]
    second_case_id = "a" * 64
    conn.execute(
        """
        INSERT INTO news_learning_cases (
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
          review_id, opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        )
        SELECT run_sha, %s, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id, evidence_version,
               %s, opened_at_ms, evidence_sha256, %s, stratum,
               stable_observation, candidate_observation,
               jsonb_set(comparison, '{pair_order}', '"candidate_B"'::jsonb), created_at_ms + 1
          FROM news_learning_cases WHERE run_sha = %s AND case_id = %s
        """,
        (second_case_id, "b" * 64, "c" * 64, run_sha, case_id),
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    page = desk.open(DeskQuery(mode="pairwise", limit=1), principal=PRINCIPAL)
    assert page["next_cursor"]
    next_page = desk.open(DeskQuery(mode="pairwise", limit=1, cursor=page["next_cursor"]), principal=PRINCIPAL)
    assert next_page["tasks"][0]["task_id"] != page["tasks"][0]["task_id"]
    task = page["tasks"][0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    assert desk.evidence(ref, principal=PRINCIPAL)["reveal"] is None
    with repositories_for_connection(conn).transaction():
        desk.submit(
            ref,
            BlindPairwiseSubmission(preference="A", evidence_refs=["output:A", "output:B"]),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    revealed = desk.evidence(ref, principal=PRINCIPAL)
    assert revealed["disclosure"]["arm_identity_revealed"] is True
    assert revealed["reveal"] == {
        "arm_identity_revealed": True,
        "outcome_revealed": True,
        "stable_side": "B",
        "candidate_side": "A",
        "accepted_preference": "A",
        "preferred_arm": "candidate",
        "candidate_sha": candidate_sha,
        "target": "program",
        "hypothesis": "修复无证据的 priced-in 判断",
        "exact_diff": exact_diff,
    }


def test_serve_role_has_only_append_review_write_privileges(conn) -> None:
    event_id = _open_event(conn)
    conn.execute("SET ROLE tracefold_serve")
    try:
        assert (
            conn.execute("SELECT event_id FROM news_review_task_source_v1 WHERE event_id = %s", (event_id,)).fetchone()[
                "event_id"
            ]
            == event_id
        )

        task = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
        with repositories_for_connection(conn).transaction():
            receipt = ReviewDesk(conn, now_ms=NOW).submit(
                TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
                _rubric(),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )
        assert receipt["receipt"]["review_id"]

        with repositories_for_connection(conn).transaction():
            external = ReviewDesk(conn, now_ms=NOW).submit(
                None,
                ExternalMissSubmission(
                    source_url="https://example.test/role-miss",
                    title="External miss through the narrow writer role",
                    body="Primary source body",
                    occurred_at_ms=NOW - 1,
                    rubric=_rubric(),
                ),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )
        assert external["receipt"]["external_snapshot_id"]

        conn.execute("BEGIN")
        conn.execute("SAVEPOINT denied_review_rewrite")
        with pytest.raises((InsufficientPrivilege, RaiseException)):
            conn.execute("DELETE FROM news_reviews")
        conn.execute("ROLLBACK TO SAVEPOINT denied_review_rewrite")
        conn.execute("RELEASE SAVEPOINT denied_review_rewrite")
        conn.execute("SAVEPOINT denied_news_write")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("INSERT INTO news_events(event_id) VALUES (%s)", ("f" * 64,))
        conn.execute("ROLLBACK TO SAVEPOINT denied_news_write")
        conn.execute("RELEASE SAVEPOINT denied_news_write")
        conn.commit()
    finally:
        conn.execute("RESET ROLE")
        conn.commit()


def test_http_review_adapter_enforces_match_auth_and_idempotency(conn) -> None:
    event_id = _open_event(conn)

    class Runtime:
        settings = Settings(ws_token="review-token")

        @contextmanager
        def repositories(self):
            yield repositories_for_connection(conn)

        @contextmanager
        def review_transaction(self):
            with repositories_for_connection(conn).transaction():
                yield conn

    app = create_app(settings=Runtime.settings)
    app.state.service = Runtime()
    api = TestClient(app)
    queue_response = api.get(
        "/api/news/review",
        params={"event": event_id},
        headers={"Authorization": "Bearer review-token"},
    )
    assert queue_response.status_code == 200
    task = queue_response.json()["data"]["tasks"][0]
    version = f'"{task["task_version"]}"'
    evidence = api.get(
        f"/api/news/review/tasks/{task['task_id']}/evidence",
        headers={"Authorization": "Bearer review-token", "If-Match": version},
    )
    assert evidence.status_code == 200
    body = _rubric().model_dump(mode="json")
    request_key = str(uuid.uuid4())
    # Mutations never accept the URL query-token compatibility path.
    query_token = api.post(
        f"/api/news/review/tasks/{task['task_id']}/responses?token=review-token",
        headers={"If-Match": version, "Idempotency-Key": request_key},
        json=body,
    )
    assert query_token.status_code == 400  # mutation query strings are rejected before authentication
    headers = {
        "Authorization": "Bearer review-token",
        "If-Match": version,
        "Idempotency-Key": request_key,
    }
    wrong_media = api.post(
        f"/api/news/review/tasks/{task['task_id']}/responses",
        headers={**headers, "Content-Type": "text/plain"},
        content=json.dumps(body),
    )
    assert wrong_media.status_code == 400
    assert wrong_media.json()["error"] == "news_review_content_type_invalid"
    no_length = api.post(
        f"/api/news/review/tasks/{task['task_id']}/responses",
        headers={**headers, "Content-Type": "application/json"},
        content=(part for part in [json.dumps(body)]),
    )
    assert no_length.status_code == 400
    assert no_length.json()["error"] == "news_review_content_length_required"
    oversized = api.post(
        f"/api/news/review/tasks/{task['task_id']}/responses",
        headers={**headers, "Content-Type": "application/json", "Content-Length": "32769"},
        content=json.dumps(body),
    )
    assert oversized.status_code == 400
    assert oversized.json()["error"] == "news_review_body_too_large"
    first = api.post(f"/api/news/review/tasks/{task['task_id']}/responses", headers=headers, json=body)
    again = api.post(f"/api/news/review/tasks/{task['task_id']}/responses", headers=headers, json=body)
    assert first.status_code == 200 and first.json()["data"]["idempotent"] is False
    assert again.status_code == 200 and again.json()["data"]["idempotent"] is True
    assert first.json()["data"]["receipt"]["review_id"] == again.json()["data"]["receipt"]["review_id"]
    reopened = api.get(
        "/api/news/review",
        params={"event": event_id, "status": "all"},
        headers={"Authorization": "Bearer review-token"},
    )
    assert reopened.status_code == 200
    accepted_review = reopened.json()["data"]["tasks"][0]["accepted_review"]
    assert accepted_review["subject_kind"] == "event"
    assert accepted_review["event_id"] == event_id
    assert accepted_review["external_snapshot_id"] is None
    conflict = api.post(
        f"/api/news/review/tasks/{task['task_id']}/responses",
        headers=headers,
        json=_rubric(why="fail").model_dump(mode="json"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "news_review_idempotency_conflict"
