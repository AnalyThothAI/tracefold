"""Application composition of the News runtime's configured model routes (#706).

One seam resolves operator settings into three generative DSPy routes -- extraction, generative
judgments and cards -- plus the optional News Jev endpoint, with secret-free identities for each.
Extraction and the generative judgments share the `news_triage_model` endpoint and its fallback;
cards use `news_reader_card` (or the extraction endpoint) and its fallback. No taxonomy slot, no
progression slot, and never Trading's `trading_semantics` route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

import dspy  # type: ignore[import-untyped]

from tracefold.app.llm import ConfiguredLMEndpoint, StructuredOutputMode, configured_lm_endpoint
from tracefold.app.news_updates import NewsJudgmentEndpoint, news_program_identity
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.updates.service import GENERATION_CALL_SECONDS
from tracefold.platform.config.models import NewsModelAvailability, news_model_availability

# Code-owned generation ceilings per role. A native Jev route has none: it is not a chat model.
EXTRACTION_MAX_TOKENS: Final = 4_000
JUDGMENT_MAX_TOKENS: Final = 2_000
CARD_MAX_TOKENS: Final = 1_200
# One provider call; the stage deadline bounds the route, fallback included.
GENERATION_TIMEOUT_SECONDS: Final = GENERATION_CALL_SECONDS


class GenerativeLM(dspy.LM):
    """A stock DSPy LM that states the structured-output capability its endpoint was configured with.

    DSPy's JSON adapter asks the LM whether it accepts `response_format` and a JSON schema; a proxied
    model name cannot answer that truthfully, so the endpoint configuration does.
    """

    def __init__(self, model: str, *, structured_output: StructuredOutputMode, **kwargs: Any) -> None:
        self._structured_output = structured_output
        super().__init__(model, **kwargs)

    @property
    def supported_params(self) -> set[str]:
        return set() if self._structured_output == "prompt_json" else {"response_format"}

    @property
    def supports_response_schema(self) -> bool:
        return self._structured_output == "json_schema"


def generative_lm(endpoint: ConfiguredLMEndpoint, *, max_tokens: int, timeout: float) -> GenerativeLM:
    """One configured generative endpoint with its existing request settings; no retries, no cache."""

    request: dict[str, Any] = {
        "api_key": endpoint.api_key,
        "api_base": endpoint.api_base,
        "timeout": float(timeout),
        "max_tokens": int(max_tokens),
        "cache": False,
        "num_retries": 0,
        "engine": "litellm",
        **dict(endpoint.model_kwargs),
    }
    if endpoint.temperature is not None:
        request["temperature"] = float(endpoint.temperature)
    return GenerativeLM(str(endpoint.model_name), structured_output=endpoint.structured_output, **request)


@dataclass(frozen=True, slots=True)
class NewsModelRoute:
    """One role's primary endpoint and its declared fallback."""

    role: str
    primary: ConfiguredLMEndpoint
    fallback: ConfiguredLMEndpoint | None
    max_tokens: int

    @property
    def identity(self) -> str:
        """Secret-free: provider, model, endpoint and request semantics of both endpoints, and the ceiling."""

        return canonical_sha(
            {
                "identity_schema": "news_generative_route_v1",
                "role": self.role,
                "primary": _endpoint_model_sha256(self.primary),
                "fallback": None if self.fallback is None else _endpoint_model_sha256(self.fallback),
                "max_tokens": self.max_tokens,
            }
        )

    def lms(self) -> tuple[GenerativeLM, ...]:
        endpoints = (self.primary,) if self.fallback is None else (self.primary, self.fallback)
        return tuple(
            generative_lm(endpoint, max_tokens=self.max_tokens, timeout=GENERATION_TIMEOUT_SECONDS)
            for endpoint in endpoints
        )


@dataclass(frozen=True, slots=True)
class NewsRuntimeModels:
    extraction: NewsModelRoute
    judgment: NewsModelRoute
    card: NewsModelRoute
    news_judgment: NewsJudgmentEndpoint | None
    availability: NewsModelAvailability

    @property
    def program_identity(self) -> str:
        return news_program_identity(
            extraction_model_identity=self.extraction.identity,
            judgment_model_identity=self.judgment.identity,
            card_model_identity=self.card.identity,
            news_judgment=self.news_judgment,
        )

    def status(self) -> dict[str, Any]:
        """The secret-free model identities Serve reports; field names are the public status contract."""

        return {
            "extraction_model": self.availability.extraction_model,
            "extraction_fallback_model": self.availability.extraction_fallback_model,
            "judgment_backend": "native" if self.news_judgment is not None else "generated",
            "judgment_model": (
                self.news_judgment.model if self.news_judgment is not None else self.availability.extraction_model
            ),
            "news_judgment_configured": self.news_judgment is not None,
            "card_model": self.availability.card_model,
            "card_fallback_model": self.availability.card_fallback_model,
            "card_dedicated": self.availability.card_dedicated,
            "program_identity": self.program_identity,
        }


def compose_news_models(settings: Any) -> NewsRuntimeModels | None:
    """Resolve operator settings once. None when no complete News generative route is configured."""

    availability = news_model_availability(settings)
    if not availability.configured or availability.extraction_model is None or availability.card_model is None:
        return None
    extraction_primary = configured_lm_endpoint(settings, model_name=availability.extraction_model)
    extraction_fallback: ConfiguredLMEndpoint | None = None
    if availability.extraction_fallback_model:
        fallback = settings.llm.news_triage_fallback
        extraction_fallback = configured_lm_endpoint(
            settings,
            model_name=availability.extraction_fallback_model,
            api_key=fallback.api_key,
            base_url=fallback.base_url,
            request_config=fallback.request,
        )
    if availability.card_dedicated:
        reader = settings.llm.news_reader_card
        card_primary = configured_lm_endpoint(
            settings,
            model_name=availability.card_model,
            api_key=reader.api_key,
            base_url=reader.base_url,
            request_config=reader.request,
        )
    else:
        card_primary = extraction_primary
    card_fallback: ConfiguredLMEndpoint | None = None
    if availability.card_fallback_dedicated and availability.card_fallback_model:
        reader_fallback = settings.llm.news_reader_card_fallback
        card_fallback = configured_lm_endpoint(
            settings,
            model_name=availability.card_fallback_model,
            api_key=reader_fallback.api_key,
            base_url=reader_fallback.base_url,
            request_config=reader_fallback.request,
        )
    elif availability.card_fallback_model:
        card_fallback = extraction_fallback
    judgment = settings.llm.news_judgment
    news_judgment = (
        NewsJudgmentEndpoint(base_url=str(judgment.base_url), model=str(judgment.model), api_key=str(judgment.api_key))
        if judgment.configured
        else None
    )
    return NewsRuntimeModels(
        extraction=NewsModelRoute("extraction", extraction_primary, extraction_fallback, EXTRACTION_MAX_TOKENS),
        judgment=NewsModelRoute("judgment", extraction_primary, extraction_fallback, JUDGMENT_MAX_TOKENS),
        card=NewsModelRoute("card", card_primary, card_fallback, CARD_MAX_TOKENS),
        news_judgment=news_judgment,
        availability=availability,
    )


def news_runtime_manifest_sha(settings: Any, *, image_digest: str, runtime_revision: str) -> str:
    """The Workers runtime manifest: the configured News program identity in this exact image.

    Workers reports it on /readyz and the deployment compares it with the value computed from the
    same configuration, so a Workers process running another program or image is visible.
    """

    models = compose_news_models(settings)
    return canonical_sha(
        {
            "identity_schema": "news_runtime_manifest_v2",
            "news_program_identity": None if models is None else models.program_identity,
            "image_digest": image_digest,
            "runtime_revision": runtime_revision,
        }
    )


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


__all__ = [
    "CARD_MAX_TOKENS",
    "EXTRACTION_MAX_TOKENS",
    "JUDGMENT_MAX_TOKENS",
    "GenerativeLM",
    "NewsModelRoute",
    "NewsRuntimeModels",
    "compose_news_models",
    "generative_lm",
    "news_runtime_manifest_sha",
]
