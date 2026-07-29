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
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.thesis import (
    MACRO_THESIS_ASSETS,
    MacroAssetOutlook,
    MacroCausalEdge,
    MacroCondition,
    MacroHorizonOutlook,
    MacroMainline,
    MacroModuleRole,
    MacroNarrativeSection,
    MacroTension,
    MacroTensionSide,
    MacroThesisBodyDraft,
    MacroThesisClaim,
    MacroThesisReviewFailure,
    MacroThesisReviewV1,
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
                    "analysis": f"{module_id} 尚未形成方向确认。",
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
                analysis=f"{module_id} 模块作用",
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
    assert replay.schema_version == "macro_outcome_replay_v1"
    assert all(horizon.status == "pending" for horizon in replay.horizons)


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
