"""Application composition for content-addressed Agent manifests."""

from __future__ import annotations

import os
from typing import Any, NamedTuple

from tracefold.news import ArmManifest, canonical_sha
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.platform.config.settings import news_model_availability


def active_arm_manifest(settings: Any) -> ArmManifest:
    """Describe the exact stable arm wired into this process."""

    availability = news_model_availability(settings)
    artifact = load_stable_program_artifact()
    primary_model = str(availability.triage_model or settings.llm.news_triage_model or "unconfigured")
    fallback_model = str(availability.triage_fallback_model or "unconfigured")
    policy = settings.news.policy.model_dump(mode="json")
    return ArmManifest(
        program_version=artifact.program_version,
        program_sha256=artifact.program_sha256,
        runtime_model_bindings_sha256=canonical_sha(
            {
                "event_semantics.primary": {"provider": "litellm", "model": primary_model},
                "reader_card.primary": {"provider": "litellm", "model": primary_model},
                "event_semantics.fallback": {"provider": "litellm", "model": fallback_model},
                "reader_card.fallback": {"provider": "litellm", "model": fallback_model},
                "snapshot_kind": "mutable_alias",
            }
        ),
        retrieval_sha256=canonical_sha(
            {"contract": "event_evidence_v1+told_sent_ledger_v2", "told_limit": 12, "window_hours": 4}
        ),
        policy=policy,
        policy_sha256=canonical_sha(policy),
    )


IMAGE_DIGEST_ENV = "TRACEFOLD_IMAGE_DIGEST"
RUNTIME_REVISION_ENV = "TRACEFOLD_RUNTIME_REVISION"
UNVERSIONED = "unversioned"


class RuntimeIdentity(NamedTuple):
    """What this process can actually prove about the binary it is running."""

    image_digest: str
    runtime_revision: str


def runtime_identity(environ: Any = None) -> RuntimeIdentity:
    """Read the deployed image identity, normalising every absent form to one value.

    ``os.getenv(name, UNVERSIONED)`` is not enough: compose renders an unset
    ``${TRACEFOLD_IMAGE_DIGEST:-}`` as an empty string, so the variable exists and
    the default never fires.  A release receipt that records ``""`` claims an
    identity it does not have, which is worse than admitting there is none.
    """

    source = os.environ if environ is None else environ
    return RuntimeIdentity(
        image_digest=str(source.get(IMAGE_DIGEST_ENV, "") or "").strip() or UNVERSIONED,
        runtime_revision=str(source.get(RUNTIME_REVISION_ENV, "") or "").strip() or UNVERSIONED,
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


__all__ = [
    "IMAGE_DIGEST_ENV",
    "RUNTIME_REVISION_ENV",
    "UNVERSIONED",
    "RuntimeIdentity",
    "active_arm_manifest",
    "canonical_sha",
    "runtime_identity",
    "runtime_manifest_sha",
]
