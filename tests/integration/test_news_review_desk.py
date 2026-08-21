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
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import (
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)
from tracefold.news.events import admit_item
from tracefold.news.opennews import parse_opennews_message
from tracefold.platform.config.settings import Settings

pytestmark = pytest.mark.integration

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
        verdict = {
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
        assert repos.news.insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version="v6",
            model_decision="push",
            rule_baseline_decision="push",
            final_decision="push",
            override_rule="model_push_actionable",
            throttled_by=None,
            verdict=verdict,
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


def _rubric(*, why: str = "pass") -> EventRubricSubmission:
    return EventRubricSubmission(
        should_push="must_push",
        dimensions={
            "factual_fidelity": "pass",
            "headline_fidelity": "pass",
            "why_support": why,
            "why_value": "pass",
            "timeliness": "pass",
        },
        novelty={"judgment": "new_fact"},
        evidence_refs=[] if why == "pass" else ["source:sentence:1", "output:why"],
        expected_correction="" if why == "pass" else "Do not claim priced-in without source evidence.",
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
    assert evidence["agent"]["cohort"] == "news_semantic_program_test/v6/test-model"
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


def test_coverage_keeps_exact_agent_bundles_separate(conn) -> None:
    first_bundle, second_bundle = "1" * 64, "2" * 64
    first_event = _open_event(
        conn,
        hit_id=112011,
        title="Micron DRAM contract prices rise in August",
        bundle_sha=first_bundle,
    )
    second_event = _open_event(
        conn,
        hit_id=112012,
        title="Federal Reserve governor announces an immediate resignation",
        bundle_sha=second_bundle,
    )
    conn.execute("UPDATE news_events SET priority = 'high' WHERE event_id = ANY(%s)", ([first_event, second_event],))

    coverage = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="coverage"), principal=PRINCIPAL)
    by_cohort = {row["cohort"]: row for row in coverage["cohorts"]}
    assert set(by_cohort) == {first_bundle, second_bundle}
    assert by_cohort[first_bundle]["agent"]["bundle_sha"] == first_bundle
    assert by_cohort[second_bundle]["agent"]["bundle_sha"] == second_bundle
    assert {row["legacy_cohort"] for row in coverage["cohorts"]} == {"news_semantic_program_test/v6/test-model"}
    default_queue = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(), principal=PRINCIPAL)
    assert {task["event_id"] for task in default_queue["tasks"]} == {first_event}
    second_queue = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(cohort=second_bundle), principal=PRINCIPAL)
    assert {task["event_id"] for task in second_queue["tasks"]} == {second_event}


def test_evidence_refs_are_bounded_per_entry() -> None:
    with pytest.raises(ValueError, match="at most 500 characters"):
        EventRubricSubmission(
            should_push="must_hold",
            dimensions={"factual_fidelity": "fail"},
            novelty={"judgment": "new_fact"},
            evidence_refs=["x" * 501],
        )


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
    assert market["reaction"]["meta"]["cohort"] == "news_semantic_program_test/v6/test-model"
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
        "selection_version": "news_review_sampler_v1",
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
    desk = ReviewDesk(conn, now_ms=NOW)
    submission = ExternalMissSubmission(
        source_url="https://example.test/missed",
        title="A material source item the receiver never ingested",
        body="Primary source body",
        occurred_at_ms=NOW - 10_000,
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
    coverage = ReviewDesk(conn).open(DeskQuery(view="coverage"), principal=PRINCIPAL)
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

    assert [(item["target"], item["target_zh"]) for item in proposals] == [("prompt", "提示词（历史审计）")]


def test_development_pair_reveals_arm_mapping_and_exact_candidate_diff_after_acceptance(conn) -> None:
    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    run_sha, case_id, candidate_sha = "1" * 64, "2" * 64, "3" * 64
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
    assert [(item["target"], item["target_zh"]) for item in proposals] == [("program", "DSPy Program")]
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
