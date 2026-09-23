from __future__ import annotations

import json
import uuid

import pytest
from psycopg.errors import CheckViolation, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import news_taxonomy
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.learning.contracts import epoch_id_for_bundle
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.program.contracts import (
    JUDGMENT_CONTRACT_VERSION,
    EditorialEnvelope,
    ScoredJudgment,
)
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.runtime import PROGRAM_SCHEMA_VERSION, PROGRAM_VERSION
from tracefold.news.review.desk import (
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExpectedCorrection,
    ExplanationCorrectionV1,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)
from tracefold.news.taxonomy import ModelTaxonomyV1

pytestmark = pytest.mark.integration

NOW = 1_787_287_000_000
PRINCIPAL = Principal(subject="operator")
# The four model axes a `news_review_v8` submission carries. Not `news_taxonomy()`: that helper builds
# the persisted seven-key shape, whose `source_authority` is a code fact the reviewer never states.
MODEL_TAXONOMY = ModelTaxonomyV1(
    subject_codes=(),
    event_family="regulatory_legal",
    change_state="reported",
    assertion_status="claimed",
)
ACTIVE_BUNDLE = "1" * 64
# The epoch the fixture deployment opens (#314): derived from the bundle it appoints, never declared.
ACTIVE_EPOCH = epoch_id_for_bundle(ACTIVE_BUNDLE)
# A superseded epoch is a superseded bundle (#314): the label is derived from the bundle, so a
# corpus sealed in an earlier epoch necessarily names the earlier bundle beside it.
SUPERSEDED_BUNDLE = "9" * 64
SUPERSEDED_EPOCH = epoch_id_for_bundle(SUPERSEDED_BUNDLE)


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    with repositories_for_connection(connection).transaction():
        repositories_for_connection(connection).news.register_agent_runtime_manifest(
            manifest_sha="a" * 64,
            stable_bundle_sha=ACTIVE_BUNDLE,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=PROGRAM_VERSION,
            program_sha256="b" * 64,
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
        editorial = EditorialEnvelope.issue(
            source_authority="reputable_secondary",
            taxonomy=news_taxonomy(
                event_family="regulatory_legal",
                change_state="reported",
                assertion_status="claimed",
            ),
        )
        judgment = ScoredJudgment.issue(verdict=verdict, editorial=editorial)
        assert repos.news.insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=JUDGMENT_CONTRACT_VERSION,
            judgment_origin="model",
            rule_baseline_decision="drop",
            final_decision=final_decision,
            override_rule="fact_kind_new_quantity",
            throttled_by=throttled_by,
            verdict=verdict.model_dump(mode="json"),
            model_editorial=editorial.model_dump(mode="json"),
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha="a" * 64,
            model="test-model",
            program_version=PROGRAM_VERSION,
            program_sha256=program_sha256,
            degraded=False,
            error_code=None,
            trace={
                "input_sha256": "a" * 64,
                "prompt_sha256": "b" * 64,
                "schema_sha256": "c" * 64,
                "gate_policy_version": "v4",
                "judgment_contract_version": JUDGMENT_CONTRACT_VERSION,
                "judgment_origin": "model",
                "judgment_sha256": judgment.scored_judgment_sha256,
                "verdict_sha256": judgment.verdict_sha256,
                "editorial_sha256": editorial.editorial_sha256,
                "runtime_manifest_sha": "a" * 64,
                "program_version": PROGRAM_VERSION,
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
    fact_kind: str | None = None,
) -> EventRubricSubmission:
    """One accepted rubric.

    `first_bad_owner` is the operator's own attribution and is what #199's Objective Plan reads to decide
    whether GEPA may try to repair the case. It is deliberately not defaulted: a rubric that leaves it
    unset is exactly the shape ReviewDesk derives an owner for, and the plan must not treat a derived
    owner as a grant.

    `fact_kind="fail"` is the *typed* failure — a stated correct value the metric can score a repair
    against. `why="fail"` is a copy complaint with no such value; #199 keeps it as an excluded diagnostic
    rather than a target, so a corpus meant to exercise optimization has to fail a typed dimension.

    It states every answer on purpose, which under `news_review_v8` is a choice rather than a
    requirement: this is the shape a reviewer who knows the whole Event submits, and the partial shapes
    are exercised by the tests that are about them.
    """

    dimensions = {
        "factual_fidelity": "pass",
        "headline_fidelity": "pass",
        "why_support": why,
        "why_value": "pass",
        "timeliness": "pass",
        "taxonomy_subject_codes": "pass",
        "taxonomy_event_family": "pass",
        "taxonomy_change_state": "pass",
        "taxonomy_assertion_status": "pass",
    }
    if fact_kind is not None:
        dimensions["fact_kind"] = fact_kind
    failed = why == "fail" or fact_kind == "fail"
    return EventRubricSubmission(
        should_push=should_push,  # type: ignore[arg-type]
        dimensions=dimensions,
        novelty={"judgment": "new_fact"},
        taxonomy=MODEL_TAXONOMY,
        first_bad_owner=first_bad_owner,  # type: ignore[arg-type]
        expected=ExpectedCorrection(fact_kind="statement") if fact_kind == "fail" else None,
        evidence_refs=["source:sentence:1", "output:why"] if failed else [],
        expected_correction="Do not claim priced-in without source evidence." if failed else "",
    )


def _insert_learning_dataset(
    conn,
    dataset_sha: str,
    *,
    learning_epoch: str = ACTIVE_EPOCH,
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
    assert evidence["agent"]["cohort"] == f"{PROGRAM_VERSION}/{TRIAGE_POLICY_VERSION}/test-model"
    assert evidence["agent"]["agent_cohort"]["cohort_sha256"] == task["agent_cohort"]["cohort_sha256"]
    source_only = desk.evidence(ref, principal=PRINCIPAL, source_only=True)
    assert set(source_only) == {
        "schema",
        "task",
        "evidence",
        "evidence_sha256",
        "projection_sha256",
    }
    assert source_only["task"]["task_id"] == ref.task_id
    assert source_only["task"]["task_version"] == ref.task_version
    assert source_only["evidence"] == evidence["evidence"]
    assert not any(key in source_only for key in ("agent", "accepted_review", "duplicate_hints"))
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


def test_event_evidence_offers_bounded_cross_source_duplicate_hints_without_unioning(conn) -> None:
    first = _open_event(conn, hit_id=112101, title="FTC opens Amazon antitrust investigation")
    second = _open_event(
        conn,
        hit_id=112102,
        title="Amazon marketplace accused of monopoly by US regulator",
        source="Bloomberg",
    )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.set_storyline_key(event_id=first, storyline_key="asset:AMZN:ftc", now_ms=NOW)
        repos.news.set_storyline_key(event_id=second, storyline_key="asset:AMZN:ftc", now_ms=NOW)

    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=first), principal=PRINCIPAL)["tasks"][0]
    evidence = desk.evidence(
        TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
        principal=PRINCIPAL,
    )

    assert [hint["event_id"] for hint in evidence["duplicate_hints"]] == [second]
    assert evidence["duplicate_hints"][0]["selection_reason"].startswith("same_storyline_family")
    assert (
        conn.execute("SELECT count(*) AS n FROM news_reviews WHERE event_id IN (%s, %s)", (first, second)).fetchone()[
            "n"
        ]
        == 0
    )


def test_two_primary_reviewers_are_retained_and_adjudication_requires_an_independent_principal(conn) -> None:
    event_id = _open_event(conn, hit_id=112002, title="AMD confirms a new data-center GPU launch window")
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])

    for reviewer in ("reviewer-alice", "reviewer-bob"):
        with repositories_for_connection(conn).transaction():
            desk.submit(
                ref,
                _rubric(),
                principal=Principal(subject=reviewer),
                idempotency_key=str(uuid.uuid4()),
            )

    rows = conn.execute(
        "SELECT review_id, reviewer, payload FROM news_review_records_v1 "
        "WHERE task_id = %s AND review_kind = 'judgment' ORDER BY review_id",
        (task["task_id"],),
    ).fetchall()
    assert {row["reviewer"] for row in rows} == {"reviewer-alice", "reviewer-bob"}
    assert len(rows) == 2

    latest = desk._latest_accepted(desk._event_task(event_id, evidence_version=task["evidence_version"]))
    assert latest is not None
    adjudication = EventRubricSubmission.model_validate(
        _rubric().model_dump(mode="json")
        | {
            "taxonomy_review": {
                "review_role": "adjudication",
                "adjudicates_review_id": latest["review_id"],
            }
        }
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_taxonomy_adjudicator_not_independent"),
    ):
        desk.submit(
            ref,
            adjudication,
            principal=Principal(subject=latest["reviewer"]),
            idempotency_key=str(uuid.uuid4()),
        )


def test_coverage_spans_every_arm_and_narrows_only_when_a_cohort_is_named(conn) -> None:
    """#651 §9: the running bundle is a filter an operator may ask for, never a fence.

    Coverage used to show only the Events the currently appointed Agent had answered. That made every
    deployment reset the visible corpus to zero, and the Events it hid were exactly the ones reviewers
    had spent the previous days on -- a review is about the words a reader saw, and those words do not
    change when a new bundle is appointed. Both arms are in the default funnel now, and `cohort` still
    narrows to one when an operator is comparing arms deliberately.

    Both Events are escalates so the default queue below is about the cohort filter rather than about
    `_sampler_selected`'s hash: `critical` is fully sampled, while the `delivered` stratum these rows
    would otherwise take is sampled at 0.25 (#675 §1 deleted the relevance-scoped strata that used to
    pull a macro row into a fully sampled one).
    """

    first_bundle, second_bundle = "1" * 64, "2" * 64
    first_event = _open_event(
        conn,
        hit_id=112011,
        title="Micron DRAM contract prices rise in August",
        bundle_sha=first_bundle,
        final_decision="escalate",
    )
    second_event = _open_event(
        conn,
        hit_id=112012,
        title="Federal Reserve governor announces an immediate resignation",
        bundle_sha=second_bundle,
        final_decision="escalate",
    )
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (ACTIVE_EPOCH,),
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
    assert set(by_cohort) == {first_bundle, second_bundle}
    assert by_cohort[first_bundle]["agent"]["bundle_sha"] == first_bundle
    assert by_cohort[second_bundle]["agent"]["bundle_sha"] == second_bundle
    assert coverage["funnel"]["total"] == 2
    assert coverage["funnel"]["accepted"] == 2
    # Both reviews are release evidence: eligibility is a property of the frozen evidence snapshot, and
    # both Events carry one. Which arm answered them is recorded on the frozen case, not used here.
    eligibility = conn.execute(
        "SELECT event_id, bool_and(release_eligible) AS release_eligible FROM news_reviews "
        "WHERE event_id = ANY(%s) GROUP BY event_id",
        ([first_event, second_event],),
    ).fetchall()
    assert {row["event_id"]: row["release_eligible"] for row in eligibility} == {
        first_event: True,
        second_event: True,
    }
    default_queue = ReviewDesk(conn, now_ms=review_now).open(DeskQuery(status="all"), principal=PRINCIPAL)
    assert {task["event_id"] for task in default_queue["tasks"]} == {first_event, second_event}
    for bundle, expected in ((first_bundle, first_event), (second_bundle, second_event)):
        narrowed = ReviewDesk(conn, now_ms=review_now).open(DeskQuery(cohort=bundle, status="all"), principal=PRINCIPAL)
        assert {task["event_id"] for task in narrowed["tasks"]} == {expected}
    narrowed_coverage = desk.open(DeskQuery(view="coverage", cohort=second_bundle), principal=PRINCIPAL)
    assert {row["cohort"] for row in narrowed_coverage["cohorts"]} == {second_bundle}


def test_coverage_counts_evidence_from_before_the_running_epoch_opened(conn) -> None:
    """#651 §9: the epoch is runtime identity and audit, and it decides no data eligibility.

    Both Events below are real, delivered and reviewed; the only thing separating them is that one
    opened a millisecond before the running deployment wrote its epoch row. Clamping the window there
    threw away hours of accepted review every time Workers restarted, and the review it threw away was
    the review of the cards a reader had actually just been sent.
    """

    prior_event = _open_event(
        conn,
        hit_id=112013,
        title="Evidence from before the epoch opened is corpus truth like any other",
    )
    current_event = _open_event(
        conn,
        hit_id=112014,
        title="Evidence from after the epoch opened is eligible for coverage",
    )
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (ACTIVE_EPOCH,),
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
    assert by_event[prior_event] == {"acceptance": True, "judgment": True}
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
        "acceptance": True,
        "judgment": True,
    }

    # The window an operator asked for is the window they get: 24 h back from now, not "back to whenever
    # this deployment started".
    assert coverage["window"]["from_ms"] == review_now - 24 * 3_600_000
    assert coverage["status"] == "ready"
    assert coverage["funnel"] == {
        "received": 2,
        "replayable": 2,
        "reviewed": 2,
        "accepted": 2,
        "holdout_ready": 0,
        "total": 2,
        "external_misses": 2,
    }
    assert sum(row["events"] for row in coverage["strata"]) == 2


def test_market_view_defaults_to_latest_homogeneous_cohort_and_hides_sparse_families(conn) -> None:
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
    assert market["reaction"]["meta"]["cohort"] == f"{PROGRAM_VERSION}/{TRIAGE_POLICY_VERSION}/test-model"
    assert market["reaction"]["meta"]["cohort_sha256"] == ACTIVE_BUNDLE
    assert market["reaction"]["meta"]["program_sha256"] == "b" * 64
    assert market["reaction"]["coverage"][0]["eligible_n"] == 1
    assert market["reaction"]["event_families"] == []
    assert "不是新闻因果" in market["disclaimer_zh"]
    with pytest.raises(ValueError, match="news_review_market_hours_too_large"):
        ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="market", hours=720), principal=PRINCIPAL)


def test_high_reaction_accepted_review_is_release_eligible_like_any_other_stratum(conn) -> None:
    """#504 D7: the sampler pulls a held case into the queue because of a post-event price move, but the reviewer
    labels `should_push` from the evidence alone, so the accepted review counts toward the freeze like every other
    stratum's. The stratum is still named as discovery-only in the task itself."""

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
        "selection_version": "news_review_sampler_v4",
    }
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    with repos.transaction():
        desk.submit(ref, _rubric(), principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))
    rows = conn.execute(
        "SELECT review_kind, release_eligible FROM news_reviews WHERE event_id = %s ORDER BY created_at_ms",
        (event_id,),
    ).fetchall()
    assert {row["review_kind"]: row["release_eligible"] for row in rows} == {
        "judgment": True,
        "acceptance": True,
    }
    coverage = desk.open(DeskQuery(view="coverage"), principal=PRINCIPAL)
    assert coverage["funnel"]["accepted"] == 1


def _accept(conn, desk: ReviewDesk, event_id: str, should_push: str) -> str:
    repos = repositories_for_connection(conn)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    with repos.transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            _rubric(should_push=should_push),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return str(task["selection"]["stratum"])


def test_the_daily_audit_ratios_group_accepted_judgments_by_stratum(conn) -> None:
    """#675 §4. `keep_ratio_sent_24h` and `missed_ratio_dropped_24h` are shares of *accepted judgments*,
    not of cards, so each one publishes the two numbers it was divided from. `uncertain` is in both
    denominators and in neither numerator, and a judgment from another stratum is in neither."""

    desk = ReviewDesk(conn, now_ms=NOW)
    delivered = [
        _open_event(conn, hit_id=112101, title="Micron lifts DRAM guidance for the December quarter"),
        _open_event(conn, hit_id=112102, title="Samsung says HBM4 qualification finished ahead of plan"),
        _open_event(conn, hit_id=112103, title="SK Hynix raises contract prices for enterprise SSDs"),
    ]
    dropped = [
        _open_event(
            conn,
            hit_id=112104,
            title="Brazil central bank leaves the Selic rate unchanged",
            delivered=False,
            final_decision="drop",
        ),
        _open_event(
            conn,
            hit_id=112105,
            title="Norwegian sovereign fund posts a quarterly loss on equities",
            delivered=False,
            final_decision="drop",
        ),
    ]
    throttled = _open_event(
        conn,
        hit_id=112106,
        title="Tokyo utility signs a ten-year LNG offtake agreement",
        delivered=False,
        final_decision="throttled",
        # A v17 throttle key: the `:budget` key this used went with the #504 budget, and `:seen` would need
        # the trace's `seen_scope`, which this helper does not write.
        throttled_by="artifact:stale",
    )
    # A task the sampler pulls into its own stratum. It is a real accepted judgment and it must not land
    # in either product ratio, which is the whole point of grouping by stratum. `critical` is that
    # stratum under the v16 sampler: #675 §1 deleted the six relevance-scoped strata with the codes they
    # read, and an escalate is the one an operator reviews for its own sake.
    elsewhere = _open_event(
        conn,
        hit_id=112107,
        title="Tokyo halts every LNG cargo out of the strait",
        delivered=False,
        final_decision="escalate",
    )

    assert [
        _accept(conn, desk, event_id, label)
        for event_id, label in zip(delivered, ("must_push", "should_hold", "uncertain"), strict=True)
    ] == ["delivered", "delivered", "delivered"]
    assert [
        _accept(conn, desk, event_id, label)
        for event_id, label in zip(dropped, ("should_push", "must_hold"), strict=True)
    ] == ["model_drop", "model_drop"]
    assert _accept(conn, desk, throttled, "uncertain") == "throttled"
    assert _accept(conn, desk, elsewhere, "must_push") == "critical"

    now_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS n").fetchone()["n"]
    )
    pipeline = repositories_for_connection(conn).news.status_snapshot(now_ms=now_ms)["pipeline"]

    assert pipeline["keep_ratio_sent_24h"] == {"ratio": round(1 / 3, 4), "numerator": 1, "denominator": 3}
    assert pipeline["missed_ratio_dropped_24h"] == {"ratio": round(1 / 3, 4), "numerator": 1, "denominator": 3}
    # The epoch-clamped release counter still sees every stratum, including the one the ratios exclude.
    assert pipeline["reviewed_should_push_24h"] == 3


def test_a_window_with_no_accepted_judgment_reports_a_null_ratio_over_zero(conn) -> None:
    """Null over zero, not 0% and not 100%: nobody audited, so there is nothing to read."""

    now_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS n").fetchone()["n"]
    )
    pipeline = repositories_for_connection(conn).news.status_snapshot(now_ms=now_ms)["pipeline"]
    empty = {"ratio": None, "numerator": 0, "denominator": 0}
    assert pipeline["keep_ratio_sent_24h"] == empty and pipeline["missed_ratio_dropped_24h"] == empty


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


def test_unjudged_new_evidence_is_not_projected_as_a_current_review_task(conn) -> None:
    """The task the desk offers is always a judged one, and #548 PR-B.2 changed which judged one.

    A member join appends an evidence snapshot without re-running triage. The view used to take the
    newest snapshot and require the newest verdict to have judged that exact version, so the whole Event
    — its verdict, its delivery and its accepted review — left the desk and the freeze the moment a
    member arrived. It now joins the verdict to the snapshot it actually judged, so the `v1` task stays
    exactly as it was, still accepted, while the unjudged `v2` evidence is still not offered as a task.
    """

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

    tasks = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"]
    assert [task["task_id"] for task in tasks] == [first["task_id"]]
    assert tasks[0]["task_version"] == first["task_version"]
    assert tasks[0]["evidence_version"] == 1
    assert tasks[0]["review_status"] == "accepted"
    # The desk never offers the version nothing judged: the view holds the judged row and only that.
    projected = conn.execute(
        "SELECT evidence_version FROM news_review_task_source_v1 WHERE event_id = %s", (event_id,)
    ).fetchall()
    assert [int(row["evidence_version"]) for row in projected] == [1]


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


def test_event_queue_cursor_matches_return_order_and_pins_the_window(conn) -> None:
    """The cursor and the window, on three tasks the sampler always takes.

    All three are fully sampled strata on purpose -- two escalates and one terminal delivery -- because
    this test is about paging order and the pinned window, and a partially sampled stratum would make
    its membership a property of `_sampler_selected`'s hash rather than of the cursor. Under the v15
    sampler the first of them landed in `systemic_macro_must_interrupt` at the same probability; #675 §1
    deleted that stratum with the relevance codes it read, so the escalate says it instead.
    """

    newest = _open_event(
        conn,
        hit_id=112101,
        title="Federal Reserve unexpectedly cuts its policy rate by 50 basis points",
        final_decision="escalate",
    )
    delivery_failed = _open_event(
        conn,
        delivered=False,
        hit_id=112102,
        title="Micron opens a new DRAM fabrication plant in Idaho",
    )
    oldest = _open_event(
        conn,
        hit_id=112103,
        title="Brazil regulator approves a new US-listed airline route",
        final_decision="escalate",
    )
    repos = repositories_for_connection(conn)
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (ACTIVE_EPOCH,),
        ).fetchone()["starts_at_ms"]
    )
    queue_now = epoch_start + 3_600_000
    with repos.transaction():
        conn.execute(
            "UPDATE news_events SET opened_at_ms = CASE event_id "
            "WHEN %s THEN %s WHEN %s THEN %s ELSE %s END WHERE event_id = ANY(%s)",
            (
                newest,
                queue_now - 100,
                delivery_failed,
                queue_now - 200,
                queue_now - 3_600_000 + 30_000,
                [newest, delivery_failed, oldest],
            ),
        )
        assert (
            repos.news.begin_delivery(event_id=delivery_failed, kind="first", card={}, now_ms=queue_now - 1_000)
            == "new"
        )
        assert repos.news.settle_delivery(
            event_id=delivery_failed,
            kind="first",
            state="terminal",
            receipt=None,
            error_code="ambiguous_after_crash",
            now_ms=queue_now,
        )

    query = DeskQuery(cohort=ACTIVE_BUNDLE, status="all", hours=1, limit=2)
    first = ReviewDesk(conn, now_ms=queue_now).open(query, principal=PRINCIPAL)
    second = ReviewDesk(conn, now_ms=queue_now + 60_000).open(
        query.model_copy(update={"cursor": first["next_cursor"]}), principal=PRINCIPAL
    )
    tasks = [*first["tasks"], *second["tasks"]]

    assert [task["event_id"] for task in tasks] == [newest, delivery_failed, oldest]
    assert len({task["task_id"] for task in tasks}) == 3

    task_by_event = {task["event_id"]: task for task in tasks}
    accepted_task = task_by_event[newest]
    with repos.transaction():
        ReviewDesk(conn, now_ms=queue_now).submit(
            TaskRef(task_id=accepted_task["task_id"], task_version=accepted_task["task_version"]),
            _rubric(),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    pending_query = query.model_copy(update={"status": "pending", "limit": 1})
    pending_first = ReviewDesk(conn, now_ms=queue_now).open(pending_query, principal=PRINCIPAL)
    pending_second = ReviewDesk(conn, now_ms=queue_now + 60_000).open(
        pending_query.model_copy(update={"cursor": pending_first["next_cursor"]}), principal=PRINCIPAL
    )
    assert [task["event_id"] for task in [*pending_first["tasks"], *pending_second["tasks"]]] == [
        delivery_failed,
        oldest,
    ]
    accepted = ReviewDesk(conn, now_ms=queue_now).open(
        query.model_copy(update={"status": "accepted", "limit": 1}), principal=PRINCIPAL
    )
    assert [task["event_id"] for task in accepted["tasks"]] == [newest]


def test_event_queue_scans_sparse_strata_past_two_thousand_real_postgres_rows(conn) -> None:
    # A temporary view shadows the production projection for this connection while retaining the exact SQL
    # seam ReviewDesk queries. Every 40th row is an escalate, which is the requested 100%-sampled stratum,
    # so the first 2,000 raw rows contain only 50 eligible tasks and cannot establish queue exhaustion.
    conn.execute(
        f"""
        CREATE TEMP VIEW news_review_task_source_v1 AS
        SELECT lpad(to_hex(i), 64, '0') AS event_id,
               1 AS evidence_version,
               repeat('e', 64) AS evidence_sha256,
               true AS evidence_release_eligible,
               jsonb_build_object('card', '{{}}'::jsonb, 'focus_fact', '{{}}'::jsonb) AS evidence_snapshot,
               {NOW} - i AS opened_at_ms,
               'candidate'::text AS admission,
               'normal'::text AS queue_priority,
               lpad(to_hex(i), 64, '0') AS storyline_key,
               'live'::text AS ingest_mode,
               {NOW} - i AS verdict_created_at_ms,
               1 AS verdict_evidence_version,
               CASE WHEN i % 40 = 0 THEN 'escalate' ELSE 'drop' END::text AS final_decision,
               false AS degraded,
               NULL::text AS verdict_error_code,
               NULL::text AS override_rule,
               NULL::text AS throttled_by,
               jsonb_build_object(
                   'novelty', 'new_fact', 'restates', -1, 'assets', '[]'::jsonb,
                   'direction', 'neutral', 'scope', 'macro',
                   'fact_kind', CASE WHEN i % 40 = 0 THEN 'official_measure' ELSE 'statement' END,
                   'evidence_ref', 'c1', 'confidence', 1.0,
                   'headline_zh', i::text, 'why_zh', 'x'
               ) AS verdict,
               jsonb_build_object('agent_assignment', jsonb_build_object('bundle_sha', '{ACTIVE_BUNDLE}')) AS trace,
               '{TRIAGE_POLICY_VERSION}'::text AS policy_version,
               'model'::text AS model,
               NULL::text AS delivery_state,
               NULL::jsonb AS delivery_card,
               NULL::bigint AS settled_at_ms,
               NULL::text AS delivery_error_code,
               NULL::integer AS max_abs_return_1h_bps,
               '{PROGRAM_VERSION}'::text AS program_version,
               repeat('b', 64) AS program_sha256,
               jsonb_build_object('editorial_origin', 'model') AS model_editorial,
               '{JUDGMENT_CONTRACT_VERSION}'::text AS judgment_contract_version,
               'model'::text AS judgment_origin,
               repeat('c', 64) AS judgment_sha256,
               repeat('d', 64) AS runtime_manifest_sha,
               'news'::text AS event_kind
          FROM generate_series(1, 5000) AS series(i)
        """
    )

    queue = ReviewDesk(conn, now_ms=NOW).open(
        DeskQuery(
            cohort=ACTIVE_BUNDLE,
            stratum="critical",
            status="all",
            hours=1,
            limit=100,
        ),
        principal=PRINCIPAL,
    )

    assert len(queue["tasks"]) == 100
    assert queue["next_cursor"]


def test_external_miss_appends_snapshot_and_accepted_judgment_atomically(conn) -> None:
    epoch_start = int(
        conn.execute(
            "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
            (ACTIVE_EPOCH,),
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


def test_external_miss_accepts_only_one_explicit_taxonomy_axis(conn) -> None:
    desk = ReviewDesk(conn, now_ms=NOW)
    submission = ExternalMissSubmission(
        source_url="https://example.test/partial-taxonomy",
        title="A source item with one reviewed taxonomy axis",
        body="Primary source body",
        occurred_at_ms=NOW - 10_000,
        rubric=EventRubricSubmission.model_validate({"dimensions": {}, "taxonomy": {"event_family": "other"}}),
    )
    with repositories_for_connection(conn).transaction():
        desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))
    row = conn.execute("SELECT payload FROM news_reviews WHERE review_kind = 'judgment'").fetchone()
    assert row["payload"]["taxonomy"] == {"event_family": "other"}
    assert row["payload"]["dimensions"] == {}


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
        "verdict": {"headline_zh": "DRAM 价格上涨", "why_zh": "需求改善。", "fact_kind": "new_quantity"},
        "final_decision": "push",
        "delivered": True,
    }
    candidate = {
        "verdict": {"headline_zh": "DRAM 合约价续涨", "why_zh": "供给偏紧改善厂商议价。", "fact_kind": "new_quantity"},
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
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.set_storyline_key(
            event_id=event_id,
            storyline_key="macro:pairwise-later-evidence",
            now_ms=NOW + 1,
        )
        newer = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=NOW + 2)
    assert int(newer["evidence_version"]) == int(source["evidence_version"]) + 1
    # #548 PR-B.2: the Event keeps the row its verdict judged when a later snapshot arrives; the newer,
    # unjudged version is still not projected.
    projected = conn.execute(
        "SELECT evidence_version FROM news_review_task_source_v1 WHERE event_id = %s", (event_id,)
    ).fetchall()
    assert [int(row["evidence_version"]) for row in projected] == [int(source["evidence_version"])]
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
            json.dumps({"learning_epoch": SUPERSEDED_EPOCH, "agent_cohort": {"bundle_sha": SUPERSEDED_BUNDLE}}),
            NOW - 8,
            current_dataset_sha,
            json.dumps({"learning_epoch": ACTIVE_EPOCH, "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
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
    assert historical["learning_epoch"] == SUPERSEDED_EPOCH
    assert historical["evidence_disposition"] == "audit_only"
    assert historical["status"] == "audit_only"
    assert historical["timeline"][0]["outcome"] == "pass"
    assert historical["timeline"][0]["evidence_disposition"] == "audit_only"

    current = by_candidate[current_candidate_sha]
    assert current["learning_epoch"] == ACTIVE_EPOCH
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

    assert proposal["learning_epoch"] == ACTIVE_EPOCH
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

    assert proposal["learning_epoch"] == ACTIVE_EPOCH
    assert proposal["evidence_disposition"] == "audit_only"
    assert proposal["status"] == "audit_only"


def test_coverage_holdout_denominator_counts_every_sealed_validation_case(conn) -> None:
    """#651 §9: the holdout funnel counts the blind pairs that exist, whichever arm sealed them.

    It used to count only pairs from a dataset whose `agent_cohort` was the appointed Agent, which meant
    a deployment landing in the middle of a holdout hid the very judgments the holdout was waiting on
    and reported the coverage denominator as zero. Whether a pair may still be *submitted* against the
    running arm is a separate question, and `_pairwise_evidence_disposition` still answers it with the
    bundle pin -- the two tests below cover that.
    """

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
            json.dumps({"learning_epoch": SUPERSEDED_EPOCH, "agent_cohort": {"bundle_sha": SUPERSEDED_BUNDLE}}),
            NOW - 2,
            current_dataset_sha,
            json.dumps({"learning_epoch": ACTIVE_EPOCH, "agent_cohort": {"bundle_sha": ACTIVE_BUNDLE}}),
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

    assert coverage["holdout"]["case_n"] == 2
    assert coverage["holdout"]["cluster_n"] == 2


def test_a_pair_from_a_superseded_arm_is_listed_but_cannot_be_judged(conn) -> None:
    """#651 §9: the queue lists what exists; the disposition decides what may be written.

    A blind pair compares one candidate against the stable arm it was registered under, so it stays
    release evidence about that arm and keeps its bundle pin -- submitting a judgment on a pair whose
    comparator is no longer running would file an opinion about a system nobody is operating. What it no
    longer does is disappear from the queue: hiding it made the desk claim there was nothing to review
    when in fact there was something that could only be read.
    """

    event_id = _open_event(conn)
    source = conn.execute(
        "SELECT evidence_version, evidence_sha256, opened_at_ms FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    old_dataset_sha, current_dataset_sha = "1" * 64, "2" * 64
    old_run_sha, current_run_sha = "3" * 64, "4" * 64
    old_case_id, current_case_id = "5" * 64, "6" * 64
    _insert_learning_dataset(conn, old_dataset_sha, learning_epoch=SUPERSEDED_EPOCH, bundle_sha=SUPERSEDED_BUNDLE)
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
    assert {task["task_id"] for task in pending["tasks"]} == {old_task_id, current_task_id}
    # `cohort` still narrows, which is how an operator asks for only the pairs they may judge.
    narrowed = desk.open(DeskQuery(mode="pairwise", cohort=ACTIVE_BUNDLE), principal=PRINCIPAL)
    assert [task["task_id"] for task in narrowed["tasks"]] == [current_task_id]

    all_tasks = desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"]
    by_id = {task["task_id"]: task for task in all_tasks}
    assert by_id[old_task_id]["learning_epoch"] == SUPERSEDED_EPOCH
    assert by_id[old_task_id]["evidence_disposition"] == "audit_only"
    assert by_id[old_task_id]["review_status"] == "audit_only"
    assert by_id[current_task_id]["learning_epoch"] == ACTIVE_EPOCH
    assert by_id[current_task_id]["evidence_disposition"] == "current"
    assert by_id[current_task_id]["review_status"] == "pending"

    direct = desk.open(DeskQuery(task=old_task_id), principal=PRINCIPAL)["tasks"][0]
    assert direct["learning_epoch"] == SUPERSEDED_EPOCH
    assert direct["evidence_disposition"] == "audit_only"
    assert direct["review_status"] == "audit_only"

    with pytest.raises(CheckViolation, match="news_review_current_task_source_missing"):
        conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, review_kind, subject_kind, task_id, task_version, pairwise_case_id,
              rubric_version, reader_contract_version, reviewer, selection, payload, accepts_review_id,
              release_eligible, created_at_ms
            ) VALUES
              (%s, 'judgment', 'pairwise', %s, %s, %s,
               'news_review_v8', 'reader_contract_v3', 'audit-reviewer', %s::jsonb, %s::jsonb, NULL, true, %s),
              (%s, 'acceptance', 'pairwise', %s, %s, %s,
               'news_review_v8', 'reader_contract_v3', 'audit-reviewer', '{}'::jsonb, '{}'::jsonb, %s, true, %s)
            """,
            (
                "9" * 64,
                old_task_id,
                direct["task_version"],
                f"{old_run_sha}:{old_case_id}",
                json.dumps(direct["selection"]),
                json.dumps(
                    {
                        "kind": "blind_pairwise",
                        "preference": "A",
                        "critical_errors": [],
                        "evidence_refs": [],
                        "note": "",
                    }
                ),
                NOW - 1,
                "a" * 64,
                old_task_id,
                direct["task_version"],
                f"{old_run_sha}:{old_case_id}",
                "9" * 64,
                NOW,
            ),
        )
    # The guard refused the row, so nothing was accepted: a pair whose dataset names a bundle that is no
    # longer the appointed Agent has no current task source to be judged against.
    assert desk.open(DeskQuery(mode="pairwise", status="accepted"), principal=PRINCIPAL)["tasks"] == []
    historical = {
        task["task_id"]: task
        for task in desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"]
    }[old_task_id]
    assert historical["review_status"] == "audit_only"
    assert historical["accepted_review"] is None

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
        == 0
    )


def test_a_pair_sealed_by_another_bundle_is_audit_only_however_recent_it_is(conn) -> None:
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

    listed = desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["tasks"]
    assert [task["task_id"] for task in listed] == [task_id]
    assert desk.open(DeskQuery(mode="pairwise", cohort=ACTIVE_BUNDLE), principal=PRINCIPAL)["tasks"] == []
    audit_task = desk.open(DeskQuery(mode="pairwise", status="all"), principal=PRINCIPAL)["tasks"][0]
    assert audit_task["task_id"] == task_id
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
    stable = {"verdict": {"headline_zh": "旧标题", "why_zh": "旧解释。", "fact_kind": "statement"}, "delivered": False}
    candidate = {
        "verdict": {"headline_zh": "新标题", "why_zh": "有证据的新解释。", "fact_kind": "new_quantity"},
        "delivered": True,
    }
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


def test_a_review_may_answer_the_explanation_alone_and_nothing_else(conn) -> None:
    """#651 §7.2: the shape a reviewer submits when the card's *why* is the only thing they judged.

    Under v6 this submission was impossible. A reviewer who had read the evidence and concluded the why
    sentence is unsupported had to also state four taxonomy axes, a novelty judgment and a push verdict
    before the desk would take it, and all three of those invented answers then counted as accepted truth
    that a metric would later score a candidate against. Here nothing is stated but the copy verdict and
    the supervision behind it, the row persists, and the absent answers stay absent in the payload.
    """

    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    submission = EventRubricSubmission(
        dimensions={"why_support": "fail"},
        evidence_refs=["source:sentence:1"],
        explanation=ExplanationCorrectionV1(
            source_spans=["Micron says DRAM contract prices rose again in August"],
            key_facts=["DRAM 合约价 8 月再次上涨"],
            forbidden_claims=["涨幅已被市场完全定价"],
            error_types=["unsupported_cause"],
        ),
    )

    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    row = conn.execute(
        "SELECT should_push, dimensions, novelty, payload FROM news_review_records_v1 WHERE review_id = %s",
        (receipt["receipt"]["review_id"],),
    ).fetchone()
    assert row["should_push"] is None
    assert row["novelty"] == {}
    assert row["dimensions"] == {"why_support": "fail"}
    assert row["payload"]["taxonomy"] is None
    assert row["payload"]["explanation_supervision"] == "present"
    assert row["payload"]["explanation"]["key_facts"] == ["DRAM 合约价 8 月再次上涨"]

    coverage = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(view="coverage"), principal=PRINCIPAL)
    assert coverage["funnel"]["accepted"] == 1
    assert coverage["funnel"]["reviewed"] == 1


def test_a_taxonomy_only_and_an_asset_only_review_state_nothing_they_did_not_judge(conn) -> None:
    """Two more partial shapes, and the guarantee that neither fabricates the other's answer."""

    taxonomy_event = _open_event(conn, hit_id=112101, title="Regulator publishes the final custody rule")
    asset_event = _open_event(conn, hit_id=112102, title="Micron names the fab the capacity expansion lands in")
    desk = ReviewDesk(conn, now_ms=NOW)

    taxonomy_task = desk.open(DeskQuery(event=taxonomy_event), principal=PRINCIPAL)["tasks"][0]
    with repositories_for_connection(conn).transaction():
        taxonomy_receipt = desk.submit(
            TaskRef(task_id=taxonomy_task["task_id"], task_version=taxonomy_task["task_version"]),
            EventRubricSubmission(
                dimensions={
                    "taxonomy_subject_codes": "pass",
                    "taxonomy_event_family": "pass",
                    "taxonomy_change_state": "pass",
                    "taxonomy_assertion_status": "pass",
                },
                taxonomy=MODEL_TAXONOMY,
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    asset_task = desk.open(DeskQuery(event=asset_event), principal=PRINCIPAL)["tasks"][0]
    with repositories_for_connection(conn).transaction():
        asset_receipt = desk.submit(
            TaskRef(task_id=asset_task["task_id"], task_version=asset_task["task_version"]),
            EventRubricSubmission(
                dimensions={"asset_grounding": "fail"},
                evidence_refs=["source:sentence:1"],
                expected=ExpectedCorrection(assets=[{"symbol": "MU", "market_type": "equity", "role": "primary"}]),
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    rows = {
        str(row["review_id"]): row
        for row in conn.execute(
            "SELECT review_id, should_push, dimensions, novelty, payload FROM news_review_records_v1 "
            "WHERE review_id = ANY(%s)",
            ([taxonomy_receipt["receipt"]["review_id"], asset_receipt["receipt"]["review_id"]],),
        ).fetchall()
    }
    taxonomy_row = rows[taxonomy_receipt["receipt"]["review_id"]]
    asset_row = rows[asset_receipt["receipt"]["review_id"]]
    assert set(taxonomy_row["dimensions"]) == {
        "taxonomy_subject_codes",
        "taxonomy_event_family",
        "taxonomy_change_state",
        "taxonomy_assertion_status",
    }
    assert taxonomy_row["payload"]["explanation"] is None
    assert taxonomy_row["payload"]["explanation_supervision"] == "not_applicable"
    assert asset_row["payload"]["taxonomy"] is None
    assert asset_row["dimensions"] == {"asset_grounding": "fail"}
    assert asset_row["payload"]["expected"]["assets"] == [{"symbol": "MU", "market_type": "equity", "role": "primary"}]


def test_a_why_support_failure_without_supervision_is_stored_and_flagged_pending(conn) -> None:
    """#651 §7.2: refusing it would lose the defect; accepting it silently would teach "change something".

    A reviewer who can say the why sentence is wrong but not yet say which facts a correct one keeps has
    recorded a real observation, and the desk keeps it. What it cannot be is explanation training data,
    because "wrong" with no "and the answer is X" scores a rewrite into a different wrong sentence exactly
    as highly as a repair. `explanation_supervision` says which of the two this row is, in the payload,
    where the freeze and the readiness report both read it.
    """

    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]

    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            EventRubricSubmission(
                dimensions={"why_support": "fail", "factual_fidelity": "pass"},
                evidence_refs=["source:sentence:1"],
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    payload = conn.execute(
        "SELECT payload FROM news_review_records_v1 WHERE review_id = %s",
        (receipt["receipt"]["review_id"],),
    ).fetchone()["payload"]
    assert payload["explanation"] is None
    assert payload["explanation_supervision"] == "pending"


def test_a_source_span_the_frozen_evidence_does_not_contain_is_refused(conn) -> None:
    """A citation nobody can follow is worse than no citation, because it is accepted release evidence.

    The span is checked against the snapshot this task froze rather than against today's Event, so a
    later evidence version can neither ground nor unground a quotation somebody already accepted.
    """

    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])

    with (
        pytest.raises(ValueError, match="news_review_explanation_source_span_not_in_evidence"),
        repositories_for_connection(conn).transaction(),
    ):
        desk.submit(
            ref,
            EventRubricSubmission(
                dimensions={"why_support": "fail"},
                evidence_refs=["source:sentence:1"],
                explanation=ExplanationCorrectionV1(source_spans=["Micron cancels the fab entirely"]),
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert conn.execute("SELECT count(*) AS n FROM news_reviews WHERE event_id = %s", (event_id,)).fetchone()["n"] == 0

    # Whitespace is collapsed on both sides, because a reviewer copying from a rendered card picks up
    # line breaks the stored text does not have. Nothing else about the excerpt is normalized.
    with repositories_for_connection(conn).transaction():
        desk.submit(
            ref,
            EventRubricSubmission(
                dimensions={"why_support": "fail"},
                evidence_refs=["source:sentence:1"],
                explanation=ExplanationCorrectionV1(source_spans=["Micron says   DRAM contract\n prices rose"]),
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    assert conn.execute("SELECT count(*) AS n FROM news_reviews WHERE event_id = %s", (event_id,)).fetchone()["n"] == 2


def test_the_rubric_contract_offers_every_dimension_and_requires_none(conn) -> None:
    """What the desk tells a reviewer they must answer, which under v8 is only that they answer something."""

    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]

    rubric = desk.evidence(TaskRef(task_id=task["task_id"], task_version=task["task_version"]), principal=PRINCIPAL)[
        "rubric"
    ]

    assert rubric["rubric_version"] == "news_review_v8"
    assert rubric["required_dimensions"] == []
    assert rubric["required_fields"] == ["dimensions"]
    assert rubric["taxonomy"]["optional"] is True
    assert "taxonomy_source_authority" not in rubric["dimensions"]
    assert rubric["explanation"]["applies_to"] == [
        "factual_fidelity",
        "headline_fidelity",
        "why_support",
        "why_value",
    ]

    with pytest.raises(ValueError, match="news_review_dimensions_required"):
        EventRubricSubmission(dimensions={})
    with pytest.raises(ValueError, match="news_review_taxonomy_required_for_dimension"):
        EventRubricSubmission(dimensions={"taxonomy_event_family": "pass"})
    assert EventRubricSubmission(dimensions={"direction": "pass"}, taxonomy=MODEL_TAXONOMY).taxonomy is not None
    with pytest.raises(ValueError, match="news_review_explanation_requires_card_dimension"):
        EventRubricSubmission(
            dimensions={"direction": "pass"},
            explanation=ExplanationCorrectionV1(key_facts=["一个事实"]),
        )
