"""Focused contracts for the native three-Predictor News Program (#501)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.program import module as native_program_module
from tracefold.news.program.artifact import build_code_owned_program_artifact
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallLedger,
    RuntimeModelIdentity,
)
from tracefold.news.program.lm import (
    ScriptedLM as TypedScriptedLM,
)
from tracefold.news.program.module import NativeNewsProgram, NativeProgramResult
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.program.signatures import ReaderCard


def _semantics(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": 4,
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
            "channels": ["security_incident", "rates"],
            "affected_markets": ["single_asset", "fx"],
            "reader_value": "realtime",
        },
    }
    value.update(updates)
    return value


def _taxonomy(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "subject_codes": ["medtop:20001279"],
        "event_family": "market_access",
        "change_state": "announced",
        "assertion_status": "confirmed",
    }
    value.update(updates)
    return value


def _card() -> dict[str, str]:
    return {"headline_zh": "  比特币出现新进展  ", "why_zh": "  值得关注。  "}


def _context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-secret",
            "evidence_version": 3,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-secret",
            "reporting_origin": "Reuters",
            "provenance": ["1018"],
            "leader_title": "BTC listed on Example Exchange",
            "raw_first_line": "$BTC listing",
            "leader_description": "Trading starts tomorrow.",
            "opened_at_ms": 1_000_000,
            "member_count": 2,
            "dedupe_family": "listing",
            "provider_score_max": 90,
            "provider_metadata": {"coins": [{"symbol": "BTC", "grade": "A"}]},
            "queue_priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=[
            {
                "event_id": "old-event",
                "at_ms": 900_000,
                "storyline_key": "asset:BTC",
                "dedupe_family": "listing",
                "comparison_title": "BTC listed on another exchange",
                "comparison_fingerprint": "f" * 64,
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "这条历史卡片只允许第一个 Predictor 看到",
                "why_zh": "历史卡片原因",
                "grounded_assets": ["BTC"],
            }
        ],
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


class _ScriptedLM(dspy.BaseLM):
    def __init__(self, *outputs: dict[str, Any]) -> None:
        super().__init__(model="scripted/native-news", cache=False, num_retries=0)
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def _next(self, *, prompt: Any, messages: Any, kwargs: dict[str, Any]) -> list[str]:
        self.calls.append({"prompt": prompt, "messages": messages, "kwargs": kwargs})
        if not self.outputs:
            raise AssertionError("unexpected LM call")
        return [json.dumps(self.outputs.pop(0), ensure_ascii=False)]

    def __call__(
        self,
        prompt: Any = None,
        messages: Any = None,
        **kwargs: Any,
    ) -> list[str]:
        return self._next(prompt=prompt, messages=messages, kwargs=kwargs)

    async def acall(
        self,
        prompt: Any = None,
        messages: Any = None,
        **kwargs: Any,
    ) -> list[str]:
        return self._next(prompt=prompt, messages=messages, kwargs=kwargs)


def _lms() -> tuple[_ScriptedLM, _ScriptedLM, _ScriptedLM]:
    return (
        _ScriptedLM({"semantics": _semantics()}),
        _ScriptedLM({"taxonomy": _taxonomy()}),
        _ScriptedLM({"card": _card()}),
    )


def _run_sync() -> tuple[NativeNewsProgram, NativeProgramResult, _ScriptedLM, _ScriptedLM, _ScriptedLM]:
    event_lm, taxonomy_lm, card_lm = _lms()
    program = NativeNewsProgram(build_code_owned_program_artifact())
    result = program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)
    return program, result, event_lm, taxonomy_lm, card_lm


def test_three_named_predictors_run_in_order_with_exact_instructions_and_bounded_inputs() -> None:
    artifact = build_code_owned_program_artifact()
    program, result, event_lm, taxonomy_lm, card_lm = _run_sync()

    assert [(name, predictor.signature.instructions) for name, predictor in program.named_predictors()] == [
        ("event_semantics", artifact.event_semantics_instruction),
        ("taxonomy", artifact.taxonomy_instruction),
        ("reader_card", artifact.reader_card_instruction),
    ]
    assert len(event_lm.calls) == len(taxonomy_lm.calls) == len(card_lm.calls) == 1
    event_request = json.dumps(event_lm.calls[0], ensure_ascii=False)
    taxonomy_request = json.dumps(taxonomy_lm.calls[0], ensure_ascii=False)
    card_request = json.dumps(card_lm.calls[0], ensure_ascii=False)
    assert "这条历史卡片只允许第一个 Predictor 看到" in event_request
    # Taxonomy classifies the Event alone: no told ledger, no semantics, no card.
    assert "这条历史卡片只允许第一个 Predictor 看到" not in taxonomy_request
    assert "event_status" not in taxonomy_request
    assert "semantics_json" not in taxonomy_request
    assert "BTC listed on Example Exchange" in taxonomy_request
    assert "这条历史卡片只允许第一个 Predictor 看到" not in card_request
    assert "event_status" not in card_request
    assert result.instruction_rejected is None
    assert result.semantics is not None and result.semantics.restates == -1
    assert result.semantics.relevance.channels == ("rates", "security_incident")
    assert result.semantics.relevance.affected_markets == ("fx", "single_asset")
    assert result.taxonomy is not None and result.taxonomy.event_family == "market_access"
    assert result.verdict is not None
    assert result.editorial is not None and result.editorial.taxonomy is not None
    assert result.editorial.taxonomy.event_family == "market_access"
    assert result.editorial.taxonomy.source_authority == "reputable_secondary"
    assert result.verdict.headline_zh == "比特币出现新进展"
    assert result.verdict.why_zh == "值得关注。"
    assert result.verdict.model_dump(mode="json") == {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "confidence": 0.8,
        "audience": "crypto",
        "headline_zh": "比特币出现新进展",
        "why_zh": "值得关注。",
    }
    assert [(trace.field, trace.input_value, trace.output_value) for trace in result.normalizations] == [
        ("channels", ("security_incident", "rates"), ("rates", "security_incident")),
        ("affected_markets", ("single_asset", "fx"), ("fx", "single_asset")),
        ("restates", 4, -1),
    ]


def test_event_semantics_no_longer_accepts_a_taxonomy_field() -> None:
    event_lm = _ScriptedLM({"semantics": {**_semantics(), "taxonomy": _taxonomy()}})
    _unused, taxonomy_lm, card_lm = _lms()
    program = NativeNewsProgram(build_code_owned_program_artifact())

    with pytest.raises(ValidationError):
        program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    assert card_lm.calls == []


def test_sync_and_async_entries_have_typed_output_parity() -> None:
    _, sync_result, _, _, _ = _run_sync()
    async_program = NativeNewsProgram(build_code_owned_program_artifact())
    async_event_lm, async_taxonomy_lm, async_card_lm = _lms()
    async_result = asyncio.run(
        async_program.acall(
            context=_context(),
            event_lm=async_event_lm,
            taxonomy_lm=async_taxonomy_lm,
            card_lm=async_card_lm,
        )
    )

    assert isinstance(sync_result, dspy.Prediction)
    assert isinstance(async_result, dspy.Prediction)
    assert sync_result.semantics == async_result.semantics
    assert sync_result.taxonomy == async_result.taxonomy
    assert sync_result.card == async_result.card
    assert sync_result.verdict == async_result.verdict
    assert sync_result.editorial == async_result.editorial
    assert sync_result.normalizations == async_result.normalizations
    assert len(async_event_lm.calls) == len(async_taxonomy_lm.calls) == len(async_card_lm.calls) == 1


def test_gepa_context_lm_is_used_when_explicit_lms_are_absent() -> None:
    lm = _ScriptedLM({"semantics": _semantics()}, {"taxonomy": _taxonomy()}, {"card": _card()})
    program = NativeNewsProgram(build_code_owned_program_artifact())

    with dspy.context(lm=lm):
        result = program(context=_context())

    assert result.instruction_rejected is None
    assert result.verdict is not None
    assert len(lm.calls) == 3


def test_candidate_guard_rejects_mutated_instructions_before_adapter_or_lm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    def reject(event_instruction: str, taxonomy_instruction: str, card_instruction: str) -> str:
        seen.append((event_instruction, taxonomy_instruction, card_instruction))
        return "news_program_candidate_growth_exceeded"

    artifact = build_code_owned_program_artifact()
    program = NativeNewsProgram(artifact, candidate_guard=reject)
    program.taxonomy.signature = program.taxonomy.signature.with_instructions("mutated instruction")
    event_lm, taxonomy_lm, card_lm = _lms()
    monkeypatch.setattr(
        native_program_module,
        "program_json_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("adapter must not be constructed")),
    )

    result = program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)
    async_result = asyncio.run(
        program.acall(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)
    )

    expected = (artifact.event_semantics_instruction, "mutated instruction", artifact.reader_card_instruction)
    assert seen == [expected, expected]
    assert result.instruction_rejected == "news_program_candidate_growth_exceeded"
    assert async_result.instruction_rejected == result.instruction_rejected
    assert result.semantics is result.taxonomy is result.card is result.verdict is result.editorial is None
    assert event_lm.calls == taxonomy_lm.calls == card_lm.calls == []


def test_invalid_restatement_stops_before_reader_predictor() -> None:
    event_lm = _ScriptedLM({"semantics": _semantics(novelty="restatement", restates=9)})
    _unused, taxonomy_lm, card_lm = _lms()
    program = NativeNewsProgram(build_code_owned_program_artifact())

    with pytest.raises(ValueError, match="news_program_restatement_index_invalid"):
        program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    assert len(event_lm.calls) == 1
    assert card_lm.calls == []


@pytest.mark.parametrize("async_entry", [False, True], ids=("sync", "async"))
def test_direct_native_scope_marks_post_predictor_domain_failure_terminal(async_entry: bool) -> None:
    artifact = build_code_owned_program_artifact()
    ledger = LMCallLedger()

    def audited(predictor: str, steps: list[Any]) -> AuditedConfiguredLM:
        delegate = TypedScriptedLM(steps, model=f"scripted/{predictor}")
        return AuditedConfiguredLM(
            delegate,
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
            predictor=predictor,
            route="compile",
            model_binding=predictor,
            ledger=ledger,
        )

    program = NativeNewsProgram(artifact)
    event_lm = audited(
        "event_semantics",
        [{"semantics": _semantics(novelty="restatement", restates=9)}],
    )
    taxonomy_lm = audited("taxonomy", [{"taxonomy": _taxonomy()}])
    card_lm = audited("reader_card", [{"card": _card()}])

    with (
        pytest.raises(ValueError, match="news_program_restatement_index_invalid"),
        ledger.scope(LMCallContext(PROGRAM_VERSION, "a" * 64, "b" * 64)),
    ):
        if async_entry:
            asyncio.run(program.acall(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm))
        else:
            program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    by_predictor = {receipt.predictor: receipt for receipt in ledger.receipts}
    assert by_predictor["event_semantics"].terminal_disposition == "domain_validation_error"
    assert by_predictor["event_semantics"].error_code == "news_program_restatement_index_invalid"
    assert "reader_card" not in by_predictor


@pytest.mark.parametrize(
    "invalid_semantics",
    [
        {key: value for key, value in _semantics().items() if key != "novelty"},
        _semantics(direction="sideways"),
        _semantics(unexpected_business_field="must fail closed"),
    ],
    ids=("missing", "invalid-enum", "extra"),
)
def test_business_output_shape_failures_never_reach_reader_predictor(
    invalid_semantics: dict[str, Any],
) -> None:
    event_lm = _ScriptedLM(
        {"semantics": invalid_semantics},
        {"semantics": invalid_semantics},
    )
    _unused, taxonomy_lm, card_lm = _lms()
    program = NativeNewsProgram(build_code_owned_program_artifact())

    with pytest.raises(ValidationError):
        program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    assert len(event_lm.calls) == 1
    assert card_lm.calls == []


@pytest.mark.parametrize(
    "invalid_taxonomy",
    [
        _taxonomy(event_family="whale"),
        _taxonomy(subject_codes=["medtop:04000000", "medtop:20001279"]),
        {key: value for key, value in _taxonomy().items() if key != "change_state"},
    ],
    ids=("unknown-family", "parent-child-subjects", "missing"),
)
def test_taxonomy_output_shape_failures_never_reach_reader_predictor(invalid_taxonomy: dict[str, Any]) -> None:
    event_lm, _unused, card_lm = _lms()
    taxonomy_lm = _ScriptedLM({"taxonomy": invalid_taxonomy}, {"taxonomy": invalid_taxonomy})
    program = NativeNewsProgram(build_code_owned_program_artifact())

    with pytest.raises(ValidationError):
        program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    assert len(taxonomy_lm.calls) == 1
    assert card_lm.calls == []


def test_outer_dspy_envelope_sibling_is_filtered_but_business_model_remains_exact() -> None:
    event_lm = _ScriptedLM({"semantics": _semantics(), "diagnostic": "adapter-owned sibling"})
    taxonomy_lm = _ScriptedLM({"taxonomy": _taxonomy(), "diagnostic": "adapter-owned sibling"})
    card_lm = _ScriptedLM({"card": _card(), "diagnostic": "adapter-owned sibling"})
    program = NativeNewsProgram(build_code_owned_program_artifact())

    result = program(context=_context(), event_lm=event_lm, taxonomy_lm=taxonomy_lm, card_lm=card_lm)

    assert result.semantics is not None
    assert result.taxonomy is not None
    assert result.card is not None
    assert "diagnostic" not in result.semantics.model_dump(mode="json")
    assert "diagnostic" not in result.taxonomy.model_dump(mode="json")
    assert "diagnostic" not in result.card.model_dump(mode="json")
    assert result.semantics.restates == -1
    assert result.semantics.relevance.channels == ("rates", "security_incident")
    assert result.semantics.relevance.affected_markets == ("fx", "single_asset")
    assert result.card.model_dump(mode="json") == _card()


@pytest.mark.parametrize(
    ("card", "error"),
    [
        pytest.param({"headline_zh": "比特币出现新进展"}, "news_program_reader_why_empty", id="why-omitted"),
        pytest.param(
            {"headline_zh": "比特币出现新进展", "why_zh": "：：：：：：：：："},
            "news_program_reader_why_empty",
            id="why-punctuation-only",
        ),
        pytest.param(
            {"headline_zh": "比特币出现新进展", "why_zh": "        "},
            "news_program_reader_why_empty",
            id="why-whitespace-only",
        ),
        pytest.param(
            {"headline_zh": "Bitcoin reclaims $70,000 after the Fed cut", "why_zh": "美联储降息推动风险资产反弹。"},
            "news_program_reader_headline_not_chinese",
            id="headline-english",
        ),
        pytest.param({"headline_zh": "   ", "why_zh": "美联储降息推动风险资产反弹。"}, None, id="headline-blank"),
    ],
)
def test_reader_card_refuses_copy_the_reader_cannot_use(card: dict[str, str], error: str | None) -> None:
    """#522 D4: the #504 receipt pushed cards with an empty why_zh, a why_zh of ":" and an English headline.

    `max_length` and a non-blank headline were the whole contract, so each of those was a valid ReaderCard.
    A rejection here is not a lost card: the JSON adapter already makes one format retry, and a card that
    reaches the reader without its mechanism sentence has nothing to say.
    """

    with pytest.raises(ValidationError) as raised:
        ReaderCard.model_validate(card)
    if error is not None:
        assert error in str(raised.value)


def test_reader_card_accepts_ordinary_chinese_copy() -> None:
    card = ReaderCard.model_validate(
        {
            "headline_zh": "美联储意外降息 25 个基点",
            "why_zh": "美元利率下行直接抬高风险资产估值，比特币与美股同步走强。",
        }
    )
    assert card.headline_zh == "美联储意外降息 25 个基点"
