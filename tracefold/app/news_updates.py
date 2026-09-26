"""Explicit composition of the new core; no implicit Trading Jev configuration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from tracefold.app.system_one import SystemOneConnection, SystemOneReceipt
from tracefold.news.updates.dspy_backend import DspyCardComposer, DspyExtractor, GeneratedJudgments, NativeJudgments
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import NATIVE_OPERATION_SECONDS, JudgmentCache, NewsJudgments
from tracefold.news.updates.notification import NotificationPlanner
from tracefold.news.updates.ports import ExistingSourceReader, NewsStore, Sender
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent, Notifications
from tracefold.news.updates.topics import CODEBOOK, CODEBOOK_SHA256


@dataclass(frozen=True, slots=True)
class NewsJudgmentEndpoint:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    @property
    def identity(self) -> str:
        """Secret-free: the endpoint and the requested model, never the key."""

        return identity("jev_endpoint", self.base_url.rstrip("/"), self.model)


@dataclass(slots=True)
class NewsUpdateRuntime:
    agent: NewsAgent
    judgments: NewsJudgments
    # None when this process has no push sender: semantic adoption still runs, and the pending
    # notification markers it writes stay durable for a process that can send.
    notifications: Notifications | None
    program_identity: str
    judgment_connection: SystemOneConnection | None

    async def aclose(self) -> None:
        if self.judgment_connection is not None:
            await self.judgment_connection.aclose()


def _source_identity() -> str:
    # Every module of the core, plus the pinned codebook it imports; no hand-kept file list to go stale.
    root = files("tracefold.news.updates")
    sources = {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in sorted(root.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(".py")
    }
    return digest({"sources": sources, "topic_codebook": CODEBOOK_SHA256})


def _unbound() -> Any:
    raise RuntimeError("news_program_identity_is_not_callable")


def _analyzer(
    *,
    relation_cache: JudgmentCache,
    extraction_lm_factory: Callable[[], Any],
    judgment_lm_factory: Callable[[], Any],
    extraction_model_identity: str,
    judgment_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None,
    native_factory: Callable[[], Any],
) -> SemanticAnalyzer:
    generated = GeneratedJudgments(judgment_lm_factory, model_identity=judgment_model_identity)
    native = None if news_judgment is None else NativeJudgments(native_factory, model_identity=news_judgment.identity)
    judgments = NewsJudgments(generated=generated, native=native, cache=relation_cache)
    extractor = DspyExtractor(extraction_lm_factory, model_identity=extraction_model_identity, topics=dict(CODEBOOK))
    return SemanticAnalyzer(extractor, judgments, topics=CODEBOOK)


def _program_identity(analyzer: SemanticAnalyzer, card_model_identity: str) -> str:
    # Small code-owned identity, no old image decoding, compatibility whitelist,
    # half-loaded artifact, registry availability check, or global model mutation.
    return identity(
        "news_program",
        _source_identity(),
        analyzer.extractor.identity,
        analyzer.judgments.identity,
        card_model_identity,
    )


class _NoCache:
    """The identity-only analyzer asks no question, so it caches none."""

    async def get(self, key: str) -> Any:
        return None

    async def put(self, key: str, answer: Any) -> None:
        return None


# Source files are read once per process and model set.
_PROGRAM_IDENTITIES: dict[tuple[str, str, str, str | None], str] = {}


def news_program_identity(
    *,
    extraction_model_identity: str,
    judgment_model_identity: str,
    card_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None = None,
) -> str:
    """The identity `compose_news_updates` gives this model set, computed without composing a runtime.

    It opens no connection and calls no model: Serve reports it, and the deployment compares it with
    what Workers runs.
    """

    key = (
        extraction_model_identity,
        judgment_model_identity,
        card_model_identity,
        None if news_judgment is None else news_judgment.identity,
    )
    cached = _PROGRAM_IDENTITIES.get(key)
    if cached is not None:
        return cached
    analyzer = _analyzer(
        relation_cache=_NoCache(),
        extraction_lm_factory=_unbound,
        judgment_lm_factory=_unbound,
        extraction_model_identity=extraction_model_identity,
        judgment_model_identity=judgment_model_identity,
        news_judgment=news_judgment,
        native_factory=_unbound,
    )
    value = _program_identity(analyzer, card_model_identity)
    _PROGRAM_IDENTITIES[key] = value
    return value


def compose_news_updates(
    *,
    store: NewsStore,
    relation_cache: JudgmentCache,
    extraction_lm_factory: Callable[[], Any],
    card_lm_factory: Callable[[], Any],
    judgment_lm_factory: Callable[[], Any],
    extraction_model_identity: str,
    card_model_identity: str,
    judgment_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None = None,
    sender: Sender | None = None,
    after_native_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
    source_reader: ExistingSourceReader | None = None,
) -> NewsUpdateRuntime:
    """Construct objects only. Does not query models, PG, exchanges or providers.

    Factories borrow the existing configured generative endpoints, with their existing generation
    settings; each returns one LM or an ordered primary/fallback route. news_judgment is the sole
    optional decision slot; this function never reads Trading's model configuration. The caller owns
    closing this runtime in the existing worker lifetime. The App relay is the only News→Trading
    relay, so nothing here reads the public outbox.
    """

    connection = None
    native_factory: Callable[[], Any] = _unbound
    if news_judgment is not None:
        # The SDK's HTTP timeout is the per-batch operation budget; each call is also bounded by the
        # remaining stage deadline. max_retries=0 is owned by SystemOneConnection.
        connection = SystemOneConnection(
            base_url=news_judgment.base_url,
            api_key=news_judgment.api_key,
            model=news_judgment.model,
            timeout_seconds=NATIVE_OPERATION_SECONDS,
        )
        bound = connection

        def bind() -> Any:
            # A new bind per batch avoids shared mutable history/callback state.
            return bound.bind(after_call=after_native_call, timeout_seconds=NATIVE_OPERATION_SECONDS)

        native_factory = bind
    analyzer = _analyzer(
        relation_cache=relation_cache,
        extraction_lm_factory=extraction_lm_factory,
        judgment_lm_factory=judgment_lm_factory,
        extraction_model_identity=extraction_model_identity,
        judgment_model_identity=judgment_model_identity,
        news_judgment=news_judgment,
        native_factory=native_factory,
    )
    program_identity = _program_identity(analyzer, card_model_identity)
    notifications = (
        None
        if sender is None
        else Notifications(store, NotificationPlanner(analyzer.judgments), DspyCardComposer(card_lm_factory), sender)
    )
    return NewsUpdateRuntime(
        agent=NewsAgent(store, analyzer, program_identity=program_identity, source_reader=source_reader),
        judgments=analyzer.judgments,
        notifications=notifications,
        program_identity=program_identity,
        judgment_connection=connection,
    )
