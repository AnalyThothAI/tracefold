"""The offline learning plane's composition of the retired three-Predictor Program.

Only the learning, baseline and release commands import this module; the Workers runtime composes
the EventUpdate agent from `tracefold.app.learning_runtime` instead. It is deleted together with that
offline plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import dspy  # type: ignore[import-untyped]

from tracefold.app.learning_runtime import _endpoint_model_sha256, _endpoint_provider
from tracefold.app.llm import ConfiguredLMEndpoint, configured_lm_endpoint
from tracefold.news.artifact_identity import canonical_sha, runtime_manifest_sha
from tracefold.news.learning.contracts import ArmManifest
from tracefold.news.program.artifact import (
    NewsProgramStateV1,
    load_stable_program_state,
)
from tracefold.news.program.contracts import SemanticJudge
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.lm import AuditedConfiguredLM, RuntimeModelIdentity
from tracefold.news.program.module import NativeNewsProgram
from tracefold.news.program.routing import RoutedSemanticJudge, RouteLMs
from tracefold.news.program.runtime import PROGRAM_ROUTE_DEADLINE_SECONDS, PROGRAM_VERSION
from tracefold.news.told_context import NEWS_RETRIEVAL_SHA256
from tracefold.platform.config.models import news_model_availability


@dataclass(frozen=True, slots=True)
class NewsProgramRuntimeComposition:
    """The application seam that owns every runtime slot, identity and Judge binding."""

    program_configured: bool
    event_semantics_primary: ConfiguredLMEndpoint
    reader_card_primary: ConfiguredLMEndpoint
    event_semantics_fallback: ConfiguredLMEndpoint | None
    reader_card_fallback: ConfiguredLMEndpoint | None
    reader_card_primary_alias: bool
    reader_card_fallback_alias: bool

    # The taxonomy Predictor (#501) has no operator setting of its own: it always runs on the Triage
    # endpoint of its route, so both of its slots are declared aliases of the EventSemantics slots.
    @property
    def taxonomy_primary(self) -> ConfiguredLMEndpoint:
        return self.event_semantics_primary

    @property
    def taxonomy_fallback(self) -> ConfiguredLMEndpoint | None:
        return self.event_semantics_fallback

    def secret_free_slot_identities(self) -> dict[str, dict[str, str] | None]:
        if not self.program_configured:
            return {
                "event_semantics.primary": None,
                "taxonomy.primary": None,
                "reader_card.primary": None,
                "event_semantics.fallback": None,
                "taxonomy.fallback": None,
                "reader_card.fallback": None,
            }
        return {
            "event_semantics.primary": _optional_endpoint_identity(self.event_semantics_primary),
            "taxonomy.primary": _optional_endpoint_identity(self.taxonomy_primary),
            "reader_card.primary": _optional_endpoint_identity(self.reader_card_primary),
            "event_semantics.fallback": _optional_endpoint_identity(self.event_semantics_fallback),
            "taxonomy.fallback": _optional_endpoint_identity(self.taxonomy_fallback),
            "reader_card.fallback": _optional_endpoint_identity(self.reader_card_fallback),
        }

    def slot_aliases(self) -> dict[str, str]:
        """Name every deliberate endpoint alias instead of inferring it from equal hashes."""

        if not self.program_configured:
            return {}
        aliases: dict[str, str] = {"taxonomy.primary": "event_semantics.primary"}
        if self.reader_card_primary_alias:
            aliases["reader_card.primary"] = "event_semantics.primary"
        if self.event_semantics_fallback is not None:
            aliases["taxonomy.fallback"] = "event_semantics.fallback"
        if self.reader_card_fallback_alias:
            aliases["reader_card.fallback"] = "event_semantics.fallback"
        return aliases

    @property
    def runtime_model_bindings_sha256(self) -> str:
        return canonical_sha(
            {
                "identity_schema": "configured_runtime_binding_v2",
                "slots": self.secret_free_slot_identities(),
                "aliases": self.slot_aliases(),
            }
        )

    def semantic_judge(
        self,
        state: NewsProgramStateV1,
        *,
        lm_type: Any = dspy.LM,
    ) -> SemanticJudge | None:
        """Bind the six configured slots to the native DSPy Program."""

        if not self.program_configured:
            return None
        timeout = float(PROGRAM_ROUTE_DEADLINE_SECONDS)

        primary = RouteLMs(
            event_semantics=_configured_program_lm(
                self.event_semantics_primary,
                max_tokens=state.event_semantics.max_tokens,
                timeout=timeout,
                predictor="event_semantics",
                route="primary",
                model_binding=state.event_semantics.model_bindings.primary,
                lm_type=lm_type,
            ),
            taxonomy=_configured_program_lm(
                self.taxonomy_primary,
                max_tokens=state.taxonomy.max_tokens,
                timeout=timeout,
                predictor="taxonomy",
                route="primary",
                model_binding=state.taxonomy.model_bindings.primary,
                lm_type=lm_type,
            ),
            reader_card=_configured_program_lm(
                self.reader_card_primary,
                max_tokens=state.reader_card.max_tokens,
                timeout=timeout,
                predictor="reader_card",
                route="primary",
                model_binding=state.reader_card.model_bindings.primary,
                lm_type=lm_type,
            ),
        )
        fallback = None
        if self.event_semantics_fallback is not None and self.reader_card_fallback is not None:
            fallback = RouteLMs(
                event_semantics=_configured_program_lm(
                    self.event_semantics_fallback,
                    max_tokens=state.event_semantics.max_tokens,
                    timeout=timeout,
                    predictor="event_semantics",
                    route="fallback",
                    model_binding=state.event_semantics.model_bindings.fallback,
                    lm_type=lm_type,
                ),
                taxonomy=_configured_program_lm(
                    self.event_semantics_fallback,
                    max_tokens=state.taxonomy.max_tokens,
                    timeout=timeout,
                    predictor="taxonomy",
                    route="fallback",
                    model_binding=state.taxonomy.model_bindings.fallback,
                    lm_type=lm_type,
                ),
                reader_card=_configured_program_lm(
                    self.reader_card_fallback,
                    max_tokens=state.reader_card.max_tokens,
                    timeout=timeout,
                    predictor="reader_card",
                    route="fallback",
                    model_binding=state.reader_card.model_bindings.fallback,
                    lm_type=lm_type,
                ),
            )
        return RoutedSemanticJudge(
            NativeNewsProgram(state),
            primary=primary,
            fallback=fallback,
        )

    def compile_semantic_judge(
        self,
        state: NewsProgramStateV1,
        *,
        lm_type: Any = dspy.LM,
    ) -> SemanticJudge | None:
        """Bind each Predictor to its own production primary slot, with no fallback route.

        Offline compile and baseline answer on exactly the endpoints production asks that Predictor on:
        `event_semantics.primary`, the taxonomy alias of it, and `reader_card.primary` — which is a
        dedicated endpoint when the operator configured one and the EventSemantics alias otherwise. Until
        #651 all three were pinned to `event_semantics_primary`, so a ReaderCard instruction optimized or
        scored here was measured against a model production never asks to write the card.

        What offline deliberately does *not* have is the rest of the route: no fallback endpoint, no
        whole-route deadline (`route_deadline_seconds=None`) and no cross-case primary breaker. Each call
        still carries its own `PROGRAM_ROUTE_DEADLINE_SECONDS` client timeout, so a hung provider ends one
        call rather than the run. Production adds the fallback route, the route deadline and the breaker on
        top of this; a compile that inherited them would attribute a candidate's score to a degraded route
        it will never run under.
        """

        if not self.program_configured:
            return None
        timeout = float(PROGRAM_ROUTE_DEADLINE_SECONDS)
        primary = RouteLMs(
            event_semantics=_configured_program_lm(
                self.event_semantics_primary,
                max_tokens=state.event_semantics.max_tokens,
                timeout=timeout,
                predictor="event_semantics",
                route="primary",
                model_binding=state.event_semantics.model_bindings.primary,
                lm_type=lm_type,
            ),
            taxonomy=_configured_program_lm(
                self.taxonomy_primary,
                max_tokens=state.taxonomy.max_tokens,
                timeout=timeout,
                predictor="taxonomy",
                route="primary",
                model_binding=state.taxonomy.model_bindings.primary,
                lm_type=lm_type,
            ),
            reader_card=_configured_program_lm(
                self.reader_card_primary,
                max_tokens=state.reader_card.max_tokens,
                timeout=timeout,
                predictor="reader_card",
                route="primary",
                model_binding=state.reader_card.model_bindings.primary,
                lm_type=lm_type,
            ),
        )
        return RoutedSemanticJudge(
            NativeNewsProgram(state),
            primary=primary,
            route_deadline_seconds=None,
            primary_breaker_enabled=False,
        )


def compose_news_program_runtime(settings: Any) -> NewsProgramRuntimeComposition:
    """Resolve operator settings once into the secret-free Program slot identities and endpoints."""

    availability = news_model_availability(settings)
    primary_model = str(availability.extraction_model or settings.llm.news_triage_model or "unconfigured")
    event_primary = configured_lm_endpoint(settings, model_name=primary_model)
    if availability.card_dedicated and availability.card_model:
        reader_settings = settings.llm.news_reader_card
        reader_primary = configured_lm_endpoint(
            settings,
            model_name=availability.card_model,
            api_key=reader_settings.api_key,
            base_url=reader_settings.base_url,
            request_config=reader_settings.request,
        )
    else:
        reader_model = availability.card_model or "unconfigured"
        reader_primary = configured_lm_endpoint(settings, model_name=reader_model)

    event_fallback: ConfiguredLMEndpoint | None = None
    reader_fallback: ConfiguredLMEndpoint | None = None
    if availability.extraction_fallback_model:
        fallback_settings = settings.llm.news_triage_fallback
        event_fallback = configured_lm_endpoint(
            settings,
            model_name=availability.extraction_fallback_model,
            api_key=fallback_settings.api_key,
            base_url=fallback_settings.base_url,
            request_config=fallback_settings.request,
        )
        reader_fallback_settings = settings.llm.news_reader_card_fallback
        if availability.card_fallback_dedicated and availability.card_fallback_model:
            reader_fallback = configured_lm_endpoint(
                settings,
                model_name=availability.card_fallback_model,
                api_key=reader_fallback_settings.api_key,
                base_url=reader_fallback_settings.base_url,
                request_config=reader_fallback_settings.request,
            )
        elif not reader_fallback_settings.configured:
            reader_fallback = event_fallback
    return NewsProgramRuntimeComposition(
        program_configured=availability.configured,
        event_semantics_primary=event_primary,
        reader_card_primary=reader_primary,
        event_semantics_fallback=event_fallback,
        reader_card_fallback=reader_fallback,
        reader_card_primary_alias=not settings.llm.news_reader_card.configured,
        reader_card_fallback_alias=bool(
            event_fallback is not None and not settings.llm.news_reader_card_fallback.configured
        ),
    )


def active_arm_manifest(
    settings: Any,
    *,
    runtime_composition: NewsProgramRuntimeComposition | None = None,
) -> ArmManifest:
    """Describe the exact stable arm wired into this process."""

    artifact = load_stable_program_state()
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


def _configured_program_lm(
    endpoint: ConfiguredLMEndpoint,
    *,
    timeout: float,
    max_tokens: int,
    predictor: str,
    route: str,
    model_binding: str,
    lm_type: Any = dspy.LM,
    ledger: Any = None,
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
    if lm_type is dspy.LM:
        request["engine"] = "litellm"
    delegate = lm_type(str(endpoint.model_name), **request)
    if not isinstance(delegate, dspy.LM):
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
        ledger=ledger,
    )


def _endpoint_identity(endpoint: ConfiguredLMEndpoint) -> dict[str, str]:
    """Use the same secret-free identity that each live Predictor request carries."""

    return RuntimeModelIdentity.issue(
        provider=_endpoint_provider(endpoint),
        model=str(endpoint.model_name),
        model_sha256=_endpoint_model_sha256(endpoint),
    ).model_dump(mode="json")


def _optional_endpoint_identity(endpoint: ConfiguredLMEndpoint | None) -> dict[str, str] | None:
    return _endpoint_identity(endpoint) if endpoint is not None else None


__all__ = [
    "NewsProgramRuntimeComposition",
    "active_arm_manifest",
    "canonical_sha",
    "compose_news_program_runtime",
    "runtime_manifest_sha",
]
