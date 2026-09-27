from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_legacy import (
    LEGACY_JUDGMENT_CONTRACT_VERSION,
    LEGACY_PROGRAM_VERSION,
    LEGACY_TRIAGE_POLICY_VERSION,
    legacy_judgment,
    legacy_taxonomy,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.review.desk import (
    DeskQuery,
    EventRubricSubmission,
    ExpectedCorrection,
    ExplanationCorrectionV1,
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
        assert repos.news.insert_verdict(
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

    `first_bad_owner` is the operator's own attribution. It is deliberately not defaulted: a rubric that
    leaves it unset is exactly the shape ReviewDesk derives an owner for.

    `fact_kind="fail"` is the *typed* failure, with a stated correct value; `why="fail"` is a copy
    complaint with no such value.

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
    }
    if fact_kind is not None:
        dimensions["fact_kind"] = fact_kind
    failed = why == "fail" or fact_kind == "fail"
    return EventRubricSubmission(
        should_push=should_push,  # type: ignore[arg-type]
        dimensions=dimensions,
        novelty={"judgment": "new_fact"},
        first_bad_owner=first_bad_owner,  # type: ignore[arg-type]
        expected=ExpectedCorrection(fact_kind="statement") if fact_kind == "fail" else None,
        evidence_refs=["source:sentence:1", "output:why"] if failed else [],
        expected_correction="Do not claim priced-in without source evidence." if failed else "",
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
    assert evidence["agent"]["cohort"] == f"{LEGACY_PROGRAM_VERSION}/{LEGACY_TRIAGE_POLICY_VERSION}/test-model"
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
    assert evidence["duplicate_hints"][0]["selection_reason"] == "same_storyline_within_24h_title_similarity"
    assert (
        conn.execute("SELECT count(*) AS n FROM news_reviews WHERE event_id IN (%s, %s)", (first, second)).fetchone()[
            "n"
        ]
        == 0
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
    window_start = APPOINTED_AT_MS
    review_now = window_start + 3_600_000
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (window_start + 1_000, first_event),
    )
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (window_start + 2_000, second_event),
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
    assert market["reaction"]["meta"]["cohort"] == f"{LEGACY_PROGRAM_VERSION}/{LEGACY_TRIAGE_POLICY_VERSION}/test-model"
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
    # The release-eligible counter still sees every stratum, including the one the ratios exclude.
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
    window_start = APPOINTED_AT_MS
    queue_now = window_start + 3_600_000
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
               '{LEGACY_TRIAGE_POLICY_VERSION}'::text AS policy_version,
               'model'::text AS model,
               NULL::text AS delivery_state,
               NULL::jsonb AS delivery_card,
               NULL::bigint AS settled_at_ms,
               NULL::text AS delivery_error_code,
               NULL::integer AS max_abs_return_1h_bps,
               '{LEGACY_PROGRAM_VERSION}'::text AS program_version,
               repeat('b', 64) AS program_sha256,
               jsonb_build_object('editorial_origin', 'model') AS model_editorial,
               '{LEGACY_JUDGMENT_CONTRACT_VERSION}'::text AS judgment_contract_version,
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
    window_start = APPOINTED_AT_MS
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
        occurred_at_ms=max(window_start, db_now - 10_000),
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


def test_a_review_may_answer_the_explanation_alone_and_nothing_else(conn) -> None:
    """#651 §7.2: the shape a reviewer submits when the card's *why* is the only thing they judged.

    Under v6 this submission was impossible. A reviewer who had read the evidence and concluded the why
    sentence is unsupported had to also state four taxonomy axes, a novelty judgment and a push verdict
    before the desk would take it, and all three of those invented answers then counted as accepted truth.
    Here nothing is stated but the copy verdict and the supervision behind it, the row persists, and the
    absent answers stay absent in the payload.
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


def test_an_asset_only_review_states_nothing_it_did_not_judge_and_no_taxonomy(conn) -> None:
    """A partial shape, and the guarantee that it fabricates no other answer -- a taxonomy least of all."""

    asset_event = _open_event(conn, hit_id=112102, title="Micron names the fab the capacity expansion lands in")
    desk = ReviewDesk(conn, now_ms=NOW)

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

    asset_row = conn.execute(
        "SELECT review_id, should_push, dimensions, novelty, payload FROM news_review_records_v1 WHERE review_id = %s",
        (asset_receipt["receipt"]["review_id"],),
    ).fetchone()
    # The `news_review_v8` row contract still names the retired taxonomy keys; every new row states none.
    assert asset_row["payload"]["taxonomy"] is None
    assert asset_row["payload"]["taxonomy_review"]["label_source"] == "human"
    assert asset_row["payload"]["taxonomy_review"]["draft_taxonomy"] is None
    assert asset_row["dimensions"] == {"asset_grounding": "fail"}
    assert asset_row["should_push"] is None and asset_row["novelty"] == {}
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
    assert "taxonomy" not in rubric
    assert not [dimension for dimension in rubric["dimensions"] if dimension.startswith("taxonomy_")]
    assert rubric["explanation"]["applies_to"] == [
        "factual_fidelity",
        "headline_fidelity",
        "why_support",
        "why_value",
    ]

    with pytest.raises(ValueError, match="news_review_dimensions_required"):
        EventRubricSubmission(dimensions={})
    # #706: there is no taxonomy to state, as a dimension or as a block.
    with pytest.raises(ValueError, match="news_review_dimension_unknown:taxonomy_event_family"):
        EventRubricSubmission(dimensions={"taxonomy_event_family": "pass"})
    with pytest.raises(ValueError):
        EventRubricSubmission.model_validate({"dimensions": {"direction": "pass"}, "taxonomy": {}})
    with pytest.raises(ValueError, match="news_review_explanation_requires_card_dimension"):
        EventRubricSubmission(
            dimensions={"direction": "pass"},
            explanation=ExplanationCorrectionV1(key_facts=["一个事实"]),
        )
