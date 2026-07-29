from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from langchain.agents.structured_output import ProviderStrategy
from pydantic import ValidationError

from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    MacroThesisDeepAgent,
    require_supported_macro_thesis_model,
)
from tracefold.macro.assets import MACRO_ASSET_DATASETS, MACRO_THESIS_ASSETS
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.thesis import compile_evidence_pack_v3
from tracefold.macro.thesis_service import _classify_error
from tracefold.macro.thesis_v2 import (
    MACRO_CONDITION_FAMILY_PREFIXES,
    MAX_CONDITION_CANDIDATES,
    MAX_EXACT_EVIDENCE_REFS,
    MAX_RESEARCH_INPUT_BYTES,
    CandidateDraftEnvelope,
    MacroDraftAssetOutlook,
    MacroDraftCausalEdge,
    MacroDraftConditionUse,
    MacroDraftMainline,
    MacroResearchInputV1,
    MacroThesisDraftV2,
    MetricConditionCandidate,
    PublicationGateFailure,
    canonical_json_bytes,
    compile_candidate_publication_v2,
    compile_research_input_v1,
    evaluate_live_delta_v2,
    evaluate_outcome_replay_v2,
    project_current_recovery,
)

SESSION = date(2026, 7, 28)
CUTOFF_MS = int(datetime(2026, 7, 28, 12, 50, tzinfo=UTC).timestamp() * 1_000)


def _modules(
    *,
    facts_per_module: int = 1,
    real_rate_history_count: int = 0,
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    for module_id in MACRO_MODULE_IDS:
        dataset_id = "fred.dgs2" if module_id == "rates_fed" else f"test.{module_id}"
        states = [_dataset_state(dataset_id)]
        facts = [_fact(f"fact:{module_id}", dataset_id, value=1.0)]
        for index in range(1, facts_per_module):
            extra_id = f"test.{module_id}.{index:02d}"
            states.append(_dataset_state(extra_id))
            facts.append(_fact(f"fact:{module_id}:{index:02d}", extra_id, value=float(index)))
        module: dict[str, Any] = {
            "schema_version": f"test:{module_id}",
            "module_id": module_id,
            "label": module_id,
            "latest_fact_at_ms": CUTOFF_MS - 1_000,
            "status": {
                "coverage": {"state": "complete"},
                "current_health": {"state": "current"},
                "history_depth": {"state": "complete"},
            },
            "summary": {
                "top_changes": [
                    {
                        "dataset_id": dataset_id,
                        "concept_id": dataset_id,
                        "source_role": "canonical",
                        "label": f"{module_id} material change",
                        "value": 1.0,
                        "unit": "percent",
                        "metrics": {"change_1w_bp": 30.0},
                        "as_of": SESSION.isoformat(),
                    }
                ]
            },
            "next_checkpoints": [],
            "evidence": {
                "dataset_states": states,
                "latest_facts": facts,
                "asset_changes": [],
                "reconciliation_receipts": [],
            },
        }
        if module_id == "rates_fed" and real_rate_history_count:
            history = [
                {
                    "date": (SESSION - timedelta(days=real_rate_history_count - index)).isoformat(),
                    "value": 1.0 + index / 100,
                }
                for index in range(real_rate_history_count)
            ]
            module["real_rate_history"] = {
                "dataset_id": "fred.dfii10",
                "history": history,
            }
            module["evidence"]["dataset_states"].append(_dataset_state("fred.dfii10"))
            module["evidence"]["latest_facts"].append(
                _fact("fact:rates-real10y", "fred.dfii10", value=history[-1]["value"])
            )
        output.append(module)

    by_module = {str(module["module_id"]): module for module in output}
    for index, symbol in enumerate(MACRO_THESIS_ASSETS, start=1):
        module_id = "volatility" if symbol == "VIX" else "cross_asset"
        dataset_id = MACRO_ASSET_DATASETS[symbol]
        module = by_module[module_id]
        module["evidence"]["asset_changes"].append(
            {
                "dataset_id": dataset_id,
                "metrics": {
                    "return_1w_pct": float(index),
                    "return_1m_pct": float(index * 2),
                },
                "as_of": SESSION.isoformat(),
            }
        )
        module["evidence"]["dataset_states"].append(_dataset_state(dataset_id))
        module["evidence"]["latest_facts"].append(_fact(f"asset-fact:{symbol}", dataset_id, value=100.0 + index))
    return tuple(output)


def _dataset_state(dataset_id: str) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "concept_id": dataset_id,
        "source_role": "decision_primary",
        "required_for_current": True,
        "required_for_history": False,
        "critical": True,
        "label": dataset_id,
        "current_health": "current",
        "history_depth": "not_required",
        "source_url": "https://example.test/source",
    }


def _fact(fact_ref: str, dataset_id: str, *, value: float) -> dict[str, Any]:
    return {
        "fact_ref": fact_ref,
        "dataset_id": dataset_id,
        "reference": SESSION.isoformat(),
        "value": value,
        "unit": "percent",
        "source_url": "https://example.test/source",
        "observed_at_ms": CUTOFF_MS - 3_000,
        "published_at_ms": CUTOFF_MS - 2_000,
        "received_at_ms": CUTOFF_MS - 1_000,
    }


def _pack(*, modules: tuple[dict[str, Any], ...] | None = None):
    return compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=modules or _modules(),
        prior_publication=None,
    )


def _research_input(
    *,
    pack=None,
    candidate_scopes: tuple[str, ...] = ("mainline", "alternative", "tension", "asset"),
) -> MacroResearchInputV1:
    base = compile_research_input_v1(pack or _pack())
    evidence_ref = base.modules[0].exact_evidence_refs[0]
    candidate = MetricConditionCandidate(
        candidate_id="rates.curve10y2y:rates.curve_10y2y:gt0",
        module_id="rates_fed",
        dataset_id="rates.curve_10y2y",
        metric="value",
        unit="basis_points",
        operator="gt",
        threshold=0.0,
        frozen_value=25.0,
        as_of=SESSION.isoformat(),
        historical_percentile_rank=None,
        quantile_window=None,
        sample_count=1,
        allowed_kinds=("confirmation", "weakening", "falsifier"),
        allowed_scopes=candidate_scopes,
        meaning="10Y-2Y curve is above zero",
        evidence_refs=(evidence_ref,),
    )
    payload = base.model_dump(mode="json")
    payload["condition_candidates"] = [candidate.model_dump(mode="json")]
    payload["allowed_condition_ids"] = [candidate.candidate_id]
    payload["modules"][0]["condition_candidate_ids"] = [candidate.candidate_id]
    return MacroResearchInputV1.model_validate(payload)


def _draft(
    research_input: MacroResearchInputV1 | None = None,
    *,
    condition_scope: str = "mainline",
) -> MacroThesisDraftV2:
    research_input = research_input or _research_input()
    evidence_ref = research_input.modules[0].exact_evidence_refs[0]
    outlook = MacroDraftAssetOutlook(
        outlook_id="outlook-spy-1w",
        symbol="SPY",
        horizon="1w",
        outlook_context="mainline",
        direction="bearish",
        causal_transmission="Higher discount rates pressure equity duration.",
        supporting_evidence_refs=(evidence_ref,),
        conflicting_evidence_refs=(),
        confidence=None,
    )
    scope_id = "outlook-spy-1w" if condition_scope == "asset" else "mainline"
    condition_use = MacroDraftConditionUse(
        candidate_id=research_input.allowed_condition_ids[0],
        kind="confirmation",
        scope_kind=condition_scope,
        scope_id=scope_id,
        symbol="SPY" if condition_scope == "asset" else None,
        horizon="1w" if condition_scope == "asset" else None,
        rationale="The declared curve predicate tests the causal transmission.",
        evidence_refs=(evidence_ref,),
    )
    return MacroThesisDraftV2(
        session_date=research_input.session_date,
        cutoff_ms=research_input.cutoff_ms,
        evidence_pack_id=research_input.evidence_pack_id,
        research_input_id=research_input.input_id,
        mainline=MacroDraftMainline(
            stance="call",
            title="Curve pressure remains the material macro driver",
            thesis="The rates impulse is transmitting through discount rates.",
            stage="developing",
            horizon="1w",
            confidence=None,
            causal_edges=(
                MacroDraftCausalEdge(
                    edge_id="edge-rates-equity",
                    source="2Y Treasury yield",
                    mechanism="higher discount rate",
                    target="equity duration",
                    evidence_refs=(evidence_ref,),
                    conflicting_evidence_refs=(),
                ),
            ),
            supporting_evidence_refs=(evidence_ref,),
            conflicting_evidence_refs=(),
            no_call_reason=None,
        ),
        alternative=None,
        tensions=(),
        module_assessments=(
            {
                "module_id": "rates_fed",
                "role": "driver",
                "analysis": "Rates provide the material causal impulse.",
                "evidence_refs": (evidence_ref,),
            },
        ),
        material_changes=(
            {
                "change_id": "change-rates",
                "status": "strengthened",
                "statement": "The weekly rates move strengthened.",
                "evidence_refs": (evidence_ref,),
            },
        ),
        asset_outlooks=(outlook,),
        condition_uses=(condition_use,),
    )


def _envelope(
    research_input: MacroResearchInputV1 | None = None,
    *,
    mapping: dict[str, Any] | None = None,
) -> CandidateDraftEnvelope:
    research_input = research_input or _research_input()
    return CandidateDraftEnvelope(
        attempt_id=f"macro-thesis:{SESSION}:attempt:1",
        provider_response_id="response-test-1",
        provider_name="test-provider",
        model_name="test-model",
        profile_version=research_input.profile_version,
        prompt_version=research_input.prompt_version,
        research_input_id=research_input.input_id,
        research_input_hash=research_input.input_hash,
        raw_structured_mapping=mapping or _draft(research_input).model_dump(mode="json"),
        received_at_ms=CUTOFF_MS + 2_000,
        model_calls=1,
    )


def _publication(*, condition_scope: str = "mainline"):
    pack = _pack()
    research_input = _research_input(pack=pack)
    draft = _draft(research_input, condition_scope=condition_scope)
    return compile_candidate_publication_v2(
        envelope=_envelope(research_input, mapping=draft.model_dump(mode="json")),
        research_input=research_input,
        evidence_pack=pack,
        published_at_ms=CUTOFF_MS + 3_000,
    )


class _Agent:
    def __init__(self, mapping_mutator=None) -> None:
        self.calls = 0
        self.mapping_mutator = mapping_mutator

    async def draft(self, *, research_input, attempt_id):
        self.calls += 1
        mapping = _draft(research_input).model_dump(mode="json")
        if self.mapping_mutator is not None:
            self.mapping_mutator(mapping)
        return _envelope(research_input, mapping=mapping).model_copy(update={"attempt_id": attempt_id})


def test_evidence_pack_uses_complete_asset_facts_not_top_change_preview() -> None:
    pack = _pack()

    assert tuple(momentum.symbol for momentum in pack.momentum) == MACRO_THESIS_ASSETS
    assert all(momentum.source_dataset_id is not None for momentum in pack.momentum)
    assert all(momentum.momentum_1w == "up" for momentum in pack.momentum)
    assert pack.momentum[-1].symbol == "VIX"
    assert pack.momentum[-1].source_dataset_id == "fred.vixcls"


def test_research_input_is_deterministic_bounded_and_round_robin() -> None:
    pack = _pack(modules=_modules(facts_per_module=20))
    first = compile_research_input_v1(pack)
    second = compile_research_input_v1(pack)

    assert first == second
    assert first.input_hash == second.input_hash
    assert tuple(module.module_id for module in first.modules) == MACRO_MODULE_IDS
    assert tuple(momentum.symbol for momentum in first.momentum) == MACRO_THESIS_ASSETS
    assert len(first.exact_evidence) <= MAX_EXACT_EVIDENCE_REFS
    assert len(first.condition_candidates) <= MAX_CONDITION_CANDIDATES
    assert len(canonical_json_bytes(first.model_dump(mode="json"))) <= MAX_RESEARCH_INPUT_BYTES
    assert all(len(module.driver_candidates) <= 3 for module in first.modules)
    assert all(len(module.material_changes) <= 2 for module in first.modules)
    assert all(len(module.counter_signal_candidates) <= 2 for module in first.modules)
    assert all(len(module.exact_evidence_refs) <= 6 for module in first.modules)
    assert all(len(module.condition_candidate_ids) <= 4 for module in first.modules)
    first_round = tuple(item.module_id for item in first.exact_evidence[:6])
    assert first_round == MACRO_MODULE_IDS
    assert first.omitted_count["exact_evidence"] > 0


def test_condition_registry_is_the_exact_closed_family_set() -> None:
    assert MACRO_CONDITION_FAMILY_PREFIXES == (
        "rates.curve10y2y",
        "rates.real10y.tail",
        "economy.release_surprise",
        "economy.release_revision",
        "economy.cpi_yoy.tail",
        "economy.payroll.tail",
        "liquidity.net_4w.tail",
        "liquidity.sofr_iorb",
        "credit.hy_ig_gap.tail",
        "credit.ccc_bb_gap.tail",
        "vol.vix_vxv_zero",
        "vol.vx_front2_zero",
        "cross.corr.tail",
        "cross.return1m.tail",
    )


def test_five_year_tail_candidates_require_at_least_twenty_samples() -> None:
    insufficient = compile_research_input_v1(_pack(modules=_modules(real_rate_history_count=19)))
    sufficient = compile_research_input_v1(_pack(modules=_modules(real_rate_history_count=20)))

    assert not any(item.candidate_id.startswith("rates.real10y.tail:") for item in insufficient.condition_candidates)
    tail = [item for item in sufficient.condition_candidates if item.candidate_id.startswith("rates.real10y.tail:")]
    assert {item.candidate_id.rsplit(":", maxsplit=1)[-1] for item in tail} == {
        "leq20",
        "geq80",
    }
    assert all(item.quantile_window == "five_years" for item in tail)
    assert all(item.sample_count == 20 for item in tail)


def test_sparse_draft_does_not_require_six_modules_or_twelve_assets() -> None:
    draft = _draft()

    assert len(draft.mainline.causal_edges) == 1
    assert len(draft.module_assessments) == 1
    assert tuple(item.symbol for item in draft.asset_outlooks) == ("SPY",)
    assert draft.mainline.confidence is None


def test_directional_and_no_call_shapes_fail_closed() -> None:
    research_input = _research_input()
    payload = _draft(research_input).model_dump(mode="json")
    payload["mainline"]["causal_edges"] = []
    with pytest.raises(ValidationError, match="directional_edge_count"):
        MacroThesisDraftV2.model_validate(payload)

    payload = _draft(research_input).model_dump(mode="json")
    payload["mainline"]["stance"] = "no_call"
    payload["mainline"]["no_call_reason"] = "Evidence is mixed."
    with pytest.raises(ValidationError, match="no_call_edges_forbidden"):
        MacroThesisDraftV2.model_validate(payload)


def test_same_candidate_can_only_be_selected_once_per_scope() -> None:
    research_input = _research_input()
    payload = _draft(research_input).model_dump(mode="json")
    payload["condition_uses"].append(deepcopy(payload["condition_uses"][0]))

    with pytest.raises(ValidationError, match="duplicate_condition_use"):
        MacroThesisDraftV2.model_validate(payload)


def test_publication_compiler_owns_parse_conditions_and_sparse_output() -> None:
    publication = _publication()

    assert publication.schema_version == "macro_thesis_v2"
    assert publication.mainline.confidence is None
    assert tuple(item.symbol for item in publication.assets) == MACRO_THESIS_ASSETS
    assert tuple(item.display_order for item in publication.assets) == tuple(range(12))
    assert tuple(item.symbol for item in publication.asset_outlooks) == ("SPY",)
    assert len(publication.citations) == 1
    assert len(publication.conditions) == 1
    assert publication.conditions[0].candidate_id.startswith("rates.curve10y2y:")
    assert publication.conditions[0].threshold == 0


def test_unparseable_mapping_is_contract_gate() -> None:
    pack = _pack()
    research_input = _research_input(pack=pack)
    envelope = _envelope(research_input, mapping={"schema_version": "macro_thesis_draft_v2"})

    with pytest.raises(PublicationGateFailure) as caught:
        compile_candidate_publication_v2(
            envelope=envelope,
            research_input=research_input,
            evidence_pack=pack,
            published_at_ms=CUTOFF_MS + 3_000,
        )

    assert caught.value.category == "contract_validity"
    assert caught.value.code == "macro_thesis_contract_schema_invalid"


def test_parseable_gate_priority_is_time_then_evidence_then_contract() -> None:
    pack = _pack()
    research_input = _research_input(pack=pack)
    mapping = _draft(research_input).model_dump(mode="json")
    mapping["session_date"] = "2026-07-27"
    mapping["mainline"]["causal_edges"][0]["evidence_refs"] = ["outside-pack"]
    mapping["condition_uses"][0]["candidate_id"] = "unknown-condition"
    with pytest.raises(PublicationGateFailure) as time_failure:
        compile_candidate_publication_v2(
            envelope=_envelope(research_input, mapping=mapping),
            research_input=research_input,
            evidence_pack=pack,
            published_at_ms=CUTOFF_MS + 3_000,
        )
    assert time_failure.value.category == "time_identity"

    mapping["session_date"] = SESSION.isoformat()
    with pytest.raises(PublicationGateFailure) as evidence_failure:
        compile_candidate_publication_v2(
            envelope=_envelope(research_input, mapping=mapping),
            research_input=research_input,
            evidence_pack=pack,
            published_at_ms=CUTOFF_MS + 3_000,
        )
    assert evidence_failure.value.category == "evidence_closure"

    mapping["mainline"]["causal_edges"][0]["evidence_refs"] = [research_input.allowed_evidence_ids[0]]
    with pytest.raises(PublicationGateFailure) as contract_failure:
        compile_candidate_publication_v2(
            envelope=_envelope(research_input, mapping=mapping),
            research_input=research_input,
            evidence_pack=pack,
            published_at_ms=CUTOFF_MS + 3_000,
        )
    assert contract_failure.value.category == "contract_validity"


def test_envelope_input_binding_is_time_identity_gate() -> None:
    pack = _pack()
    research_input = _research_input(pack=pack)
    envelope = _envelope(research_input).model_copy(update={"research_input_id": "mri1_wrong"})

    with pytest.raises(PublicationGateFailure) as caught:
        compile_candidate_publication_v2(
            envelope=envelope,
            research_input=research_input,
            evidence_pack=pack,
            published_at_ms=CUTOFF_MS + 3_000,
        )
    assert caught.value.category == "time_identity"
    assert "envelope_research_input_id_mismatch" in caught.value.diagnostics


def test_live_delta_identity_is_immutable_and_asset_scope_does_not_elevate_mainline() -> None:
    publication = _publication(condition_scope="asset")
    modules = _modules()
    first = evaluate_live_delta_v2(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 10_000,
    )
    same_input_later = evaluate_live_delta_v2(
        publication=publication,
        modules=modules,
        evaluated_at_ms=CUTOFF_MS + 20_000,
    )
    changed = deepcopy(modules)
    changed[0]["latest_fact_at_ms"] = CUTOFF_MS + 30_000
    new_input = evaluate_live_delta_v2(
        publication=publication,
        modules=changed,
        evaluated_at_ms=CUTOFF_MS + 30_000,
    )

    assert first.live_delta_id == same_input_later.live_delta_id
    assert first.input_hash == same_input_later.input_hash
    assert new_input.live_delta_id != first.live_delta_id
    assert first.mainline_validity == "insufficient"
    assert first.items[0].scope_kind == "asset"


def test_outcome_replay_only_evaluates_declared_material_1w_or_1m_outlooks() -> None:
    publication = _publication()
    expires = CUTOFF_MS + 7 * 86_400_000
    rows = [
        {
            "dataset_id": "nasdaq.spy.daily",
            "observed_at_ms": CUTOFF_MS,
            "value_numeric": 100.0,
        },
        {
            "dataset_id": "nasdaq.spy.daily",
            "observed_at_ms": expires,
            "value_numeric": 95.0,
        },
        {
            "dataset_id": "nasdaq.qqq.daily",
            "observed_at_ms": expires,
            "value_numeric": 200.0,
        },
    ]
    replay = evaluate_outcome_replay_v2(
        publication=publication,
        market_rows=rows,
        evaluated_at_ms=expires,
    )

    assert tuple(item.horizon for item in replay.horizons) == ("1w",)
    assert tuple(result.symbol for horizon in replay.horizons for result in horizon.asset_results) == ("SPY",)
    assert replay.horizons[0].asset_results[0].direction_correct is True


def test_recovery_keeps_publication_and_current_fact_clocks_separate() -> None:
    publication = _publication()
    modules = deepcopy(_modules())
    spy = next(row for row in modules[-1]["evidence"]["asset_changes"] if row["dataset_id"] == "nasdaq.spy.daily")
    spy["metrics"]["return_1m_pct"] = None
    recovery = project_current_recovery(publication=publication, modules=modules)
    spy_recovery = next(item for item in recovery if item.scope_id == "SPY")
    qqq_recovery = next(item for item in recovery if item.scope_id == "QQQ")

    assert spy_recovery.state == "degraded"
    assert spy_recovery.publication.value is not None
    assert spy_recovery.current.value is None
    assert qqq_recovery.state == "unchanged"


class _FakeModel:
    def _get_ls_params(self):
        return {"ls_provider": "fake-provider"}


class _Graph:
    def __init__(self, *, graph_kwargs, mapping) -> None:
        self.graph_kwargs = graph_kwargs
        self.mapping = mapping
        self.input = None
        self.config = None

    async def ainvoke(self, input, *, config=None):
        self.input = input
        self.config = config
        self.graph_kwargs["middleware"][0]._claim()
        return {"structured_response": self.mapping, "response_id": "provider-response-1"}


def test_thin_deepagent_uses_one_graph_one_model_call_and_native_mapping() -> None:
    research_input = _research_input()
    captured: dict[str, Any] = {}

    def factory(**kwargs):
        graph = _Graph(
            graph_kwargs=kwargs,
            mapping=_draft(research_input).model_dump(mode="json"),
        )
        captured["kwargs"] = kwargs
        captured["graph"] = graph
        return graph

    adapter = MacroThesisDeepAgent(
        model=_FakeModel(),
        model_name="test-model-thin",
        agent_factory=factory,
        clock_ms=lambda: CUTOFF_MS + 2_000,
    )
    envelope = asyncio.run(adapter.draft(research_input=research_input, attempt_id="attempt-thin-1"))
    kwargs = captured["kwargs"]
    graph = captured["graph"]

    assert kwargs["tools"] == ()
    assert kwargs["subagents"] == ()
    assert kwargs["checkpointer"] is None
    assert isinstance(kwargs["response_format"], ProviderStrategy)
    assert '"properties"' not in kwargs["system_prompt"]
    assert graph.input["messages"][0]["content"] == canonical_json_bytes(research_input.model_dump(mode="json")).decode(
        "utf-8"
    )
    assert envelope.model_calls == 1
    assert envelope.raw_structured_mapping["schema_version"] == "macro_thesis_draft_v2"
    assert envelope.provider_response_id == "provider-response-1"


def test_thin_deepagent_fails_before_envelope_without_exactly_one_model_call() -> None:
    research_input = _research_input()

    class _NoCallGraph:
        async def ainvoke(self, input, *, config=None):
            return {
                "structured_response": _draft(research_input).model_dump(mode="json"),
                "response_id": "response-no-call",
            }

    adapter = MacroThesisDeepAgent(
        model=_FakeModel(),
        model_name="test-model-no-call",
        agent_factory=lambda **_kwargs: _NoCallGraph(),
    )
    with pytest.raises(RuntimeError, match="model_call_count_invalid"):
        asyncio.run(adapter.draft(research_input=research_input, attempt_id="attempt-1"))


def test_pre_draft_failures_are_run_states_not_publication_gates() -> None:
    assert _classify_error(RuntimeError("invalid_api_key")) == (
        "macro_thesis_configuration_error",
        False,
        "config_error",
    )
    assert _classify_error(RuntimeError("macro_thesis_provider_structured_mapping_missing")) == (
        "macro_thesis_provider_no_mapping",
        False,
        "failed",
    )
    timeout_code, retryable, terminal = _classify_error(TimeoutError("provider timeout"))
    assert timeout_code == "macro_thesis_timeouterror"
    assert retryable is True
    assert terminal == "failed"


def test_codex_models_are_not_accepted_for_macro_research() -> None:
    assert require_supported_macro_thesis_model("openai/gpt-5.4-mini") == ("openai/gpt-5.4-mini")
    with pytest.raises(ValueError, match="unsupported_model"):
        require_supported_macro_thesis_model("openai/gpt-5.6-codex")
