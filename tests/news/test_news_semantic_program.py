from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.resources
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import dspy
import pytest
from pydantic import ValidationError

from tracefold.news import EditorialEnvelope, ScoredJudgment, SemanticJudgeError, SemanticJudgment, TradeRelevanceV1
from tracefold.news.agents import semantic_program as semantic_program_module
from tracefold.news.agents.semantic_program import (
    PROGRAM_DEPENDENCY_LOCK_SHA256,
    PROGRAM_TOPOLOGY_SHA256,
    READER_CARD_SIGNATURE_SHA256,
    TOLD_MAX,
    TOLD_STORYLINE_TIER_MAX,
    CompileReceipt,
    DemoBank,
    DemoRecord,
    DemoRefOrder,
    DspyCompileProgram,
    DspyNewsSemanticProgram,
    DspyPredictorAdapter,
    EligibleDemoBank,
    EventSemantics,
    LearnedStrategy,
    PredictorAdapterError,
    PredictorRequest,
    PredictorResponse,
    ProgramArtifact,
    ProgramArtifactCodec,
    ProgramPatchV2,
    ProviderCallObservation,
    ReaderCard,
    ReaderCardSemanticView,
    RecordReplayPredictorAdapter,
    ScriptedPredictorAdapter,
    TriageContext,
    apply_program_patch_v2,
    build_code_owned_program_artifact_v2,
    extract_optimizer_patch,
    load_program_artifact,
    load_stable_program_artifact,
    render_model_evidence_json,
)
from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.models import TriageAsset, TriageVerdict


def _semantics(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "listing",
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "confidence": 0.8,
        "audience": "crypto",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unknown",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
    }
    value.update(updates)
    return value


def _card(**updates: Any) -> dict[str, Any]:
    value = {"headline_zh": "比特币出现新进展", "why_zh": "值得关注。"}
    value.update(updates)
    return value


def _reader_semantics(semantics: dict[str, Any]) -> dict[str, Any]:
    relevance = semantics["relevance"]
    return ReaderCardSemanticView.model_validate(
        {
            "event_type": semantics["event_type"],
            "assets": semantics["assets"],
            "direction": semantics["direction"],
            "magnitude": semantics["magnitude"],
            "novelty": semantics["novelty"],
            "restates": semantics["restates"],
            "scope": semantics["scope"],
            "channels": relevance["channels"],
            "affected_markets": relevance["affected_markets"],
        }
    ).model_dump(mode="json")


def _context(*, told_rows: list[dict[str, Any]] | None = None) -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-secret",
            "evidence_version": 2,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-secret",
            "reporting_origin": "wire",
            "provenance": ["1018"],
            "leader_title": "BTC listed on Example Exchange",
            "raw_first_line": "$BTC listing",
            "leader_description": "Trading starts tomorrow.",
            "opened_at_ms": 1_000_000,
            "member_count": 2,
            "family": "listing",
            "provider_score_max": 90,
            "provider_metadata": {"coins": [{"symbol": "BTC", "grade": "A"}]},
            "queue_priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=told_rows or [],
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


def _program(
    primary: ScriptedPredictorAdapter,
    *,
    fallback: ScriptedPredictorAdapter | None = None,
    artifact: ProgramArtifact | None = None,
) -> DspyNewsSemanticProgram:
    return DspyNewsSemanticProgram(
        artifact or load_stable_program_artifact(),
        primary_adapter=primary,
        fallback_adapter=fallback,
    )


def _artifact_with_execution(**updates: Any) -> ProgramArtifact:
    base = load_stable_program_artifact()
    execution = base.execution.model_copy(update=updates)
    artifact = base.model_copy(update={"execution": execution})
    return artifact.model_copy(update={"program_sha256": artifact.computed_sha256()})


def test_builtin_artifact_is_registered_and_canonical() -> None:
    artifact = load_stable_program_artifact()
    assert load_program_artifact(artifact.program_sha256) == artifact
    manifest, state = ProgramArtifactCodec.encode(artifact)
    assert ProgramArtifactCodec.decode(manifest, state) == artifact
    assert artifact.program_sha256 == artifact.computed_sha256()
    assert artifact.state_sha256 == artifact.computed_state_sha256()


def test_stable_root_is_the_v6_generation_v2_ownership_contract() -> None:
    artifact = load_stable_program_artifact()

    assert artifact.schema_version == "news_semantic_program_artifact_v2"
    assert artifact.factory_id == "tracefold.news.semantic_program.factory_v4"
    assert artifact.program_version == "news_semantic_program_v4"
    assert artifact.parent_program_sha256 is None
    assert artifact.quality_kernel.factory_id == artifact.factory_id
    assert [pack.order for pack in artifact.rule_packs] == list(range(1, 10))
    assert {strategy.predictor for strategy in artifact.learned_strategies} == {
        "event_semantics",
        "reader_card",
    }
    assert artifact.demo_bank.records == ()
    assert artifact.demo_bank.refs.event_semantics == ()
    assert artifact.demo_bank.refs.reader_card == ()
    assert [slot.slot for slot in artifact.route_spec.slots] == [
        "event_semantics.primary",
        "reader_card.primary",
        "event_semantics.fallback",
        "reader_card.fallback",
    ]
    assert set(artifact.state()) == {"rule_packs", "learned_strategies", "demo_bank"}


def test_quality_kernel_hashes_every_package_owned_behavior_source_and_verdict_schema() -> None:
    artifact = build_code_owned_program_artifact_v2()
    news_root = importlib.resources.files("tracefold.news")
    sources = {
        "news/artifact_identity.py": ("artifact_identity.py",),
        "news/semantic_contract.py": ("semantic_contract.py",),
        "news/agents/quality_baseline.py": ("agents", "quality_baseline.py"),
        "news/agents/semantic_program.py": ("agents", "semantic_program.py"),
    }
    expected_source = canonical_sha(
        {name: hashlib.sha256(news_root.joinpath(*parts).read_bytes()).hexdigest() for name, parts in sources.items()}
    )

    assert artifact.quality_kernel.factory_source_sha256 == expected_source
    assert artifact.quality_kernel.verdict_contract_sha256 == canonical_sha(
        {
            "TriageAsset": TriageAsset.model_json_schema(),
            "TriageVerdict": TriageVerdict.model_json_schema(),
        }
    )
    assert artifact.quality_kernel.trade_relevance_contract_sha256 == canonical_sha(
        TradeRelevanceV1.model_json_schema()
    )
    assert artifact.quality_kernel.reader_card_semantic_view_sha256 == canonical_sha(
        ReaderCardSemanticView.model_json_schema()
    )
    assert artifact.quality_kernel.editorial_contract_sha256 == canonical_sha(EditorialEnvelope.model_json_schema())
    assert artifact.quality_kernel.semantic_judgment_contract_sha256 == canonical_sha(
        SemanticJudgment.model_json_schema()
    )
    assert artifact.quality_kernel.scored_judgment_contract_sha256 == canonical_sha(ScoredJudgment.model_json_schema())


def test_program_topology_identity_includes_the_deterministic_normalizer() -> None:
    expected = canonical_sha(
        {
            "nodes": ["event_semantics", "semantic_normalizer", "reader_card", "verdict_assembler"],
            "edges": [[0, 1], [1, 2], [2, 3]],
        }
    )
    assert expected == PROGRAM_TOPOLOGY_SHA256


def test_stable_artifact_encodes_the_restatement_index_contract() -> None:
    restates_schema = EventSemantics.model_json_schema()["properties"]["restates"]
    assert restates_schema["description"] == (
        "Visible event_status.told index if and only if novelty is restatement; -1 for new_fact or progression."
    )
    instruction = load_stable_program_artifact().event_semantics.instruction
    assert "progression: told covers the story but this event adds a material development" in instruction
    assert "restates=-1 even when it follows an earlier card" in instruction
    assert "Set restates to that visible i." in instruction


def test_packaged_dependency_lock_identity_matches_the_source_lock() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    assert hashlib.sha256((repository_root / "uv.lock").read_bytes()).hexdigest() == PROGRAM_DEPENDENCY_LOCK_SHA256


def test_built_wheel_loads_the_image_carried_program_without_a_repository_root(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("tracefold-*.whl"))
    unpacked = tmp_path / "unpacked-wheel"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(unpacked)
    installed_path = tmp_path / "installed-wheel"
    installed_path.symlink_to(unpacked, target_is_directory=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed_path)
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from tracefold.news.agents import semantic_program as module; "
                "print(Path(module.__file__).resolve()); "
                "print(module.load_stable_program_artifact().program_sha256)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr
    module_path, program_sha = loaded.stdout.splitlines()
    assert Path(module_path).is_relative_to(unpacked)
    assert program_sha == load_stable_program_artifact().program_sha256


def test_codec_rejects_noncanonical_documents_and_nonfinite_numbers() -> None:
    artifact = load_stable_program_artifact()
    manifest_document, state_document = ProgramArtifactCodec.encode(artifact)
    manifest = json.loads(manifest_document)
    state = json.loads(state_document)

    with pytest.raises(ValueError, match="manifest_json_noncanonical"):
        ProgramArtifactCodec.decode(json.dumps(manifest, indent=2), state_document)
    with pytest.raises(ValueError, match="state_json_noncanonical"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state) + "\n\n")

    manifest["route_spec"]["event_semantics_max_tokens"] = float("nan")
    with pytest.raises(ValueError, match="manifest_json_invalid"):
        ProgramArtifactCodec.decode(canonical_json(manifest), state_document)
    manifest["route_spec"]["event_semantics_max_tokens"] = float("inf")
    with pytest.raises(ValueError, match="manifest_json_invalid"):
        ProgramArtifactCodec.decode(canonical_json(manifest), state_document)


def test_codec_rejects_coercive_state_that_cannot_round_trip_exactly() -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["rule_packs"][0]["order"] = str(state["rule_packs"][0]["order"])

    with pytest.raises(ValueError, match="artifact_round_trip_mismatch"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update({"parent_program_sha256": "f" * 64}),
        lambda manifest: manifest["compile_receipt"].update({"accepted_by": "unaccepted_candidate"}),
        lambda manifest: manifest["compile_receipt"].update({"compiler": "another_compiler"}),
        lambda manifest: manifest["compile_receipt"].update({"source": "issue_134/unreviewed"}),
    ],
)
def test_baseline_parent_and_receipt_semantics_cannot_be_recombined(mutate: Any) -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    manifest = json.loads(manifest_document)
    mutate(manifest)
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )

    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(canonical_json(manifest), state_document)


def test_context_hides_audit_ids_from_both_predictors() -> None:
    context = _context(
        told_rows=[
            {
                "event_id": "told-secret",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "旧卡片",
            }
        ]
    )
    visible = json.dumps(context.event_semantics_payload(), ensure_ascii=False)
    assert "event-secret" not in visible
    assert "fact-secret" not in visible
    assert "told-secret" not in visible
    assert "a" * 64 not in visible
    assert context.told.entries[0].event_id == "told-secret"


def test_context_caps_every_variable_length_model_input() -> None:
    card = {
        "event_id": "event-capped",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-capped",
        "leader_title": "Bounded input",
        "opened_at_ms": 1_000_000,
        "storyline_key": "theme:bounded",
        "provenance": [f"strategy-{index}" for index in range(40)],
        "grounded_assets": [f"ASSET{index}" for index in range(40)],
    }
    context = TriageContext.from_card(
        card,
        watchlist=[f"WATCH{index}" for index in range(100)],
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=0,
    )

    assert context.evidence.strategies == tuple(f"strategy-{index}" for index in range(16))
    assert context.gate.grounded_assets == tuple(f"ASSET{index}" for index in range(16))
    assert context.watchlist == tuple(f"WATCH{index}" for index in range(64))
    payload = context.event_semantics_payload()
    assert len(payload["event"]["strategies"]) == 16
    assert len(payload["gate"]["grounded_assets"]) == 16
    assert "watchlist" not in payload["gate"]


def test_told_selection_is_candidate_conditioned_not_a_recency_quota() -> None:
    """The candidate's own storyline outranks recency, and nothing is reserved for unrelated recent cards."""

    rows = [
        {
            "event_id": f"same-{index}",
            "at_ms": 1_009_000 - index,
            "storyline_key": "asset:BTC",
            "magnitude": 1,
            "direction": "bullish",
            "headline_zh": "same",
        }
        for index in range(10)
    ] + [
        {
            "event_id": f"other-{index}",
            "at_ms": 1_009_900 - index,
            "storyline_key": "macro:general",
            "magnitude": 1,
            "direction": "neutral",
            "headline_zh": "other",
        }
        for index in range(10)
    ]
    entries = _context(told_rows=rows).told.entries
    assert len(entries) == TOLD_MAX
    # The candidate's own storyline comes first even though all ten unrelated cards are newer — but it is
    # capped, so the tiers below it can still be reached. Ranking storyline first with no cap scored below the
    # predecessor on the accepted corpus for exactly this reason.
    assert [entry.event_id for entry in entries[:TOLD_STORYLINE_TIER_MAX]] == [
        f"same-{index}" for index in range(TOLD_STORYLINE_TIER_MAX)
    ]
    assert all(entry.tier == "storyline" for entry in entries[:TOLD_STORYLINE_TIER_MAX])
    # The cap's overflow is offered the remaining slots before any recency filler: filler must never displace
    # evidence, or an unrelated delivery would change the evidence set and buy a second paid execution.
    assert [entry.event_id for entry in entries[TOLD_STORYLINE_TIER_MAX:10]] == ["same-8", "same-9"]
    assert [entry.tier for entry in entries[10:]] == ["recency"] * (TOLD_MAX - 10)
    assert [entry.i for entry in entries] == list(range(TOLD_MAX))


def test_reader_card_input_cannot_carry_told_history_at_all() -> None:
    """#138: the two Predictors used to receive one payload, so the copy step could re-read old cards.

    The boundary is the schema, not a prompt reminder: ``ModelVisibleCardInput`` has no ``event_status`` field
    and forbids extras, so a payload or a recorded demo that carries told history is rejected here.
    """

    context = _context(
        told_rows=[
            {
                "event_id": "told-secret",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "旧卡片",
            }
        ]
    )
    semantics_payload = context.event_semantics_payload()
    card_payload = context.reader_card_payload()

    assert semantics_payload["event_status"]["told"][0]["headline_zh"] == "旧卡片"
    assert set(card_payload) == {"event", "gate"}
    assert "旧卡片" not in json.dumps(card_payload, ensure_ascii=False)

    with pytest.raises(ValidationError):
        render_model_evidence_json(semantics_payload, predictor="reader_card")


def test_neither_predictor_ever_receives_an_audit_identity() -> None:
    context = _context(
        told_rows=[
            {
                "event_id": "told-secret",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "旧卡片",
            }
        ]
    )
    for payload in (context.event_semantics_payload(), context.reader_card_payload()):
        rendered = json.dumps(payload, ensure_ascii=False)
        assert "event-secret" not in rendered
        assert "fact-secret" not in rendered
        assert "told-secret" not in rendered
        assert "a" * 64 not in rendered
    # The selector keeps the identity for `news why` and `restates_event_id`, on the audit side only.
    assert context.told.entries[0].event_id == "told-secret"


def test_neither_predictor_receives_queue_provider_macro_watchlist_or_lag_hints() -> None:
    context = _context()
    semantics = context.event_semantics_payload()
    card = context.reader_card_payload()

    for payload in (semantics, card):
        assert "provider_score" not in payload["event"]
        assert "queue_priority" not in payload["event"]
        assert "macro_lexicon" not in payload["gate"]
        assert "watchlist" not in payload["gate"]
    assert "queue_lag_s" not in semantics["event_status"]


def test_selected_context_sha_moves_only_when_the_model_visible_selection_moves() -> None:
    rows = [
        {
            "event_id": f"e{index}",
            "at_ms": 1_009_000 - index * 1_000,
            "storyline_key": "asset:BTC",
            "magnitude": 1,
            "direction": "bullish",
            "headline_zh": f"卡 {index}",
        }
        for index in range(3)
    ]
    base = _context(told_rows=rows).selected_context_sha256()
    assert _context(told_rows=list(reversed(rows))).selected_context_sha256() == base
    grew = [*rows, dict(rows[0], event_id="new", at_ms=1_009_500)]
    assert _context(told_rows=grew).selected_context_sha256() != base


def test_two_predictors_assemble_one_verdict_and_trace_usage() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            PredictorResponse(
                output=_semantics(),
                model="semantic-model",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                provider_cost_microusd=17,
            ),
            PredictorResponse(
                output=_card(),
                model="reader-model",
                input_tokens=12,
                output_tokens=6,
                total_tokens=18,
                provider_cost_microusd=19,
            ),
        ]
    )
    judgment = asyncio.run(_program(adapter).judge(_context()))
    assert judgment.verdict.title_zh == ""
    assert judgment.answering_model == "reader-model"
    assert judgment.usage.call_count == 2
    assert judgment.usage.physical_call_count == 2
    assert judgment.usage.total_tokens == 33
    assert judgment.usage.provider_cost_microusd == 36
    assert [call.predictor for call in judgment.trace.calls] == ["event_semantics", "reader_card"]
    assert judgment.trace.calls[0].validated_output == _semantics()
    assert judgment.trace.calls[1].upstream_sha256 == judgment.trace.event_semantics_sha256
    assert judgment.trace.editorial_sha256 == judgment.editorial.editorial_sha256
    assert judgment.scored().verdict == judgment.verdict
    assert judgment.scored().editorial == judgment.editorial


def test_trade_relevance_is_canonicalized_and_reader_card_gets_only_its_view() -> None:
    raw = _semantics(
        relevance={
            "impact_breadth": "regional",
            "tradability": "direct",
            "surprise": "unknown",
            "development_delta": "material_detail",
            "channels": ["regulation", "rates", "regulation"],
            "affected_markets": ["single_asset", "rates", "single_asset"],
            "reader_value": "realtime",
        }
    )
    adapter = ScriptedPredictorAdapter([raw, _card()])

    judgment = asyncio.run(_program(adapter).judge(_context()))

    relevance = judgment.editorial.relevance
    assert relevance is not None
    assert relevance.channels == ("rates", "regulation")
    assert relevance.affected_markets == ("rates", "single_asset")
    assert judgment.verdict.decision == "push"
    assert judgment.verdict.actionable is True
    assert [item.model_dump(mode="json") for item in judgment.trace.calls[0].normalizations] == [
        {
            "normalizer_id": "semantic_normalizer_v2",
            "field": "channels",
            "reason": "canonical_set_order",
            "input_value": ["regulation", "rates", "regulation"],
            "output_value": ["rates", "regulation"],
        },
        {
            "normalizer_id": "semantic_normalizer_v2",
            "field": "affected_markets",
            "reason": "canonical_set_order",
            "input_value": ["single_asset", "rates", "single_asset"],
            "output_value": ["rates", "single_asset"],
        },
    ]
    reader_view = json.loads(adapter.requests[1].inputs["semantics_json"])
    assert set(reader_view) == {
        "event_type",
        "assets",
        "direction",
        "magnitude",
        "novelty",
        "restates",
        "scope",
        "channels",
        "affected_markets",
    }
    assert not {"reader_value", "tradability", "surprise", "development_delta", "confidence", "audience"} & set(
        reader_view
    )


def test_trade_relevance_rejects_unknown_or_more_than_four_unique_codes() -> None:
    base = _semantics()["relevance"]

    with pytest.raises(ValidationError):
        TradeRelevanceV1.model_validate({**base, "channels": ["rates", "unknown_channel"]})
    with pytest.raises(ValidationError):
        TradeRelevanceV1.model_validate(
            {
                **base,
                "channels": [
                    "rates",
                    "liquidity",
                    "risk_premium",
                    "energy_supply",
                    "commodity_supply",
                ],
            }
        )


def test_assembler_derives_legacy_intent_and_actionability_from_relevance() -> None:
    background = _semantics(
        relevance={
            "impact_breadth": "regional",
            "tradability": "contextual",
            "surprise": "in_line",
            "development_delta": "color_only",
            "channels": [],
            "affected_markets": [],
            "reader_value": "background",
        }
    )
    judgment = asyncio.run(_program(ScriptedPredictorAdapter([background, _card()])).judge(_context()))

    assert judgment.verdict.decision == "drop"
    assert judgment.verdict.actionable is False
    with pytest.raises(ValidationError):
        EventSemantics.model_validate({**background, "decision": "push"})
    with pytest.raises(ValidationError):
        EventSemantics.model_validate({**background, "actionable": True})


def test_reader_card_schema_omits_title_but_public_verdict_keeps_empty_sentinel() -> None:
    reader_state = load_stable_program_artifact().reader_card
    assert reader_state.signature_id == "tracefold.news.ReaderCard.v2"
    assert reader_state.signature_sha256 == READER_CARD_SIGNATURE_SHA256
    assert "title_zh" not in ReaderCard.model_json_schema()["properties"]

    judgment = asyncio.run(_program(ScriptedPredictorAdapter([_semantics(), _card()])).judge(_context()))

    assert judgment.verdict.title_zh == ""
    reader_output = judgment.trace.calls[1].validated_output
    assert reader_output is not None
    assert "title_zh" not in reader_output


def test_codec_rejects_superseded_reader_card_signature_identity() -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    manifest = json.loads(manifest_document)
    manifest["quality_kernel"].update(
        {
            "reader_card_signature_id": "tracefold.news.ReaderCard.v1",
            "reader_card_signature_sha256": canonical_sha(
                {
                    "signature": "ReaderCard.v1",
                    "inputs": ["evidence_json", "semantics_json"],
                    "outputs": ["card"],
                }
            ),
        }
    )
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )

    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(canonical_json(manifest), state_document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"program_sha256": "f" * 64}),
        lambda value: value["trace"].update({"verdict_sha256": "f" * 64}),
        lambda value: value["usage"].update({"total_tokens": 999}),
        lambda value: value.update({"fallback_from": "forged"}),
    ],
)
def test_semantic_judgment_rejects_verdict_trace_and_usage_divergence(mutate: Any) -> None:
    judgment = asyncio.run(_program(ScriptedPredictorAdapter([_semantics(), _card()])).judge(_context()))
    data = judgment.model_dump(mode="json")
    mutate(data)

    with pytest.raises(ValueError, match=r"judgment_(trace_identity|usage)_mismatch"):
        type(judgment).model_validate(data)


def test_partial_provider_cost_never_looks_like_complete_program_cost() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            PredictorResponse(output=_semantics(), provider_cost_microusd=17),
            PredictorResponse(output=_card(), provider_cost_microusd=None),
        ]
    )

    judgment = asyncio.run(_program(adapter).judge(_context()))

    assert judgment.trace.calls[0].provider_cost_microusd == 17
    assert judgment.trace.calls[1].provider_cost_microusd is None
    assert judgment.usage.provider_cost_microusd is None


def test_one_retry_is_shared_by_route_and_fallback_restarts_graph() -> None:
    primary = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    fallback = ScriptedPredictorAdapter([_semantics(direction="neutral"), _card()])
    judgment = asyncio.run(_program(primary, fallback=fallback).judge(_context()))
    assert judgment.fallback_from == "news_program_reader_card_invalid"
    assert [request.predictor for request in primary.requests] == [
        "event_semantics",
        "reader_card",
        "reader_card",
    ]
    assert [request.predictor for request in fallback.requests] == ["event_semantics", "reader_card"]
    assert judgment.usage.call_count == 5


@pytest.mark.parametrize("novelty", ["new_fact", "progression"])
def test_non_restatement_index_is_normalized_before_reader_without_retry(novelty: str) -> None:
    adapter = ScriptedPredictorAdapter(
        [
            _semantics(novelty=novelty, restates=0),
            _card(),
        ]
    )
    context = _context(
        told_rows=[
            {
                "event_id": "prior-card",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "比特币此前进展",
            }
        ]
    )

    judgment = asyncio.run(_program(adapter).judge(context))

    assert [(request.predictor, request.attempt) for request in adapter.requests] == [
        ("event_semantics", 1),
        ("reader_card", 1),
    ]
    assert judgment.usage.call_count == 2
    assert judgment.usage.physical_call_count == 2
    assert judgment.verdict.novelty == novelty
    assert judgment.verdict.restates == -1
    semantic_call = judgment.trace.calls[0]
    assert semantic_call.validated_output == _semantics(novelty=novelty, restates=0)
    assert [normalization.model_dump(mode="json") for normalization in semantic_call.normalizations] == [
        {
            "normalizer_id": "semantic_normalizer_v2",
            "field": "restates",
            "reason": "non_restatement_index_ignored",
            "input_value": 0,
            "output_value": -1,
        }
    ]
    assert semantic_call.output_sha256 == canonical_sha(_semantics(novelty=novelty, restates=0))
    assert judgment.trace.event_semantics_sha256 == canonical_sha(_semantics(novelty=novelty, restates=-1))
    normalized = _semantics(novelty=novelty, restates=-1)
    assert adapter.requests[1].inputs["semantics_json"] == canonical_json(_reader_semantics(normalized))


def test_valid_restatement_preserves_visible_index_without_normalization() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            _semantics(novelty="restatement", restates=0),
            _card(),
        ]
    )
    context = _context(
        told_rows=[
            {
                "event_id": "prior-card",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "比特币此前进展",
            }
        ]
    )

    judgment = asyncio.run(_program(adapter).judge(context))

    assert judgment.verdict.restates == 0
    assert judgment.trace.calls[0].normalizations == ()
    assert [request.predictor for request in adapter.requests] == ["event_semantics", "reader_card"]


@pytest.mark.parametrize("restates", [None, 1])
def test_exhausted_invalid_restatement_retry_never_calls_reader_card(restates: int | None) -> None:
    invalid_semantics = _semantics(novelty="restatement", restates=restates)
    if restates is None:
        invalid_semantics.pop("restates")
    adapter = ScriptedPredictorAdapter(
        [
            invalid_semantics,
            invalid_semantics,
            _card(),
        ]
    )

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(_program(adapter).judge(_context()))

    assert caught.value.code == "news_program_restatement_index_invalid"
    assert [(request.predictor, request.attempt) for request in adapter.requests] == [
        ("event_semantics", 1),
        ("event_semantics", 2),
    ]


def test_chain_budget_is_six_calls_and_reports_partial_trace() -> None:
    primary = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    fallback = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(_program(primary, fallback=fallback).judge(_context()))
    assert caught.value.attempts == 6
    assert caught.value.output_failure is True
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 6


def test_truncation_never_fast_retries() -> None:
    adapter = ScriptedPredictorAdapter([PredictorResponse(output={"bad": True}, finish_reason="length"), _semantics()])
    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(_program(adapter).judge(_context()))
    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.attempts == 1
    assert len(adapter.requests) == 1


def test_missing_novelty_defaults_only_after_retry_exhaustion() -> None:
    missing = _semantics()
    missing.pop("novelty")
    adapter = ScriptedPredictorAdapter([missing, missing, _card()])
    judgment = asyncio.run(_program(adapter).judge(_context()))
    assert judgment.verdict.novelty == "new_fact"
    assert judgment.trace.novelty_defaulted is True
    assert judgment.usage.call_count == 3


def test_record_replay_uses_full_request_identity_and_real_assembler() -> None:
    scripted = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(_program(scripted).judge(_context()))
    responses = [
        PredictorResponse(
            output=_semantics(),
            model="resolved-replay-sem",
            runtime_binding_sha256=scripted.requests[0].runtime_binding_sha256,
        ),
        PredictorResponse(
            output=_card(),
            model="resolved-replay-card",
            runtime_binding_sha256=scripted.requests[1].runtime_binding_sha256,
        ),
    ]
    recordings = {
        request.request_sha256: {
            "request": {**request.model_dump(mode="json"), "request_sha256": request.request_sha256},
            "response": response.model_dump(mode="json"),
        }
        for request, response in zip(scripted.requests, responses, strict=True)
    }
    replay = RecordReplayPredictorAdapter(recordings)
    repeated = asyncio.run(
        DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=replay).judge(_context())
    )
    assert repeated.verdict == original.verdict
    assert repeated.answering_model == "resolved-replay-card"
    assert [request.request_sha256 for request in replay.requests] == list(recordings)

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(
                load_stable_program_artifact(), primary_adapter=RecordReplayPredictorAdapter({})
            ).judge(_context())
        )
    assert caught.value.code == "news_program_recording_missing"


def test_output_failure_does_not_open_primary_transport_breaker() -> None:
    artifact = _artifact_with_execution(primary_breaker_failures=1)
    primary = ScriptedPredictorAdapter([{"bad": True}, {"bad": True}, _semantics(), _card()])
    fallback = ScriptedPredictorAdapter([_semantics(), _card()])
    program = _program(primary, fallback=fallback, artifact=artifact)
    first = asyncio.run(program.judge(_context()))
    second = asyncio.run(program.judge(_context()))
    assert first.fallback_from == "news_program_event_semantics_invalid"
    assert second.fallback_from is None
    assert second.trace.answering_route == "primary"
    assert len(primary.requests) == 4


def test_transport_failure_opens_only_instance_local_primary_breaker() -> None:
    artifact = _artifact_with_execution(primary_breaker_failures=1)
    primary = ScriptedPredictorAdapter(
        [
            PredictorAdapterError("provider_busy", retryable=True),
            PredictorAdapterError("provider_busy", retryable=True),
        ]
    )
    fallback = ScriptedPredictorAdapter([_semantics(), _card(), _semantics(), _card()])
    program = _program(primary, fallback=fallback, artifact=artifact)
    first = asyncio.run(program.judge(_context()))
    second = asyncio.run(program.judge(_context()))
    assert first.fallback_from == "provider_busy"
    assert second.fallback_from == "primary_circuit_open"
    assert len(primary.requests) == 2
    assert second.usage.call_count == 2
    assert second.usage.physical_call_count == 2
    assert all(call.physical_provider_call for call in second.trace.calls)

    isolated_primary = ScriptedPredictorAdapter([_semantics(), _card()])
    isolated = asyncio.run(_program(isolated_primary, artifact=artifact).judge(_context()))
    assert isolated.trace.answering_route == "primary"


@pytest.mark.parametrize(
    "error_type",
    [
        dspy.LMTransportError,
        dspy.LMServerError,
        dspy.LMTimeoutError,
        dspy.LMRateLimitError,
    ],
)
def test_dspy_transient_lm_errors_are_retryable(error_type: type[Exception]) -> None:
    assert semantic_program_module._is_retryable_exception(error_type("provider failed")) is True


@pytest.mark.parametrize(
    "error_type",
    [
        dspy.LMAuthError,
        dspy.LMInvalidRequestError,
        dspy.ContextWindowExceededError,
    ],
)
def test_dspy_permanent_lm_errors_are_not_retryable(error_type: type[Exception]) -> None:
    assert semantic_program_module._is_retryable_exception(error_type(message="request rejected")) is False


def test_usage_distinguishes_synthetic_trace_entries_from_provider_attempts() -> None:
    class UnresolvedIdentityAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            del model_binding
            raise PredictorAdapterError("identity_unavailable")

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            raise AssertionError("invoke must not run")

    judgment = asyncio.run(
        DspyNewsSemanticProgram(
            load_stable_program_artifact(),
            primary_adapter=UnresolvedIdentityAdapter(),
            fallback_adapter=ScriptedPredictorAdapter([_semantics(), _card()]),
        ).judge(_context())
    )

    assert judgment.usage.call_count == 3
    assert judgment.usage.physical_call_count == 2
    assert [call.physical_provider_call for call in judgment.trace.calls] == [False, True, True]
    forged = judgment.trace.calls[1].model_dump(mode="json")
    forged["physical_provider_call"] = False
    with pytest.raises(ValueError, match="synthetic_call_provider_usage_invalid"):
        semantic_program_module.ProgramCallTrace.model_validate(forged)


def test_transport_error_trace_has_elapsed_time_without_forged_provider_metadata() -> None:
    class SlowTransportAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(0.002)
            raise ConnectionError("provider unavailable")

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=SlowTransportAdapter()).judge(
                _context()
            )
        )

    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 2
    for call in caught.value.partial_trace.calls:
        assert call.latency_ms > 0
        assert call.provider is None
        assert call.model is None
        assert call.provider_cost_microusd is None


def test_per_predictor_adapter_bindings_are_explicit() -> None:
    semantics_adapter = ScriptedPredictorAdapter([_semantics()], model_name="semantic-only")
    reader_adapter = ScriptedPredictorAdapter([_card()], model_name="reader-only")
    judgment = asyncio.run(
        DspyNewsSemanticProgram(
            load_stable_program_artifact(),
            primary_adapter={
                "event_semantics.primary": semantics_adapter,
                "reader_card.primary": reader_adapter,
            },
        ).judge(_context())
    )
    assert judgment.answering_model == "reader-only"
    assert len(semantics_adapter.requests) == len(reader_adapter.requests) == 1


def test_route_deadline_is_shared_and_audited() -> None:
    class SlowAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(2)
            return PredictorResponse(output=_semantics())

    artifact = _artifact_with_execution(route_deadline_seconds=1)
    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=SlowAdapter()).judge(_context()))
    assert caught.value.code == "news_program_route_deadline"
    assert caught.value.retryable is True
    assert caught.value.attempts == 1
    assert caught.value.partial_trace is not None
    assert caught.value.partial_trace.calls[0].error_code == "news_program_route_deadline"
    assert caught.value.partial_trace.calls[0].latency_ms >= 900


def test_reader_card_deadline_is_attributed_to_reader_predictor() -> None:
    class SlowReaderAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(2)
            return PredictorResponse(output=_card())

    artifact = _artifact_with_execution(route_deadline_seconds=1)
    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(
                artifact,
                primary_adapter={
                    "event_semantics.primary": ScriptedPredictorAdapter([_semantics()]),
                    "reader_card.primary": SlowReaderAdapter(),
                },
            ).judge(_context())
        )
    assert caught.value.partial_trace is not None
    assert [call.predictor for call in caught.value.partial_trace.calls] == [
        "event_semantics",
        "reader_card",
    ]
    assert caught.value.partial_trace.calls[-1].error_code == "news_program_route_deadline"
    assert caught.value.partial_trace.calls[-1].latency_ms >= 900


def test_runtime_lm_factory_rejects_retry_or_cache_override() -> None:
    with pytest.raises(ValueError, match="runtime_model_kwargs_owned"):
        DspyPredictorAdapter.from_runtime(
            model_name="openai/test",
            api_key="secret",
            api_base="https://provider.invalid/v1",
            timeout=5,
            max_tokens=100,
            model_kwargs={"cache": True},
        )


def test_dspy_adapter_fails_closed_without_an_exact_provider_response() -> None:
    class FakePrediction:
        def toDict(self) -> dict[str, Any]:
            return _semantics()

        def get_lm_usage(self) -> dict[str, dict[str, Any]]:
            return {
                "openai/test": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "response_cost": 0.000012,
                }
            }

    class FakePredictor:
        async def acall(self, **inputs: Any) -> FakePrediction:
            assert inputs == {"evidence_json": "{}"}
            return FakePrediction()

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )
    with pytest.raises(PredictorAdapterError, match="provider_metadata_unavailable"):
        asyncio.run(adapter.invoke(request, FakePredictor()))  # type: ignore[arg-type]


def test_dspy_adapter_observes_each_exact_33_provider_response_under_concurrency() -> None:
    """Provider metadata belongs to the in-flight call, never shared ``LM.history[-1]``."""

    from litellm import ModelResponse

    class FakePrediction:
        def __init__(self, marker: str) -> None:
            self._marker = marker

        def toDict(self) -> dict[str, Any]:
            return _semantics(event_type=self._marker)

        def get_lm_usage(self) -> dict[str, Any]:
            # DSPy usage aggregation intentionally omits finish reason and cost.
            return {}

    class FakePredictor:
        async def acall(self, **inputs: Any) -> FakePrediction:
            marker = str(inputs["evidence_json"])
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": marker}])
            return FakePrediction(marker)

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, kwargs
        marker = str(messages[0]["content"])
        if marker == "slow":
            await asyncio.sleep(0.02)
        tokens = 11 if marker == "slow" else 23
        response = ModelResponse(
            model=f"resolved-{marker}",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "length" if marker == "slow" else "stop",
                }
            ],
            usage={
                "prompt_tokens": tokens,
                "completion_tokens": 5,
                "total_tokens": tokens + 5,
                "prompt_tokens_details": {"cached_tokens": tokens - 10},
            },
        )
        response._hidden_params["response_cost"] = tokens / 1_000_000
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]

    def request(marker: str) -> PredictorRequest:
        return PredictorRequest(
            program_version="test",
            program_sha256="a" * 64,
            context_sha256=canonical_sha(marker),
            predictor="event_semantics",
            route="primary",
            attempt=1,
            signature_sha256="c" * 64,
            instruction_sha256="d" * 64,
            demos_sha256="e" * 64,
            adapter_sha256="f" * 64,
            model_binding="event_semantics.primary",
            runtime_provider="openai",
            runtime_model="openai/test",
            runtime_model_sha256=canonical_sha({"provider": "openai", "model": "openai/test"}),
            runtime_binding_sha256=canonical_sha(
                {
                    "provider": "openai",
                    "model": "openai/test",
                    "model_sha256": canonical_sha({"provider": "openai", "model": "openai/test"}),
                }
            ),
            inputs={"evidence_json": marker},
        )

    async def run() -> tuple[PredictorResponse, PredictorResponse]:
        slow, fast = await asyncio.gather(
            adapter.invoke(request("slow"), FakePredictor()),  # type: ignore[arg-type]
            adapter.invoke(request("fast"), FakePredictor()),  # type: ignore[arg-type]
        )
        return slow, fast

    slow, fast = asyncio.run(run())
    assert (slow.finish_reason, slow.input_tokens, slow.cached_tokens, slow.provider_cost_microusd) == (
        "length",
        11,
        1,
        11,
    )
    assert (fast.finish_reason, fast.input_tokens, fast.cached_tokens, fast.provider_cost_microusd) == (
        "stop",
        23,
        13,
        23,
    )


def test_request_and_strict_replay_are_bound_to_one_runtime_model_identity() -> None:
    model_a_sha = canonical_sha({"provider": "openai", "model": "model-a"})
    model_b_sha = canonical_sha({"provider": "openai", "model": "model-b"})
    shared = {
        "program_version": "test",
        "program_sha256": "a" * 64,
        "context_sha256": "b" * 64,
        "predictor": "event_semantics",
        "route": "primary",
        "attempt": 1,
        "signature_sha256": "c" * 64,
        "instruction_sha256": "d" * 64,
        "demos_sha256": "e" * 64,
        "adapter_sha256": "f" * 64,
        "model_binding": "event_semantics.primary",
        "runtime_provider": "openai",
        "inputs": {"evidence_json": "{}"},
    }
    request_a = PredictorRequest(
        **shared,
        runtime_model="model-a",
        runtime_model_sha256=model_a_sha,
        runtime_binding_sha256=canonical_sha({"provider": "openai", "model": "model-a", "model_sha256": model_a_sha}),
    )
    request_b = PredictorRequest(
        **shared,
        runtime_model="model-b",
        runtime_model_sha256=model_b_sha,
        runtime_binding_sha256=canonical_sha({"provider": "openai", "model": "model-b", "model_sha256": model_b_sha}),
    )
    assert request_a.request_sha256 != request_b.request_sha256

    response_a = PredictorResponse(
        output=_semantics(),
        provider="openai",
        model="model-a",
        model_sha256=model_a_sha,
        runtime_binding_sha256=request_a.runtime_binding_sha256,
    )
    replay = RecordReplayPredictorAdapter(
        {
            request_a.request_sha256: {
                "request": {**request_a.model_dump(mode="json"), "request_sha256": request_a.request_sha256},
                "response": response_a.model_dump(mode="json"),
            }
        }
    )
    assert asyncio.run(replay.invoke(request_a, object())) == response_a  # type: ignore[arg-type]
    with pytest.raises(PredictorAdapterError, match="recording_missing"):
        asyncio.run(replay.invoke(request_b, object()))  # type: ignore[arg-type]

    mismatched = response_a.model_copy(update={"runtime_binding_sha256": request_b.runtime_binding_sha256})
    with pytest.raises(ValueError, match="recording_model_identity_mismatch"):
        RecordReplayPredictorAdapter(
            {
                request_a.request_sha256: {
                    "request": {**request_a.model_dump(mode="json"), "request_sha256": request_a.request_sha256},
                    "response": mismatched.model_dump(mode="json"),
                }
            }
        )


def test_real_dspy_33_finish_reason_classifies_parse_failure_as_truncation() -> None:
    from dspy.utils.exceptions import AdapterParseError
    from litellm import ModelResponse

    class TruncatedPredictor:
        async def acall(self, **inputs: Any) -> Any:
            del inputs
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": "truncated"}])
            partial = _semantics()
            partial.pop("novelty")
            raise AdapterParseError(
                "DspyStrictJSONAdapter",
                dspy.Signature("evidence_json -> semantics"),
                '{"semantics":',
                parsed_result={"semantics": partial},  # type: ignore[arg-type]
            )

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        response = ModelResponse(
            model="resolved-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"semantics":'},
                    "finish_reason": "length",
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.00001
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )
    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(request, TruncatedPredictor()))  # type: ignore[arg-type]
    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.finish_reason == "length"
    observation = caught.value.provider_observation
    assert isinstance(observation, ProviderCallObservation)
    assert (observation.input_tokens, observation.output_tokens, observation.total_tokens) == (7, 3, 10)
    assert observation.provider_cost_microusd == 10
    assert observation.finish_reason == "length"
    assert caught.value.partial_output is not None
    assert adapter._lm.history == []

    reader = ScriptedPredictorAdapter([_card()])
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter={
            "event_semantics.primary": adapter,
            "reader_card.primary": reader,
        },
    )
    program.event_semantics = TruncatedPredictor()  # type: ignore[assignment]
    with pytest.raises(SemanticJudgeError) as program_error:
        asyncio.run(program.judge(_context()))
    assert program_error.value.attempts == 1
    assert program_error.value.code == "news_program_output_truncated"
    assert program_error.value.partial_trace is not None
    assert program_error.value.partial_trace.novelty_defaulted is False
    assert reader.requests == []


@pytest.mark.parametrize(("finish_reason", "expected_attempts"), [("stop", 2), ("length", 1)])
def test_real_dspy_parse_failure_trace_keeps_exact_usage_and_truncation_does_not_retry(
    finish_reason: str,
    expected_attempts: int,
) -> None:
    from litellm import ModelResponse

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        await asyncio.sleep(0.002)
        response = ModelResponse(
            model="resolved-parse-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": finish_reason,
                }
            ],
            usage={"prompt_tokens": 13, "completion_tokens": 2, "total_tokens": 15},
        )
        response._hidden_params["response_cost"] = 0.000012
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    program = DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=adapter)

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(program.judge(_context()))

    assert caught.value.attempts == expected_attempts
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == expected_attempts
    for call in caught.value.partial_trace.calls:
        assert call.provider == "openai"
        assert call.model == "resolved-parse-test"
        assert call.model_sha256 == canonical_sha({"provider": "openai", "model": "resolved-parse-test"})
        assert call.latency_ms > 0
        assert (call.input_tokens, call.output_tokens, call.total_tokens) == (13, 2, 15)
        assert call.provider_cost_microusd == 12
        assert call.finish_reason == finish_reason
    assert adapter._lm.history == []


def test_real_adapter_partial_semantics_defaults_novelty_only_after_retry_is_exhausted() -> None:
    from dspy.utils.exceptions import AdapterParseError
    from litellm import ModelResponse

    class PartialSemanticsPredictor:
        async def acall(self, **inputs: Any) -> Any:
            del inputs
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": "partial"}])
            partial = _semantics()
            partial.pop("novelty")
            raise AdapterParseError(
                "DspyStrictJSONAdapter",
                dspy.Signature("evidence_json -> semantics"),
                "safe partial result",
                parsed_result={"semantics": partial},  # type: ignore[arg-type]
            )

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        response = ModelResponse(
            model="resolved-partial-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.000007
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    reader = ScriptedPredictorAdapter([_card()])
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter={
            "event_semantics.primary": adapter,
            "reader_card.primary": reader,
        },
    )
    program.event_semantics = PartialSemanticsPredictor()  # type: ignore[assignment]

    judgment = asyncio.run(program.judge(_context()))

    assert judgment.verdict.novelty == "new_fact"
    assert judgment.verdict.restates == -1
    assert judgment.trace.novelty_defaulted is True
    assert judgment.usage.call_count == 3
    assert judgment.usage.provider_cost_microusd is None
    assert [call.error_code for call in judgment.trace.calls[:2]] == [
        "news_program_dspy_output_adapterparseerror",
        "news_program_novelty_defaulted",
    ]
    assert all(call.provider_cost_microusd == 7 for call in judgment.trace.calls[:2])
    assert adapter._lm.history == []


def test_real_dspy_33_predict_path_returns_exact_response_metadata() -> None:
    from litellm import ModelResponse

    class Signature(dspy.Signature):
        evidence_json: str = dspy.InputField()
        semantics: dict[str, Any] = dspy.OutputField()

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/configured-alias",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages
        assert kwargs["response_format"] == {"type": "json_object"}
        response = ModelResponse(
            model="resolved-model-2026-08-22",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps({"semantics": {"ok": True}})},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.000009
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )

    response = asyncio.run(adapter.invoke(request, dspy.Predict(Signature)))
    assert response.output == {"semantics": {"ok": True}}
    assert response.model == "resolved-model-2026-08-22"
    assert (response.finish_reason, response.input_tokens, response.total_tokens) == ("stop", 8, 10)
    assert response.provider_cost_microusd == 9
    assert response.runtime_binding_sha256 == request.runtime_binding_sha256


def test_artifact_rejects_tamper_unknown_binding_and_unregistered_identity(tmp_path: Path) -> None:
    artifact = load_stable_program_artifact()
    manifest_document, state_document = ProgramArtifactCodec.encode(artifact)
    state = json.loads(state_document)
    manifest = json.loads(manifest_document)
    manifest["route_spec"]["slots"][0]["endpoint"] = "https://evil.invalid/model"
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )
    with pytest.raises(ValueError, match="unsafe_state_key"):
        ProgramArtifactCodec.decode(canonical_json(manifest), canonical_json(state))
    with pytest.raises(ValueError, match="not_registered"):
        load_program_artifact("0" * 64)
    with pytest.raises(ValueError, match="path_invalid"):
        ProgramArtifactCodec.load("../not-an-image")

    image = tmp_path / artifact.program_sha256
    image.mkdir()
    (image / "manifest.json").write_text(manifest_document, encoding="utf-8")
    (image / "state.json").write_text(state_document, encoding="utf-8")
    (image / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="files_invalid"):
        ProgramArtifactCodec.load(str(image))

    symlink_root = tmp_path / "symlink-case"
    symlink_image = symlink_root / artifact.program_sha256
    symlink_image.mkdir(parents=True)
    manifest_source = symlink_root / "reviewed-manifest.json"
    manifest_source.write_text(manifest_document, encoding="utf-8")
    (symlink_image / "manifest.json").symlink_to(manifest_source)
    (symlink_image / "state.json").write_text(state_document, encoding="utf-8")
    with pytest.raises(ValueError, match="files_invalid"):
        ProgramArtifactCodec.load(str(symlink_image))


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "History",
        "request-callback",
        "clientCredential",
        "auth",
        "Authorization",
        "httpHeaders",
        "providerEndpoint",
        "apiBase",
        "baseURL",
        "clientSecret",
        "accessToken",
        "password",
        "modelList",
    ],
)
def test_artifact_recursively_rejects_runtime_and_secret_key_variants(unsafe_key: str) -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["demo_bank"]["nestedRuntimeState"] = {unsafe_key: "must-not-load"}

    with pytest.raises(ValueError, match="unsafe_state_key"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


@pytest.mark.parametrize(
    "secret",
    [
        "sk-1234567890abcdefghijklmnop",
        "github_pat_1234567890abcdefghijklmnop",
        "-".join(("xoxb", "1234567890", "abcdefghijklmnop")),
        "Bearer abcdefghijklmnopqrstuvwxyz.123456",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_artifact_recursively_rejects_high_confidence_secret_values(secret: str) -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["learned_strategies"][0]["text"] = secret
    state["learned_strategies"][0]["text_sha256"] = canonical_sha(secret)

    with pytest.raises(ValueError, match="secret_value"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


def test_artifact_dlp_rejects_secret_in_a_mapping_key_without_echoing_it() -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    secret = "sk-1234567890abcdefghijklmnop"
    state[secret] = "must-not-load"

    with pytest.raises(ValueError, match="secret_value") as caught:
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))
    assert secret not in str(caught.value)


def test_demo_dlp_allows_ordinary_api_key_news_without_a_credential_value() -> None:
    payload = _context().event_semantics_payload()
    payload["event"]["content"] = "The company updated its API key rotation policy; no credential was disclosed."

    record = DemoRecord.issue(
        predictor="event_semantics",
        signature_inputs={"evidence_json": render_model_evidence_json(payload, predictor="event_semantics")},
        validated_output=_semantics(),
        source_kind="accepted_development",
        development_dataset_sha256="1" * 64,
        case_sha256="2" * 64,
        cluster_sha256="3" * 64,
        review_sha256="4" * 64,
        evidence_receipt_sha256="5" * 64,
    )

    assert "API key" in record.signature_inputs["evidence_json"]


def _accepted_event_demo() -> DemoRecord:
    return DemoRecord.issue(
        predictor="event_semantics",
        signature_inputs={
            "evidence_json": render_model_evidence_json(
                _context().event_semantics_payload(), predictor="event_semantics"
            )
        },
        validated_output=_semantics(),
        source_kind="accepted_development",
        development_dataset_sha256="1" * 64,
        case_sha256="2" * 64,
        cluster_sha256="3" * 64,
        review_sha256="4" * 64,
        evidence_receipt_sha256="5" * 64,
    )


def test_demo_record_is_typed_delimited_and_contains_only_hashed_provenance() -> None:
    record = _accepted_event_demo()

    assert record.case_sha256 == "2" * 64
    assert "case_id" not in record.model_dump()
    assert "event-secret" not in canonical_json(record.model_dump(mode="json"))
    assert record.dspy_demo()["evidence_json"].startswith("<tracefold-untrusted-event-json-v1>\n")

    payload = record.model_dump(mode="json")
    payload["case_id"] = "private-case-id"
    with pytest.raises(ValueError):
        DemoRecord.model_validate(payload)

    with pytest.raises(ValueError, match="delimiter_invalid"):
        DemoRecord.issue(
            predictor="event_semantics",
            signature_inputs={"evidence_json": canonical_json(_context().event_semantics_payload())},
            validated_output=_semantics(),
            source_kind="code_owned_expert",
        )


def test_demo_bank_separates_full_eligible_root_from_selected_records() -> None:
    record = _accepted_event_demo()
    eligible = EligibleDemoBank.issue((record,))
    selected_root = canonical_sha([record.model_dump(mode="json")])
    bank = DemoBank(
        records=(record,),
        refs=DemoRefOrder(event_semantics=(record.demo_id,)),
        selected_record_root_sha256=selected_root,
        eligible_demo_bank_root_sha256=eligible.eligible_demo_bank_root_sha256,
    )

    assert bank.selected_record_root_sha256 == selected_root
    assert bank.eligible_demo_bank_root_sha256 == eligible.eligible_demo_bank_root_sha256
    with pytest.raises(ValueError, match="unselected_record"):
        DemoBank(
            records=(record,),
            refs=DemoRefOrder(),
            selected_record_root_sha256=selected_root,
            eligible_demo_bank_root_sha256=eligible.eligible_demo_bank_root_sha256,
        )


def test_codec_hard_cuts_artifact_v1_before_schema_loading() -> None:
    manifest = {
        "schema_version": "news_semantic_program_artifact_v1",
        "program_sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="artifact_version_unsupported"):
        ProgramArtifactCodec.decode(canonical_json(manifest), canonical_json({}))


def test_optimizer_extractor_changes_only_strategies_and_rejects_demos() -> None:
    parent = load_stable_program_artifact()
    record = _accepted_event_demo()
    eligible = EligibleDemoBank.issue((record,))
    cold = DspyCompileProgram(parent)
    assert cold.event_semantics.signature.instructions == ""
    assert cold.reader_card.signature.instructions == ""
    assert "SEALED TRACEFOLD QUALITYKERNEL" not in cold.event_semantics.signature.instructions
    cold.event_semantics.signature = cold.event_semantics.signature.with_instructions("Prefer explicit causal facts.")

    patch = extract_optimizer_patch(cold, parent, eligible)

    assert patch.learned_strategies[0].text == "Prefer explicit causal facts."
    assert patch.demo_refs == DemoRefOrder()
    cold.event_semantics.demos = [
        dspy.Example(
            evidence_json=render_model_evidence_json(_context().event_semantics_payload(), predictor="event_semantics"),
            semantics={**_semantics(), "magnitude": 2},
        ).with_inputs("evidence_json")
    ]
    with pytest.raises(ValueError, match="optimizer_demos_forbidden"):
        extract_optimizer_patch(cold, parent, eligible)


def test_trusted_patch_applier_changes_only_strategies_and_keeps_demo_bank_empty() -> None:
    parent = load_stable_program_artifact()
    record = _accepted_event_demo()
    eligible = EligibleDemoBank.issue((record,))
    patch = ProgramPatchV2.issue(
        parent=parent,
        learned_strategies=(
            LearnedStrategy.issue(
                predictor="event_semantics",
                text="Prefer explicit causal facts.",
                source="optimizer_patch",
            ),
            LearnedStrategy.issue(
                predictor="reader_card",
                text="Keep the mechanism concrete.",
                source="optimizer_patch",
            ),
        ),
        demo_refs=DemoRefOrder(),
        eligible_demo_bank_root_sha256=eligible.eligible_demo_bank_root_sha256,
    )

    def sha(value: str) -> str:
        return canonical_sha(value)

    receipt = CompileReceipt(
        mode="optimizer_candidate",
        development_dataset_sha=sha("dataset"),
        learning_epoch="program_v6",
        learning_epoch_started_at_ms=1,
        projection_schema_id="news_program_v4_projection_v1",
        optimizer="dspy.GEPA@3.3.0/gepa@0.1.1",
        gepa_version="0.1.1",
        metric_sha256=sha("metric"),
        optimizer_config_sha256=sha("config"),
        seed=7,
        max_metric_calls=20,
        max_task_model_calls=30,
        max_reflection_model_calls=10,
        max_metric_judge_model_calls=10,
        max_cost_microusd=10_000,
        max_call_cost_microusd=1_000,
        # GEPA checks this budget between steps; the sealed optimizer receipt binds the bounded final-step
        # overshoot, while the artifact retains the operator's requested value.
        metric_calls=21,
        task_model_calls=15,
        reflection_model_calls=3,
        metric_judge_attempts=2,
        metric_judge_model_calls=2,
        metric_judge_failures=0,
        task_cost_microusd=6_000,
        reflection_cost_microusd=2_000,
        metric_judge_cost_microusd=1_000,
        actual_cost_microusd=9_000,
        trajectory_sha256=sha("trajectory"),
        checkpoint_sha256=sha("checkpoint"),
        parent_program_sha256=parent.program_sha256,
        parent_state_sha256=parent.state_sha256,
        quality_kernel_sha256=parent.quality_kernel.sha256,
        rule_pack_root_sha256=parent.rule_pack_root_sha256,
        development_dataset_payload_sha256=sha("dataset-payload"),
        case_root_sha256=sha("cases"),
        cluster_root_sha256=sha("clusters"),
        episode_projection_root_sha256=sha("episodes"),
        episode_count=1,
        eligible_demo_bank_root_sha256=eligible.eligible_demo_bank_root_sha256,
        patch_sha256=patch.patch_sha256,
        receipt_payload_root_sha256=sha("receipts"),
        sandbox_launch_receipt_sha256=sha("launch"),
        target_runtime_manifest_sha256=sha("runtime"),
        task_endpoint_identity_sha256=sha("task-endpoint"),
        reflection_endpoint_identity_sha256=sha("reflection-endpoint"),
        metric_judge_endpoint_identity_sha256=sha("metric-judge-endpoint"),
        compiler_source_sha256=sha("compiler-source"),
        compiler_lock_sha256=sha("compiler-lock"),
        sandbox_policy_sha256=sha("sandbox-policy"),
        compiler="tracefold.news.dspy_gepa_compiler_v3",
        source="trusted_compiler_launcher_v3",
        accepted_by="unaccepted_candidate",
    )

    candidate = apply_program_patch_v2(parent, patch, eligible, receipt)

    assert candidate.parent_program_sha256 == parent.program_sha256
    assert candidate.quality_kernel == parent.quality_kernel
    assert candidate.rule_packs == parent.rule_packs
    assert candidate.route_spec == parent.route_spec
    assert candidate.execution == parent.execution
    assert candidate.demo_bank.records == ()
    assert candidate.demo_bank.refs == DemoRefOrder()
    assert candidate.compile_receipt.accepted_by == "unaccepted_candidate"
    manifest, state = ProgramArtifactCodec.encode(candidate)
    assert ProgramArtifactCodec.decode(manifest, state) == candidate

    tampered_manifest = json.loads(manifest)
    tampered_state = json.loads(state)
    tampered_state["learned_strategies"][0]["text"] = "A different advisory strategy."
    tampered_state["learned_strategies"][0]["text_sha256"] = canonical_sha(
        tampered_state["learned_strategies"][0]["text"]
    )
    tampered_manifest["state_sha256"] = canonical_sha(tampered_state)
    tampered_manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in tampered_manifest.items() if key != "program_sha256"}
    )
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(canonical_json(tampered_manifest), canonical_json(tampered_state))

    with pytest.raises(ValidationError, match="v6_demo_refs_forbidden"):
        ProgramPatchV2.issue(
            parent=parent,
            learned_strategies=patch.learned_strategies,
            demo_refs=DemoRefOrder(event_semantics=(record.demo_id,)),
            eligible_demo_bank_root_sha256=eligible.eligible_demo_bank_root_sha256,
        )


def test_production_registry_and_image_directories_reject_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = load_stable_program_artifact()
    manifest, state = ProgramArtifactCodec.encode(artifact)
    package_root = tmp_path / "agents"
    programs = package_root / "programs"
    programs.mkdir(parents=True)
    monkeypatch.setattr(importlib.resources, "files", lambda package: package_root)

    real_registry = tmp_path / "registry-source.json"
    real_registry.write_text(
        canonical_json({"stable": artifact.program_sha256, "images": [artifact.program_sha256]}) + "\n",
        encoding="utf-8",
    )
    (programs / "registry.json").symlink_to(real_registry)
    with pytest.raises(ValueError, match="artifact_path_invalid"):
        load_program_artifact(artifact.program_sha256)

    (programs / "registry.json").unlink()
    (programs / "registry.json").write_text(real_registry.read_text(encoding="utf-8"), encoding="utf-8")
    external_image = tmp_path / "external-image"
    external_image.mkdir()
    (external_image / "manifest.json").write_text(manifest, encoding="utf-8")
    (external_image / "state.json").write_text(state, encoding="utf-8")
    (programs / artifact.program_sha256).symlink_to(external_image, target_is_directory=True)
    with pytest.raises(ValueError, match="artifact_path_invalid"):
        load_program_artifact(artifact.program_sha256)

    symlinked_package_root = tmp_path / "symlinked-agents"
    symlinked_package_root.mkdir()
    external_programs = tmp_path / "external-programs"
    external_programs.mkdir()
    (symlinked_package_root / "programs").symlink_to(external_programs, target_is_directory=True)
    monkeypatch.setattr(importlib.resources, "files", lambda package: symlinked_package_root)
    with pytest.raises(ValueError, match="registry_path_invalid"):
        load_program_artifact(artifact.program_sha256)


def test_adapter_error_is_domain_classified() -> None:
    adapter = ScriptedPredictorAdapter(
        [PredictorAdapterError("provider_busy", retryable=True), PredictorAdapterError("provider_busy", retryable=True)]
    )
    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(_program(adapter).judge(_context()))
    assert caught.value.retryable is True
    assert caught.value.output_failure is False
    assert caught.value.attempts == 2


def test_no_unsafe_serialization_surface_in_production_module() -> None:
    source = Path(__file__).resolve().parents[2] / "src/tracefold/news/agents/semantic_program.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint({"pickle", "cloudpickle"})
