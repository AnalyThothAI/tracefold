"""What a task-level review freezes into, against real PostgreSQL (#651 §7.2, §9).

The review desk suite proves a partial submission is stored. This one proves the half that decides
whether storing it was worth anything: a corpus made of evidence and accepted labels, where each case
says which questions its reviewer answered and no target reads an answer nobody wrote.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.integration.test_news_candidate_evaluator import (
    NOW,
    ReviewDesk,
    _arm,
    _datasets,
    _open_event,
)
from tests.integration.test_news_candidate_evaluator import conn as conn  # noqa: PLC0414 - the fixture
from tests.integration.test_news_review_desk import MODEL_TAXONOMY, PRINCIPAL, _rubric
from tests.support.news_judgment import news_taxonomy
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import ClosedWindow
from tracefold.news.learning.dataset import DatasetSpec
from tracefold.news.learning.metric import CandidatePrediction, accepted_review_metric, build_compile_example
from tracefold.news.learning.objective import (
    DevelopmentEpisode,
    build_gepa_objective_plan,
    build_readiness_report,
)
from tracefold.news.review.desk import (
    REVIEW_TASK_VERSION,
    DeskQuery,
    EventRubricSubmission,
    ExpectedCorrection,
    ExplanationCorrectionV1,
    TaskRef,
)

pytestmark = pytest.mark.integration

_WINDOW = ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW)
# Three unrelated facts, not three phrasings of one. The Deduper joins near-identical titles into a single
# Event, which would give a corpus meant to hold three connected fact clusters exactly one.
_DISTINCT_TITLES = (
    "Micron says DRAM contract prices rose again in August",
    "Kraken lists a new perpetual market today",
    "The central bank leaves its policy rate unchanged",
)


def _submit(conn, event_id: str, submission: EventRubricSubmission) -> str:
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return str(receipt["receipt"]["review_id"])


def _freeze(conn):
    return asyncio.run(_datasets(conn, _arm()).freeze_dataset(DatasetSpec(role="development", window=_WINDOW)))


def _targets_by_event(conn, manifest) -> dict[str, tuple[str, ...]]:
    """Each frozen case's `applicable_targets`, keyed by the Event a reader would recognize it as."""

    return {str(case.event_id): tuple(case.applicable_targets) for case in manifest.cases}


def test_an_explanation_only_review_freezes_as_explanation_evidence_and_carries_its_key_facts(conn) -> None:
    """The whole point of the task-level rubric, end to end.

    Under v6 this review could not be submitted at all, so a reviewer who had read the evidence and found
    the why sentence unsupported had to invent a taxonomy, a novelty judgment and a push verdict first.
    Here nothing is invented: the case is explanation evidence and nothing else, and the `key_facts` the
    reviewer wrote reach the ruler that scores the card -- which is the first thing a `why_support`
    failure has ever had that a metric can check.
    """

    event_id = _open_event(conn)
    _submit(
        conn,
        event_id,
        EventRubricSubmission(
            dimensions={"why_support": "fail"},
            evidence_refs=["source:sentence:1"],
            explanation=ExplanationCorrectionV1(
                source_spans=["Micron says DRAM contract prices rose again in August"],
                key_facts=["DRAM 合约价 8 月再次上涨"],
                forbidden_claims=["涨幅已被市场完全定价"],
                error_types=["unsupported_cause"],
            ),
        ),
    )

    manifest = _freeze(conn)

    assert _targets_by_event(conn, manifest)[event_id] == ("explanation",)
    assert manifest.targets == ("explanation",)
    assert manifest.counts["targets"] == {
        "classification": {"case_n": 0, "cluster_n": 0},
        "understanding": {"case_n": 0, "cluster_n": 0},
        "explanation": {"case_n": 1, "cluster_n": 1},
    }

    export = _datasets(conn, _arm()).development_compile_export(manifest.artifact_sha)
    episode = DevelopmentEpisode.model_validate(export.episodes[0])
    assert episode.accepted_review["taxonomy"] is None
    assert episode.accepted_review["explanation_supervision"] == "present"
    assert episode.accepted_review["explanation"]["key_facts"] == ["DRAM 合约价 8 月再次上涨"]

    from tracefold.news.learning.optimizer import _explanation_example

    example = _explanation_example(episode)
    assert tuple(example.gold_key_facts) == ("DRAM 合约价 8 月再次上涨",)


def test_a_taxonomy_only_and_an_asset_only_review_each_reach_one_target_and_fabricate_no_other(conn) -> None:
    """Two partial reviews, two disjoint answers, and nothing invented in between.

    A corpus that handed every target every case would let a `classification` run train on a case whose
    reviewer never looked at the taxonomy, scoring the candidate against a label nobody wrote. The
    asset-only review states an instrument and no taxonomy; the taxonomy-only review states four axes and
    no verdict on anything else. Neither becomes evidence for the other's question.
    """

    taxonomy_event = _open_event(conn, hit_id=113001, title="Regulator publishes the final custody rule")
    asset_event = _open_event(conn, hit_id=113002, title="Micron names the fab the expansion lands in")

    _submit(
        conn,
        taxonomy_event,
        EventRubricSubmission(
            dimensions={
                "taxonomy_subject_codes": "pass",
                "taxonomy_event_family": "pass",
                "taxonomy_change_state": "pass",
                "taxonomy_assertion_status": "pass",
            },
            taxonomy=MODEL_TAXONOMY,
        ),
    )
    _submit(
        conn,
        asset_event,
        EventRubricSubmission(
            dimensions={"asset_grounding": "fail"},
            evidence_refs=["source:sentence:1"],
            expected=ExpectedCorrection(assets=[{"symbol": "MU", "market_type": "equity", "role": "primary"}]),
        ),
    )

    manifest = _freeze(conn)
    by_event = _targets_by_event(conn, manifest)

    assert by_event[taxonomy_event] == ("classification",)
    assert by_event[asset_event] == ("understanding",)
    assert manifest.counts["targets"]["classification"]["case_n"] == 1
    assert manifest.counts["targets"]["understanding"]["case_n"] == 1
    assert manifest.counts["targets"]["explanation"]["case_n"] == 0

    episodes = tuple(
        DevelopmentEpisode.model_validate(episode)
        for episode in _datasets(conn, _arm()).development_compile_export(manifest.artifact_sha).episodes
    )
    for target, owner in (("classification", taxonomy_event), ("understanding", asset_event)):
        plan = build_gepa_objective_plan(episodes, target)  # type: ignore[arg-type]
        included = {case.case_id for case in plan.cases if case.disposition == "included"}
        by_case = {episode.case_id: episode for episode in episodes}
        assert {by_case[case_id].provenance["bundle_sha"] for case_id in included} == {_arm().bundle_sha}
        assert len(included) == 1
        assert plan.exclusion_reasons == {"target_not_labelled_by_review": 1}
        # The one included case is the one whose reviewer answered this target's question.
        (case_id,) = included
        assert by_case[case_id].context.evidence.title.startswith("Regulator" if owner == taxonomy_event else "Micron")


def test_a_corpus_spans_arms_and_reaches_back_past_the_epoch_the_deployment_opened(conn) -> None:
    """#651 §9: eligibility is a property of the evidence, and the arm is provenance on the case.

    Both Events below are real, delivered and reviewed. One was answered by a bundle that is no longer
    appointed and one opened before the running deployment wrote its epoch row -- the two conditions that
    used to discard a review outright, which meant every deployment threw away the review of the cards a
    reader had just been sent. They are both in the corpus, and each case says which arm answered it.
    """

    other_bundle = "c" * 64
    current_event = _open_event(conn, hit_id=113101, title="Micron confirms the August contract price move")
    other_arm_event = _open_event(
        conn,
        hit_id=113102,
        title="Kraken lists a new perpetual market today",
        bundle_sha=other_bundle,
    )
    for event_id in (current_event, other_arm_event):
        _submit(conn, event_id, _rubric())
    epoch_start = int(
        conn.execute("SELECT min(starts_at_ms) AS starts_at_ms FROM news_learning_epochs").fetchone()["starts_at_ms"]
    )
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
        (epoch_start - 1, other_arm_event),
    )

    manifest = asyncio.run(
        _datasets(conn, _arm()).freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=min(epoch_start - 2, _WINDOW.from_ms), to_ms=NOW),
            )
        )
    )

    provenance = {str(case.event_id): case.provenance for case in manifest.cases}
    assert set(provenance) == {current_event, other_arm_event}
    assert provenance[current_event].bundle_sha == _arm().bundle_sha
    assert provenance[other_arm_event].bundle_sha == other_bundle
    assert manifest.counts["eligibility"]["unit"] == "evidence_snapshot_and_accepted_review"
    assert sorted(manifest.counts["eligibility"]["case_arms"]) == sorted({_arm().bundle_sha, other_bundle})


def test_a_why_support_failure_without_supervision_is_frozen_counted_and_not_trained_on(conn) -> None:
    """Visible, not trainable, and the difference is a number an operator can act on.

    "Wrong" with no statement of what a correct card keeps scores a rewrite into a different wrong
    sentence exactly as highly as a repair, so the case cannot enter the explanation train split. Dropping
    it silently would hide a real defect the reviewer found, so it is frozen and counted instead.
    """

    supervised = _open_event(conn, hit_id=113201, title="Micron says DRAM contract prices rose again in August")
    pending = _open_event(conn, hit_id=113202, title="Micron raises the capacity plan for the next quarter")
    _submit(
        conn,
        supervised,
        EventRubricSubmission(
            dimensions={"why_support": "fail"},
            evidence_refs=["source:sentence:1"],
            explanation=ExplanationCorrectionV1(key_facts=["DRAM 合约价 8 月再次上涨"]),
        ),
    )
    _submit(
        conn,
        pending,
        EventRubricSubmission(dimensions={"why_support": "fail"}, evidence_refs=["source:sentence:1"]),
    )

    manifest = _freeze(conn)

    assert _targets_by_event(conn, manifest)[pending] == ("explanation",)
    assert manifest.counts["explanation_supervision_pending_n"] == 1

    episodes = tuple(
        DevelopmentEpisode.model_validate(episode)
        for episode in _datasets(conn, _arm()).development_compile_export(manifest.artifact_sha).episodes
    )
    plan = build_gepa_objective_plan(episodes, "explanation")
    assert plan.exclusion_reasons == {"explanation_supervision_pending": 1}
    trained = {episode.case_id for episode in (*plan.train_episodes, *plan.development_selection_episodes)}
    by_case = {episode.case_id: episode for episode in episodes}
    assert all(by_case[case_id].accepted_review["explanation_supervision"] == "present" for case_id in trained)


def _accept_v6_review(conn, event_id: str) -> None:
    """Append the accepted `news_review_v6` pair production held before the cut, exactly as it held it.

    Written through raw SQL because `ReviewDesk` only writes the current contract, which is the property
    under test: a v6 row is real, readable audit history that no new corpus may read. The task id is
    recomputed here under the v6 rubric for the same reason the desk recomputes it under v7 -- the rubric
    version is inside the task identity, so a v6 row names a task id no v7 desk would ever mint.

    The reader contract is spelled out rather than imported: `news_current_review_valid` admits the two
    *pairs* (v6 with `reader_contract_v2`, v7 with `reader_contract_v3`), and a v6 row that named the
    current contract would be a row production never wrote.
    """

    v6_reader_contract = "reader_contract_v2"
    v6_reader_contract_sha256 = "bb7f436d232b02446c4f0f17c7b0b4f56c421aa4daf1a3869c5baa9b89970082"

    source = conn.execute(
        "SELECT evidence_version, trace #>> '{agent_assignment,bundle_sha}' AS bundle_sha "
        "FROM news_review_task_source_v1 WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    evidence_version = int(source["evidence_version"])
    identity = canonical_sha(
        {
            "task": REVIEW_TASK_VERSION,
            "event_id": event_id,
            "evidence_version": evidence_version,
            "rubric": "news_review_v6",
            "reader_contract": v6_reader_contract,
            "reader_contract_sha256": v6_reader_contract_sha256,
            "agent_cohort_sha256": str(source["bundle_sha"]),
        }
    )
    task_id = f"evt.{event_id}.{evidence_version}.{identity[:16]}"
    # Everything v6 made mandatory, including the fifth taxonomy dimension and the `timeliness` a
    # `must_push` verdict had to carry. That completeness is exactly what makes the row unreadable now:
    # a v7 corpus cannot tell a stated `pass` from one the contract extracted.
    dimensions = {
        "factual_fidelity": "pass",
        "timeliness": "pass",
        "taxonomy_subject_codes": "pass",
        "taxonomy_event_family": "pass",
        "taxonomy_change_state": "pass",
        "taxonomy_source_authority": "pass",
        "taxonomy_assertion_status": "pass",
    }
    novelty = {"judgment": "new_fact", "duplicate_of": ""}
    # A v6 row carried `source_authority` inside the taxonomy; since #651 §5.3 the current
    # `NewsTaxonomyV1` has no such field, so the historical shape is written as the dict production held.
    taxonomy = news_taxonomy(
        event_family="regulatory_legal",
        change_state="reported",
        assertion_status="claimed",
    ).model_dump(mode="json") | {"source_authority": "reputable_secondary"}
    payload = {
        "kind": "event_rubric",
        "should_push": "must_push",
        "dimensions": dimensions,
        "novelty": novelty,
        "first_bad_owner": None,
        "evidence_refs": [],
        "expected": None,
        "taxonomy": taxonomy,
        "taxonomy_review": {
            "label_source": "human",
            "draft_author": "",
            "review_role": "primary",
            "adjudicates_review_id": "",
            "draft_taxonomy": None,
        },
        "expected_correction": "",
        "note": "",
    }
    selection = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0][
        "selection"
    ]
    judgment_id, acceptance_id = canonical_sha({"v6": task_id}), canonical_sha({"v6-acceptance": task_id})
    with repositories_for_connection(conn).transaction():
        conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, review_kind, subject_kind, task_id, task_version, event_id, evidence_version,
              rubric_version, reader_contract_version, reviewer, should_push, dimensions, novelty,
              first_bad_owner, evidence_refs, expected_correction, note, selection, payload,
              release_eligible, created_at_ms
            ) VALUES (
              %s, 'judgment', 'event', %s, %s, %s, %s, 'news_review_v6', 'reader_contract_v2',
              'historic-reviewer', 'must_push', %s::jsonb, %s::jsonb, 'unknown', '[]'::jsonb, '', '',
              %s::jsonb, %s::jsonb, true, %s
            )
            """,
            (
                judgment_id,
                task_id,
                identity,
                event_id,
                evidence_version,
                json.dumps(dimensions),
                json.dumps(novelty),
                json.dumps(selection),
                json.dumps(payload),
                NOW - 60_000,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, review_kind, subject_kind, task_id, task_version, event_id, evidence_version,
              rubric_version, reader_contract_version, reviewer, accepts_review_id, release_eligible,
              created_at_ms
            ) VALUES (
              %s, 'acceptance', 'event', %s, %s, %s, %s, 'news_review_v6', 'reader_contract_v2',
              'historic-reviewer', %s, true, %s
            )
            """,
            (acceptance_id, task_id, identity, event_id, evidence_version, judgment_id, NOW - 59_000),
        )


def test_a_v6_review_is_readable_history_counted_as_ineligible_and_never_frozen(conn) -> None:
    """A v6 row means "every dimension below was answered"; a v7 row does not.

    Mixing the two contracts would let an absent answer read as a stated one, which is the fabrication
    the task-level rubric exists to stop. The row is neither deleted nor hidden: it stays readable
    through `news_review_records_v1` and the corpus counts it, because "nobody reviewed this window" and
    "every review here predates the current rubric" have different operator actions behind them.
    """

    historic = _open_event(conn, hit_id=113301, title="Micron reported the August contract prices last month")
    current = _open_event(conn, hit_id=113302, title="Micron confirms the September contract price move")
    _accept_v6_review(conn, historic)
    _submit(conn, current, _rubric())

    assert (
        conn.execute(
            "SELECT count(*) AS n FROM news_review_records_v1 WHERE event_id = %s AND rubric_version = %s",
            (historic, "news_review_v6"),
        ).fetchone()["n"]
        == 2
    )

    manifest = _freeze(conn)

    assert set(_targets_by_event(conn, manifest)) == {current}
    assert manifest.counts["rubric_ineligible_n"] == 1


def test_readiness_is_ready_for_the_target_that_was_reviewed_and_train_empty_for_the_one_that_was_not(
    conn,
) -> None:
    """#651 §9: "is this corpus ready" has no answer until somebody says ready for what.

    Three reviewed explanation cases and no taxonomy Gold make an excellent explanation corpus and a
    useless classification one. The old report called that situation not-ready on a corpus-wide quota and
    never said which kind of evidence was missing; this one answers for the target it was asked about and
    publishes the others' counts beside it, and its blocker vocabulary names a structural fact about the
    split rather than a threshold.
    """

    for index in range(3):
        event_id = _open_event(
            conn,
            hit_id=113401 + index,
            title=_DISTINCT_TITLES[index],
        )
        _submit(
            conn,
            event_id,
            EventRubricSubmission(
                dimensions={"why_support": "fail"},
                evidence_refs=["source:sentence:1"],
                explanation=ExplanationCorrectionV1(key_facts=[f"事实 {index}"]),
            ),
        )

    manifest = _freeze(conn)
    export = _datasets(conn, _arm()).development_compile_export(manifest.artifact_sha)
    episodes = tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes)
    coverage = dict(manifest.counts)

    explanation = build_readiness_report(
        build_gepa_objective_plan(episodes, "explanation"),
        episodes=episodes,
        identity={"development_dataset_sha": manifest.artifact_sha},
        coverage=coverage,
        target="explanation",
    )
    classification = build_readiness_report(
        build_gepa_objective_plan(episodes, "classification"),
        episodes=episodes,
        identity={"development_dataset_sha": manifest.artifact_sha},
        coverage=coverage,
        target="classification",
    )

    assert explanation["objective"]["compilable"] is True
    assert explanation["objective"]["blockers"] == []
    assert classification["objective"]["compilable"] is False
    assert classification["objective"]["blockers"] == ["train_empty", "selection_empty"]
    assert not any("development_" in blocker for blocker in classification["objective"]["blockers"])

    by_target = classification["targets"]["by_target"]
    assert by_target["classification"]["planned"] is True
    assert by_target["classification"]["case_n"] == 0
    assert by_target["explanation"]["planned"] is False
    assert by_target["explanation"]["case_n"] == 3
    assert "development_profile" not in classification


def test_the_release_metric_does_not_charge_a_candidate_for_a_taxonomy_nobody_stated(conn) -> None:
    """A reviewer's silence is not a schema failure, and it must not score like one.

    `accepted_review_metric` used to zero the whole case under a `schema_invalid` gate whenever the
    accepted review carried no taxonomy. Under v6 that could only mean a corrupt corpus, because every
    submission carried four axes. Under v7 it is the ordinary shape of a reviewer who judged the copy and
    nothing else, and zeroing the case would blame the model for a question nobody asked -- with feedback
    telling it to return axes the corpus has no answer for.
    """

    event_id = _open_event(conn)
    _submit(
        conn,
        event_id,
        EventRubricSubmission(
            dimensions={"why_support": "fail", "factual_fidelity": "pass"},
            evidence_refs=["source:sentence:1"],
            explanation=ExplanationCorrectionV1(key_facts=["DRAM \u5408\u7ea6\u4ef7 8 \u6708\u518d\u6b21\u4e0a\u6da8"]),
        ),
    )
    manifest = _freeze(conn)
    export = _datasets(conn, _arm()).development_compile_export(manifest.artifact_sha)
    episode = DevelopmentEpisode.model_validate(export.episodes[0])

    example = build_compile_example(episode)
    production = example.production_judgment
    assert production is not None
    outcome = accepted_review_metric(
        example,
        CandidatePrediction(verdict=production["verdict"], editorial=production["editorial"]),
    )

    assert outcome.hard_gate == ""
    assert not any(dimension.startswith("taxonomy_") for dimension, _result in outcome.dimension_outcomes)
    assert "Taxonomy" not in " ".join(outcome.feedback)
    assert outcome.component_denominators["semantics_novelty"] == 0
