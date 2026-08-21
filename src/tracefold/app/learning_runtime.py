"""Application composition for content-addressed Agent manifests."""

from __future__ import annotations

from typing import Any

from tracefold.news import ArmManifest, canonical_sha
from tracefold.news.agents.prompts import TRIAGE_PROMPT_SHA256, TRIAGE_SCHEMA_SHA256, TRIAGE_SYSTEM_PROMPT
from tracefold.news.models import TRIAGE_PROMPT_VERSION
from tracefold.platform.config.settings import news_model_availability


def active_arm_manifest(settings: Any) -> ArmManifest:
    """Describe the exact stable arm wired into this process."""

    availability = news_model_availability(settings)
    model = str(availability.triage_model or settings.llm.news_triage_model or "unconfigured")
    policy = settings.news.policy.model_dump(mode="json")
    return ArmManifest(
        prompt_version=TRIAGE_PROMPT_VERSION,
        prompt_text=TRIAGE_SYSTEM_PROMPT,
        prompt_sha256=TRIAGE_PROMPT_SHA256,
        schema_sha256=TRIAGE_SCHEMA_SHA256,
        retrieval_sha256=canonical_sha(
            {"contract": "event_evidence_v1+told_sent_ledger_v2", "told_limit": 12, "window_hours": 4}
        ),
        provider="litellm",
        model=model,
        model_snapshot_kind="mutable_alias",
        model_sha256=canonical_sha({"provider": "litellm", "model": model, "snapshot_kind": "mutable_alias"}),
        execution_contract_sha256=canonical_sha(
            {
                "temperature": 0,
                "max_tokens": 700,
                "deadline_seconds": settings.news.triage.deadline_seconds,
                "structured_schema_sha": TRIAGE_SCHEMA_SHA256,
                "fallback_model": availability.triage_fallback_model or None,
                "primary_breaker_failures": settings.news.triage.circuit_failures,
                "primary_breaker_open_seconds": settings.news.triage.circuit_open_seconds,
                "fast_retry_contract": "triage_model_v1",
            }
        ),
        policy=policy,
        policy_sha256=canonical_sha(policy),
    )


def runtime_manifest_sha(
    *, stable_bundle_sha: str, candidate_shas: list[str], image_digest: str, runtime_revision: str
) -> str:
    return canonical_sha(
        {
            "stable_bundle_sha": stable_bundle_sha,
            "candidate_shas": sorted(candidate_shas),
            "image_digest": image_digest,
            "runtime_revision": runtime_revision,
        }
    )


__all__ = ["active_arm_manifest", "canonical_sha", "runtime_manifest_sha"]
