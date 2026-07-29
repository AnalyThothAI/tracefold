from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from tests.test_macro_thesis import CUTOFF_MS, SESSION, _modules
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.evaluation import (
    MacroEvalLabelsV1,
    MacroEvalMeasurementV1,
    MacroEvalProfileRunV1,
    compare_macro_eval_profiles,
    freeze_macro_eval_manifest,
    inspect_macro_eval_readiness,
    select_macro_eval_case_seeds,
)
from tracefold.macro.thesis import compile_evidence_pack_v3
from tracefold.macro.thesis_v2 import (
    MacroResearchInputV1,
    MetricConditionCandidate,
    compile_research_input_v1,
)

_REQUIRED_DATASETS = {
    "rates_fed": "treasury.daily_nominal_curve",
    "economy_inflation": "fred.gdpc1",
    "liquidity_funding": "fred.walcl",
    "credit": "fred.bamlc0a0cm",
    "volatility": "fred.vixcls",
    "cross_asset": "yfinance.spy.intraday",
}


def _eval_session(offset: int):
    session = SESSION + timedelta(days=offset)
    cutoff_ms = CUTOFF_MS + offset * 86_400_000
    modules = deepcopy(_modules())
    for module in modules:
        module_id = module["module_id"]
        dataset_id = _REQUIRED_DATASETS[module_id]
        fact = module["evidence"]["latest_facts"][0]
        fact["dataset_id"] = dataset_id
        fact["fact_ref"] = f"fact:{module_id}:{offset}"
        module["evidence"]["dataset_states"][0]["dataset_id"] = dataset_id
        module["summary"]["top_changes"][0]["dataset_id"] = dataset_id
    pack = compile_evidence_pack_v3(
        session_date=session,
        cutoff_ms=cutoff_ms,
        sealed_at_ms=cutoff_ms + 1_000,
        modules=modules,
        prior_publication=None,
    )
    base = compile_research_input_v1(pack)
    ranks = (0.95, 0.05, 0.90, 0.10, 0.85, 0.15)
    candidates = []
    condition_ids_by_module: dict[str, list[str]] = {}
    evidence_by_module = {module.module_id: module.exact_evidence_refs[0] for module in base.modules}
    for module_id, rank in zip(MACRO_MODULE_IDS, ranks, strict=True):
        candidate_id = f"test.tail:{module_id}:{offset}"
        candidates.append(
            MetricConditionCandidate(
                candidate_id=candidate_id,
                module_id=module_id,
                dataset_id=_REQUIRED_DATASETS[module_id],
                metric="value",
                unit="percent",
                operator="gte",
                threshold=1.0,
                frozen_value=2.0,
                as_of=session.isoformat(),
                historical_percentile_rank=rank,
                quantile_window="five_years",
                sample_count=40,
                allowed_kinds=("confirmation", "weakening", "falsifier"),
                allowed_scopes=("mainline", "alternative", "tension"),
                meaning="Frozen tail predicate for corpus selection.",
                evidence_refs=(evidence_by_module[module_id],),
            )
        )
        condition_ids_by_module[module_id] = [candidate_id]
    payload = base.model_dump(mode="json")
    payload["condition_candidates"] = [candidate.model_dump(mode="json") for candidate in candidates]
    payload["allowed_condition_ids"] = [candidate.candidate_id for candidate in candidates]
    for module in payload["modules"]:
        module["condition_candidate_ids"] = condition_ids_by_module[module["module_id"]]
    research_input = MacroResearchInputV1.model_validate(payload)
    return pack, research_input


def _manifest():
    seeds = select_macro_eval_case_seeds(tuple(_eval_session(offset) for offset in range(1, 10)))
    labels = {
        seed.case_id: MacroEvalLabelsV1(
            allowed_primary_driver_predicate_ids=(),
            required_counterevidence_refs=(),
            allowed_material_assets=(),
            forbidden_factual_claims=(),
            allowed_condition_ids=(),
        )
        for seed in seeds
    }
    return freeze_macro_eval_manifest(
        seeds=seeds,
        labels=labels,
        production_model="production-model",
        research_owner="research-owner",
        signed_at_ms=CUTOFF_MS,
        signature="human-signature",
    )


def _run(*, profile: str, repeat: int, factual_errors: int = 0):
    manifest = _manifest()
    return MacroEvalProfileRunV1(
        profile=profile,
        repeat=repeat,
        model_name="production-model",
        measurements=tuple(
            MacroEvalMeasurementV1(
                case_id=case.case_id,
                factual_errors=factual_errors if index == 0 else 0,
                citation_closure_errors=0,
                condition_errors=0,
                causal_sufficient_edges=1,
                causal_edges=1,
                recalled_counterevidence=1,
                required_counterevidence=1,
                recalled_material_assets=1,
                allowed_material_assets=1,
                duplicate_claim_count=2 if profile == "baseline" else 1,
                body_characters=1_000,
                latency_ms=1_000,
                input_tokens=1_000,
                output_tokens=500,
                provider_failed=False,
                selected_material_assets=("SPY",),
            )
            for index, case in enumerate(manifest.cases)
        ),
        adjudicator="research-owner",
        signed_at_ms=CUTOFF_MS,
        signature=f"{profile}-{repeat}-signature",
    )


def test_eval_selector_builds_six_modules_three_mixed_and_three_gap_cases() -> None:
    seeds = select_macro_eval_case_seeds(tuple(_eval_session(offset) for offset in range(1, 10)))

    assert len(seeds) == 12
    assert tuple(seed.module_id for seed in seeds[:6]) == MACRO_MODULE_IDS
    assert tuple(seed.case_kind for seed in seeds[6:9]) == ("mixed",) * 3
    assert tuple(seed.derived_from for seed in seeds[9:]) == tuple(seed.case_id for seed in seeds[:3])
    assert all(seed.removed_evidence_ref for seed in seeds[9:])
    assert all(
        seed.evidence_pack.payload_hash != source.evidence_pack.payload_hash
        for seed, source in zip(seeds[9:], seeds[:3], strict=True)
    )


def test_eval_readiness_reports_exact_real_session_shortfall_without_selecting_cases() -> None:
    readiness = inspect_macro_eval_readiness(tuple(_eval_session(offset)[0] for offset in range(1, 4)))

    assert readiness.state == "insufficient_real_sessions"
    assert readiness.required_real_sessions == 9
    assert readiness.available_real_sessions == 3
    assert readiness.missing_real_sessions == 6
    assert readiness.selected_case_ids == ()
    assert readiness.reason_code == "macro_eval_real_sessions_insufficient"


def test_eval_readiness_proves_the_exact_twelve_case_selection(monkeypatch) -> None:
    sessions = tuple(_eval_session(offset) for offset in range(1, 10))
    inputs = {pack.evidence_pack_id: research_input for pack, research_input in sessions}
    monkeypatch.setattr(
        "tracefold.macro.evaluation.compile_research_input_v1",
        lambda pack: inputs.get(pack.evidence_pack_id) or compile_research_input_v1(pack),
    )

    readiness = inspect_macro_eval_readiness(tuple(pack for pack, _ in sessions))

    assert readiness.state == "ready"
    assert readiness.available_real_sessions == 9
    assert readiness.missing_real_sessions == 0
    assert len(readiness.selected_case_ids) == 12
    assert readiness.reason_code is None


def test_signed_ablation_requires_zero_candidate_errors_and_one_improvement() -> None:
    manifest = _manifest()
    evidence = compare_macro_eval_profiles(
        manifest=manifest,
        runs=(
            _run(profile="baseline", repeat=1),
            _run(profile="baseline", repeat=2),
            _run(profile="candidate", repeat=1),
            _run(profile="candidate", repeat=2),
        ),
    )

    assert evidence.eligible_for_human_cutover is True
    assert evidence.release_vetoes == ()
    assert evidence.strict_improvements == ("duplicate_claim_count",)


def test_factual_error_is_an_ablation_release_veto() -> None:
    evidence = compare_macro_eval_profiles(
        manifest=_manifest(),
        runs=(
            _run(profile="baseline", repeat=1),
            _run(profile="baseline", repeat=2),
            _run(profile="candidate", repeat=1, factual_errors=1),
            _run(profile="candidate", repeat=2),
        ),
    )

    assert evidence.eligible_for_human_cutover is False
    assert "candidate_factual_error" in evidence.release_vetoes
