"""One pure taxonomy ruler over accepted Gold and recorded Stable predictions."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..taxonomy import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    IPTC_CODEBOOK_SHA256,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITIES,
    SOURCE_AUTHORITY_CLASSIFIER_VERSION,
    SOURCE_AUTHORITY_REGISTRY_SHA256,
    TAXONOMY_VERSION,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
)

TAXONOMY_METRIC_ID: Final = "tracefold.news.recorded_taxonomy_v1"
TAXONOMY_BASELINE_SCHEMA: Final = "tracefold.news.recorded_taxonomy_baseline.v1"
MIN_INDEPENDENT_CLUSTER_N: Final = 60


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaxonomyMetricResult(_ExactModel):
    case_n: int = Field(ge=0)
    independent_cluster_n: int = Field(ge=0)
    scored_case_n: int = Field(ge=0)
    primary: dict[str, Any]
    diagnostics: dict[str, Any]
    source_authority_registry_coverage: dict[str, Any]
    outcome: Literal["MEASURED", "INSUFFICIENT_DATA"]


class RecordedTaxonomyBaselineReport(_ExactModel):
    schema_id: Literal["tracefold.news.recorded_taxonomy_baseline.v1"] = TAXONOMY_BASELINE_SCHEMA
    identity: dict[str, str]
    case_n: int = Field(ge=0)
    independent_cluster_n: int = Field(ge=0)
    scored_case_n: int = Field(ge=0)
    primary: dict[str, Any]
    diagnostics: dict[str, Any]
    source_authority_registry_coverage: dict[str, Any]
    outcome: Literal["MEASURED", "INSUFFICIENT_DATA"]

    @property
    def report_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class _Case:
    case_id: str
    cluster_id: str
    opened_at_ms: int
    gold: ModelTaxonomyV1
    prediction: NewsTaxonomyV1


def _safe_div(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    return 0.0 if precision + recall == 0 else round(2 * precision * recall / (precision + recall), 6)


def _class_metrics(pairs: Sequence[tuple[str, str]], labels: Sequence[str]) -> dict[str, Any]:
    confusion = {gold: {prediction: 0 for prediction in labels} for gold in labels}
    for gold, prediction in pairs:
        confusion[gold][prediction] += 1
    per_class: dict[str, Any] = {}
    for label in labels:
        support = sum(confusion[label].values())
        predicted = sum(row[label] for row in confusion.values())
        true_positive = confusion[label][label]
        precision = _safe_div(true_positive, predicted)
        recall = _safe_div(true_positive, support)
        if support and precision is None:
            precision = 0.0
        per_class[label] = {
            "support": support,
            "predicted": predicted,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall) if support else None,
        }
    supported = [row["f1"] for row in per_class.values() if row["support"] > 0]
    return {
        "legal_labels": list(labels),
        "confusion_matrix": confusion,
        "per_class": per_class,
        "accuracy": _safe_div(sum(gold == prediction for gold, prediction in pairs), len(pairs)),
        "macro_f1": round(sum(supported) / len(supported), 6) if supported else None,
    }


def _subject_metrics(cases: Sequence[_Case]) -> dict[str, Any]:
    true_positive = false_positive = false_negative = exact = 0
    per_code = {code: {"support": 0, "predicted": 0, "tp": 0, "fp": 0, "fn": 0} for code in IPTC_SUBJECT_CODES}
    for case in cases:
        gold = set(case.gold.subject_codes)
        prediction = set(case.prediction.subject_codes)
        exact += gold == prediction
        true_positive += len(gold & prediction)
        false_positive += len(prediction - gold)
        false_negative += len(gold - prediction)
        for code, row in per_code.items():
            row["support"] += code in gold
            row["predicted"] += code in prediction
            row["tp"] += code in gold and code in prediction
            row["fp"] += code not in gold and code in prediction
            row["fn"] += code in gold and code not in prediction
    precision = _safe_div(true_positive, true_positive + false_positive)
    recall = _safe_div(true_positive, true_positive + false_negative)
    return {
        "legal_labels": list(IPTC_SUBJECT_CODES),
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": _safe_div(2 * true_positive, 2 * true_positive + false_positive + false_negative),
        "exact_accuracy": _safe_div(exact, len(cases)),
        "per_class": per_code,
    }


def _four_axis_exact(case: _Case) -> bool:
    return bool(
        case.gold.subject_codes == case.prediction.subject_codes
        and case.gold.event_family == case.prediction.event_family
        and case.gold.change_state == case.prediction.change_state
        and case.gold.assertion_status == case.prediction.assertion_status
    )


def _model_non_abstain(case: _Case) -> bool:
    return bool(
        case.prediction.subject_codes
        and case.prediction.event_family != "other"
        and case.prediction.change_state != "unknown"
        and case.prediction.assertion_status != "unknown"
    )


def _cases(episodes: Sequence[Mapping[str, Any]]) -> tuple[int, list[_Case]]:
    parsed: list[_Case] = []
    case_ids: set[str] = set()
    for episode in episodes:
        accepted = episode.get("accepted_review")
        if not isinstance(accepted, Mapping) or not isinstance(accepted.get("taxonomy"), Mapping):
            raise ValueError("news_taxonomy_metric_gold_missing")
        case_id = str(episode.get("case_id") or "")
        cluster_id = str(episode.get("cluster_id") or "")
        if not case_id or not cluster_id:
            raise ValueError("news_taxonomy_metric_case_identity_required")
        if case_id in case_ids:
            raise ValueError("news_taxonomy_metric_case_id_duplicate")
        case_ids.add(case_id)
        production = episode.get("production_judgment")
        if production is None:
            # Accepted external misses belong to the existing development Dataset but have no Stable
            # prediction by definition. They stay visible in the Dataset receipt and are outside this
            # metric's honestly scorable population.
            continue
        if not isinstance(production, Mapping):
            raise ValueError(f"news_taxonomy_recorded_prediction_invalid:{case_id[:16]}")
        editorial = production.get("editorial")
        prediction = editorial.get("taxonomy") if isinstance(editorial, Mapping) else None
        if not isinstance(prediction, Mapping):
            raise ValueError(f"news_taxonomy_recorded_prediction_missing:{case_id[:16]}")
        context = episode.get("context")
        parsed.append(
            _Case(
                case_id=case_id,
                cluster_id=cluster_id,
                opened_at_ms=int(context.get("now_ms") or 0) if isinstance(context, Mapping) else 0,
                gold=ModelTaxonomyV1.model_validate(accepted["taxonomy"]),
                prediction=NewsTaxonomyV1.model_validate(prediction),
            )
        )
    return len(case_ids), sorted(parsed, key=lambda case: (case.opened_at_ms, case.case_id))


def _representatives(cases: Sequence[_Case]) -> list[_Case]:
    by_cluster: dict[str, _Case] = {}
    gold_by_cluster: dict[str, tuple[Any, ...]] = {}
    for case in cases:
        model_gold = (
            case.gold.subject_codes,
            case.gold.event_family,
            case.gold.change_state,
            case.gold.assertion_status,
        )
        if case.cluster_id in gold_by_cluster and gold_by_cluster[case.cluster_id] != model_gold:
            raise ValueError("news_taxonomy_metric_cluster_gold_conflict")
        gold_by_cluster[case.cluster_id] = model_gold
        by_cluster.setdefault(case.cluster_id, case)
    return list(by_cluster.values())


def taxonomy_metric(episodes: Sequence[Mapping[str, Any]]) -> TaxonomyMetricResult:
    """Aggregate one representative per connected fact cluster; no I/O and no model call."""

    case_n, cases = _cases(episodes)
    scored = _representatives(cases)
    event_family = _class_metrics(
        [(case.gold.event_family, case.prediction.event_family) for case in scored],
        EVENT_FAMILIES,
    )
    change_state = _class_metrics(
        [(case.gold.change_state, case.prediction.change_state) for case in scored],
        CHANGE_STATES,
    )
    assertion_status = _class_metrics(
        [(case.gold.assertion_status, case.prediction.assertion_status) for case in scored],
        ASSERTION_STATUSES,
    )
    exact_n = sum(_four_axis_exact(case) for case in scored)
    non_abstained = [case for case in scored if _model_non_abstain(case)]
    source_counts = {label: 0 for label in SOURCE_AUTHORITIES}
    for case in scored:
        source_counts[case.prediction.source_authority] += 1
    source_covered_n = len(scored) - source_counts["unknown"]
    independent_cluster_n = len(scored)
    return TaxonomyMetricResult(
        case_n=case_n,
        independent_cluster_n=independent_cluster_n,
        scored_case_n=len(scored),
        primary={"event_family_supported_label_macro_f1": event_family["macro_f1"]},
        diagnostics={
            "event_family": event_family,
            "subject_codes": _subject_metrics(scored),
            "change_state": change_state,
            "assertion_status": assertion_status,
            "four_axis_exact_match": {
                "match_n": exact_n,
                "accuracy": _safe_div(exact_n, len(scored)),
            },
            "model_non_abstain": {
                "case_n": len(non_abstained),
                "coverage": _safe_div(len(non_abstained), len(scored)),
                "four_axis_exact_accuracy": _safe_div(
                    sum(_four_axis_exact(case) for case in non_abstained),
                    len(non_abstained),
                ),
            },
        },
        source_authority_registry_coverage={
            "owner": "code",
            "classifier_version": SOURCE_AUTHORITY_CLASSIFIER_VERSION,
            "registry_sha256": SOURCE_AUTHORITY_REGISTRY_SHA256,
            "legal_labels": list(SOURCE_AUTHORITIES),
            "covered_case_n": source_covered_n,
            "coverage": _safe_div(source_covered_n, len(scored)),
            "per_class": {label: {"support": source_counts[label]} for label in SOURCE_AUTHORITIES},
        },
        outcome="MEASURED" if independent_cluster_n >= MIN_INDEPENDENT_CLUSTER_N else "INSUFFICIENT_DATA",
    )


_METRIC_CONTRACT: Final = {
    "metric_id": TAXONOMY_METRIC_ID,
    "taxonomy_version": TAXONOMY_VERSION,
    "codebook_sha256": IPTC_CODEBOOK_SHA256,
    "primary": "event_family macro-F1 over legal labels with Gold support > 0",
    "diagnostics": (
        "subject_codes micro-F1",
        "change_state accuracy",
        "assertion_status macro-F1 over legal labels with Gold support > 0",
        "four model-owned axes exact match",
        "model non-abstain excludes code-owned source_authority",
    ),
    "source_authority": "deterministic registry coverage only; excluded from model scores",
    "scored_population": "accepted Gold with a recorded Stable prediction; external misses remain in case_n",
    "cluster_election": "earliest (context.now_ms, case_id) per connected cluster",
    "minimum_independent_cluster_n": MIN_INDEPENDENT_CLUSTER_N,
}


@cache
def taxonomy_metric_identity() -> dict[str, str]:
    implementation = canonical_sha(
        "\n".join(
            inspect.getsource(function).replace("\r\n", "\n")
            for function in (
                _safe_div,
                _f1,
                _class_metrics,
                _subject_metrics,
                _four_axis_exact,
                _model_non_abstain,
                _cases,
                _representatives,
                taxonomy_metric,
            )
        )
    )
    return {
        "metric_id": TAXONOMY_METRIC_ID,
        "metric_sha256": canonical_sha({"contract": _METRIC_CONTRACT, "implementation_sha256": implementation}),
    }


def recorded_taxonomy_baseline_report(
    episodes: Sequence[Mapping[str, Any]],
    *,
    dataset_sha: str,
    agent_cohort: Mapping[str, Any],
) -> RecordedTaxonomyBaselineReport:
    """Bind the pure metric to the frozen Dataset and its recorded Stable cohort."""

    metric = taxonomy_metric(episodes)
    identity = {
        "dataset_sha": str(dataset_sha),
        "stable_bundle_sha": str(agent_cohort.get("bundle_sha") or ""),
        "stable_program_version": str(agent_cohort.get("program_version") or ""),
        "stable_program_sha256": str(agent_cohort.get("program_sha256") or ""),
        "recorded_runtime_model_bindings_sha256": str(agent_cohort.get("runtime_model_bindings_sha256") or ""),
        **taxonomy_metric_identity(),
    }
    if not all(identity.values()):
        raise ValueError("news_taxonomy_baseline_identity_incomplete")
    return RecordedTaxonomyBaselineReport(
        identity=identity,
        **metric.model_dump(mode="json"),
    )


__all__ = [
    "MIN_INDEPENDENT_CLUSTER_N",
    "TAXONOMY_BASELINE_SCHEMA",
    "TAXONOMY_METRIC_ID",
    "RecordedTaxonomyBaselineReport",
    "TaxonomyMetricResult",
    "recorded_taxonomy_baseline_report",
    "taxonomy_metric",
    "taxonomy_metric_identity",
]
