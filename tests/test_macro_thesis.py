from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from unittest.mock import Mock

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage
from pydantic import ValidationError

from tracefold.app.http import routes_macro
from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    _AnalysisDraft,
    _compile_analysis_draft,
    _graph_config,
    _MacroThesisRequestBoundary,
    _module_research_view,
    _research_context,
    register_macro_thesis_harness_profile,
    require_supported_macro_thesis_model,
)
from tracefold.macro.assets import MACRO_ASSET_DATASETS
from tracefold.macro.domain import MACRO_MODULE_IDS, MACRO_MODULE_LABELS
from tracefold.macro.read_models import (
    MacroAssetHorizonPresentation,
    MacroLiveDeltaItemRead,
    MacroOutcomeAssetResultRead,
    MacroOutcomeHorizonRead,
    project_asset_presentation,
    project_claim_presentation,
    project_live_delta_for_read,
    project_module_annotations,
    project_outcome_replay_for_read,
    project_publication_appendix,
)
from tracefold.macro.reasons import macro_reason
from tracefold.macro.thesis import (
    MACRO_THESIS_ASSETS,
    MacroAssetOutlook,
    MacroCausalEdge,
    MacroCondition,
    MacroHorizonOutlook,
    MacroMainline,
    MacroModuleRole,
    MacroMomentum,
    MacroNarrativeSection,
    MacroOutcomeAssetResult,
    MacroOutcomeHorizon,
    MacroTension,
    MacroTensionSide,
    MacroThesisBodyDraft,
    MacroThesisClaim,
    MacroThesisReviewFailure,
    MacroThesisReviewV1,
    build_publication,
    compile_evidence_pack_v3,
    evaluate_live_delta,
    evaluate_outcome_replay,
    payload_hash,
    pending_outcome_replay,
    run_thesis_review_cycle,
)
from tracefold.macro.thesis_service import _classify_error

SESSION = date(2026, 7, 27)
CUTOFF_MS = 1_785_157_800_000


def _modules() -> tuple[dict, ...]:
    output = []
    for module_id in MACRO_MODULE_IDS:
        dataset_id = "fred.dgs2" if module_id == "rates_fed" else f"test.{module_id}"
        output.append(
            {
                "schema_version": f"test:{module_id}",
                "module_id": module_id,
                "label": module_id,
                "latest_fact_at_ms": CUTOFF_MS - 1_000,
                "status": {
                    "coverage": {"state": "complete"},
                    "current_health": {"state": "current"},
                    "history_depth": {"state": "not_required"},
                },
                "summary": {
                    "top_changes": [
                        {
                            "dataset_id": dataset_id,
                            "metrics": {"change_1w_bp": 30.0},
                            "as_of": "2026-07-27",
                        }
                    ]
                },
                "next_checkpoints": [],
                "evidence": {
                    "dataset_states": [{"dataset_id": dataset_id}],
                    "latest_facts": [
                        {
                            "fact_ref": f"fact:{module_id}",
                            "dataset_id": dataset_id,
                            "observed_at_ms": CUTOFF_MS - 1_000,
                            "published_at_ms": None,
                            "received_at_ms": CUTOFF_MS - 1_000,
                        }
                    ],
                },
            }
        )
    return tuple(output)


def _pack():
    return compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=_modules(),
        prior_publication=None,
    )


def _modules_with_complete_assets() -> tuple[dict, ...]:
    modules = deepcopy(_modules())
    by_module = {str(module["module_id"]): module for module in modules}
    for index, symbol in enumerate(MACRO_THESIS_ASSETS, start=1):
        module_id = "volatility" if symbol == "VIX" else "cross_asset"
        by_module[module_id]["evidence"].setdefault("asset_changes", []).append(
            {
                "dataset_id": MACRO_ASSET_DATASETS[symbol],
                "metrics": {
                    "return_1w_pct": float(index),
                    "return_1m_pct": float(index * 2),
                },
                "as_of": "2026-07-27",
            }
        )
        by_module[module_id]["evidence"]["latest_facts"].append(
            {
                "fact_ref": f"asset-fact:{symbol}",
                "dataset_id": MACRO_ASSET_DATASETS[symbol],
                "observed_at_ms": CUTOFF_MS - 1_000,
                "published_at_ms": CUTOFF_MS - 900,
                "received_at_ms": CUTOFF_MS - 800,
            }
        )
    return modules


def _directional_analysis() -> _AnalysisDraft:
    return _AnalysisDraft.model_validate(
        {
            "mainline": {
                "stance": "call",
                "title": "利率冲击主导短期定价",
                "thesis": "利率变化是当前跨资产重定价的主导矛盾。",
                "stage": "developing",
                "confidence": "medium",
                "horizon": "1w",
                "claim": "2Y 利率上行形成估值压力。",
                "causal_source": "2Y 利率上行",
                "causal_mechanism": "贴现率上升",
                "causal_target": "风险资产估值承压",
                "supporting_modules": ["rates_fed"],
                "conflicting_modules": ["credit"],
            },
            "module_assessments": [
                {
                    "module_id": module_id,
                    "role": "driver" if module_id == "rates_fed" else "confirming",
                    "analysis": f"{MACRO_MODULE_LABELS[module_id]}对主线提供冻结证据。",
                }
                for module_id in MACRO_MODULE_IDS
            ],
            "asset_outlooks": [
                {
                    "symbol": symbol,
                    "direction_1w": "bearish",
                    "channel_1w": "贴现率冲击经由资产自身动量传导。",
                    "confidence_1w": "medium",
                    "direction_1m": "bearish",
                    "channel_1m": "贴现率冲击经由资产自身动量传导。",
                    "confidence_1m": "medium",
                    "supporting_modules": ["rates_fed"],
                    "conflicting_modules": ["credit"],
                }
                for symbol in MACRO_THESIS_ASSETS
            ],
        }
    )


def _directional_publication():
    pack = compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=_modules_with_complete_assets(),
        prior_publication=None,
    )
    draft = _compile_analysis_draft(
        analysis=_directional_analysis(),
        evidence_pack=pack,
    )
    review = MacroThesisReviewV1(
        draft_hash=payload_hash(draft.model_dump(mode="json")),
        disposition="pass",
        findings=("完整资产证据与条件绑定已复核",),
        invocation_id="complete-assets-review",
        model_name="openai/gpt-5.4-mini",
        prompt_version="review-v1",
    )
    return build_publication(
        evidence_pack=pack,
        draft=draft,
        review=review,
        research_provenance={
            "invocation_id": "complete-assets-research",
            "model_name": "openai/gpt-5.4-mini",
            "prompt_version": "research-v1",
        },
        published_at_ms=CUTOFF_MS + 2_000,
    )


def test_evidence_pack_uses_complete_asset_facts_not_top_change_preview() -> None:
    pack = compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=_modules_with_complete_assets(),
        prior_publication=None,
    )

    assert tuple(momentum.symbol for momentum in pack.momentum) == MACRO_THESIS_ASSETS
    assert all(momentum.source_dataset_id is not None for momentum in pack.momentum)
    assert all(momentum.momentum_1w == "up" for momentum in pack.momentum)
    vix = next(momentum for momentum in pack.momentum if momentum.symbol == "VIX")
    assert vix.source_dataset_id == MACRO_ASSET_DATASETS["VIX"]
    assert vix.return_1w_pct == 12


def test_asset_conditions_bind_to_the_module_that_owns_the_asset_fact() -> None:
    publication = _directional_publication()
    spy = next(asset for asset in publication.assets if asset.symbol == "SPY")
    vix = next(asset for asset in publication.assets if asset.symbol == "VIX")

    assert {condition.module_id for condition in spy.outlook_1w.confirmation_triggers} == {"cross_asset"}
    assert {condition.module_id for condition in vix.outlook_1w.confirmation_triggers} == {"volatility"}
    annotations = project_module_annotations(
        publication.model_dump(mode="json"),
        module_id="volatility",
    )
    assert any(
        item.binding_id == "asset:VIX:1w"
        and item.kind == "confirmation"
        and item.condition.dataset_id == MACRO_ASSET_DATASETS["VIX"]
        for item in annotations
    )


def test_analysis_compiler_forces_all_assets_to_no_call_when_mainline_has_no_call() -> None:
    analysis = _AnalysisDraft.model_validate(
        {
            "mainline": {
                "stance": "no_call",
                "title": "证据尚未形成可交易主线",
                "thesis": "六个模块之间仍有关键冲突，暂不输出方向判断。",
                "stage": "uncertain",
                "confidence": "low",
                "horizon": "1w_to_1m",
                "claim": "当前证据只能支持继续观察。",
                "causal_source": "宏观证据冲突",
                "causal_mechanism": "驱动模块没有形成同向确认",
                "causal_target": "风险资产方向不确定",
                "supporting_modules": ["rates_fed"],
                "conflicting_modules": ["credit"],
            },
            "module_assessments": [
                {
                    "module_id": module_id,
                    "role": "uncertain",
                    "analysis": f"{MACRO_MODULE_LABELS[module_id]}尚未形成方向确认。",
                }
                for module_id in MACRO_MODULE_IDS
            ],
            "asset_outlooks": [
                {
                    "symbol": symbol,
                    "direction_1w": "bullish",
                    "channel_1w": "模型提出方向，但主线没有形成。",
                    "confidence_1w": "high",
                    "direction_1m": "bearish",
                    "channel_1m": "模型提出方向，但主线没有形成。",
                    "confidence_1m": "high",
                    "supporting_modules": ["rates_fed"],
                    "conflicting_modules": ["credit"],
                }
                for symbol in MACRO_THESIS_ASSETS
            ],
        }
    )

    compiled = _compile_analysis_draft(analysis=analysis, evidence_pack=_pack())

    assert compiled.mainline.stance == "no_call"
    assert all(asset.outlook_1w.direction == "no_call" for asset in compiled.asset_outlooks)
    assert all(asset.outlook_1m.direction == "no_call" for asset in compiled.asset_outlooks)
    assert all(asset.outlook_1w.confidence == "low" for asset in compiled.asset_outlooks)
    assert all(asset.outlook_1m.confidence == "low" for asset in compiled.asset_outlooks)
    assert all(
        "no_call" not in outlook.causal_channel
        for asset in compiled.asset_outlooks
        for outlook in (asset.outlook_1w, asset.outlook_1m)
    )
    assert tuple(
        section.title for section in compiled.narrative_sections if section.section_id.startswith("module-")
    ) == tuple(f"{MACRO_MODULE_LABELS[module_id]}模块判断" for module_id in MACRO_MODULE_IDS)


def _draft() -> MacroThesisBodyDraft:
    module_refs = {module_id: f"macro-module:{SESSION.isoformat()}:{module_id}" for module_id in MACRO_MODULE_IDS}
    confirming = MacroCondition(
        condition_id="claim-rates-confirm",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="gte",
        threshold=25,
        effect="confirming",
        rationale="2Y 周变化达到阈值",
    )
    falsifier = MacroCondition(
        condition_id="falsifier-rates",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="lte",
        threshold=-25,
        effect="invalidation_triggered",
        rationale="2Y 周变化反向",
    )
    checkpoint = MacroCondition(
        condition_id="checkpoint-rates",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="gte",
        threshold=20,
        effect="confirming",
        rationale="利率变化延续",
    )
    tension_trigger = MacroCondition(
        condition_id="tension-credit-resolve",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="lte",
        threshold=0,
        effect="weakening",
        rationale="利率压力消退将解决信用未确认的张力",
    )

    def no_call_outlook(symbol: str, horizon: str) -> MacroHorizonOutlook:
        return MacroHorizonOutlook(
            horizon=horizon,
            direction="no_call",
            causal_channel=f"{symbol} 当前证据不足，等待利率与信用共同确认。",
            supporting_evidence_refs=(module_refs["rates_fed"],),
            conflicting_evidence_refs=(module_refs["credit"],),
            confidence="low",
        )

    return MacroThesisBodyDraft(
        mainline=MacroMainline(
            stance="call",
            title="利率冲击主导短期定价",
            thesis="利率变化是当前跨资产重定价的主导矛盾。",
            stage="developing",
            confidence="medium",
            horizon="1w",
            claims=(
                MacroThesisClaim(
                    claim_id="claim-rates",
                    statement="2Y 利率上行形成估值压力。",
                    causal_edges=(
                        MacroCausalEdge(
                            source="2Y 利率上行",
                            mechanism="贴现率上升",
                            target="风险资产估值承压",
                            evidence_refs=(module_refs["rates_fed"],),
                            conflicting_evidence_refs=(module_refs["credit"],),
                        ),
                    ),
                    supporting_evidence_refs=(module_refs["rates_fed"],),
                    conflicting_evidence_refs=(module_refs["credit"],),
                    conditions=(confirming,),
                ),
            ),
            supporting_evidence_refs=(module_refs["rates_fed"],),
            conflicting_evidence_refs=(module_refs["credit"],),
            falsifiers=(falsifier,),
            checkpoints=(checkpoint,),
        ),
        core_tensions=(
            MacroTension(
                tension_id="tension-credit",
                statement="信用尚未确认利率压力。",
                side_a=MacroTensionSide(
                    label="利率压力",
                    statement="2Y 利率上行压制估值。",
                    evidence_refs=(module_refs["rates_fed"],),
                ),
                side_b=MacroTensionSide(
                    label="信用韧性",
                    statement="信用尚未发生系统性走弱。",
                    evidence_refs=(module_refs["credit"],),
                ),
                leading_side="side_a",
                lagging_signal="信用利差仍未确认。",
                unresolved_reason="融资成本与信用风险尚未同步共振。",
                resolution_triggers=(tension_trigger,),
            ),
        ),
        module_assessments=tuple(
            MacroModuleRole(
                module_id=module_id,
                role="driver" if module_id == "rates_fed" else "confirming",
                analysis=f"{MACRO_MODULE_LABELS[module_id]}模块作用",
                claim_ids=("claim-rates",),
                supporting_evidence_refs=(module_refs[module_id],),
            )
            for module_id in MACRO_MODULE_IDS
        ),
        asset_outlooks=tuple(
            MacroAssetOutlook(
                symbol=symbol,
                outlook_1w=no_call_outlook(symbol, "1w"),
                outlook_1m=no_call_outlook(symbol, "1m"),
            )
            for symbol in MACRO_THESIS_ASSETS
        ),
        narrative_sections=(
            MacroNarrativeSection(
                section_id="market-mainline",
                title="市场主线",
                markdown="利率冲击仍是当前主导变量，信用是关键反证。",
                evidence_refs=(
                    module_refs["rates_fed"],
                    module_refs["credit"],
                ),
            ),
        ),
    )


class _Agent:
    def __init__(self) -> None:
        self.calls = 0

    async def draft(self, **_kwargs):
        self.calls += 1
        return _draft(), {
            "invocation_id": f"agent-{self.calls}",
            "model_name": "openai/gpt-5.4-mini",
            "prompt_version": "research-v1",
        }


class _Reviewer:
    def __init__(self, dispositions: list[str]) -> None:
        self.dispositions = dispositions
        self.calls = 0

    async def review(self, *, draft_hash: str, **_kwargs):
        disposition = self.dispositions[self.calls]
        self.calls += 1
        return MacroThesisReviewV1(
            draft_hash=draft_hash,
            disposition=disposition,
            findings=("检查完成",),
            required_changes=("补充反证",) if disposition != "pass" else (),
            invocation_id=f"review-{self.calls}",
            model_name="openai/gpt-5.4-mini",
            prompt_version="review-v1",
        )


def test_review_cycle_binds_independent_review_and_allows_one_revision() -> None:
    agent = _Agent()
    reviewer = _Reviewer(["revise", "pass"])

    publication, reviews = asyncio.run(
        run_thesis_review_cycle(
            evidence_pack=_pack(),
            agent=agent,
            reviewer=reviewer,
            published_at_ms=CUTOFF_MS + 2_000,
        )
    )

    assert agent.calls == 2
    assert reviewer.calls == 2
    assert len(reviews) == 2
    assert publication.review.invocation_id == "review-2"
    assert publication.review.draft_hash == payload_hash(_draft().model_dump(mode="json"))
    assert publication.schema_version == "macro_thesis_v1"
    assert len(publication.module_assessments) == 6
    assert tuple(asset.symbol for asset in publication.assets) == MACRO_THESIS_ASSETS


def test_review_cycle_never_allows_a_second_revision() -> None:
    with pytest.raises(
        MacroThesisReviewFailure,
        match="macro_thesis_reviewer_not_passed_after_revision",
    ) as failure:
        asyncio.run(
            run_thesis_review_cycle(
                evidence_pack=_pack(),
                agent=_Agent(),
                reviewer=_Reviewer(["revise", "revise"]),
                published_at_ms=CUTOFF_MS + 2_000,
            )
        )
    assert tuple(review.disposition for review in failure.value.reviews) == (
        "revise",
        "revise",
    )


def test_delta_pack_compares_against_the_prior_sealed_pack() -> None:
    prior_modules = deepcopy(_modules())
    prior_modules[0]["summary"]["top_changes"][0]["metrics"]["change_1w_bp"] = 10.0
    prior_pack = compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=prior_modules,
        prior_publication=None,
    )

    current_pack = compile_evidence_pack_v3(
        session_date=date(2026, 7, 28),
        cutoff_ms=CUTOFF_MS + 86_400_000,
        sealed_at_ms=CUTOFF_MS + 86_401_000,
        modules=_modules(),
        prior_publication={"publication_id": "mth_prior"},
        prior_evidence_pack=prior_pack.model_dump(mode="json"),
    )

    changes = {(change["module_id"], change["dataset_id"]): change for change in current_pack.delta_pack["changes"]}
    assert current_pack.delta_pack["state"] == "compared"
    assert current_pack.delta_pack["comparison_basis"] == "prior_evidence_pack"
    assert changes[("rates_fed", "fred.dgs2")]["status"] == "changed"
    assert changes[("rates_fed", "fred.dgs2")]["prior"]["metrics"]["change_1w_bp"] == 10.0
    assert changes[("rates_fed", "fred.dgs2")]["current"]["metrics"]["change_1w_bp"] == 30.0


def test_thesis_shape_enforces_at_most_three_tensions() -> None:
    payload = _draft().model_dump(mode="python")
    payload["core_tensions"] = tuple(payload["core_tensions"]) * 4
    with pytest.raises(ValidationError):
        MacroThesisBodyDraft.model_validate(payload)


def test_live_delta_is_deterministic_and_binds_claim_checkpoint() -> None:
    publication, _ = asyncio.run(
        run_thesis_review_cycle(
            evidence_pack=_pack(),
            agent=_Agent(),
            reviewer=_Reviewer(["pass"]),
            published_at_ms=CUTOFF_MS + 2_000,
        )
    )

    initial_delta = evaluate_live_delta(
        publication=publication,
        modules=_modules(),
        evaluated_at_ms=CUTOFF_MS + 3_000,
    )
    post_cutoff_modules = deepcopy(_modules())
    post_cutoff_modules[0]["latest_fact_at_ms"] = CUTOFF_MS + 2_000
    post_cutoff_modules[0]["evidence"]["latest_facts"][0]["observed_at_ms"] = CUTOFF_MS + 1_400
    post_cutoff_modules[0]["evidence"]["latest_facts"][0]["published_at_ms"] = CUTOFF_MS + 1_500
    post_cutoff_modules[0]["evidence"]["latest_facts"][0]["received_at_ms"] = CUTOFF_MS + 1_900
    delta = evaluate_live_delta(
        publication=publication,
        modules=post_cutoff_modules,
        evaluated_at_ms=CUTOFF_MS + 3_000,
    )
    replay = pending_outcome_replay(
        publication=publication,
        evaluated_at_ms=CUTOFF_MS + 3_000,
    )

    assert initial_delta.status == "insufficient"
    assert all(item.reason_code == "post_cutoff_fact_missing" for item in initial_delta.items)
    assert initial_delta.live_delta_id == delta.live_delta_id
    assert initial_delta.input_hash != delta.input_hash
    assert delta.schema_version == "macro_live_delta_v1"
    assert delta.status == "confirming"
    assert delta.matched_claim_ids == ("claim-rates",)
    assert delta.matched_checkpoint_ids == ("checkpoint-rates",)
    assert delta.matched_falsifier_ids == ()
    assert {item.observed_at_ms for item in delta.items if item.dataset_id == "fred.dgs2"} == {CUTOFF_MS + 1_400}
    delta_read = project_live_delta_for_read(
        payload=delta.model_dump(mode="json"),
        publication=publication.model_dump(mode="json"),
    )
    assert {item.observed_at_ms for scope in delta_read.scopes for item in scope.items} == {CUTOFF_MS + 1_400}
    assert delta.module_fact_cutoff_ms == CUTOFF_MS + 1_400
    assert {item.observation_cutoff_ms for scope in delta_read.scopes for item in scope.items} == {CUTOFF_MS}
    mainline_items = {
        item.condition_id: item for scope in delta_read.scopes if scope.scope == "mainline" for item in scope.items
    }
    assert next(scope for scope in delta_read.scopes if scope.scope == "mainline").label == "整体主线"
    assert mainline_items["falsifier-rates"].reason.affected_claim_ids == ("claim-rates",)
    assert mainline_items["checkpoint-rates"].reason.affected_claim_ids == ("claim-rates",)
    assert replay.schema_version == "macro_outcome_replay_v1"
    assert all(horizon.status == "pending" for horizon in replay.horizons)


def test_live_delta_never_promotes_a_condition_from_an_unbound_module_clock() -> None:
    publication, _ = asyncio.run(
        run_thesis_review_cycle(
            evidence_pack=_pack(),
            agent=_Agent(),
            reviewer=_Reviewer(["pass"]),
            published_at_ms=CUTOFF_MS + 2_000,
        )
    )
    modules = deepcopy(_modules())
    modules[0]["latest_fact_at_ms"] = CUTOFF_MS + 10_000
    fact = modules[0]["evidence"]["latest_facts"][0]
    fact["observed_at_ms"] = CUTOFF_MS
    fact["published_at_ms"] = CUTOFF_MS + 8_000
    fact["received_at_ms"] = CUTOFF_MS + 9_000

    pre_cutoff = evaluate_live_delta(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 11_000,
    )
    assert pre_cutoff.status == "insufficient"
    assert all(item.status == "insufficient" for item in pre_cutoff.items)
    assert all(item.reason_code == "post_cutoff_fact_missing" for item in pre_cutoff.items)
    assert {item.observed_at_ms for item in pre_cutoff.items} == {CUTOFF_MS}

    fact.pop("observed_at_ms")
    fact.pop("published_at_ms")
    fact.pop("received_at_ms")
    missing = evaluate_live_delta(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 12_000,
    )
    assert missing.status == "insufficient"
    assert all(item.reason_code == "post_cutoff_fact_missing" for item in missing.items)
    assert {item.observed_at_ms for item in missing.items} == {None}

    fact["published_at_ms"] = CUTOFF_MS + 13_000
    fact["received_at_ms"] = CUTOFF_MS + 14_000
    published_fallback = evaluate_live_delta(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 15_000,
    )
    assert published_fallback.status == "confirming"
    assert {item.observed_at_ms for item in published_fallback.items} == {CUTOFF_MS + 13_000}

    fact.pop("published_at_ms")
    received_fallback = evaluate_live_delta(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 15_000,
    )
    assert received_fallback.status == "confirming"
    assert {item.observed_at_ms for item in received_fallback.items} == {CUTOFF_MS + 14_000}


def test_live_delta_read_keeps_asset_confirmation_out_of_mainline_validity() -> None:
    publication = _directional_publication()
    post_cutoff_modules = deepcopy(_modules_with_complete_assets())
    for module in post_cutoff_modules:
        module["latest_fact_at_ms"] = CUTOFF_MS + 3_000
        for fact in module["evidence"]["latest_facts"]:
            fact["observed_at_ms"] = CUTOFF_MS + 3_000
            fact["published_at_ms"] = CUTOFF_MS + 3_100
            fact["received_at_ms"] = CUTOFF_MS + 3_200
    post_cutoff_modules[0]["summary"]["top_changes"][0]["metrics"]["change_1w_bp"] = 0.0
    stored = evaluate_live_delta(
        publication=publication,
        modules=post_cutoff_modules,
        evaluated_at_ms=CUTOFF_MS + 4_000,
    )
    read = project_live_delta_for_read(
        payload=stored.model_dump(mode="json"),
        publication=publication.model_dump(mode="json"),
    )
    payload = read.model_dump(mode="json")
    spy_scope = next(scope for scope in read.scopes if scope.scope_id == "asset:SPY:1w")

    assert stored.status == "confirming"
    assert "status" not in payload
    assert read.mainline_validity == "unrelated"
    assert read.matched_claim_ids == ()
    assert read.matched_falsifier_ids == ()
    assert read.matched_checkpoint_ids == ()
    assert spy_scope.status == "confirming"
    assert spy_scope.label == "SPY · 1 周"
    assert spy_scope.items[0].unit is not None
    assert spy_scope.items[0].observed_at_ms == CUTOFF_MS + 3_000
    assert spy_scope.items[0].observation_cutoff_ms == CUTOFF_MS
    assert spy_scope.items[0].rationale
    assert spy_scope.items[0].reason.code == "condition_threshold_matched"

    retired_payload = stored.model_dump(mode="json")
    for item in retired_payload["items"]:
        item.pop("observed_at_ms")
    with pytest.raises(ValidationError):
        project_live_delta_for_read(
            payload=retired_payload,
            publication=publication.model_dump(mode="json"),
        )


def test_reader_projects_claim_asset_links_and_typed_outcome_reasons() -> None:
    publication = _directional_publication()
    publication_payload = publication.model_dump(mode="json")
    claims = project_claim_presentation(publication_payload)
    assets = project_asset_presentation(publication_payload)
    outcome = project_outcome_replay_for_read(
        pending_outcome_replay(
            publication=publication,
            evaluated_at_ms=CUTOFF_MS + 3_000,
        ).model_dump(mode="json")
    )

    assert claims[0].asset_implications
    assert tuple(condition.condition_id for condition in claims[0].falsifiers) == ("mainline-falsifier",)
    assert tuple(condition.condition_id for condition in claims[0].checkpoints) == ("mainline-checkpoint",)
    assert set(assets[0].claim_ids) == {claims[0].claim_id}
    assert outcome.schema_version == "macro_outcome_replay_read_v1"
    assert outcome.source_schema_version == "macro_outcome_replay_v1"
    assert outcome.horizons[0].source_reason_code == "horizon_not_expired"
    assert outcome.horizons[0].reason.next_check_at_ms == outcome.horizons[0].expires_at_ms
    assert outcome.horizons[1].asset_results[0].reason.affected_dataset_ids == (MACRO_ASSET_DATASETS["SPY"],)
    reader_reason_text = " ".join(
        value for horizon in outcome.horizons for value in (horizon.reason.message, horizon.reason.next_action) if value
    )
    assert all(
        token not in reader_reason_text
        for token in ("no_call", "canonical", "checkpoint", "publication", "Outcome Replay", "worker")
    )


def test_publication_appendix_keeps_all_authoritative_fact_clocks() -> None:
    pack = _pack()
    publication, _ = asyncio.run(
        run_thesis_review_cycle(
            evidence_pack=pack,
            agent=_Agent(),
            reviewer=_Reviewer(["pass"]),
            published_at_ms=CUTOFF_MS + 2_000,
        )
    )

    appendix = project_publication_appendix(
        publication=publication.model_dump(mode="json"),
        evidence_pack=pack.model_dump(mode="json"),
    )
    rates = next(item for item in appendix.source_lineage if item.dataset_id == "fred.dgs2")

    assert rates.observed_at_ms == CUTOFF_MS - 1_000
    assert rates.published_at_ms is None
    assert rates.received_at_ms == CUTOFF_MS - 1_000


def test_publication_gap_reason_extracts_typed_message_instead_of_dict_text() -> None:
    modules = deepcopy(_modules())
    modules[0]["evidence"]["dataset_states"] = [
        {
            "dataset_id": "fred.dgs2",
            "source_role": "decision_primary",
            "current_health": "unavailable",
            "history_depth": "insufficient",
            "current_reason": macro_reason(
                code="no_valid_fact",
                message="该 Dataset 尚无有效事实。",
                impact="blocked",
                affected_dataset_ids=("fred.dgs2",),
                retryable=True,
                recovery="automatic",
                next_action="等待下一次采集。",
            ),
            "history_reason": macro_reason(
                code="history_facts_missing",
                message="该 Dataset 的历史事实缺失。",
                impact="blocked",
                affected_dataset_ids=("fred.dgs2",),
                retryable=True,
                recovery="automatic",
                next_action="等待历史回填。",
            ),
        }
    ]
    pack = compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=modules,
        prior_publication=None,
    )
    draft = _draft()
    review = MacroThesisReviewV1(
        draft_hash=payload_hash(draft.model_dump(mode="json")),
        disposition="pass",
        findings=("缺口原因可读性已复核",),
        invocation_id="gap-reason-review",
        model_name="openai/gpt-5.4-mini",
        prompt_version="review-v1",
    )
    publication = build_publication(
        evidence_pack=pack,
        draft=draft,
        review=review,
        research_provenance={
            "invocation_id": "gap-reason-research",
            "model_name": "openai/gpt-5.4-mini",
            "prompt_version": "research-v1",
        },
        published_at_ms=CUTOFF_MS + 2_000,
    )

    reasons = [gap.reason for gap in publication.gaps if gap.dataset_id == "fred.dgs2"]
    assert "该 Dataset 尚无有效事实。 [no_valid_fact]" in reasons
    assert "该 Dataset 的历史事实缺失。 [history_facts_missing]" in reasons
    assert all("{" not in reason and "'code'" not in reason for reason in reasons)


def test_nullable_macro_values_are_required_in_public_read_contracts() -> None:
    required_by_model = {
        MacroMomentum: {
            "return_1w_pct",
            "return_1m_pct",
            "source_dataset_id",
            "as_of",
        },
        MacroOutcomeAssetResult: {
            "realized_return_pct",
            "direction_correct",
        },
        MacroOutcomeHorizon: {
            "realized_return_pct",
            "direction_correct",
            "asset_results",
        },
        MacroAssetHorizonPresentation: {
            "momentum_value",
            "reason",
        },
        MacroOutcomeAssetResultRead: {
            "realized_return_pct",
            "direction_correct",
        },
        MacroOutcomeHorizonRead: {
            "realized_return_pct",
            "direction_correct",
            "asset_results",
        },
        MacroLiveDeltaItemRead: {
            "observed_value",
            "observed_at_ms",
        },
    }

    for model, expected in required_by_model.items():
        assert expected <= set(model.model_json_schema()["required"])


def test_unsupported_codex_model_is_terminal_configuration_error() -> None:
    assert require_supported_macro_thesis_model("openai/gpt-5.4-mini") == "openai/gpt-5.4-mini"
    with pytest.raises(ValueError, match="macro_thesis_unsupported_model"):
        require_supported_macro_thesis_model("openai/gpt-5.6-terra")


def test_deepagent_graph_has_a_hard_step_limit() -> None:
    assert _graph_config("research:pack:draft", recursion_limit=48) == {
        "configurable": {"thread_id": "research:pack:draft"},
        "recursion_limit": 48,
    }
    with pytest.raises(
        ValueError,
        match="macro_thesis_graph_recursion_limit_invalid",
    ):
        _graph_config("research:pack:draft", recursion_limit=1_000)

    class GraphRecursionError(RuntimeError):
        pass

    assert _classify_error(GraphRecursionError("Recursion limit of 48 reached")) == (
        "macro_thesis_agent_step_limit",
        False,
        "not_published",
    )


def test_thesis_reason_distinguishes_run_lifecycle_failure_classes() -> None:
    cutoff_ms = CUTOFF_MS
    provider_code, provider_retryable, _ = _classify_error(TimeoutError("provider timeout"))
    reviewer_code, reviewer_retryable, reviewer_status = _classify_error(RuntimeError("macro_thesis_reviewer_block"))
    step_code, step_retryable, step_status = _classify_error(RuntimeError("recursion limit reached"))
    config_code, config_retryable, config_status = _classify_error(RuntimeError("unsupported_model"))

    cases = (
        (
            None,
            "macro_thesis_run_missing",
            "operator_action",
            True,
            None,
        ),
        (
            {"status": "pending", "due_at_ms": cutoff_ms},
            "macro_thesis_pending",
            "automatic",
            True,
            cutoff_ms,
        ),
        (
            {"status": "running", "leased_until_ms": cutoff_ms + 5_000},
            "macro_thesis_running",
            "automatic",
            True,
            cutoff_ms + 5_000,
        ),
        (
            {
                "status": "retryable",
                "last_error_code": provider_code,
                "due_at_ms": cutoff_ms + 10_000,
            },
            "macro_thesis_provider_transient",
            "automatic",
            True,
            cutoff_ms + 10_000,
        ),
        (
            {"status": reviewer_status, "last_error_code": reviewer_code},
            "macro_thesis_reviewer_block",
            "next_session",
            False,
            None,
        ),
        (
            {"status": step_status, "last_error_code": step_code},
            "macro_thesis_agent_step_limit",
            "next_session",
            False,
            None,
        ),
        (
            {"status": config_status, "last_error_code": config_code},
            "macro_thesis_configuration_error",
            "operator_action",
            False,
            None,
        ),
        (
            {"status": "failed", "last_error_code": provider_code},
            "macro_thesis_provider_retry_exhausted",
            "next_session",
            False,
            None,
        ),
    )

    assert provider_retryable is True
    assert reviewer_retryable is False
    assert step_retryable is False
    assert config_retryable is False
    for row, code, recovery, retryable, next_check_at_ms in cases:
        reason = routes_macro._thesis_reason(
            row,
            session_date=SESSION,
            cutoff_ms=cutoff_ms,
            read_at_ms=cutoff_ms + 1_000,
        )
        assert reason is not None
        assert reason["code"] == code
        assert reason["recovery"] == recovery
        assert reason["retryable"] is retryable
        assert reason["next_check_at_ms"] == next_check_at_ms


def test_transient_provider_reason_keeps_module_context_generating() -> None:
    reason = routes_macro._thesis_reason(
        {
            "status": "retryable",
            "last_error_code": "macro_thesis_provider_transient",
            "due_at_ms": CUTOFF_MS + 10_000,
        },
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        read_at_ms=CUTOFF_MS + 1_000,
    )

    context = routes_macro._module_thesis_context(
        "rates_fed",
        thesis=None,
        displayed_session_date=None,
        requested_session_date=SESSION,
        requested_reason=reason,
    )

    assert reason is not None
    assert reason["code"] == "macro_thesis_provider_transient"
    assert context["state"] == "generating"


def test_macro_thesis_deepagent_registers_exact_fileless_single_graph_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "tracefold.integrations.deepagents.macro_thesis_deepagent.register_harness_profile",
        lambda key, profile: registrations.append((key, profile)),
    )
    model = Mock()
    model._get_ls_params.return_value = {"ls_provider": "litellm"}

    key = register_macro_thesis_harness_profile(
        model=model,
        model_name="openai/deepseek-v4-flash",
    )

    assert key == "litellm:openai/deepseek-v4-flash"
    assert len(registrations) == 1
    _, profile = registrations[0]
    assert profile.excluded_tools == frozenset(
        {
            "edit_file",
            "execute",
            "glob",
            "grep",
            "ls",
            "read_file",
            "task",
            "write_file",
            "write_todos",
        }
    )
    assert profile.general_purpose_subagent.enabled is False
    assert profile.excluded_middleware == frozenset(
        {
            TodoListMiddleware,
            "SummarizationMiddleware",
        }
    )


def test_macro_thesis_uses_compact_embedded_sealed_evidence() -> None:
    view = _module_research_view(
        module={
            "module_id": "rates_fed",
            "summary": {"top_changes": [{"dataset_id": "fred.dgs2"}]},
            "curve": {
                "indicators": [
                    {
                        "dataset_id": "fred.dgs2",
                        "value": 4.2,
                        "history": [{"date": "2026-07-25", "value": 4.1}],
                    }
                ]
            },
            "evidence": {
                "latest_facts": [
                    {
                        "fact_ref": "macro-series:fred.dgs2:2026-07-27",
                        "dataset_id": "fred.dgs2",
                    }
                ],
                "dataset_states": [{"dataset_id": "fred.dgs2"}],
                "reconciliation_receipts": [{"receipt_id": "receipt"}],
            },
        },
        evidence_ref="macro-module:2026-07-27:rates_fed",
    )

    assert "history" not in str(view)
    assert "evidence" not in view["module"]
    context = _research_context(_pack())
    assert len(str(context)) < 200_000
    assert len(context["modules"]) == 6
    assert context["allowed_evidence_refs"]


def test_macro_thesis_request_boundary_removes_generic_prompt_and_tools() -> None:
    observed: dict[str, object] = {}
    request = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[],
        system_message=SystemMessage(content="generic deep agent prompt"),
        tools=[
            {"name": "read_file"},
            {"name": "task"},
            {"name": "MacroThesisBodyDraft"},
        ],
    )

    def handler(bounded: ModelRequest) -> object:
        observed["system"] = bounded.system_message.text
        observed["tools"] = [tool["name"] for tool in bounded.tools if isinstance(tool, dict)]
        return object()

    _MacroThesisRequestBoundary("sealed macro prompt").wrap_model_call(
        request,
        handler,  # type: ignore[arg-type]
    )
    assert observed == {
        "system": "sealed macro prompt",
        "tools": ["MacroThesisBodyDraft"],
    }


def test_outcome_replay_stays_pending_until_horizon_then_uses_persisted_prices() -> None:
    publication, _ = asyncio.run(
        run_thesis_review_cycle(
            evidence_pack=_pack(),
            agent=_Agent(),
            reviewer=_Reviewer(["pass"]),
            published_at_ms=CUTOFF_MS + 2_000,
        )
    )
    rows = [
        {
            "dataset_id": "nasdaq.spy.daily",
            "observed_at_ms": publication.published_at_ms,
            "received_at_ms": publication.published_at_ms,
            "value_numeric": 100.0,
        },
        {
            "dataset_id": "nasdaq.spy.daily",
            "observed_at_ms": publication.published_at_ms + 86_400_000,
            "received_at_ms": publication.published_at_ms + 86_400_000,
            "value_numeric": 101.0,
        },
    ]

    pending = evaluate_outcome_replay(
        publication=publication,
        market_rows=rows,
        evaluated_at_ms=publication.published_at_ms + 1_000,
    )
    evaluated = evaluate_outcome_replay(
        publication=publication,
        market_rows=rows,
        evaluated_at_ms=publication.published_at_ms + 86_400_000,
    )

    assert all(item.status == "pending" for item in pending.horizons)
    assert evaluated.horizons[0].status == "evaluated"
    assert evaluated.horizons[0].realized_return_pct == 1.0
    assert evaluated.horizons[1].status == "pending"
