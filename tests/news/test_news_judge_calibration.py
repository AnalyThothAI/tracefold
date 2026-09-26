"""The card judge and the calibration corpus that measures it (#651 §7.3, #706).

The harness tests use table-driven judges, so a calibration arithmetic test never depends on a model. The
judge tests put a deterministic engine below the real DSPy JSON adapter, exactly where a provider answers.
"""

from __future__ import annotations

import json
import threading
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest
from dspy.lm15 import Message, Request, Response, Usage, response_to_events

from tracefold.app.cli.commands import news_learning
from tracefold.app.cli.parser import build_parser
from tracefold.news.artifact_identity import canonical_json
from tracefold.news.learning.judge import (
    JUDGE_ID,
    CardClaimAssessment,
    CardEvidenceJudge,
    FactualEvidenceAssessment,
    FactualEvidenceSupport,
    JudgeEndpoint,
    card_text,
)
from tracefold.news.learning.judge_calibration import (
    CALIBRATION_RECEIPT_SCHEMA,
    MUST_PASS_CLASSES,
    PERTURBATION_CLASSES,
    JudgeCalibrationCase,
    calibration_receipt_sha256,
    load_calibration_cases,
    run_judge_calibration,
)

_EVIDENCE = canonical_json({"title": "Circle files to offer 2.4 million additional Class A shares"})
_CARD = {
    "headline_zh": "Circle增发240万股A类股",
    "lines": [{"claim_ref": "claim_1", "text_zh": "Circle在S-1修订中称将增发240万股A类股。"}],
}


class _ScriptedJudge:
    """A judge that answers from a table; `unavailable=True` makes every question unanswerable."""

    def __init__(self, *, supported: bool = True, unavailable: bool = False) -> None:
        self._supported = supported
        self._unavailable = unavailable
        self.questions: list[str] = []

    @property
    def identity(self) -> dict[str, Any]:
        return {"judge_id": "scripted"}

    @property
    def stats(self) -> dict[str, int]:
        return {"questions": len(self.questions)}

    def facts_supported(self, evidence_json: str, card: Any) -> FactualEvidenceAssessment:
        del evidence_json, card
        self.questions.append("facts_supported")
        if self._unavailable:
            return FactualEvidenceAssessment(status="unavailable", verdict=None, error_code="judge_unavailable")
        return FactualEvidenceAssessment(
            status="answered", verdict=FactualEvidenceSupport(supported_by_evidence=self._supported)
        )

    def key_facts_covered(self, evidence_json: str, card: Any, key_facts: Any) -> CardClaimAssessment:
        del evidence_json, card
        self.questions.append("key_facts_covered")
        if self._unavailable:
            return CardClaimAssessment(status="unavailable", answers=None, error_code="judge_unavailable")
        return CardClaimAssessment(status="answered", answers=tuple(True for _ in key_facts))


class _PerfectJudge(_ScriptedJudge):
    """Answers every calibration case the way the corpus says a competent reader would."""

    def __init__(self, cases: Any) -> None:
        super().__init__()
        self._by_card = {card_text(case.card): case for case in cases}

    def facts_supported(self, evidence_json: str, card: Any) -> FactualEvidenceAssessment:
        del evidence_json
        case = self._by_card[card_text(card)]
        return FactualEvidenceAssessment(
            status="answered", verdict=FactualEvidenceSupport(supported_by_evidence=case.expected_supported)
        )

    def key_facts_covered(self, evidence_json: str, card: Any, key_facts: Any) -> CardClaimAssessment:
        del evidence_json, key_facts
        return CardClaimAssessment(status="answered", answers=self._by_card[card_text(card)].expected_key_facts_covered)


def test_the_calibration_corpus_spans_every_perturbation_class_with_two_frozen_shape_cards_each() -> None:
    cases = load_calibration_cases()

    assert len(cases) == 14
    by_class = {name: [case for case in cases if case.perturbation == name] for name in PERTURBATION_CLASSES}
    assert {name: len(rows) for name, rows in by_class.items()} == dict.fromkeys(PERTURBATION_CLASSES, 2)
    # The two must-pass classes carry the card a judge has to accept, and every other class carries one
    # it has to refuse. A corpus of refusals alone cannot see a judge that refuses everything.
    for name, rows in by_class.items():
        assert all(case.expected_supported is (name in MUST_PASS_CLASSES) for case in rows), name
    # Every card is the frozen reader shape the deliverer sends: a headline and one line per claim.
    assert all(set(case.card) == {"headline_zh", "lines"} and case.card["lines"] for case in cases)


def test_a_case_whose_card_is_not_the_frozen_reader_shape_is_refused() -> None:
    case = load_calibration_cases()[0].model_dump(mode="json")

    with pytest.raises(ValueError):
        JudgeCalibrationCase.model_validate({**case, "card": {"headline_zh": "x", "why_zh": "y"}})


def test_a_judge_that_answers_the_corpus_correctly_scores_one_on_every_class() -> None:
    cases = load_calibration_cases()

    receipt = run_judge_calibration(_PerfectJudge(cases), cases)

    assert receipt["schema"] == CALIBRATION_RECEIPT_SCHEMA
    assert receipt["case_n"] == 14
    assert receipt["unavailable_n"] == 0
    assert receipt["support_accuracy"] == receipt["key_fact_accuracy"] == 1.0
    assert receipt["disagreements"] == []
    assert {row["support_accuracy"] for row in receipt["per_class"].values()} == {1.0}
    assert calibration_receipt_sha256(receipt)


def test_a_judge_that_supports_everything_is_caught_by_the_five_perturbation_classes() -> None:
    cases = load_calibration_cases()

    receipt = run_judge_calibration(_ScriptedJudge(supported=True), cases)

    per_class = receipt["per_class"]
    assert {name: per_class[name]["support_accuracy"] for name in MUST_PASS_CLASSES} == dict.fromkeys(
        MUST_PASS_CLASSES, 1.0
    )
    caught = {name for name in PERTURBATION_CLASSES if name not in MUST_PASS_CLASSES}
    assert {per_class[name]["support_accuracy"] for name in caught} == {0.0}
    assert receipt["support_accuracy"] == pytest.approx(4 / 14)
    assert len([row for row in receipt["disagreements"] if row["question"] == "facts_supported"]) == 10


def test_an_unreachable_judge_is_counted_as_unavailable_and_not_as_a_miscalibration() -> None:
    cases = load_calibration_cases()

    receipt = run_judge_calibration(_ScriptedJudge(unavailable=True), cases)

    assert receipt["unavailable_n"] == 28  # one support question and one key-fact question per case
    assert receipt["questions_answered_n"] == 0
    assert receipt["support_accuracy"] is None and receipt["key_fact_accuracy"] is None
    assert receipt["disagreements"] == []


def test_a_receipt_of_another_schema_has_no_address() -> None:
    with pytest.raises(ValueError, match="news_judge_calibration_receipt_schema_unknown"):
        calibration_receipt_sha256({"schema": "tracefold.news.judge_calibration_receipt.v1"})


# ------------------------------------------------------------------------------------ the real judge


class _Engine:
    """A provider double below the real DSPy JSON adapter: a queue of answers, then a default."""

    def __init__(self, owner: _JudgeLM) -> None:
        self.owner = owner

    def complete(self, request: Request) -> Response:
        return self.owner.answer(request)

    def stream(self, request: Request) -> Iterator[Any]:
        return iter(response_to_events(self.complete(request)))

    def close(self) -> None:
        return None


class _JudgeLM(dspy.LM):
    def __init__(self, *, supported: bool = True, covered: list[bool] | None = None, fail: bool = False) -> None:
        super().__init__("scripted/judge", cache=False, num_retries=0, engine=_Engine(self))
        self.supported = supported
        self.covered = covered
        self.fail = fail
        self.calls = 0
        self.requests: list[Request] = []
        self._lock = threading.Lock()

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    @property
    def supports_response_schema(self) -> bool:
        return True

    def answer(self, request: Request) -> Response:
        with self._lock:
            self.calls += 1
            self.requests.append(request)
        if self.fail:
            raise dspy.LMServerError("provider unavailable", status=503)
        schema = canonical_json(request.config.response_format) if request.config.response_format else ""
        if "supported_by_evidence" in schema:
            verdict: dict[str, Any] = {"supported_by_evidence": self.supported}
        else:
            verdict = {"answers": list(self.covered or [])}
        return Response(
            id=None,
            model=self.model,
            message=Message.assistant(json.dumps({"verdict": verdict})),
            finish_reason="stop",
            usage=Usage(input_tokens=12, output_tokens=8, total_tokens=20),
        )


def _judge(lm: _JudgeLM) -> CardEvidenceJudge:
    return CardEvidenceJudge(JudgeEndpoint(lm))


def _request_text(request: Request) -> str:
    return "\n".join(str(getattr(part, "text", "")) for message in request.messages for part in message.parts)


def test_the_judge_reads_the_frozen_card_and_the_evidence_and_nothing_else() -> None:
    lm = _JudgeLM(supported=False)

    assessment = _judge(lm).facts_supported(_EVIDENCE, _CARD)

    assert assessment.status == "answered" and assessment.verdict is not None
    assert assessment.verdict.supported_by_evidence is False
    text = _request_text(lm.requests[0])
    assert "Circle在S-1修订中称将增发240万股A类股。" in text
    assert "Circle files to offer" in text


def test_a_card_in_the_retired_verdict_shape_is_refused_before_any_call() -> None:
    lm = _JudgeLM()

    with pytest.raises(ValueError):
        _judge(lm).facts_supported(_EVIDENCE, {"headline_zh": "x", "why_zh": "y"})
    assert lm.calls == 0


def test_an_answer_is_cached_and_a_failure_is_unavailable_and_never_cached() -> None:
    lm = _JudgeLM()
    judge = _judge(lm)

    first = judge.facts_supported(_EVIDENCE, _CARD)
    second = judge.facts_supported(_EVIDENCE, _CARD)

    assert first == second and lm.calls == 1
    assert judge.stats == {"questions": 1, "answered": 1, "failures": 0, "cache_entries": 1}

    failing = _JudgeLM(fail=True)
    unreachable = _judge(failing)
    assert unreachable.facts_supported(_EVIDENCE, _CARD).status == "unavailable"
    calls_after_first_failure = failing.calls
    assert unreachable.facts_supported(_EVIDENCE, _CARD).status == "unavailable"
    assert failing.calls > calls_after_first_failure  # asked again rather than replaying the failure
    assert unreachable.stats["cache_entries"] == 0 and unreachable.stats["failures"] == 2


def test_a_key_fact_answer_of_the_wrong_length_is_unavailable_rather_than_aligned() -> None:
    judge = _judge(_JudgeLM(covered=[True]))

    assessment = judge.key_facts_covered(_EVIDENCE, _CARD, ["Circle增发", "240万股"])

    assert assessment.status == "unavailable" and assessment.answers is None


def test_key_facts_are_answered_one_per_fact_in_order_and_an_empty_list_asks_nothing() -> None:
    lm = _JudgeLM(covered=[True, False])
    judge = _judge(lm)

    assert judge.key_facts_covered(_EVIDENCE, _CARD, ["Circle增发", "Visa增发"]).answers == (True, False)
    assert judge.key_facts_covered(_EVIDENCE, _CARD, []).answers == ()
    assert lm.calls == 1


def test_the_endpoint_refuses_a_cached_or_retrying_lm() -> None:
    with pytest.raises(dspy.LMConfigurationError):
        JudgeEndpoint(dspy.LM("scripted/judge", cache=True, num_retries=0))
    with pytest.raises(dspy.LMConfigurationError):
        JudgeEndpoint(dspy.LM("scripted/judge", cache=False, num_retries=2))


def test_the_calibration_receipt_pins_the_judge_identity() -> None:
    cases = load_calibration_cases()
    lm = _JudgeLM(supported=True, covered=[True])
    receipt = run_judge_calibration(_judge(lm), cases)

    assert receipt["judge"]["judge_id"] == JUDGE_ID
    assert receipt["judge"]["model"] == "scripted/judge"
    # The double always answers one key fact, so exactly the multi-fact cases are unavailable.
    assert receipt["unavailable_n"] == sum(len(case.key_facts) > 1 for case in cases)


# ------------------------------------------------------------------------------------------- the CLI


def test_the_learning_group_is_judge_calibration_alone() -> None:
    parser = build_parser()

    args = parser.parse_args(["news", "learning", "judge-calibration", "--model", "judge-model"])
    assert (args.learning_command, args.model, args.out) == ("judge-calibration", "judge-model", "")
    for retired in ("run", "readiness", "baseline", "draft-reviews", "freeze"):
        with pytest.raises(SystemExit):
            parser.parse_args(["news", "learning", retired])
    with pytest.raises(SystemExit):
        parser.parse_args(["news", "release", "canary", "status"])


def _settings(*, configured: bool) -> Any:
    fallback = SimpleNamespace(
        configured=configured, api_key="key", base_url="https://judge.example/v1", request=SimpleNamespace()
    )
    return SimpleNamespace(llm=SimpleNamespace(news_triage_fallback=fallback))


def test_calibration_without_an_endpoint_is_an_error_not_a_receipt() -> None:
    args = Namespace(learning_command="judge-calibration", model="judge-model", out="")

    with pytest.raises(ValueError, match="news_judge_calibration_endpoint_not_configured"):
        news_learning._handle_learning_judge_calibration(args, _settings(configured=False))


def test_calibration_writes_one_receipt_through_the_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built: list[tuple[str, int, float]] = []

    def configured(settings: Any, *, model_name: str, **_: Any) -> Any:
        del settings
        return SimpleNamespace(model_name=model_name)

    def generative(endpoint: Any, *, max_tokens: int, timeout: float) -> _JudgeLM:
        built.append((endpoint.model_name, max_tokens, timeout))
        return _JudgeLM(supported=True, covered=[True])

    monkeypatch.setattr("tracefold.app.llm.configured_lm_endpoint", configured)
    monkeypatch.setattr("tracefold.app.learning_runtime.generative_lm", generative)
    out = tmp_path / "receipts" / "judge.json"

    code, payload = news_learning._handle_learning_judge_calibration(
        Namespace(learning_command="judge-calibration", model="judge-model", out=str(out)),
        _settings(configured=True),
    )

    assert code == 0 and payload["ok"] is True
    assert built == [("judge-model", 4_096, 120.0)]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["schema"] == CALIBRATION_RECEIPT_SCHEMA
    assert written["receipt_sha256"] == payload["data"]["receipt_sha256"]
    assert payload["data"]["receipt_written_to"] == str(out)
