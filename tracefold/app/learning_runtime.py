"""Application composition for content-addressed Agent manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import dspy  # type: ignore[import-untyped]

from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint
from tracefold.news import NEWS_RETRIEVAL_SHA256, PROGRESSION_REVIEW_TIMEOUT_SECONDS
from tracefold.news.artifact_identity import canonical_sha, runtime_manifest_sha
from tracefold.news.learning.contracts import ArmManifest, CandidateManifest
from tracefold.news.program.artifact import (
    ProgramStrategyArtifactV1,
    load_program_artifact,
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import SemanticJudge
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.lm import AuditedConfiguredLM, RuntimeModelIdentity
from tracefold.news.program.module import NativeNewsProgram
from tracefold.news.program.progression_review import (
    PROGRESSION_REVIEW_MAX_TOKENS,
    ProgressionReviewProgram,
)
from tracefold.news.program.routing import RoutedSemanticJudge, RouteLMs
from tracefold.news.program.runtime import PROGRAM_ROUTE_DEADLINE_SECONDS, PROGRAM_VERSION
from tracefold.platform.config.models import news_model_availability


@dataclass(frozen=True, slots=True)
class ConfiguredProgramFallbackRoute:
    """The two endpoints and explicit alias state for one ordered fallback route."""

    event_semantics: ConfiguredLMEndpoint
    reader_card: ConfiguredLMEndpoint
    reader_card_alias: bool


@dataclass(frozen=True, slots=True)
class NewsProgramRuntimeComposition:
    """The application seam that owns every runtime slot, identity and Judge binding."""

    program_configured: bool
    event_semantics_primary: ConfiguredLMEndpoint
    reader_card_primary: ConfiguredLMEndpoint
    fallbacks: tuple[ConfiguredProgramFallbackRoute, ...]
    reader_card_primary_alias: bool

    def secret_free_slot_identities(self) -> dict[str, dict[str, str] | None]:
        slots: dict[str, dict[str, str] | None] = {
            "event_semantics.primary": (
                _optional_endpoint_identity(self.event_semantics_primary) if self.program_configured else None
            ),
            "reader_card.primary": (
                _optional_endpoint_identity(self.reader_card_primary) if self.program_configured else None
            ),
        }
        if self.program_configured:
            for index, fallback in enumerate(self.fallbacks, start=1):
                slots[f"event_semantics.fallback_{index}"] = _optional_endpoint_identity(fallback.event_semantics)
                slots[f"reader_card.fallback_{index}"] = _optional_endpoint_identity(fallback.reader_card)
        return slots

    def slot_aliases(self) -> dict[str, str]:
        """Name every deliberate endpoint alias instead of inferring it from equal hashes."""

        if not self.program_configured:
            return {}
        aliases: dict[str, str] = {}
        if self.reader_card_primary_alias:
            aliases["reader_card.primary"] = "event_semantics.primary"
        for index, fallback in enumerate(self.fallbacks, start=1):
            if fallback.reader_card_alias:
                aliases[f"reader_card.fallback_{index}"] = f"event_semantics.fallback_{index}"
        return aliases

    @property
    def runtime_model_bindings_sha256(self) -> str:
        return canonical_sha(
            {
                "identity_schema": "configured_runtime_binding_v3",
                "slots": self.secret_free_slot_identities(),
                "aliases": self.slot_aliases(),
            }
        )

    def semantic_judge(
        self,
        artifact: ProgramStrategyArtifactV1,
        *,
        lm_type: Any = dspy.LM,
    ) -> SemanticJudge | None:
        """Bind four configured endpoints to the native DSPy Program."""

        if not self.program_configured:
            return None
        timeout = float(PROGRAM_ROUTE_DEADLINE_SECONDS)

        primary = RouteLMs(
            event_semantics=_configured_program_lm(
                self.event_semantics_primary,
                max_tokens=artifact.event_semantics.max_tokens,
                timeout=timeout,
                predictor="event_semantics",
                route="primary",
                model_binding=artifact.event_semantics.model_bindings.primary,
                lm_type=lm_type,
            ),
            reader_card=_configured_program_lm(
                self.reader_card_primary,
                max_tokens=artifact.reader_card.max_tokens,
                timeout=timeout,
                predictor="reader_card",
                route="primary",
                model_binding=artifact.reader_card.model_bindings.primary,
                lm_type=lm_type,
            ),
        )
        fallbacks = tuple(
            RouteLMs(
                event_semantics=_configured_program_lm(
                    fallback.event_semantics,
                    max_tokens=artifact.event_semantics.max_tokens,
                    timeout=timeout,
                    predictor="event_semantics",
                    route="fallback",
                    model_binding=artifact.event_semantics.model_bindings.fallback,
                    lm_type=lm_type,
                ),
                reader_card=_configured_program_lm(
                    fallback.reader_card,
                    max_tokens=artifact.reader_card.max_tokens,
                    timeout=timeout,
                    predictor="reader_card",
                    route="fallback",
                    model_binding=artifact.reader_card.model_bindings.fallback,
                    lm_type=lm_type,
                ),
            )
            for fallback in self.fallbacks
        )
        return RoutedSemanticJudge(
            NativeNewsProgram(artifact),
            primary=primary,
            fallbacks=fallbacks,
        )

    def compile_semantic_judge(
        self,
        artifact: ProgramStrategyArtifactV1,
        *,
        lm_type: Any = dspy.LM,
    ) -> SemanticJudge | None:
        """Bind both Predictors to the one task endpoint used by offline GEPA.

        This keeps the production native Module and audited task endpoint but
        disables the whole-route deadline and cross-case breaker that GEPA does
        not run. A baseline built here therefore measures the same single-endpoint
        student without teaching the CLI how to construct model clients.
        """

        if not self.program_configured:
            return None
        endpoint = self.event_semantics_primary
        timeout = float(PROGRAM_ROUTE_DEADLINE_SECONDS)
        primary = RouteLMs(
            event_semantics=_configured_program_lm(
                endpoint,
                max_tokens=artifact.event_semantics.max_tokens,
                timeout=timeout,
                predictor="event_semantics",
                route="primary",
                model_binding=artifact.event_semantics.model_bindings.primary,
                lm_type=lm_type,
            ),
            reader_card=_configured_program_lm(
                endpoint,
                max_tokens=artifact.reader_card.max_tokens,
                timeout=timeout,
                predictor="reader_card",
                route="primary",
                model_binding=artifact.reader_card.model_bindings.primary,
                lm_type=lm_type,
            ),
        )
        return RoutedSemanticJudge(
            NativeNewsProgram(artifact),
            primary=primary,
            route_deadline_seconds=None,
            primary_breaker_enabled=False,
        )

    def progression_verifier(
        self,
        *,
        lm_type: Any = dspy.LM,
    ) -> ProgressionReviewProgram | None:
        """Bind the post-delivery relationship check to the primary event-semantics endpoint."""

        if not self.program_configured:
            return None
        endpoint = self.event_semantics_primary
        lm = _configured_program_lm(
            endpoint,
            timeout=PROGRESSION_REVIEW_TIMEOUT_SECONDS,
            max_tokens=PROGRESSION_REVIEW_MAX_TOKENS,
            predictor="progression_review",
            route="primary",
            model_binding="progression_review.primary",
            lm_type=lm_type,
        )
        return ProgressionReviewProgram(lm)


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
            request_config=reader_settings.request,
        )
    else:
        reader_model = availability.reader_card_model or "unconfigured"
        reader_primary = configured_lm_endpoint(settings, model_name=reader_model)

    fallback_routes: list[ConfiguredProgramFallbackRoute] = []
    valid_fallback_settings = tuple(
        fallback
        for fallback in settings.llm.news_fallbacks
        if (
            fallback.configured
            and _is_http_endpoint(fallback.base_url)
            and (not fallback.reader_card.configured or _is_http_endpoint(fallback.reader_card.base_url))
        )
    )
    for fallback_settings, event_model, reader_model, reader_dedicated in zip(
        valid_fallback_settings,
        availability.triage_fallback_models,
        availability.reader_card_fallback_models,
        availability.reader_card_fallback_dedicated,
        strict=True,
    ):
        event_fallback = configured_lm_endpoint(
            settings,
            model_name=event_model,
            api_key=fallback_settings.api_key,
            base_url=fallback_settings.base_url,
            request_config=fallback_settings.request,
        )
        reader_fallback_settings = fallback_settings.reader_card
        if reader_dedicated:
            reader_fallback = configured_lm_endpoint(
                settings,
                model_name=reader_model,
                api_key=reader_fallback_settings.api_key,
                base_url=reader_fallback_settings.base_url,
                request_config=reader_fallback_settings.request,
            )
        else:
            reader_fallback = event_fallback
        fallback_routes.append(
            ConfiguredProgramFallbackRoute(
                event_semantics=event_fallback,
                reader_card=reader_fallback,
                reader_card_alias=not reader_fallback_settings.configured,
            )
        )
    return NewsProgramRuntimeComposition(
        program_configured=availability.program_configured,
        event_semantics_primary=event_primary,
        reader_card_primary=reader_primary,
        fallbacks=tuple(fallback_routes),
        reader_card_primary_alias=not settings.llm.news_reader_card.configured,
    )


def _is_http_endpoint(value: str | None) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


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
        program_version=PROGRAM_VERSION,
        program_sha256=artifact.program_sha256,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
        runtime_model_bindings_sha256=composition.runtime_model_bindings_sha256,
        # Composite identity for both bounded source assembly and candidate-conditioned selection.
        retrieval_sha256=NEWS_RETRIEVAL_SHA256,
        policy=policy,
        policy_sha256=canonical_sha(policy),
    )


def candidate_program_artifact(
    candidate: CandidateManifest,
    stable_artifact: ProgramStrategyArtifactV1,
) -> ProgramStrategyArtifactV1:
    """Resolve and validate the Program executable carried by one candidate.

    A candidate must resolve to an image-carried artifact whose parent, recorded on the candidate's own
    proposal receipt, is that exact stable Program — lineage belongs to the candidate, not to the running
    behavior. This resolver is shared by worker composition and the canary control CLI so an artifact
    rejected at startup cannot later be armed from its manifest alone. The policy-candidate branch is gone
    with the policy candidate itself (#202 §1.3).
    """

    arm = candidate.candidate_arm
    if (
        arm.program_version != PROGRAM_VERSION
        or candidate.proposal_receipt.program_parent_sha256 != stable_artifact.program_sha256
        or candidate.proposal_receipt.program_candidate_sha256 != arm.program_sha256
    ):
        raise ValueError("news_candidate_program_parent_mismatch")
    return load_program_artifact(arm.program_sha256)


def artifact_valid_candidate_bundles(
    stable: ArmManifest,
    candidates: Mapping[str, CandidateManifest],
) -> dict[str, str]:
    """Return only same-parent candidates whose executable artifact validates."""

    stable_artifact = load_stable_program_artifact()
    if stable.program_version != PROGRAM_VERSION or stable_artifact.program_sha256 != stable.program_sha256:
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


def _configured_program_lm(
    endpoint: ConfiguredLMEndpoint,
    *,
    timeout: float,
    max_tokens: int,
    predictor: str,
    route: str,
    model_binding: str,
    lm_type: Any = dspy.LM,
) -> AuditedConfiguredLM:
    """Create a stock DSPy LM and add only Tracefold's secret-free audit seam."""

    request: dict[str, Any] = {
        "api_key": endpoint.api_key,
        "api_base": endpoint.api_base,
        "timeout": float(timeout),
        "max_tokens": int(max_tokens),
        "cache": False,
        "num_retries": 0,
        **dict(endpoint.model_kwargs),
    }
    if endpoint.temperature is not None:
        request["temperature"] = float(endpoint.temperature)
    delegate = lm_type(str(endpoint.model_name), **request)
    if not isinstance(delegate, dspy.BaseLM):
        raise TypeError("news_program_configured_lm_factory_invalid")
    return AuditedConfiguredLM(
        delegate,
        structured_output=endpoint.structured_output,
        runtime_identity=RuntimeModelIdentity.issue(
            provider=_endpoint_provider(endpoint),
            model=str(endpoint.model_name),
            model_sha256=_endpoint_model_sha256(endpoint),
        ),
        predictor=predictor,
        route=route,
        model_binding=model_binding,
    )


def _endpoint_identity(endpoint: ConfiguredLMEndpoint) -> dict[str, str]:
    """Use the same secret-free identity that each live Predictor request carries."""

    model = str(endpoint.model_name)
    provider = _endpoint_provider(endpoint)
    return RuntimeModelIdentity.issue(
        provider=provider,
        model=model,
        model_sha256=_endpoint_model_sha256(endpoint),
    ).model_dump(mode="json")


def _endpoint_model_sha256(endpoint: ConfiguredLMEndpoint) -> str:
    """Fingerprint one configured backend and its secret-free request semantics."""

    model = str(endpoint.model_name)
    provider = _endpoint_provider(endpoint)
    return canonical_sha(
        {
            "identity_schema": "configured_endpoint_model_v3",
            "provider": provider,
            "model": model,
            "endpoint_sha256": _canonical_endpoint_sha256(endpoint.api_base),
            "temperature": endpoint.temperature,
            "structured_output": endpoint.structured_output,
            "model_kwargs_sha256": canonical_sha(endpoint.model_kwargs),
        }
    )


def _endpoint_provider(endpoint: ConfiguredLMEndpoint) -> str:
    model = str(endpoint.model_name)
    return model.split("/", maxsplit=1)[0] if "/" in model else "unknown"


def _canonical_endpoint_sha256(value: str) -> str:
    """Fingerprint an equivalent HTTP endpoint identically without retaining its URL."""

    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("news_runtime_model_endpoint_identity_invalid") from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("news_runtime_model_endpoint_identity_invalid")
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    canonical_endpoint = urlunsplit(SplitResult(scheme, netloc, path, "", ""))
    return canonical_sha(
        {
            "identity_schema": "configured_endpoint_v1",
            "canonical_endpoint": canonical_endpoint,
        }
    )


def _optional_endpoint_identity(endpoint: ConfiguredLMEndpoint | None) -> dict[str, str] | None:
    return _endpoint_identity(endpoint) if endpoint is not None else None


__all__ = [
    "NewsProgramRuntimeComposition",
    "active_arm_manifest",
    "artifact_valid_candidate_bundles",
    "candidate_program_artifact",
    "canonical_sha",
    "compose_news_program_runtime",
    "runtime_manifest_sha",
]
