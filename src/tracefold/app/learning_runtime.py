"""Application composition for content-addressed Agent manifests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint
from tracefold.news import ArmManifest, CandidateManifest, canonical_sha
from tracefold.news.agents.semantic_program import (
    DspyNewsSemanticProgram,
    DspyPredictorAdapter,
    ProgramArtifact,
    RuntimeModelIdentity,
    load_program_artifact,
    load_stable_program_artifact,
)
from tracefold.platform.config.settings import news_model_availability


@dataclass(frozen=True, slots=True)
class NewsProgramRuntimeComposition:
    """The application seam that owns every runtime slot, identity and Judge binding."""

    program_configured: bool
    event_semantics_primary: ConfiguredLMEndpoint
    reader_card_primary: ConfiguredLMEndpoint
    event_semantics_fallback: ConfiguredLMEndpoint | None
    reader_card_fallback: ConfiguredLMEndpoint | None

    def secret_free_slot_identities(self) -> dict[str, dict[str, str] | None]:
        return {
            "event_semantics.primary": _optional_endpoint_identity(self.event_semantics_primary),
            "reader_card.primary": _optional_endpoint_identity(self.reader_card_primary),
            "event_semantics.fallback": _optional_endpoint_identity(self.event_semantics_fallback),
            "reader_card.fallback": _optional_endpoint_identity(self.reader_card_fallback),
        }

    @property
    def runtime_model_bindings_sha256(self) -> str:
        return canonical_sha(
            {
                "identity_schema": "configured_runtime_binding_v1",
                **self.secret_free_slot_identities(),
            }
        )

    def semantic_judge(
        self,
        artifact: ProgramArtifact,
        *,
        adapter_type: Any = DspyPredictorAdapter,
    ) -> DspyNewsSemanticProgram | None:
        """Bind artifact slot names to Predictor-local Adapters without changing the News Interface."""

        if not self.program_configured:
            return None
        timeout = float(artifact.execution.route_deadline_seconds)

        def adapter(endpoint: ConfiguredLMEndpoint, *, max_tokens: int) -> Any:
            return adapter_type.from_runtime(
                model_name=endpoint.model_name,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                timeout=timeout,
                max_tokens=max_tokens,
                model_sha256=_endpoint_model_sha256(endpoint),
                model_kwargs=endpoint.model_kwargs,
            )

        primary_adapters = {
            artifact.event_semantics.model_bindings.primary: adapter(
                self.event_semantics_primary,
                max_tokens=artifact.event_semantics.max_tokens,
            ),
            artifact.reader_card.model_bindings.primary: adapter(
                self.reader_card_primary,
                max_tokens=artifact.reader_card.max_tokens,
            ),
        }
        fallback_adapters = None
        if self.event_semantics_fallback is not None and self.reader_card_fallback is not None:
            fallback_adapters = {
                artifact.event_semantics.model_bindings.fallback: adapter(
                    self.event_semantics_fallback,
                    max_tokens=artifact.event_semantics.max_tokens,
                ),
                artifact.reader_card.model_bindings.fallback: adapter(
                    self.reader_card_fallback,
                    max_tokens=artifact.reader_card.max_tokens,
                ),
            }
        return DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary_adapters,
            fallback_adapter=fallback_adapters,
        )


def compose_news_program_runtime(settings: Any) -> NewsProgramRuntimeComposition:
    """Resolve operator settings once into the four secret-free Program slot identities and endpoints."""

    availability = news_model_availability(settings)
    primary_model = str(availability.triage_model or settings.llm.news_triage_model or "unconfigured")
    event_primary = configured_lm_endpoint(settings, model_name=primary_model)
    if availability.reader_card_dedicated and availability.reader_card_model:
        reader_settings = settings.llm.news_reader_card
        reader_primary = configured_lm_endpoint(
            settings,
            model_name=availability.reader_card_model,
            api_key=reader_settings.api_key,
            base_url=reader_settings.base_url,
        )
    else:
        reader_model = availability.reader_card_model or "unconfigured"
        reader_primary = configured_lm_endpoint(settings, model_name=reader_model)

    event_fallback: ConfiguredLMEndpoint | None = None
    reader_fallback: ConfiguredLMEndpoint | None = None
    if availability.triage_fallback_model:
        fallback_settings = settings.llm.news_triage_fallback
        fallback_kwargs = {
            "model_name": availability.triage_fallback_model,
            "api_key": fallback_settings.api_key,
            "base_url": fallback_settings.base_url,
        }
        event_fallback = configured_lm_endpoint(settings, **fallback_kwargs)
        reader_fallback = configured_lm_endpoint(settings, **fallback_kwargs)
    return NewsProgramRuntimeComposition(
        program_configured=availability.program_configured,
        event_semantics_primary=event_primary,
        reader_card_primary=reader_primary,
        event_semantics_fallback=event_fallback,
        reader_card_fallback=reader_fallback,
    )


def active_arm_manifest(
    settings: Any,
    *,
    runtime_composition: NewsProgramRuntimeComposition | None = None,
) -> ArmManifest:
    """Describe the exact stable arm wired into this process."""

    artifact = load_stable_program_artifact()
    composition = runtime_composition or compose_news_program_runtime(settings)
    policy = settings.news.policy.model_dump(mode="json")
    return ArmManifest(
        program_version=artifact.program_version,
        program_sha256=artifact.program_sha256,
        runtime_model_bindings_sha256=composition.runtime_model_bindings_sha256,
        retrieval_sha256=canonical_sha(
            {"contract": "event_evidence_v1+told_sent_ledger_v2", "told_limit": 12, "window_hours": 4}
        ),
        policy=policy,
        policy_sha256=canonical_sha(policy),
    )


def candidate_program_artifact(candidate: CandidateManifest, stable_artifact: ProgramArtifact) -> ProgramArtifact:
    """Resolve and validate the Program executable carried by one candidate.

    Policy candidates reuse the stable artifact identity. Program candidates
    must resolve to an image-carried child of that exact stable Program. This
    resolver is shared by worker composition and the canary control CLI so an
    artifact rejected at startup cannot later be armed from its manifest alone.
    """

    arm = candidate.candidate_arm
    if candidate.target == "policy":
        if (
            arm.program_version != stable_artifact.program_version
            or arm.program_sha256 != stable_artifact.program_sha256
        ):
            raise ValueError("news_policy_candidate_program_identity_changed")
        return stable_artifact
    artifact = load_program_artifact(arm.program_sha256)
    if (
        artifact.program_version != arm.program_version
        or artifact.parent_program_sha256 != stable_artifact.program_sha256
    ):
        raise ValueError("news_candidate_program_parent_mismatch")
    return artifact


def artifact_valid_candidate_bundles(
    stable: ArmManifest,
    candidates: Mapping[str, CandidateManifest],
) -> dict[str, str]:
    """Return only same-parent candidates whose executable artifact validates."""

    stable_artifact = load_stable_program_artifact()
    if (
        stable_artifact.program_version != stable.program_version
        or stable_artifact.program_sha256 != stable.program_sha256
    ):
        raise ValueError("news_stable_program_manifest_mismatch")
    shipped: dict[str, str] = {}
    for candidate_sha, candidate in candidates.items():
        if candidate.parent_stable_sha != stable.bundle_sha:
            continue
        try:
            candidate_program_artifact(candidate, stable_artifact)
        except (OSError, ValueError):
            continue
        shipped[candidate_sha] = candidate.candidate_arm.bundle_sha
    return shipped


def _endpoint_identity(endpoint: ConfiguredLMEndpoint) -> dict[str, str]:
    """Use the same secret-free identity that each live Predictor request carries."""

    model = str(endpoint.model_name)
    provider = model.split("/", maxsplit=1)[0] if "/" in model else "unknown"
    return RuntimeModelIdentity.issue(
        provider=provider,
        model=model,
        model_sha256=_endpoint_model_sha256(endpoint),
    ).model_dump(mode="json")


def _endpoint_model_sha256(endpoint: ConfiguredLMEndpoint) -> str:
    """Fingerprint one configured backend without exposing its URL or credential."""

    model = str(endpoint.model_name)
    provider = model.split("/", maxsplit=1)[0] if "/" in model else "unknown"
    return canonical_sha(
        {
            "identity_schema": "configured_endpoint_model_v1",
            "provider": provider,
            "model": model,
            "endpoint_sha256": canonical_sha({"api_base": str(endpoint.api_base)}),
        }
    )


def _optional_endpoint_identity(endpoint: ConfiguredLMEndpoint | None) -> dict[str, str] | None:
    return _endpoint_identity(endpoint) if endpoint is not None else None


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
    "NewsProgramRuntimeComposition",
    "RuntimeIdentity",
    "active_arm_manifest",
    "artifact_valid_candidate_bundles",
    "candidate_program_artifact",
    "canonical_sha",
    "compose_news_program_runtime",
    "runtime_identity",
    "runtime_manifest_sha",
]
