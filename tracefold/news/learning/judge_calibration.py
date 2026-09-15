"""Does the metric judge actually answer the two questions the explanation ruler asks it (#651 §7.3)?

The explanation target's score is `F1(evidence_support, key_facts_covered)`, and both halves are a model's
opinion. That is a defensible ruler only while the model's opinion tracks the perturbation it is supposed
to catch, and nothing in the release path measures whether it does: a judge that answered `supported=True`
to everything would publish a rising explanation score on a Program that got worse, and every gate
downstream would read it as evidence.

This harness is the measurement. A small fixed corpus of synthetic `(evidence, card)` pairs spans the
seven ways a *why* goes wrong or stays right — entity swap, number/unit swap, condition removed,
plan presented as executed, unsupported cause added, faithful paraphrase (which must pass), and a
supported strong conclusion (which must also pass) — each with the verdict a competent reader would give.
The last two classes are not decoration: a judge that catches every perturbation by calling everything
unsupported is useless in exactly the opposite way, and they are the only cases that can see it.

The command that runs it against a real model writes a receipt, and the metric receipt records that
receipt's sha when one is supplied, so a run's explanation numbers carry the evidence that the judge
behind them was checked. Nothing here gates anything: it is a measurement an operator reads before
spending a run, exactly as `calibrate_taxonomy` is for the codebook.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha

CALIBRATION_RECEIPT_SCHEMA: Final = "tracefold.news.judge_calibration_receipt.v1"
CALIBRATION_CASES_SCHEMA: Final = "tracefold.news.judge_calibration_cases.v1"

PerturbationClass = Literal[
    "entity_swap",
    "number_unit_swap",
    "condition_removed",
    "plan_to_executed",
    "unsupported_cause_added",
    "faithful_paraphrase",
    "supported_strong_conclusion",
]

PERTURBATION_CLASSES: Final[tuple[PerturbationClass, ...]] = (
    "entity_swap",
    "number_unit_swap",
    "condition_removed",
    "plan_to_executed",
    "unsupported_cause_added",
    "faithful_paraphrase",
    "supported_strong_conclusion",
)

# The two classes a judge must not fail. Everything else measures whether it catches a defect; these two
# measure whether it can tell a correct card from one, which is the failure mode a perturbation-only
# corpus cannot see.
MUST_PASS_CLASSES: Final[frozenset[str]] = frozenset({"faithful_paraphrase", "supported_strong_conclusion"})

_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "news" / "judge_calibration_cases.json"


class JudgeCalibrationCase(BaseModel):
    """One synthetic (evidence, card) pair and the verdict a competent reader would give it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    perturbation: PerturbationClass
    note: str = ""
    evidence: dict[str, Any]
    card: dict[str, Any]
    expected_supported: bool
    key_facts: tuple[str, ...] = ()
    expected_key_facts_covered: tuple[bool, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    expected_forbidden_asserted: tuple[bool, ...] = ()

    @model_validator(mode="after")
    def answers_match_their_questions(self) -> JudgeCalibrationCase:
        if len(self.key_facts) != len(self.expected_key_facts_covered):
            raise ValueError("news_judge_calibration_key_fact_answers_mismatch")
        if len(self.forbidden_claims) != len(self.expected_forbidden_asserted):
            raise ValueError("news_judge_calibration_forbidden_answers_mismatch")
        if self.perturbation in MUST_PASS_CLASSES and not self.expected_supported:
            raise ValueError("news_judge_calibration_must_pass_case_expects_failure")
        return self

    @property
    def evidence_json(self) -> str:
        return canonical_json(self.evidence)


def load_calibration_cases(path: Path | None = None) -> tuple[JudgeCalibrationCase, ...]:
    """The fixed corpus, refused rather than defaulted when it does not span all seven classes."""

    payload = json.loads((path or _FIXTURE).read_text(encoding="utf-8"))
    if str(payload.get("schema") or "") != CALIBRATION_CASES_SCHEMA:
        raise ValueError("news_judge_calibration_cases_schema_unknown")
    cases = tuple(JudgeCalibrationCase.model_validate(row) for row in payload.get("cases") or ())
    missing = sorted(set(PERTURBATION_CLASSES) - {case.perturbation for case in cases})
    if missing:
        raise ValueError(f"news_judge_calibration_cases_incomplete:{','.join(missing)}")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("news_judge_calibration_cases_duplicate_id")
    return cases


def _rate(hits: int, total: int) -> float | None:
    return None if not total else round(hits / total, 6)


def run_judge_calibration(
    judge: Any,
    cases: Sequence[JudgeCalibrationCase] | None = None,
) -> dict[str, Any]:
    """Ask one judge every calibration question and report how often it agreed, per perturbation class.

    An unavailable answer is counted as unavailable and never as a miss: a judge that could not be reached
    has not disagreed with anything, and scoring it as wrong would make a provider outage look like a
    miscalibrated model — the same rule the explanation ruler follows on a live run.
    """

    corpus = tuple(cases if cases is not None else load_calibration_cases())
    per_class: dict[str, dict[str, int]] = {
        name: {"n": 0, "support_hit": 0, "support_answered": 0, "fact_hit": 0, "fact_answered": 0}
        for name in PERTURBATION_CLASSES
    }
    unavailable = 0
    disagreements: list[dict[str, Any]] = []
    for case in corpus:
        row = per_class[case.perturbation]
        row["n"] += 1
        support = judge.facts_supported(case.evidence_json, case.card)
        if support.status == "unavailable" or support.verdict is None:
            unavailable += 1
        else:
            row["support_answered"] += 1
            answered = bool(support.verdict.supported_by_evidence)
            if answered == case.expected_supported:
                row["support_hit"] += 1
            else:
                disagreements.append(
                    {
                        "case_id": case.case_id,
                        "perturbation": case.perturbation,
                        "question": "facts_supported",
                        "expected": case.expected_supported,
                        "answered": answered,
                    }
                )
        if case.key_facts:
            covered = judge.key_facts_covered(case.evidence_json, case.card, case.key_facts)
            if covered.status == "unavailable" or covered.answers is None:
                unavailable += 1
            else:
                answers = tuple(bool(value) for value in covered.answers)
                row["fact_answered"] += len(answers)
                row["fact_hit"] += sum(
                    1 for answer, expected in zip(answers, case.expected_key_facts_covered, strict=True)
                    if answer == expected
                )
                if answers != case.expected_key_facts_covered:
                    disagreements.append(
                        {
                            "case_id": case.case_id,
                            "perturbation": case.perturbation,
                            "question": "key_facts_covered",
                            "expected": list(case.expected_key_facts_covered),
                            "answered": list(answers),
                        }
                    )
    questions_asked = sum(row["support_answered"] for row in per_class.values()) + sum(
        row["fact_answered"] for row in per_class.values()
    )
    return {
        "schema": CALIBRATION_RECEIPT_SCHEMA,
        "cases_sha256": canonical_sha([case.model_dump(mode="json") for case in corpus]),
        "case_n": len(corpus),
        "judge": judge.identity,
        "judge_stats": judge.stats,
        "unavailable_n": unavailable,
        "questions_answered_n": questions_asked,
        "per_class": {
            name: {
                "n": row["n"],
                "must_pass": name in MUST_PASS_CLASSES,
                "support_accuracy": _rate(row["support_hit"], row["support_answered"]),
                "support_answered_n": row["support_answered"],
                "key_fact_accuracy": _rate(row["fact_hit"], row["fact_answered"]),
                "key_fact_answered_n": row["fact_answered"],
            }
            for name, row in per_class.items()
        },
        "support_accuracy": _rate(
            sum(row["support_hit"] for row in per_class.values()),
            sum(row["support_answered"] for row in per_class.values()),
        ),
        "key_fact_accuracy": _rate(
            sum(row["fact_hit"] for row in per_class.values()),
            sum(row["fact_answered"] for row in per_class.values()),
        ),
        "disagreements": disagreements,
    }


def calibration_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """The address of one calibration measurement, for the metric receipt to point at."""

    if str(receipt.get("schema") or "") != CALIBRATION_RECEIPT_SCHEMA:
        raise ValueError("news_judge_calibration_receipt_schema_unknown")
    return canonical_sha(dict(receipt))


__all__ = [
    "CALIBRATION_CASES_SCHEMA",
    "CALIBRATION_RECEIPT_SCHEMA",
    "MUST_PASS_CLASSES",
    "PERTURBATION_CLASSES",
    "JudgeCalibrationCase",
    "calibration_receipt_sha256",
    "load_calibration_cases",
    "run_judge_calibration",
]
