"""Explicit composition of the new core; no implicit Trading Jev configuration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from tracefold.app.system_one import SystemOneConnection, SystemOneReceipt
from tracefold.news.adapters.card_copy import DspyCardComposer
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.adapters.reader_judge import READER_NATIVE_SECONDS, DspyReaderJudge
from tracefold.news.adapters.semantic_judgments import GeneratedJudgments, NativeJudgments
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.ports import NotificationStore
from tracefold.news.notifications.service import Notifications
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import NATIVE_OPERATION_SECONDS, JudgmentCache, NewsJudgments
from tracefold.news.updates.ports import ExistingSourceReader, SemanticStore
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent
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
    notifications: Notifications
    program_identity: str
    judgment_connection: SystemOneConnection | None
    reader_connection: SystemOneConnection | None = None

    async def aclose(self) -> None:
        for connection in (self.judgment_connection, self.reader_connection):
            if connection is not None:
                await connection.aclose()


def _source_identity() -> str:
    # Fingerprint the semantic core's packaged sources and codebook. Model adapters bind their
    # prompt/schema/model contracts separately; this is not an entire image/dependency fingerprint.
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


def _program_identity(analyzer: SemanticAnalyzer) -> str:
    # Small code-owned identity, no old image decoding, compatibility whitelist,
    # half-loaded artifact, registry availability check, or global model mutation.
    return identity(
        "news_program",
        _source_identity(),
        analyzer.extractor.identity,
        analyzer.judgments.identity,
    )


class _NoCache:
    """The identity-only analyzer asks no question, so it caches none."""

    async def get_many(self, keys: tuple[str, ...]) -> dict[str, Any]:
        return {}

    async def put_many(self, answers: Any) -> None:
        return None


# Source files are read once per process and model set.
_PROGRAM_IDENTITIES: dict[tuple[str, str, str | None], str] = {}


def news_program_identity(
    *,
    extraction_model_identity: str,
    judgment_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None = None,
) -> str:
    """The identity `compose_news_updates` gives this model set, computed without composing a runtime.

    It opens no connection and calls no model: Serve reports it, and the deployment compares it with
    what Workers runs.
    """

    key = (
        extraction_model_identity,
        judgment_model_identity,
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
    value = _program_identity(analyzer)
    _PROGRAM_IDENTITIES[key] = value
    return value


def compose_reader_judge(
    *,
    generated_lm_factory: Callable[[], Any],
    generated_model_identity: str,
    reader_judgment: NewsJudgmentEndpoint | None = None,
    after_native_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
) -> tuple[DspyReaderJudge, SystemOneConnection | None]:
    """The notification decision layer's reader judge (#742): its own System One route, else generative only.

    `reader_judgment` is `llm.news_reader_judgment`; the semantic `news_judgment` route and Trading's route
    are never borrowed. The generative route is the News judgment route. The caller closes the connection.
    """

    if reader_judgment is None:
        return DspyReaderJudge(generated_lm_factory, generated_model_identity=generated_model_identity), None
    connection = SystemOneConnection(
        base_url=reader_judgment.base_url,
        api_key=reader_judgment.api_key,
        model=reader_judgment.model,
        timeout_seconds=READER_NATIVE_SECONDS,
    )

    def bind() -> Any:
        return connection.bind(after_call=after_native_call, timeout_seconds=READER_NATIVE_SECONDS)

    judge = DspyReaderJudge(
        generated_lm_factory,
        generated_model_identity=generated_model_identity,
        native_lm_factory=bind,
        native_model_identity=reader_judgment.identity,
    )
    return judge, connection


def compose_news_updates(
    *,
    semantic_store: SemanticStore,
    notification_store: NotificationStore,
    relation_cache: JudgmentCache,
    extraction_lm_factory: Callable[[], Any],
    card_lm_factory: Callable[[], Any],
    judgment_lm_factory: Callable[[], Any],
    extraction_model_identity: str,
    card_model_identity: str,
    judgment_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None = None,
    news_reader_judgment: NewsJudgmentEndpoint | None = None,
    after_native_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
    source_reader: ExistingSourceReader | None = None,
) -> NewsUpdateRuntime:
    """Construct objects only. Does not query models, PG, exchanges or providers.

    Factories borrow the existing configured generative endpoints, with their existing generation
    settings; each returns one LM or an ordered primary/fallback route. news_judgment is the sole
    semantic decision slot and news_reader_judgment the notification decision slot; neither is inferred
    from the other, and this function never reads Trading's model configuration. The caller owns
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
    program_identity = _program_identity(analyzer)
    # The notification decision layer's own route; its answers are cached apart, keyed by its identity.
    reader_judge, reader_connection = compose_reader_judge(
        generated_lm_factory=judgment_lm_factory,
        generated_model_identity=judgment_model_identity,
        reader_judgment=news_reader_judgment,
        after_native_call=after_native_call,
    )
    return NewsUpdateRuntime(
        agent=NewsAgent(semantic_store, analyzer, program_identity=program_identity, source_reader=source_reader),
        judgments=analyzer.judgments,
        # The Deliverer owns the provider side and hands its sender to each notification turn.
        notifications=Notifications(
            notification_store,
            NotificationPlanner(reader_judge, relation_cache),
            DspyCardComposer(card_lm_factory, model_identity=card_model_identity),
        ),
        program_identity=program_identity,
        judgment_connection=connection,
        reader_connection=reader_connection,
    )
