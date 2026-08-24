"""Trusted host-side Program compiler adapter.

This module owns the corpus-to-demo projection, restricted patch application,
hash-only diff and state-only candidate writer.  It deliberately does not
import the optimizer module, GEPA, or any provider client; the CLI can perform
all privileged post-container work without loading untrusted compiler code.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...agents.semantic_program import (
    CompileReceipt,
    DemoRecord,
    EligibleDemoBank,
    EventSemantics,
    ProgramArtifact,
    ProgramArtifactCodec,
    ProgramPatchV2,
    ReaderCard,
    _reader_card_semantic_view,
    load_stable_program_artifact,
    render_model_evidence_json,
)
from ...agents.semantic_program import (
    apply_program_patch_v2 as _apply_program_patch_v2,
)
from ...artifact_identity import canonical_json, canonical_sha
from ...semantic_contract import ScoredJudgment, TriageContext
from ..metric import production_decision
from .security import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    OptimizerCompileProvenanceV3,
)

LEARNING_EPOCH: Literal["program_v6"] = "program_v6"
# The reflection endpoint's budget, declared on the trusted seam because the CLI may not import the optimizer
# module. GEPA's reflection call reads a minibatch of failures and emits a whole replacement instruction, so it
# is nothing like a Program route: DSPy documents 32k output tokens for it, while the task route's ceiling here
# is 1,200 — below even what `LearnedStrategy` itself accepts.


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _DevelopmentEpisode(_ExactModel):
    case_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    context: TriageContext
    accepted_review: dict[str, Any]
    production_judgment: ScoredJudgment | None = None
    # Sealed policy-metric projection. The demo builder ignores it; it exists so this validator does not
    # reject the same sealed episode the compiler reads.
    policy_metric: dict[str, Any] = Field(default_factory=dict)


def build_eligible_demo_bank(
    *,
    dataset_sha: str,
    dataset_payload: Mapping[str, Any],
    episodes: Sequence[BaseModel | Mapping[str, Any]],
) -> EligibleDemoBank:
    """Derive typed, exact-match demos from accepted development truth only."""

    payload = _json_safe(dict(dataset_payload))
    if dataset_sha != canonical_sha({"kind": "dataset", "payload": payload}):
        raise ValueError("news_program_compile_development_dataset_hash_mismatch")
    if payload.get("role") != "development" or payload.get("learning_epoch") != LEARNING_EPOCH:
        raise ValueError("news_program_compile_demo_dataset_invalid")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("news_program_compile_dataset_cases_invalid")
    cases: dict[str, dict[str, Any]] = {}
    ordered_case_ids: list[str] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise ValueError("news_program_compile_dataset_cases_invalid")
        case = _json_safe(dict(raw_case))
        case_id = str(case.get("case_id") or "")
        if not case_id or case_id in cases:
            raise ValueError("news_program_compile_dataset_case_identity_invalid")
        cases[case_id] = case
        ordered_case_ids.append(case_id)
    parsed_episodes = tuple(
        _DevelopmentEpisode.model_validate(
            episode.model_dump(mode="json") if isinstance(episode, BaseModel) else episode
        )
        for episode in episodes
    )
    if [episode.case_id for episode in parsed_episodes] != ordered_case_ids:
        raise ValueError("news_program_compile_dataset_episode_membership_mismatch")

    records: list[DemoRecord] = []
    for episode in parsed_episodes:
        case = cases[episode.case_id]
        if str(case.get("cluster_id") or "") != episode.cluster_id:
            raise ValueError("news_program_compile_dataset_episode_membership_mismatch")
        production_judgment = episode.production_judgment
        if production_judgment is None:
            continue
        verdict_payload = production_judgment.verdict.model_dump(mode="json")
        decision = production_decision(production_judgment, episode.policy_metric)
        if not _accepted_review_is_demo_truth(
            episode.accepted_review,
            verdict_payload,
            decision.final,
        ):
            continue
        relevance = production_judgment.editorial.relevance
        if relevance is None:
            continue
        semantics_payload = {
            **{name: verdict_payload[name] for name in EventSemantics.model_fields if name != "relevance"},
            "relevance": relevance.model_dump(mode="json"),
        }
        semantics = EventSemantics.model_validate(semantics_payload)
        card = ReaderCard.model_validate({name: verdict_payload[name] for name in ReaderCard.model_fields})
        semantics_evidence_json = render_model_evidence_json(
            episode.context.event_semantics_payload(), predictor="event_semantics"
        )
        card_evidence_json = render_model_evidence_json(episode.context.reader_card_payload(), predictor="reader_card")
        case_evidence_sha = str(case.get("evidence_sha256") or "")
        if not _is_sha256(case_evidence_sha):
            raise ValueError("news_program_compile_demo_evidence_receipt_invalid")
        case_sha = canonical_sha(case)
        cluster_sha = canonical_sha({"cluster_id": episode.cluster_id})
        review_sha = canonical_sha(
            {
                "review_id": str(episode.accepted_review.get("review_id") or case.get("review_id") or ""),
                "accepted_review": episode.accepted_review,
            }
        )
        semantics_payload = semantics.model_dump(mode="json")
        records.append(
            DemoRecord.issue(
                predictor="event_semantics",
                signature_inputs={"evidence_json": semantics_evidence_json},
                validated_output=semantics_payload,
                source_kind="accepted_development",
                development_dataset_sha256=dataset_sha,
                case_sha256=case_sha,
                cluster_sha256=cluster_sha,
                review_sha256=review_sha,
                evidence_receipt_sha256=case_evidence_sha,
            )
        )
        records.append(
            DemoRecord.issue(
                predictor="reader_card",
                signature_inputs={
                    "evidence_json": card_evidence_json,
                    "semantics_json": canonical_json(_reader_card_semantic_view(semantics).model_dump(mode="json")),
                },
                validated_output=card.model_dump(mode="json"),
                source_kind="accepted_development",
                development_dataset_sha256=dataset_sha,
                case_sha256=case_sha,
                cluster_sha256=cluster_sha,
                review_sha256=review_sha,
                evidence_receipt_sha256=case_evidence_sha,
            )
        )
    return EligibleDemoBank.issue(records)


def apply_trusted_program_patch(
    parent: ProgramArtifact,
    patch: ProgramPatchV2,
    eligible_demo_bank: EligibleDemoBank,
    provenance: OptimizerCompileProvenanceV3,
) -> ProgramArtifact:
    """Apply the sole optimizer write-set under the exact candidate receipt."""

    receipt = CompileReceipt(
        **provenance.model_dump(mode="json"),
        compiler="tracefold.news.dspy_gepa_compiler_v3",
        source="trusted_compiler_launcher_v3",
        accepted_by="unaccepted_candidate",
    )
    return _apply_program_patch_v2(parent, patch, eligible_demo_bank, receipt)


def reapply_exact_candidate(
    parent: ProgramArtifact,
    patch: ProgramPatchV2,
    eligible_demo_bank: EligibleDemoBank,
    candidate: ProgramArtifact,
) -> ProgramArtifact:
    """Rebuild a loaded candidate under its exact retained receipt."""

    rebuilt = _apply_program_patch_v2(
        parent,
        patch,
        eligible_demo_bank,
        candidate.compile_receipt,
    )
    if rebuilt != candidate:
        raise ValueError("news_learning_program_trusted_reapply_mismatch")
    return rebuilt


def load_exact_stable_program() -> ProgramArtifact:
    parent = load_stable_program_artifact()
    if (
        parent.parent_program_sha256 is not None
        or parent.schema_version != "news_semantic_program_artifact_v2"
        or parent.factory_id != "tracefold.news.semantic_program.factory_v4"
        or parent.compile_receipt.accepted_by != "code_owner"
    ):
        raise ValueError("news_program_compile_parent_must_be_exact_stable_root")
    return parent


def load_program_artifact(path: str | None = None) -> ProgramArtifact:
    return ProgramArtifactCodec.load(path)


def optimizer_provenance_from_artifact(artifact: ProgramArtifact) -> OptimizerCompileProvenanceV3:
    receipt = artifact.compile_receipt
    return OptimizerCompileProvenanceV3.model_validate(
        {name: getattr(receipt, name) for name in OptimizerCompileProvenanceV3.model_fields}
    )


def parse_program_patch(payload: Mapping[str, Any]) -> ProgramPatchV2:
    return ProgramPatchV2.model_validate(payload)


def program_machine_diff(parent: ProgramArtifact, candidate: ProgramArtifact) -> dict[str, Any]:
    """Return the canonical hash/ID-only v2 diff; never prompt/demo content."""

    if (
        candidate.parent_program_sha256 != parent.program_sha256
        or candidate.factory_id != parent.factory_id
        or candidate.quality_kernel != parent.quality_kernel
        or candidate.route_spec != parent.route_spec
        or candidate.execution != parent.execution
        or candidate.rule_packs != parent.rule_packs
    ):
        raise ValueError("news_program_compile_machine_diff_immutable_change")
    strategies = []
    for predictor in ("event_semantics", "reader_card"):
        before = parent.strategy_for(predictor)
        after = candidate.strategy_for(predictor)
        strategies.append(
            {
                "predictor": predictor,
                "before_text_sha256": before.text_sha256,
                "after_text_sha256": after.text_sha256,
                "before_source": before.source,
                "after_source": after.source,
                "changed": before.text_sha256 != after.text_sha256,
            }
        )
    demo_refs = {
        predictor: {
            "before": list(getattr(parent.demo_bank.refs, predictor)),
            "after": list(getattr(candidate.demo_bank.refs, predictor)),
        }
        for predictor in ("event_semantics", "reader_card")
    }
    if not any(item["changed"] for item in strategies) and all(
        value["before"] == value["after"] for value in demo_refs.values()
    ):
        raise ValueError("news_program_compile_machine_diff_empty")
    payload = {
        "schema_version": "tracefold.news.program_machine_diff.v3",
        "parent_program_sha256": parent.program_sha256,
        "parent_state_sha256": parent.state_sha256,
        "candidate_program_sha256": candidate.program_sha256,
        "candidate_state_sha256": candidate.state_sha256,
        "immutable": {
            "factory_id": parent.factory_id,
            "quality_kernel_sha256": parent.quality_kernel.sha256,
            "rule_pack_root_sha256": parent.rule_pack_root_sha256,
            "route_spec_sha256": canonical_sha(parent.route_spec.model_dump(mode="json")),
            "execution_sha256": canonical_sha(parent.execution.model_dump(mode="json")),
        },
        "learned_strategies": strategies,
        "demo_refs": demo_refs,
        "selected_record_root_sha256": candidate.demo_bank.selected_record_root_sha256,
        "eligible_demo_bank_root_sha256": candidate.demo_bank.eligible_demo_bank_root_sha256,
    }
    return {**payload, "diff_sha256": canonical_sha(payload)}


def write_program_candidate_artifact(artifact: ProgramArtifact, *, artifact_root: Path) -> str:
    """Persist one already trusted/applied two-file state image atomically."""

    requested_root = Path(artifact_root)
    if ".." in requested_root.parts or requested_root.is_symlink():
        raise ValueError("news_program_compile_artifact_root_invalid")
    requested_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("news_program_compile_artifact_root_invalid") from exc
    if not root.is_dir() or requested_root.absolute().resolve() != root:
        raise ValueError("news_program_compile_artifact_root_invalid")
    destination = root / artifact.program_sha256
    if destination.exists():
        if ProgramArtifactCodec.load(str(destination)) != artifact:
            raise ValueError("news_program_compile_artifact_collision")
        return str(destination)
    temporary = root / f".{artifact.program_sha256}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    manifest, state = ProgramArtifactCodec.encode(artifact)
    try:
        _write_exclusive(temporary / "manifest.json", manifest)
        _write_exclusive(temporary / "state.json", state)
        os.rename(temporary, destination)
    except Exception:
        for child in temporary.iterdir() if temporary.exists() else ():
            child.unlink(missing_ok=True)
        if temporary.exists():
            temporary.rmdir()
        raise
    if ProgramArtifactCodec.load(str(destination)) != artifact:
        raise ValueError("news_program_compile_artifact_write_verification_failed")
    return str(destination)


def _accepted_review_is_demo_truth(
    review: Mapping[str, Any],
    verdict_payload: Mapping[str, Any],
    final_decision: str,
) -> bool:
    dimensions = dict(review.get("dimensions") or {})
    required = {"factual_fidelity", "headline_fidelity", "why_support", "why_value"}
    if verdict_payload.get("assets"):
        required.add("asset_grounding")
    if verdict_payload.get("direction") in {"bullish", "bearish"}:
        required.update(("direction", "magnitude"))
    if any(dimensions.get(dimension) != "pass" for dimension in required):
        return False
    if str(review.get("expected_correction") or "").strip():
        return False
    novelty = dict(review.get("novelty") or {})
    if novelty.get("judgment") not in {"new_fact", "progression", "restatement"}:
        return False
    if novelty.get("judgment") != verdict_payload.get("novelty"):
        return False
    should_push = str(review.get("should_push") or "uncertain")
    if should_push in {"must_push", "should_push"}:
        return final_decision in {"push", "escalate"}
    if should_push in {"must_hold", "should_hold"}:
        return final_decision in {"drop", "throttled"}
    return False


def _write_exclusive(path: Path, document: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = document.encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("news_program_compile_non_string_json_key")
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"news_program_compile_non_json_receipt_value:{type(value).__name__}")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "ProgramPatchV2",
    "apply_trusted_program_patch",
    "build_eligible_demo_bank",
    "load_exact_stable_program",
    "load_program_artifact",
    "optimizer_provenance_from_artifact",
    "parse_program_patch",
    "program_machine_diff",
    "reapply_exact_candidate",
    "write_program_candidate_artifact",
]
