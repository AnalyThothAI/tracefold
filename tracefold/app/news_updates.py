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
from tracefold.news.updates.ports import ExistingSourceReader, NewsStore, Sender, TradingReceiver
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent, Notifications, PublicRelay
from tracefold.news.updates.topics import CODEBOOK, CODEBOOK_SHA256


@dataclass(frozen=True, slots=True)
class NewsJudgmentEndpoint:
    base_url: str
    model: str
    api_key: str = field(repr=False)


@dataclass(slots=True)
class NewsUpdateRuntime:
    agent: NewsAgent
    notifications: Notifications
    public_relay: PublicRelay
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


def compose_news_updates(
    *,
    store: NewsStore,
    relation_cache: JudgmentCache,
    sender: Sender,
    trading_receiver: TradingReceiver,
    extraction_lm_factory: Callable[[], Any],
    card_lm_factory: Callable[[], Any],
    judgment_lm_factory: Callable[[], Any],
    extraction_model_identity: str,
    card_model_identity: str,
    judgment_model_identity: str,
    news_judgment: NewsJudgmentEndpoint | None = None,
    after_native_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
    source_reader: ExistingSourceReader | None = None,
) -> NewsUpdateRuntime:
    """Construct objects only. Does not query models, PG, exchanges or providers.

    Factories borrow the existing configured/audited generative endpoints, with
    their existing generation settings. news_judgment is the sole optional
    decision slot; this function never reads Trading's model configuration.
    The caller owns closing this runtime in the existing worker lifetime.
    """
    generated = GeneratedJudgments(judgment_lm_factory, model_identity=judgment_model_identity)
    connection = None
    native = None
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

        endpoint = identity("jev_endpoint", news_judgment.base_url.rstrip("/"), news_judgment.model)
        native = NativeJudgments(bind, model_identity=endpoint)
    judgments = NewsJudgments(generated=generated, native=native, cache=relation_cache)
    extractor = DspyExtractor(extraction_lm_factory, model_identity=extraction_model_identity, topics=dict(CODEBOOK))
    analyzer = SemanticAnalyzer(extractor, judgments, topics=CODEBOOK)
    # Small code-owned identity, no old image decoding, compatibility whitelist,
    # half-loaded artifact, registry availability check, or global model mutation.
    program_identity = identity(
        "news_program",
        _source_identity(),
        extractor.identity,
        judgments.identity,
        card_model_identity,
    )
    return NewsUpdateRuntime(
        agent=NewsAgent(store, analyzer, program_identity=program_identity, source_reader=source_reader),
        notifications=Notifications(store, NotificationPlanner(judgments), DspyCardComposer(card_lm_factory), sender),
        public_relay=PublicRelay(store, trading_receiver),
        judgment_connection=connection,
    )
