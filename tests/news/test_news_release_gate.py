"""The release gate (#81): sequential replay, the two failure directions, and the reviewed boundary fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.eval.harness import (
    EXPECTATIONS,
    candidate_policy,
    freeze_corpus,
    load_corpus,
    replay_corpus,
    validate_candidate,
)
from tracefold.news.triage_rules import DecidePolicy

NOW_MS = 1_800_000_000_000
HOUR_MS = 3600_000


def _verdict(headline: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "regulation",
        "assets": [],
        "direction": "bearish",
        "scope": "macro",
        "magnitude": 2,
        "actionable": True,
        "confidence": 0.8,
        "decision": "push",
        "headline_zh": headline,
    }
    base.update(overrides)
    return base


def _case(index: int, headline: str, *, key: str = "theme:mideast_energy", **overrides: Any) -> dict[str, Any]:
    return {
        "event_id": f"ev-{index:03d}",
        "at_ms": NOW_MS + index * 60_000,
        "storyline_key": key,
        "verdict": _verdict(headline, **overrides),
        "gate": {"grounded_assets": [], "provider_score": 70.0, "priority": "normal", "admission": "candidate"},
        "told": [],
        "degraded": False,
        "subject": headline,
        "expect": "may_push",
        "label": "",
        "stored_final": "push",
    }


def _payload(cases: list[dict[str, Any]]) -> dict[str, Any]:
    from tracefold.news.eval.harness import CORPUS_VERSION, _sha256

    payload: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "created_at_ms": NOW_MS,
        "from_ms": min(c["at_ms"] for c in cases),
        "to_ms": max(c["at_ms"] for c in cases),
        "watchlist_symbols": [],
        "prompt_versions": {"news_triage_prompt_v8": len(cases)},
        "skipped_unreplayable_verdicts": 0,
        "cases": cases,
    }
    payload["sha256"] = _sha256(payload)
    return payload


# A storyline that floods: four cards saying the same thing, then four distinct facts.
_REPEATS = [
    "特朗普宣布对伊朗实施史上最强经济行动",
    "特朗普宣布对伊朗实施史上最强的经济行动",
    "特朗普宣布对伊朗实施史上最强经济行动，威胁第三国",
    "特朗普宣布对伊朗实施史上最强经济制裁行动",
]
_DISTINCT = [
    "俄军二十分钟内向基辅发射十五枚导弹",
    "美财政部将长债回购上限翻倍至四十亿美元",
    "韩国股市盘初跌超百分之六触发熔断",
    "以色列战机夜间空袭黎巴嫩南部村庄",
]


def _flood_corpus() -> Any:
    cases = [_case(i, headline) for i, headline in enumerate(_REPEATS + _DISTINCT)]
    return load_corpus(_payload(cases))


def test_corpus_hash_is_verified_on_load() -> None:
    payload = _payload([_case(0, _DISTINCT[0])])
    assert load_corpus(payload).cases[0].event_id == "ev-000"
    tampered = {**payload, "cases": [_case(0, "改过的标题")]}
    with pytest.raises(ValueError, match="news_corpus_sha_mismatch"):
        load_corpus(tampered)


def test_expectations_overlay_is_validated_and_applied() -> None:
    payload = _payload([_case(0, _DISTINCT[0])])
    assert load_corpus(payload, expectations={"ev-000": "must_push"}).boundary()[0].event_id == "ev-000"
    with pytest.raises(ValueError, match="news_corpus_expectation_invalid"):
        load_corpus(payload, expectations={"ev-000": "should_probably_push"})
    assert "must_push" in EXPECTATIONS


def test_replay_releases_distinct_cards_and_withholds_repeats() -> None:
    """The content throttle in one picture: the same fact four times costs the reader one card, and four
    different facts cost four — the count cap could not tell those two situations apart."""

    report = replay_corpus(_flood_corpus(), DecidePolicy(theme_cap_4h=1), hourly_cap=30)
    delivered = set(report.delivered)
    assert {f"ev-{i:03d}" for i in range(4, 8)} <= delivered  # every distinct fact
    assert len(delivered & {f"ev-{i:03d}" for i in range(4)}) == 1  # one card out of four repeats
    assert all(key.endswith(":seen") for key in report.throttled_by), report.throttled_by


def test_replay_is_sequential_so_a_release_changes_what_later_cards_see() -> None:
    """A first-order replay reuses the stored window; this one rebuilds it, so the second copy of a fact is
    measured against the first copy *this arm* delivered rather than against history."""

    cases = [_case(0, _REPEATS[0]), _case(1, _REPEATS[1])]
    report = replay_corpus(load_corpus(_payload(cases)), DecidePolicy(theme_cap_4h=1), hourly_cap=30)
    assert report.delivered == ("ev-000",)
    assert report.withheld_by_rule == ("ev-001",)


def test_gate_blocks_a_candidate_that_buys_quiet_with_misses() -> None:
    corpus = _flood_corpus()
    live = DecidePolicy(theme_cap_4h=1)
    decision = validate_candidate(
        corpus,
        stable=live,
        candidate=candidate_policy(live, {"similarity_max": 0.0}),  # the pre-v5 count cap
        hourly_cap=30,
    )
    assert not decision.accepted
    assert "missed_facts_not_worse" in decision.failed_checks
    assert decision.evidence["delta"]["missed_facts"] > 0


def test_gate_blocks_a_candidate_that_buys_recall_with_repetition() -> None:
    corpus = _flood_corpus()
    live = DecidePolicy(theme_cap_4h=1)
    decision = validate_candidate(
        corpus, stable=live, candidate=candidate_policy(live, {"storyline_throttle": False}), hourly_cap=30
    )
    assert not decision.accepted
    assert "strong_duplicates_not_worse" in decision.failed_checks


def test_gate_accepts_an_unchanged_candidate_and_seals_its_evidence() -> None:
    corpus = _flood_corpus()
    live = DecidePolicy(theme_cap_4h=1)
    decision = validate_candidate(corpus, stable=live, candidate=live, hourly_cap=30)
    assert decision.accepted and decision.failed_checks == ()
    evidence = dict(decision.evidence)
    assert evidence["corpus"]["sha256"] == corpus.sha256
    assert evidence["candidate"]["policy"] == live.as_dict()
    assert len(evidence["sha256"]) == 64
    assert evidence["duplicate_trade_ratio"] > 0


def test_gate_blocks_losing_a_must_push_case_and_keeps_the_open_debt_visible() -> None:
    corpus = load_corpus(
        _payload([_case(i, headline) for i, headline in enumerate(_REPEATS + _DISTINCT)]),
        # ev-004 the stable arm delivers; ev-001 neither arm can deliver (it is a repeat) — that one stays open.
        expectations={"ev-004": "must_push", "ev-001": "must_push"},
    )
    live = DecidePolicy(theme_cap_4h=1)
    passing = validate_candidate(corpus, stable=live, candidate=live, hourly_cap=30)
    assert passing.accepted
    assert passing.evidence["boundary"] == {
        "cases": 2,
        "stable_delivered": 1,
        "candidate_delivered": 1,
        "recovered": [],
        "open": [_REPEATS[1]],
        "critical_misses": [],
    }
    losing = validate_candidate(
        corpus, stable=live, candidate=candidate_policy(live, {"similarity_max": 0.0}), hourly_cap=30
    )
    assert not losing.accepted and "no_critical_miss" in losing.failed_checks


def test_candidate_policy_rejects_unknown_fields_and_coerces_strings() -> None:
    live = DecidePolicy()
    assert candidate_policy(live, {"similarity_max": "0.4"}).similarity_max == 0.4
    assert candidate_policy(live, {"storyline_throttle": "false"}).storyline_throttle is False
    assert candidate_policy(live, {"distinct_hard_cap_4h": "24"}).distinct_hard_cap_4h == 24
    with pytest.raises(ValueError, match="news_policy_unknown_field:novel_min_magnitude"):
        candidate_policy(live, {"novel_min_magnitude": 2})


def test_freeze_corpus_skips_unreplayable_verdicts_instead_of_shrinking_silently() -> None:
    rows = [
        {
            "event_id": "ev-ok",
            "created_at_ms": NOW_MS,
            "final_decision": "push",
            "degraded": False,
            "verdict": _verdict("一条可以重放的判决"),
            "trace": {"storyline_key": "theme:rates", "told": []},
            "storyline_key": "theme:rates",
            "leader_title": "A replayable verdict",
            "grounded_assets": [],
            "priority": "normal",
            "admission": "candidate",
            "provider_score_max": 70.0,
            "prompt_version": "news_triage_prompt_v8",
            "label": "must_push",
        },
        {
            "event_id": "ev-retired",
            "created_at_ms": NOW_MS + 1,
            "final_decision": "drop",
            "degraded": False,
            "verdict": {**_verdict("旧 schema"), "rationale": "a field the schema retired"},
            "trace": {},
            "storyline_key": "theme:rates",
            "leader_title": "Retired schema",
            "grounded_assets": [],
            "priority": "normal",
            "admission": "candidate",
            "provider_score_max": 70.0,
            "prompt_version": "news_triage_prompt_v1",
            "label": None,
        },
    ]

    class _Repos:
        class conn:  # the psycopg cursor shape the eval lane reads through
            @staticmethod
            def execute(_sql: str, _params: Any) -> Any:
                return type("Cursor", (), {"fetchall": staticmethod(lambda: rows)})

    payload = freeze_corpus(_Repos(), now_ms=NOW_MS + 2, hours=24)
    assert payload["skipped_unreplayable_verdicts"] == 1
    assert [case["event_id"] for case in payload["cases"]] == ["ev-ok"]
    assert payload["cases"][0]["expect"] == "must_push"  # the label plane feeds the boundary set
    assert load_corpus(payload).sha256 == payload["sha256"]


def test_reviewed_boundary_fixture_loads_through_the_production_path() -> None:
    """The fixture is part of the trusted root, and `news validate-candidate --expectations` hands it to
    ``load_corpus`` verbatim. Passing it through a filter the CLI does not have would test nothing: the shipped
    file leads with a `_comment` block, and rejecting that made the documented command exit 2 with its own prose
    quoted back as an invalid expectation."""

    path = Path(__file__).resolve().parents[1] / "fixtures" / "news_recall_boundary_v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["_comment"], "the fixture explains what a marking means, to whoever edits it next"

    cases = [_case(i, headline) for i, headline in enumerate(_REPEATS + _DISTINCT)]
    corpus = load_corpus(_payload(cases), expectations=document)  # verbatim, comment block and all
    assert corpus.cases  # loading did not raise

    expectations = {k: v for k, v in document.items() if not k.startswith("_")}
    assert expectations, "an empty overlay makes the boundary gate vacuous"
    assert set(expectations.values()) <= EXPECTATIONS
    assert all(len(event_id) == 64 for event_id in expectations), "keys are Event ids"
    assert sum(1 for v in expectations.values() if v == "must_push") >= 10
    assert sum(1 for v in expectations.values() if v == "may_drop") >= 1


def test_peak_check_is_a_delta_so_a_strict_improvement_is_never_blocked() -> None:
    """The deployed arm already sits one card under `hourly_cap`. An absolute peak check would reject every
    candidate the moment a busy hour touches the budget — the same deadlock `no_critical_miss` avoids."""

    # `escalate` is exempt from the hourly cap in both `decide()` and the Deliverer, so a burst of m3 cards
    # breaches any budget whatever the policy says — which is exactly why the peak is not the policy's to control.
    cases = [_case(i, headline, magnitude=3) for i, headline in enumerate(_DISTINCT)]
    corpus = load_corpus(_payload(cases))
    live = DecidePolicy(theme_cap_4h=1)
    decision = validate_candidate(corpus, stable=live, candidate=live, hourly_cap=1)
    assert decision.evidence["stable"]["per_hour_peak"] > 1  # both arms are over the budget
    assert decision.checks["peak_within_reader_budget"] is True  # ...and the candidate is still judged on merit
    assert decision.accepted


def test_must_push_counts_as_a_miss_in_the_offline_evaluation_too() -> None:
    """`must_push` feeds the release gate's boundary set, but it also has to move `news eval`: a label that
    changes no metric anywhere the operator looks is a label nobody will keep writing."""

    from tracefold.news.eval.offline import _outcome

    for label in ("must_push", "missed", "good", "wrong_direction", "late"):
        assert _outcome({"label": label}) == "moved", label
    for label in ("noise", "dup"):
        assert _outcome({"label": label}) == "flat", label
    assert _outcome({"label": None}) is None
