"""Explicit composition of the new core; no implicit Trading Jev configuration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from tracefold.app.system_one import SystemOneConnection
from tracefold.news.updates.dspy_backend import (
    DspyCardComposer, DspyExtractor, GeneratedJudgments, NativeJudgments,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import JudgmentCache, NewsJudgments
from tracefold.news.updates.notification import NotificationPlanner
from tracefold.news.updates.ports import ExistingSourceReader, NewsStore, Sender, TradingReceiver
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent, Notifications, PublicRelay


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


def compose_news_updates(
    *, store: NewsStore, relation_cache: JudgmentCache, sender: Sender, trading_receiver: TradingReceiver,
    extraction_lm_factory: Callable[[], Any], card_lm_factory: Callable[[], Any],
    judgment_lm_factory: Callable[[], Any], extraction_model_identity: str,
    card_model_identity: str, judgment_model_identity: str,
    topics: dict[str, str], news_judgment: NewsJudgmentEndpoint | None = None,
    after_native_call: Any = None, source_reader: ExistingSourceReader | None = None,
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
        connection = SystemOneConnection(base_url=news_judgment.base_url, api_key=news_judgment.api_key,
            model=news_judgment.model, timeout_seconds=2.0)
        # A new bind per batch avoids shared mutable history/callback state.
        native = NativeJudgments(lambda: connection.bind(after_call=after_native_call, timeout_seconds=2.0),
            model_identity=identity("jev_endpoint", news_judgment.base_url.rstrip("/"), news_judgment.model))
    judgments = NewsJudgments(generated=generated, native=native, cache=relation_cache)
    extractor = DspyExtractor(extraction_lm_factory, model_identity=extraction_model_identity, topics=topics)
    analyzer = SemanticAnalyzer(extractor, judgments, topics=tuple(topics.items()))
    # Small code-owned identity, no old image decoding, compatibility whitelist,
    # half-loaded artifact, registry availability check, or global model mutation.
    root = files("tracefold.news.updates")
    source_identity = digest({name: root.joinpath(name).read_text(encoding="utf-8") for name in (
        "admission.py", "contracts.py", "identity.py", "judgment.py", "topics.py", "dspy_backend.py", "semantics.py", "notification.py", "public.py", "ports.py", "service.py",
    )})
    program_identity = identity("news_program", source_identity, extractor.identity, judgments.identity, card_model_identity)
    return NewsUpdateRuntime(
        agent=NewsAgent(store, analyzer, program_identity=program_identity, source_reader=source_reader),
        notifications=Notifications(store, NotificationPlanner(judgments), DspyCardComposer(card_lm_factory), sender),
        public_relay=PublicRelay(store, trading_receiver), judgment_connection=connection,
    )
