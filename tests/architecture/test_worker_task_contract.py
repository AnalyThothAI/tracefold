"""The Workers root and its two business pipelines share one ordered task declaration."""

from __future__ import annotations

from typing import Any, cast

from tracefold.app.workers.task_contract import worker_task_names
from tracefold.news.consumers import NewsPipeline
from tracefold.trading import TradingConfig, build_pipeline


def _port() -> Any:
    return cast(Any, object())


def test_enabled_worker_task_names_are_the_runtime_declarations() -> None:
    news = NewsPipeline(
        receiver=_port(),
        recovery=_port(),
        deduper=_port(),
        triage=_port(),
        deliverer=_port(),
        janitor=_port(),
        instruments=_port(),
        quotes=_port(),
        reactions=_port(),
    )
    trading = build_pipeline(db=object(), config=TradingConfig(), bars=lambda _venue: None)

    assert worker_task_names(news_pipeline=news, trading_pipeline=trading) == (
        "workers-probe",
        "news-receiver",
        "news-recovery",
        "news-deduper",
        "news-triage",
        "news-deliverer",
        "news-janitor",
        "news-instruments",
        "news-quotes",
        "news-reactions",
        "trading-candidate",
        "trading-reconcile",
        "workers-control",
    )
