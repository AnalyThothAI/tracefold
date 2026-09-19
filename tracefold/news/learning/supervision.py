"""Pure accepted supervision, shared by freeze, planning, examples and reports.

Only an explicit pass binds recorded output; predictions alone never become Gold.
Masks depend on accepted labels and frozen input, never the candidate's answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from ..taxonomy import ReviewTaxonomyV1

SEMANTIC_FIELDS = {
    "asset_grounding": "assets",
    "direction": "direction",
    "magnitude": "magnitude",
    "trade_impact_breadth": "impact_breadth",
    "trade_tradability": "tradability",
    "trade_surprise": "surprise",
    "trade_development_delta": "development_delta",
    "trade_channels": "channels",
    "trade_affected_markets": "affected_markets",
    "reader_value": "reader_value",
}
CARD_DIMENSIONS = ("factual_fidelity", "headline_fidelity", "why_support", "why_value")


def project_supervision(
    review: Mapping[str, Any],
    judgment: Mapping[str, Any] | None = None,
    *,
    told_event_ids: Sequence[str] | None = None,
    unsettled_event_ids: Sequence[str] = (),
    context_exact: bool | None = None,
) -> dict[str, Any]:
    payload = dict(review.get("payload") or review)
    dimensions = dict(review.get("dimensions") or payload.get("dimensions") or {})
    expected = dict(payload.get("expected") or {})
    recorded = dict(judgment or {})
    verdict = dict(recorded.get("verdict") or {})
    relevance = dict(dict(recorded.get("editorial") or {}).get("relevance") or {})
    labels: dict[str, Any] = {}
    sources: dict[str, str] = {}
    missing: dict[str, str] = {}
    # A sealed projection is already bound to its original reviewed output.
    sealed = dict(review.get("supervision") or {})
    if told_event_ids is None:
        told_event_ids = sealed.get("told_event_ids")
    if context_exact is None:
        context_exact = sealed.get("context_exact")
    for dimension, field in SEMANTIC_FIELDS.items():
        label = dimensions.get(dimension)
        if label not in {"pass", "fail"}:
            continue
        if dimension in dict(sealed.get("labels") or {}):
            value = sealed["labels"][dimension]
            source = sealed["sources"][dimension]
        elif label == "pass":
            owner = verdict if dimension in {"asset_grounding", "direction", "magnitude"} else relevance
            value = owner.get(field)
            source = "accepted_recorded_output"
        else:
            value = expected.get("assets" if dimension == "asset_grounding" else dimension)
            source = "accepted_correction"
        if value is None:
            missing[dimension] = "reviewed_output_missing" if label == "pass" else "correction_missing"
        else:
            labels[dimension], sources[dimension] = value, source
    raw_taxonomy = payload.get("taxonomy")
    if raw_taxonomy:
        try:
            taxonomy = ReviewTaxonomyV1.model_validate(raw_taxonomy).model_dump(exclude_none=True, mode="json")
        except ValidationError:
            taxonomy = {}
            missing["taxonomy"] = "invalid_label"
        labels.update({f"taxonomy.{axis}": value for axis, value in taxonomy.items()})
        sources.update({f"taxonomy.{axis}": "accepted_label" for axis in taxonomy})
    novelty = dict(review.get("novelty") or payload.get("novelty") or {})
    if novelty.get("judgment") in {"new_fact", "progression", "restatement"}:
        targets = tuple(dict.fromkeys([str(novelty.get("duplicate_of") or ""), *novelty.get("equivalent_targets", ())]))
        targets = tuple(target for target in targets if target)
        if context_exact is False:
            missing["novelty"] = "historical_context_missing"
        elif sealed.get("missing", {}).get("novelty") == "delivery_unsettled" or (
            novelty["judgment"] == "restatement" and set(targets) & set(unsettled_event_ids)
        ):
            missing["novelty"] = "delivery_unsettled"
        elif (
            novelty["judgment"] == "restatement"
            and told_event_ids is not None
            and not set(targets) & set(told_event_ids)
        ):
            missing["novelty"] = "retrieval_miss"
        else:
            labels["novelty"] = novelty["judgment"]
            sources["novelty"] = "accepted_label"
        labels["duplicate_targets"] = list(targets)
    explanation = dict(payload.get("explanation") or {})
    # Error names are diagnostic clues. Support requires a reviewed card dimension;
    # coverage and forbidden claims require actual stated facts.
    if any(dimensions.get(name) in {"pass", "fail"} for name in CARD_DIMENSIONS):
        labels["explanation.support"] = True
        sources["explanation.support"] = "frozen_evidence"
    for key in ("key_facts", "forbidden_claims"):
        if explanation.get(key):
            labels[f"explanation.{key}"] = list(explanation[key])
            sources[f"explanation.{key}"] = "accepted_correction"
    mask = sorted(key for key in labels if key != "duplicate_targets")
    targets = tuple(
        target
        for target, enabled in (
            ("classification", any(key.startswith("taxonomy.") for key in mask)),
            ("understanding", any(key in SEMANTIC_FIELDS or key == "novelty" for key in mask)),
            ("explanation", any(key.startswith("explanation.") for key in mask)),
        )
        if enabled
    )
    return {
        "labels": labels,
        "mask": mask,
        "context_exact": context_exact,
        "sources": sources,
        "missing": missing,
        "targets": targets,
        "review_id": review.get("review_id"),
        "judgment_sha256": recorded.get("scored_judgment_sha256") or sealed.get("judgment_sha256"),
        "label_source": dict(payload.get("taxonomy_review") or {}).get("label_source"),
        "told_event_ids": list(told_event_ids) if told_event_ids is not None else sealed.get("told_event_ids"),
        "should_push": review.get("should_push"),
    }
