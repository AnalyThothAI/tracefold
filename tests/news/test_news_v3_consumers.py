"""News V3 consumer unit tests: fake bus + fake repositories, no PostgreSQL and no broker.

What this module owns is what a consumer *computes*: the card and presentation it renders, the
verdict and trace it materializes, the routing keys and priorities it publishes, the fallback
reasons and error codes it chooses, its own circuit and budget rules, and the telemetry it emits.

What it does not own, and must not claim, is durability. `FakeWorkerDatabase` opens no transaction,
takes no lock and loses no compare-and-swap: `read` and `tx` are direct calls onto one recording
object, so a recorded call order cannot tell one transaction from two, and a scripted `False` is not
a CAS that was actually lost. Atomicity, row locks, CAS, uniqueness, crash survival and replay are
asserted against real rows in `tests/integration/test_news_crash_replay.py`,
`test_news_durable_event_plane.py`, `test_news_v3_pipeline.py`, `test_news_learning_retention.py`
and the twin `tests/integration/test_news_v3_consumers.py` (#598 D8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerNewsColdDatabase, WorkerNewsDatabase
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_RAW_RECOVERY,
    BrokerBackpressure,
    BrokerPublishFailure,
    BrokerUnavailable,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
)
from tracefold.news.delivery_contracts import COMMIT_PHASE_NOT_SENT, COMMIT_PHASE_UNKNOWN
from tracefold.news.market_review.pricing import Candle, PriceInstrument, PricePoint
from tracefold.news.models import (
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
)
from tracefold.news.opennews import OpenNewsHistoryError
from tracefold.news.pipeline import admission as admission_module
from tracefold.news.pipeline import delivery as delivery_module
from tracefold.news.pipeline import recovery as recovery_module
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererLoop
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.reader_card import ReaderCard
from tracefold.news.reader_history import ReaderHistorySnapshot
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.resource import ResourceAdmissionTimeout

NOW_MS = 1_800_000_000_000
WATCHLIST = frozenset({"BTC", "NVDA"})
PROGRAM_SHA256 = "9" * 64


class FakeBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []
        self.consumed: list[str] = []
        self.last_publish_failure: BrokerPublishFailure | None = None

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    async def consume(self, queue: str, handler: Any, *, prefetch: int, stop_event: Any) -> None:
        self.consumed.append(queue)

    def routing_keys(self) -> list[str]:
        return [message.routing_key for message in self.published]


class RecordingHandoffTelemetry:
    def __init__(self) -> None:
        self.states: list[tuple[str, int, float, int]] = []
        self.repairs: list[tuple[str, str]] = []
        self.incidents: list[tuple[str, str, int, float]] = []
        self.raw_retention: list[dict[str, int | float]] = []

    def set_news_handoff_state(self, stage: str, *, pending: int, oldest_age_seconds: float, expired: int) -> None:
        self.states.append((stage, pending, oldest_age_seconds, expired))

    def record_news_handoff_repair(self, stage: str, outcome: str) -> None:
        self.repairs.append((stage, outcome))

    def set_news_opennews_incident(self, *, provider: str, cause: str, count: int, oldest_age_seconds: float) -> None:
        self.incidents.append((provider, cause, count, oldest_age_seconds))

    def record_news_raw_retention(
        self,
        *,
        deleted_rows: int,
        batches: int,
        wall_seconds: float,
        backlog_rows: int,
        backlog_capped: bool,
        oldest_age_seconds: float,
    ) -> None:
        self.raw_retention.append(
            {
                "deleted_rows": deleted_rows,
                "batches": batches,
                "wall_seconds": wall_seconds,
                "backlog_rows": backlog_rows,
                "backlog_capped": int(backlog_capped),
                "oldest_age_seconds": oldest_age_seconds,
            }
        )


class RecordingNews:
    """Minimal NewsRepository double: records every call and answers from a scripted table."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}))
            if name in {"evidence_material", "evidence_member_metadata"} and name not in self.responses:
                return []
            if name == "evidence_candidates" and name not in self.responses:
                return []
            if name == "reader_history" and name not in self.responses:
                return ReaderHistorySnapshot()  # nothing pushed yet
            if name == "reader_history_revision" and name not in self.responses:
                history = self.responses.get("reader_history", ReaderHistorySnapshot())
                value = history(**kwargs) if callable(history) else history
                return value.ledger_revision
            if name == "latest_evidence_snapshot" and name not in self.responses:
                card = self.responses.get("event_card") or {}
                return {
                    "evidence_version": int(card.get("evidence_version") or 1),
                    "evidence_sha256": str(card.get("evidence_sha256") or "e" * 64),
                    "focus_fact_id": str(card.get("focus_fact_id") or "fact-1"),
                }
            if name == "latest_evidence_identity" and name not in self.responses:
                evidence = self.responses.get("latest_evidence_snapshot")
                card = self.responses.get("event_card") or {}
                source = evidence if isinstance(evidence, dict) else card
                return (
                    int(source.get("evidence_version") or 1),
                    str(source.get("evidence_sha256") or "e" * 64),
                )
            if name == "evidence_snapshot_material" and name not in self.responses:
                event_id = str(kwargs.get("event_id") or "event")
                focused_item = kwargs.get("focus_item_id")
                return {
                    "card": {
                        "event_id": event_id,
                        "leader_item_id": focused_item or "item",
                        "leader_title": "fixture event",
                        "focus_fact_id": "fact-1",
                        "focus_fact_text": "fixture event",
                        "focus_fact_method": "whole_item",
                    },
                    "members": [],
                    "latest": None,
                    "focus_item_id": focused_item,
                    "focus_source": (
                        {
                            "leader_item_id": focused_item,
                            "leader_url": None,
                            "reporting_origin": "opennews",
                            "provider_metadata": {},
                            "provenance": [],
                            "leader_published_at_ms": NOW_MS,
                            "raw_first_line": "fixture event",
                        }
                        if focused_item is not None
                        else None
                    ),
                }
            if name == "append_prepared_evidence_snapshot" and name not in self.responses:
                snapshot = args[0]
                return {
                    "evidence_version": snapshot["evidence_version"],
                    "evidence_sha256": snapshot["evidence_sha256"],
                }
            if name == "semantic_wake_route" and name not in self.responses:
                event_id = str(args[0] if args else kwargs.get("event_id"))
                return {
                    "event_id": event_id,
                    "wanted_revision": 1,
                    "dedupe_family": "general",
                    "queue_priority": "normal",
                    "trace_id": "trace-1",
                }
            if name in {"pending_semantic_event_ids", "pending_notification_event_ids", "item_event_ids"} and (
                name not in self.responses
            ):
                return []
            if name == "record_item_revision" and name not in self.responses:
                return False
            if name == "purge_semantic_caches" and name not in self.responses:
                return 0
            if name == "event_admission" and name not in self.responses:
                response = self.responses.get("event_card") or {}
                card = response if isinstance(response, dict) else {}
                return {
                    "admission": str(card.get("admission") or "candidate"),
                    "event_kind": str(card.get("event_kind") or "news"),
                    "storyline_key": str(card.get("storyline_key") or ""),
                }
            if name == "semantic_wake_state" and name not in self.responses:
                return {"pending": 0, "oldest_pending_at_ms": None, "expired": self.responses.get("expired_count", 0)}
            value = self.responses.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return _call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_of(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.calls if called == name)


class RecordingInstruments:
    """The #75 universe as the consumers see it: empty by default, so the Gate falls back to the `XYZ-` prefix and
    the alias table stays inert — every pre-existing expectation holds unchanged."""

    def __init__(
        self,
        *,
        classes: dict[str, str] | None = None,
        aliases: dict[str, str] | None = None,
        candidates: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.classes = classes or {}
        self.aliases = aliases or {}
        self.candidates = candidates or {}

    def instrument_classes(self) -> dict[str, str]:
        return dict(self.classes)

    def instrument_class_candidates(self, symbols: Any) -> dict[str, tuple[str, ...]]:
        wanted = {str(symbol).upper().replace("XYZ-", "") for symbol in symbols}
        return {symbol: classes for symbol, classes in self.candidates.items() if symbol in wanted}

    def alias_map(self) -> dict[str, str]:
        return dict(self.aliases)


class RecordingPrice:
    """Quote-plane double; silent by default so non-price tests keep their contract."""

    def __init__(
        self,
        *,
        quotes: list[dict[str, Any]] | None = None,
        reactions: list[dict[str, Any]] | None = None,
        instruments: dict[str, tuple[PriceInstrument, ...]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.quotes = quotes or []
        self.reactions = reactions or []
        self.instruments = instruments or {}
        self.error = error
        self.requested: list[list[str]] = []
        self.requested_markets: list[list[str]] = []
        self.requested_reaction_versions: list[str | None] = []

    def quotes_for_symbols(self, requests: Any, *, now_ms: int) -> list[dict[str, Any]]:
        del now_ms
        self.requested.append([request.symbol for request in requests])
        self.requested_markets.append([request.market_type for request in requests])
        if self.error is not None:
            raise self.error
        return list(self.quotes)

    def event_reactions(self, event_id: str, *, metric_version: str | None = None) -> list[dict[str, Any]]:
        del event_id
        self.requested_reaction_versions.append(metric_version)
        if self.error is not None:
            raise self.error
        return list(self.reactions)

    def instruments_for_symbols(self, requests: Any) -> dict[Any, tuple[PriceInstrument, ...]]:
        return {request: self.instruments[request.symbol] for request in requests if request.symbol in self.instruments}


class FakeWorkerDatabase:
    """Stands in for `WorkerDatabase`; the ports it hands out are the production adapters over it.

    Reaching a consumer's own `read`/`tx` lands on the News lane and appends to `operations`; the
    Janitor's cold port lands on `run_business` and appends to `heavy_operations`. A consumer that took
    the wrong lane is therefore visible as a wrong list, not as a passing test.
    """

    def __init__(
        self,
        news: RecordingNews,
        *,
        admission_timeout_for: set[str] | None = None,
        instruments: RecordingInstruments | None = None,
        price: RecordingPrice | None = None,
    ) -> None:
        self.news = news
        self.instruments = instruments or RecordingInstruments()
        self.price = price or RecordingPrice()
        self.operations: list[str] = []
        self.heavy_operations: list[str] = []
        self.operation_timeouts: list[tuple[str, float]] = []
        self.admission_timeout_for = admission_timeout_for or set()
        self._port = WorkerNewsDatabase(self)
        self.cold_port = WorkerNewsColdDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield SimpleNamespace(news=self.news, instruments=self.instruments, price=self.price)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        self.operation_timeouts.append((name, operation_timeout_seconds))
        self.operations.append(name)
        if name in self.admission_timeout_for:
            raise ResourceAdmissionTimeout(f"worker_database_admission_timeout:{name}")
        return fn(*args, **kwargs)

    def heavy_business(self) -> FakeWorkerDatabase:
        return self

    async def run_business(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        self.operation_timeouts.append((name, operation_timeout_seconds))
        self.heavy_operations.append(name)
        if name in self.admission_timeout_for:
            raise ResourceAdmissionTimeout(f"worker_database_admission_timeout:{name}")
        return fn(*args, **kwargs)


class InlineFinite:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return fn(*args, **kwargs)


def _card(**overrides: Any) -> dict[str, Any]:
    card = {
        "event_id": "ev-strong",
        "dedupe_family": "general",
        "leader_title": "NVIDIA to invest $100bn in OpenAI data centre",
        "leader_url": "https://example.test/nvda",
        "leader_description": "",
        "reporting_origin": "FT",
        "admission": "candidate",
        "queue_priority": "high",
        "provider_score_max": 92.0,
        "asset_class": "equity_or_commodity",
        "grounded_assets": ["NVDA"],
        "watchlist_hits": ["NVDA"],
        "macro_lexicon": False,
        "storyline_key": "asset:NVDA",
        "comparison_fingerprint": "f" * 64,
        "trace_id": "trace-1",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-1",
        "evidence_schema_version": "news_event_evidence_v3",
    }
    card.update(overrides)
    return card


def _message(kind: str, payload: dict[str, Any], *, routing_key: str = "", priority: int = 0) -> BusMessage:
    return BusMessage(
        kind=kind,  # type: ignore[arg-type]
        message_id=f"{kind}:{payload.get('event_id', 'x')}",
        routing_key=routing_key,
        payload=payload,
        trace_id="trace-1",
        occurred_at_ms=NOW_MS,
        priority=priority,
    )


# ---------------------------------------------------------------- Deduper
def _admitted(event_id: str, dedupe_family: str, *, inserted: bool = True) -> Any:
    return SimpleNamespace(
        item_inserted=inserted,
        event_id=event_id,
        dedupe_family=dedupe_family,
        evidence_focus_changed=False,
        body_revised=False,
    )


def test_deduper_wakes_semantic_work_for_new_evidence_of_admitted_live_events(monkeypatch: pytest.MonkeyPatch) -> None:
    admissions = iter(
        [
            _admitted("ev-1", "macro"),
            # A suppressed Event records its evidence and wakes nothing.
            _admitted("ev-2", "general"),
            # `listing_deterministic` is an admitted admission, not a suppression: exchange listing/delisting
            # frames must reach the semantic stage like any candidate (#72).
            _admitted("ev-3", "listing"),
            # #126: a Strategy Tracefold has no local knowledge of is ordinary work.
            _admitted("ev-4", "general"),
            # Recovery ingest stores evidence and never wakes semantics, even for an admitted Event.
            _admitted("ev-1", "macro", inserted=False),
        ]
    )
    seen: list[dict[str, Any]] = []

    def fake_admit(repos: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return next(admissions)

    admission = {
        "ev-1": "candidate",
        "ev-2": "suppressed_ungrounded",
        "ev-3": "listing_deterministic",
        "ev-4": "candidate",
    }
    routes = {
        "ev-1": ("macro", "high", 3),
        "ev-3": ("listing", "high", 1),
        "ev-4": ("general", "normal", 1),
    }
    monkeypatch.setattr(admission_module, "admit_item", fake_admit)
    news = RecordingNews(
        find_band_candidates=[],
        event_admission=lambda event_id: {"admission": admission[event_id], "event_kind": "news", "storyline_key": ""},
        semantic_wake_route=lambda event_id: {
            "event_id": event_id,
            "wanted_revision": routes[event_id][2],
            "dedupe_family": routes[event_id][0],
            "queue_priority": routes[event_id][1],
            "trace_id": "trace-event",
        },
    )
    bus = FakeBus()
    deduper = DeduperConsumer(bus=bus, db=FakeWorkerDatabase(news), watchlist_symbols=frozenset({"BTC"}))
    params = {
        "id": 3_568_501,
        "engineType": "news",
        "text": "U.S. 30-Year Treasury Yield Climbs to 5.32%, Highest Since 2007",
        "ts": NOW_MS,
        "strategy": {"id": 1018, "name": "News Score > 70"},
        "aiRating": {"score": 88},
    }
    raw = _message(
        "raw",
        {"params": params, "strategy_id": "1018", "ingest_mode": "live", "observed_at_ms": NOW_MS - 5},
        routing_key=RK_RAW_LIVE.format(strategy_id="1018"),
    )

    async def scenario() -> None:
        await deduper.handle(raw)
        await deduper.handle(raw)  # a suppressed admission never reaches the semantic stage
        await deduper.handle(raw)  # a listing admission does
        foreign = _message(
            "raw",
            {"params": {**params, "strategy": {"id": 4242, "name": "other"}}, "strategy_id": "4242"},
            routing_key=RK_RAW_LIVE.format(strategy_id="4242"),
        )
        await deduper.handle(foreign)
        recovered = _message(
            "raw",
            {"params": params, "strategy_id": "1018", "ingest_mode": "recovery", "observed_at_ms": NOW_MS - 5},
            routing_key=RK_RAW_RECOVERY.format(strategy_id="1018"),
        )
        await deduper.handle(recovered)
        with pytest.raises(PermanentError, match="news_raw_params_missing"):
            await deduper.handle(_message("raw", {}))

    asyncio.run(scenario())

    assert len(seen) == 5
    assert seen[0]["ingest_mode"] == "live" and seen[0]["observed_at_ms"] == NOW_MS - 5
    assert seen[0]["trace_id"] == "trace-1" and seen[0]["watchlist_symbols"] == frozenset({"BTC"})
    assert seen[0]["event"].provider_record_id == "3568501"
    assert seen[4]["ingest_mode"] == "recovery"
    assert bus.routing_keys() == ["event.macro.high", "event.listing.high", "event.general.normal"]
    assert [message.message_id for message in bus.published] == ["event:ev-1:3", "event:ev-3:1", "event:ev-4:1"]
    assert bus.published[0].payload == {"event_id": "ev-1", "revision": 3}
    assert bus.published[0].priority == 5 and bus.published[0].trace_id == "trace-1"
    # Every admission appends its evidence; only admitted live Events want a semantic revision.
    assert news.names().count("append_prepared_evidence_snapshot") == 5
    requested = [kwargs["event_id"] for name, kwargs in news.calls if name == "request_semantic_revision"]
    assert requested == ["ev-1", "ev-3", "ev-4"]
    marked = [kwargs for name, kwargs in news.calls if name == "mark_semantic_work_published"]
    assert [(row["event_id"], row["revision"]) for row in marked] == [("ev-1", 3), ("ev-3", 1), ("ev-4", 1)]
    assert news.names().count("mark_event_published") == 3


def test_deduper_wakes_every_event_of_an_item_whose_body_was_revised(monkeypatch: pytest.MonkeyPatch) -> None:
    revised = SimpleNamespace(
        item_inserted=False, event_id="ev-new", dedupe_family="general", evidence_focus_changed=False, body_revised=True
    )
    monkeypatch.setattr(admission_module, "admit_item", lambda *_a, **_k: revised)
    news = RecordingNews(find_band_candidates=[], item_event_ids=["ev-new", "ev-old"])
    bus = FakeBus()
    deduper = DeduperConsumer(bus=bus, db=FakeWorkerDatabase(news), watchlist_symbols=frozenset())
    raw = _message(
        "raw",
        {
            "params": {"id": 7, "engineType": "news", "text": "x y z", "ts": NOW_MS, "strategy": {"id": 1018}},
            "strategy_id": "1018",
            "ingest_mode": "live",
        },
    )

    asyncio.run(deduper.handle(raw))

    assert sorted(kwargs["event_id"] for name, kwargs in news.calls if name == "request_semantic_revision") == [
        "ev-new",
        "ev-old",
    ]
    assert sorted(message.message_id for message in bus.published) == ["event:ev-new:1", "event:ev-old:1"]


def test_deduper_admission_timeout_defers_uncounted_and_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission_module, "admit_item", lambda *_a, **_k: pytest.fail("db never admitted"))
    news = RecordingNews(find_band_candidates=[])
    bus = FakeBus()
    deduper = DeduperConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news, admission_timeout_for={"news_deduper_admit"}),
        watchlist_symbols=frozenset(),
    )
    raw = _message(
        "raw",
        {
            "params": {"id": 1, "engineType": "news", "text": "x", "ts": NOW_MS, "strategy": {"id": 1018, "name": "n"}},
            "strategy_id": "1018",
        },
    )
    with pytest.raises(DeferError, match="db_admission_timeout:news_deduper_admit"):
        asyncio.run(deduper.handle(raw))
    assert bus.published == [] and news.calls == []


# ---------------------------------------------------------------- Deliverer
class RecordingSender:
    def __init__(self, order: list[str] | None = None) -> None:
        # `cards` is the frozen channel payload the ledger stores; `reader_cards` is the value
        # object a model-rendering channel serializes for itself (#562 PR-C).
        self.cards: list[dict[str, Any]] = []
        self.reader_cards: list[ReaderCard] = []
        self.presentations: list[ReaderDeliveryPresentation] = []
        self.order = order

    def prepare(self) -> None:
        if self.order is not None:
            self.order.append("prepare")

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        if self.order is not None:
            self.order.append("send")
        self.cards.append(dict(channel_payload))
        self.reader_cards.append(card)
        self.presentations.append(presentation or ReaderDeliveryPresentation())
        return {"status_code": 200, "code": 0}

    def close(self) -> None:
        return None


class RecordingEditableSender(RecordingSender):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.edited_cards: list[dict[str, Any]] = []
        self.edited_reader_cards: list[ReaderCard] = []
        self.edited_presentations: list[ReaderDeliveryPresentation] = []
        self._message_id = 41

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        super().send_card(card, channel_payload=channel_payload, presentation=presentation)
        self._message_id += 1
        return {
            "provider": "telegram",
            "message_id": self._message_id,
            "pushed_at_ms": NOW_MS,
            "target_sha256": "a" * 64,
        }

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        assert isinstance(receipt["message_id"], int) and receipt["message_id"] >= 42
        self.order.append("edit")
        self.edited_cards.append(dict(channel_payload))
        self.edited_reader_cards.append(card)
        self.edited_presentations.append(presentation or ReaderDeliveryPresentation())
        return {**dict(receipt), "edited_at_ms": NOW_MS + 1_000}


class BlockingEditSender(RecordingEditableSender):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def edit_card(
        self,
        receipt: Mapping[str, Any],
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise RuntimeError("blocking edit test timed out")
        return super().edit_card(receipt, card, channel_payload=channel_payload, presentation=presentation)

    def close(self) -> None:
        assert self.release.is_set()
        self.closed = True
        self.order.append("close")


class BlockingProgressionVerifier:
    def __init__(self, order: list[str], *, response: Mapping[str, Any] | None = None) -> None:
        self.order = order
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[dict[str, Any]] = []
        self.response = dict(
            response
            or {
                "state": "confirmed",
                "candidate_i": 0,
                "candidate_headline_zh": "美光工会此前启动劳资协商",
                "reason_zh": "同一工会行动进入罢工投票阶段，新增了明确比例和下一步程序。",
                "verifier_id": "scripted-progression-review-v1",
            }
        )

    async def review(
        self,
        *,
        event: Mapping[str, Any],
        verdict: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        self.order.append("review")
        self.calls.append(
            {
                "event": dict(event),
                "verdict": dict(verdict),
                "candidates": [dict(candidate) for candidate in candidates],
            }
        )
        self.started.set()
        await self.release.wait()
        return self.response


class ScriptedTradabilityVerifier:
    def __init__(self, response: Mapping[str, Any], order: list[str] | None = None) -> None:
        self.response = dict(response)
        self.order = order

    async def review(self, **_kwargs: Any) -> Mapping[str, Any]:
        if self.order is not None:
            self.order.append("market-search")
        return self.response


def _deliverer(
    news: RecordingNews,
    *,
    price: RecordingPrice | None = None,
    sender: RecordingSender | None = None,
    candle_fetcher_for: Any | None = None,
    price_fetcher_for: Any | None = None,
    progression_verifier: Any | None = None,
    tradability_verifier: Any | None = None,
    min_interval_seconds: float = 0.0,
) -> DelivererLoop:
    return DelivererLoop(
        db=FakeWorkerDatabase(news, price=price),
        sender=sender,
        finite_operations=InlineFinite(),
        min_interval_seconds=min_interval_seconds,
        candle_fetcher_for=candle_fetcher_for,
        price_fetcher_for=price_fetcher_for,
        progression_verifier=progression_verifier,
        tradability_verifier=tradability_verifier,
    )


def _delivery_news(**overrides: Any) -> RecordingNews:
    states = iter(overrides.pop("begin_states", ["new"]))
    responses: dict[str, Any] = {
        "event_card": _card(),
        "latest_verdict": lambda *, event_id, stage: (
            {
                "final_decision": "push",
                "verdict": {
                    "novelty": "new_fact",
                    "restates": -1,
                    "assets": [{"symbol": "NVDA", "market_type": "equity", "role": "primary"}],
                    "direction": "bullish",
                    "scope": "single_name",
                    "fact_kind": "state_change",
                    "confidence": 0.8,
                    "headline_zh": "英伟达投资",
                    "why_zh": "投资扩大算力供给。",
                },
            }
            if stage == "triage"
            else None
        ),
        "begin_delivery": lambda **_k: next(states),
        "settle_delivery": True,
        "begin_delivery_edit": True,
        "settle_delivery_edit": True,
    }
    responses.update(overrides)
    return RecordingNews(**responses)


def test_deliverer_holds_a_queued_push_reclassified_by_the_current_source_contract() -> None:
    sender = RecordingSender()
    news = _delivery_news(
        event_admission={
            "admission": "unsupported_market_contract",
            "event_kind": "unsupported_market",
            "storyline_key": "asset:NVDA",
        },
        latest_verdict=lambda **_kwargs: pytest.fail("held Event must not read the historical push verdict"),
    )

    asyncio.run(_deliverer(news, sender=sender).deliver(event_id="ev-strong"))

    assert sender.cards == []
    assert "latest_verdict" not in news.names()
    assert "begin_delivery" not in news.names()
    assert "settle_delivery" not in news.names()


def test_deliverer_prepares_the_provider_before_creating_the_sending_row() -> None:
    order: list[str] = []
    news = _delivery_news(begin_delivery=lambda **_kwargs: order.append("begin") or "new")
    sender = RecordingSender(order)

    asyncio.run(_deliverer(news, sender=sender).deliver(event_id="ev-strong"))

    assert order == ["prepare", "begin", "send"]


class _FailingPrepareSender(RecordingSender):
    """A provider whose target check fails, saying what that failure proved about the message."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def prepare(self) -> None:
        raise self._error


def _provider_error(code: str, *, commit_phase: str, retryable: bool = False) -> RuntimeError:
    """The adapters' own error shape, in the two attributes every delivery loop reads (#604 N1)."""

    error = RuntimeError(code)
    error.code = code  # type: ignore[attr-defined]
    error.commit_phase = commit_phase  # type: ignore[attr-defined]
    error.retryable = retryable  # type: ignore[attr-defined]
    return error


def test_deliverer_settles_a_refused_preflight_without_calling_send() -> None:
    """A target the provider refuses is settled on the spot: waiting cannot make a bad channel good."""

    news = _delivery_news()
    sender = _FailingPrepareSender(
        _provider_error("news_delivery_telegram_preflight_bot_not_admin", commit_phase=COMMIT_PHASE_NOT_SENT)
    )

    asyncio.run(_deliverer(news, sender=sender).deliver(event_id="ev-strong"))

    begin = news.kwargs_of("begin_delivery")
    settle = news.kwargs_of("settle_delivery")
    assert begin["card"] == {}
    assert settle["state"] == "terminal"
    assert settle["error_code"] == "news_delivery_telegram_preflight_bot_not_admin"
    assert sender.cards == []


def test_deliverer_keeps_an_intent_whose_preflight_provably_never_reached_the_provider() -> None:
    """#604 N1. A rate limit or a connect failure on the target check is not this card's ending.

    It used to be: any preflight exception was settled `terminal`, the queue row was deleted behind
    it, and the reader never saw a card that nothing was wrong with. The attempt is spent, no ledger
    row is written at all, and the claim's own lease brings the Event back.
    """

    news = _delivery_news()
    sender = _FailingPrepareSender(
        _provider_error(
            "news_delivery_telegram_preflight_transport_failed",
            commit_phase=COMMIT_PHASE_NOT_SENT,
            retryable=True,
        )
    )

    with pytest.raises(TransientError, match="news_delivery_telegram_preflight_transport_failed"):
        asyncio.run(_deliverer(news, sender=sender).deliver(event_id="ev-strong", attempts=1))

    assert "begin_delivery" not in news.names()
    assert "settle_delivery" not in news.names()
    assert sender.cards == []


def test_deliverer_gives_the_sending_row_back_when_the_card_provably_never_left() -> None:
    """#604 N1. The `sending` row is a claim on the identity, not evidence a reader saw anything."""

    class RateLimitedSender(RecordingSender):
        def send_card(self, card: Any, **kwargs: Any) -> dict[str, Any]:
            raise _provider_error(
                "news_delivery_feishu_business_rate_limited", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True
            )

    news = _delivery_news()

    with pytest.raises(TransientError, match="news_delivery_feishu_business_rate_limited"):
        asyncio.run(_deliverer(news, sender=RateLimitedSender()).deliver(event_id="ev-strong", attempts=2))

    assert news.kwargs_of("begin_delivery")["kind"] == "first"
    assert news.kwargs_of("release_delivery") == {"event_id": "ev-strong", "kind": "first"}
    assert "settle_delivery" not in news.names(), "nothing is settled while the intent still has attempts"


def test_deliverer_settles_the_last_attempt_terminal_and_makes_the_intent_a_dead_letter() -> None:
    """#604 N1. The budget is spent: the ledger takes the provider's last word, the intent dies."""

    class RateLimitedSender(RecordingSender):
        def send_card(self, card: Any, **kwargs: Any) -> dict[str, Any]:
            raise _provider_error(
                "news_delivery_feishu_business_rate_limited", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True
            )

    news = _delivery_news()

    with pytest.raises(PermanentError, match="news_delivery_attempts_exhausted"):
        asyncio.run(_deliverer(news, sender=RateLimitedSender()).deliver(event_id="ev-strong", attempts=3))

    settle = news.kwargs_of("settle_delivery")
    assert (settle["state"], settle["error_code"]) == ("terminal", "news_delivery_feishu_business_rate_limited")
    assert "release_delivery" not in news.names(), "the ledger row is the evidence now, not a released claim"


def test_deliverer_never_retries_a_send_whose_outcome_the_provider_did_not_report() -> None:
    """#604 N1. A read timeout may already be on a reader's screen; a second card is the worse answer."""

    class UnreadableSender(RecordingSender):
        def send_card(self, card: Any, **kwargs: Any) -> dict[str, Any]:
            raise _provider_error(
                "news_delivery_feishu_transport_unreadable", commit_phase=COMMIT_PHASE_UNKNOWN, retryable=True
            )

    news = _delivery_news()

    asyncio.run(_deliverer(news, sender=UnreadableSender()).deliver(event_id="ev-strong", attempts=1))

    settle = news.kwargs_of("settle_delivery")
    assert (settle["state"], settle["error_code"]) == ("terminal", "news_delivery_feishu_transport_unreadable")
    assert "release_delivery" not in news.names()


def test_the_enrichment_edit_is_paced_by_the_same_entry_the_initial_send_uses() -> None:
    """#604 N3: one process, one pacer, whether the outbound message is a send or an edit.

    The Deliverer used to hold a second lock and a second stamp for its edit, so on Telegram -- which
    edits every News card once -- the provider saw twice the rate the operator configured. Both calls
    now queue at `InitialSendEntry`, which is what makes `min_interval_seconds` mean one thing.
    """

    interval = 0.05

    class TimingSender(RecordingEditableSender):
        def __init__(self) -> None:
            super().__init__([])
            self.at: list[tuple[str, float]] = []

        def send_card(self, card: Any, **kwargs: Any) -> dict[str, Any]:
            self.at.append(("send", time.monotonic()))
            return super().send_card(card, **kwargs)

        def edit_card(self, receipt: Mapping[str, Any], card: Any, **kwargs: Any) -> dict[str, Any]:
            self.at.append(("edit", time.monotonic()))
            return super().edit_card(receipt, card, **kwargs)

    async def scenario() -> TimingSender:
        sender = TimingSender()
        consumer = _deliverer(_delivery_news(), sender=sender, min_interval_seconds=interval)
        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return sender

    sender = asyncio.run(scenario())

    assert [operation for operation, _ in sender.at] == ["send", "edit"]
    assert sender.at[1][1] - sender.at[0][1] >= interval
    # And the loop keeps no pacing state of its own for anyone to forget about.
    assert not hasattr(_deliverer(_delivery_news()), "_edit_lock")


def test_a_deferred_claim_keeps_its_lease_unless_the_provider_asked_for_longer() -> None:
    """#604 N3: `retry_after` may raise this lane's flat 30 s wait; nothing may lower it.

    The claim already leased the row until one retry delay from now. A provider that answered a rate
    limit with a number of its own is obeyed when the number is larger -- coming back sooner earns
    another refusal and spends an attempt on nothing -- and `GREATEST` in `defer_delivery_claim` is
    what makes every other deferral leave the lease exactly where the claim put it.
    """

    class RateLimited(TransientError):
        retry_after_seconds = 120.0

    class Ordinary(TransientError):
        pass

    def _deferred(failure: type[TransientError]) -> dict[str, Any]:
        def refuse(**_kwargs: Any) -> None:
            raise failure("the provider refused this attempt")

        news = _delivery_news(latest_verdict=refuse, defer_delivery_claim=True)
        consumer = _deliverer(news, sender=RecordingSender())
        asyncio.run(consumer._deliver_claim(event_id="ev-strong", kind="first", attempts=1))
        return news.kwargs_of("defer_delivery_claim")

    advised = _deferred(RateLimited)
    plain = _deferred(Ordinary)

    # The advice is a due time in the future; the ordinary deferral asks for nothing beyond `now`, so
    # `GREATEST` leaves the claim's own lease standing.
    assert advised["next_attempt_at_ms"] - advised["now_ms"] == 120_000
    assert plain["next_attempt_at_ms"] == plain["now_ms"]


def test_deliverer_has_no_reader_count_input() -> None:
    news = _delivery_news()

    asyncio.run(_deliverer(news).deliver(event_id="ev-strong"))

    assert "sent_count_since" not in news.names()
    settle = news.kwargs_of("settle_delivery")
    assert settle["state"] == "terminal" and settle["error_code"] == "delivery_unavailable"


def test_deliverer_skips_dropped_first_cards() -> None:
    news = _delivery_news(latest_verdict=lambda *, event_id, stage: {"final_decision": "drop", "verdict": {}})
    asyncio.run(_deliverer(news).deliver(event_id="ev-strong"))
    assert "begin_delivery" not in news.names()

    # There is no `news_event_id_missing` case left: the claim's own primary key and its foreign key
    # to `news_events` are what used to be re-checked here against a message payload (#598 D2).
    with pytest.raises(PermanentError, match="news_delivery_inputs_missing"):
        asyncio.run(_deliverer(_delivery_news(event_card=None)).deliver(event_id="ghost"))


def test_deliverer_passes_macro_scope_and_shows_no_badge_for_a_review_that_will_never_run() -> None:
    """#562 §5 rows 3 and 6. The scope is a verdict fact and ships; the reference is a reviewed claim.

    With no verifier composed -- none configured, or the editorial Program faulted and left one
    unwired (#553 PR-3) -- no edit is coming, and `⏳ 关联确认中` used to stay on the card for the rest
    of its life because nothing existed to clear it. The reference is still withheld, which is the risk
    the badge was protecting; what the reader now sees is the plain `新进展` the verdict earned. The
    reviewed headline still reaches them through the confirmed edit, which
    `test_telegram_sends_a_progression_before_llm_association_review_then_edits_with_reason` proves,
    and that test also proves `pending` is still shown while a real review is in flight.
    """

    news = _delivery_news(
        event_card=_card(grounded_assets=[]),
        latest_verdict=lambda **_kwargs: {
            "final_decision": "escalate",
            "verdict": {
                "direction": "bearish",
                "fact_kind": "state_change",
                "scope": "macro",
                "novelty": "progression",
                "headline_zh": "美国就业基准继续下修",
                "assets": [],
            },
            "trace": {
                "told": [
                    {
                        "tier": "storyline",
                        "similarity": 0.0,
                        "headline_zh": "同属宏观大类但与就业无关的旧闻",
                    },
                    {
                        "tier": "exact_fact",
                        "similarity": 0.0,
                        "headline_zh": "美国此前公布初步就业基准修订",
                    },
                ]
            },
        },
    )
    sender = RecordingEditableSender([])

    asyncio.run(_deliverer(news, sender=sender).deliver(event_id="ev-strong"))

    assert sender.presentations == [
        ReaderDeliveryPresentation(
            market_data_state="pending",
            market_scope="macro",
            novelty="progression",
        )
    ]
    assert sender.presentations[0].progression_review_state is None
    assert sender.presentations[0].progression_from_headline is None
    assert sender.edited_presentations == []


def test_telegram_sends_a_progression_before_llm_association_review_then_edits_with_reason() -> None:
    async def scenario() -> tuple[RecordingNews, RecordingEditableSender, BlockingProgressionVerifier, list[str]]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bearish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "progression",
                    "headline_zh": "美光台湾工会初步投票支持罢工比例达 80%",
                    "why_zh": "工会行动从劳资协商进入有明确门槛的罢工程序。",
                    "assets": [],
                },
                "trace": {
                    "told": [
                        {
                            "i": 0,
                            "event_id": "ev-parent",
                            "tier": "storyline",
                            "similarity": 0.31,
                            "headline_zh": "美光工会此前启动劳资协商",
                            "symbols": ["MU"],
                            "at_ms": NOW_MS - 150_000,
                        }
                    ]
                },
            },
            delivery=lambda *, event_id, kind: (
                {
                    "event_id": event_id,
                    "kind": kind,
                    "state": "sent",
                    "delete_state": None,
                    "receipt": {
                        "provider": "telegram",
                        "message_id": 41,
                        "pushed_at_ms": NOW_MS - 150_000,
                        "target_sha256": "a" * 64,
                    },
                }
                if event_id == "ev-parent" and kind == "first"
                else None
            ),
        )
        sender = RecordingEditableSender(order)
        verifier = BlockingProgressionVerifier(order)
        consumer = _deliverer(
            news,
            sender=sender,
            progression_verifier=verifier,
        )

        await asyncio.wait_for(
            consumer.deliver(event_id="ev-strong"),
            timeout=0.2,
        )
        assert order[:2] == ["prepare", "send"]
        assert "edit" not in order
        assert sender.presentations[0].progression_from_headline is None
        assert sender.presentations[0].progression_review_state == "pending"
        assert news.kwargs_of("settle_delivery")["state"] == "sent"

        await asyncio.wait_for(verifier.started.wait(), timeout=0.2)
        assert order == ["prepare", "send", "review"]
        verifier.release.set()
        await consumer.close()
        return news, sender, verifier, order

    news, sender, verifier, order = asyncio.run(scenario())

    assert order == ["prepare", "send", "review", "edit"]
    assert verifier.calls[0]["candidates"][0]["headline_zh"] == "美光工会此前启动劳资协商"
    updated = sender.edited_presentations[0]
    assert updated.progression_from_headline == "美光工会此前启动劳资协商"
    assert updated.progression_review_state == "confirmed"
    assert updated.progression_review_reason == "同一工会行动进入罢工投票阶段，新增了明确比例和下一步程序。"
    assert updated.progression_review_parent_age_minutes == 2
    assert updated.progression_review_parent_message_id == 41
    assert sender.edited_cards[0]["progression_review"]["state"] == "confirmed"
    assert news.kwargs_of("begin_delivery_edit")["card"] == sender.edited_cards[0]


def test_a_told_entry_with_a_new_upstream_field_still_reaches_the_reader() -> None:
    """#562 §5 row 4. An added key in the told ledger used to raise and block the whole card.

    Every field the candidate builder reads is named one at a time, with its own type check and its own
    bound, so a key it does not read cannot change a candidate. Refusing the Event over one meant that
    the day retrieval grew a column, no progression card was delivered at all -- the message failed in
    `handle` before the send. The reader gets the card; the unknown key is simply not carried.
    """

    async def scenario() -> tuple[RecordingEditableSender, list[str], BlockingProgressionVerifier]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "macro",
                    "novelty": "progression",
                    "headline_zh": "巴拿马运河拥堵迫使液化气油轮绕行",
                    "assets": [],
                },
                "trace": {
                    "told": [
                        {
                            "i": 0,
                            "tier": "storyline",
                            "similarity": 0.35,
                            "headline_zh": "巴拿马运河管理局上调通行费",
                            "retrieval_lane_v2": "a field this builder has never been shown",
                        }
                    ]
                },
            },
        )
        sender = RecordingEditableSender(order)
        verifier = BlockingProgressionVerifier(
            order,
            response={
                "state": "rejected",
                "reason_zh": "两条新闻只是被归入了同一条主题线。",
                "verifier_id": "scripted-progression-review-v1",
            },
        )
        verifier.release.set()
        consumer = _deliverer(news, sender=sender, progression_verifier=verifier)

        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return sender, order, verifier

    sender, order, verifier = asyncio.run(scenario())

    assert order == ["prepare", "send", "review", "edit"]
    assert sender.presentations[0].progression_review_state == "pending"
    candidate = verifier.calls[0]["candidates"][0]
    assert candidate["headline_zh"] == "巴拿马运河管理局上调通行费"
    assert "retrieval_lane_v2" not in candidate


def test_telegram_removes_an_unverified_previous_headline_and_marks_the_reason() -> None:
    async def scenario() -> tuple[RecordingEditableSender, list[str]]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "macro",
                    "novelty": "progression",
                    "headline_zh": "巴拿马运河拥堵迫使液化气油轮绕行",
                    "assets": [],
                },
                "trace": {
                    "told": [
                        {
                            "i": 0,
                            "tier": "storyline",
                            "similarity": 0.35,
                            "headline_zh": "a16z 推出 Machine Age Fund",
                        }
                    ]
                },
            },
        )
        sender = RecordingEditableSender(order)
        verifier = BlockingProgressionVerifier(
            order,
            response={
                "state": "rejected",
                "reason_zh": "两条新闻的主体、事件和影响链路均不同，只是被归入了宽泛主题。",
                "verifier_id": "scripted-progression-review-v1",
            },
        )
        verifier.release.set()
        consumer = _deliverer(news, sender=sender, progression_verifier=verifier)

        await consumer.deliver(event_id="ev-strong")
        assert sender.presentations[0].progression_review_state == "pending"
        assert sender.presentations[0].progression_from_headline is None
        await consumer.close()
        return sender, order

    sender, order = asyncio.run(scenario())

    assert order == ["prepare", "send", "review", "edit"]
    updated = sender.edited_presentations[0]
    assert updated.novelty == "new_fact"
    assert updated.progression_from_headline is None
    assert updated.progression_review_state == "rejected"
    assert updated.progression_review_reason == "两条新闻的主体、事件和影响链路均不同，只是被归入了宽泛主题。"
    assert sender.edited_cards[0]["progression_review"]["state"] == "rejected"


def test_telegram_downgrades_a_confirmed_progression_without_a_receipted_parent_to_new_fact() -> None:
    async def scenario() -> RecordingEditableSender:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "progression",
                    "headline_zh": "美光台湾工会初步投票支持罢工比例达 80%",
                    "assets": [],
                },
                "trace": {
                    "told": [
                        {
                            "i": 0,
                            "event_id": "ev-parent-without-delivery",
                            "tier": "storyline",
                            "similarity": 0.63,
                            "headline_zh": "美光工会此前启动劳资协商",
                            "at_ms": NOW_MS - 60_000,
                        }
                    ]
                },
            },
            delivery=lambda **_kwargs: None,
        )
        sender = RecordingEditableSender(order)
        verifier = BlockingProgressionVerifier(order)
        verifier.release.set()
        consumer = _deliverer(news, sender=sender, progression_verifier=verifier)

        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return sender

    sender = asyncio.run(scenario())

    updated = sender.edited_presentations[0]
    assert updated.novelty == "new_fact"
    assert updated.progression_from_headline is None
    assert updated.progression_review_state == "unavailable"
    assert updated.progression_review_reason == "未找到可引用的历史推送。"
    assert updated.progression_review_parent_age_minutes is None
    assert updated.progression_review_parent_message_id is None


def test_single_name_is_sent_first_then_edited_with_a_fresh_cross_venue_contract() -> None:
    async def scenario() -> tuple[RecordingNews, RecordingEditableSender]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(
                leader_title="MetaLight (02605.HK) announces interim results",
                grounded_assets=["2605"],
            ),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "new_fact",
                    "headline_zh": "MetaLight（02605.HK）公布中期业绩",
                    "assets": [{"symbol": "2605", "market_type": "equity", "role": "primary"}],
                },
            },
        )
        sender = RecordingEditableSender(order)
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "matched",
                "candidates": ["2605", "02605", "HK2605", "METALIGHT"],
                "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": [],
                "matches": [
                    {
                        "requested_symbol": "2605",
                        "venue_family": "bitget",
                        "venue": "bitget.perp",
                        "venue_symbol": "METALIGHTUSDT",
                        "price_symbol": "METALIGHTUSDT",
                        "base_symbol": "METALIGHT",
                        "quote_asset": "USDT",
                        "instrument_class": "equity",
                    }
                ],
                "reason_zh": "已在 bitget.perp 官方市场目录命中可交易合约。",
            },
            order,
        )

        async def fetch(_venue_symbol: str, targets_ms: Sequence[int]) -> Mapping[int, PricePoint]:
            return {
                target: PricePoint(at_ms=target, price=Decimal("10") + Decimal(index), basis="trade")
                for index, target in enumerate(targets_ms)
            }

        consumer = _deliverer(
            news,
            sender=sender,
            tradability_verifier=verifier,
            price_fetcher_for=lambda venue: fetch if venue == "bitget.perp" else None,
        )
        await consumer.deliver(event_id="ev-strong")
        assert order[:2] == ["prepare", "send"]
        await consumer.close()
        return news, sender

    news, sender = asyncio.run(scenario())

    assert sender.edited_cards[0]["tradability_review"]["state"] == "matched"
    assert sender.edited_presentations[0].trade_targets == (
        ReaderTradeTarget(
            ticker="2605",
            venue="bitget.perp",
            venue_symbol="METALIGHTUSDT",
            base_symbol="METALIGHT",
            quote_asset="USDT",
        ),
    )
    assert news.kwargs_of("begin_delivery_edit")["card"] == sender.edited_cards[0]


def test_single_name_without_a_grounded_asset_is_edited_when_the_title_resolves_to_a_contract() -> None:
    async def scenario() -> tuple[RecordingNews, RecordingEditableSender, list[str]]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(
                leader_title="MetaLight (02605.HK) announces interim results",
                grounded_assets=[],
            ),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "new_fact",
                    "headline_zh": "MetaLight（02605.HK）公布中期业绩",
                    "assets": [],
                },
            },
        )
        sender = RecordingEditableSender(order)
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "matched",
                "candidates": ["02605.HK", "2605", "METALIGHT"],
                "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": [],
                "matches": [
                    {
                        "requested_symbol": "METALIGHT",
                        "venue_family": "bitget",
                        "venue": "bitget.perp",
                        "venue_symbol": "METALIGHTUSDT",
                        "price_symbol": "METALIGHTUSDT",
                        "base_symbol": "METALIGHT",
                        "quote_asset": "USDT",
                        "instrument_class": "equity",
                    }
                ],
                "reason_zh": "已在 bitget.perp 官方市场目录命中可交易合约。",
            },
            order,
        )

        async def fetch(_venue_symbol: str, targets_ms: Sequence[int]) -> Mapping[int, PricePoint]:
            return {target: PricePoint(at_ms=target, price=Decimal("10"), basis="trade") for target in targets_ms}

        consumer = _deliverer(
            news,
            sender=sender,
            tradability_verifier=verifier,
            price_fetcher_for=lambda venue: fetch if venue == "bitget.perp" else None,
        )
        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return news, sender, order

    news, sender, order = asyncio.run(scenario())

    assert order == ["prepare", "send", "market-search", "edit"]
    assert sender.edited_presentations[0].trade_targets == (
        ReaderTradeTarget("METALIGHT", "bitget.perp", "METALIGHTUSDT", "METALIGHT", "USDT"),
    )
    assert sender.edited_presentations[0].market_movements[0].ticker == "METALIGHT"
    assert news.kwargs_of("begin_delivery_edit")["card"] == sender.edited_cards[0]


def test_single_name_without_a_grounded_asset_is_edited_to_say_so_after_authoritative_absence() -> None:
    """#562 §5 row 5. The five-venue absence now edits the card; it never takes it back.

    A card the reader has already read was deleted out of their channel on an LLM-derived candidate list
    plus a title heuristic. The review is unchanged -- same five catalogues, same authoritative-absence
    rule, same identity confidence -- and what it authorises is a line on the card saying what the
    catalogues answered.
    """

    async def scenario() -> tuple[RecordingNews, RecordingEditableSender]:
        news = _delivery_news(
            event_card=_card(leader_title="MetaLight (02605.HK) announces interim results", grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "new_fact",
                    "headline_zh": "MetaLight（02605.HK）公布中期业绩",
                    "assets": [],
                },
            },
        )
        sender = RecordingEditableSender([])
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "absent",
                "candidates": ["02605.HK", "2605", "METALIGHT"],
                "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": [],
                "matches": [],
                "deletion_safe": True,
                "reason_zh": "Binance、Hyperliquid、OKX、Lighter、Bitget 均未发现可交易合约。",
            }
        )
        consumer = _deliverer(news, sender=sender, tradability_verifier=verifier)
        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return news, sender

    news, sender = asyncio.run(scenario())

    assert "delete" not in sender.order
    assert sender.edited_cards[0]["elements"][0]["content"].startswith("未找到可交易标的\n")
    # The notice is a fact about the card, not one channel's line: the model carries it and every
    # serializer reads it from there (#562 PR-C).
    assert sender.edited_reader_cards[0].untradeable is True
    assert sender.edited_cards[0]["tradability_review"]["state"] == "absent"
    assert news.kwargs_of("begin_delivery_edit")["card"] == sender.edited_cards[0]


def test_the_untradeable_notice_survives_all_five_catalogues_and_reaches_the_sent_message() -> None:
    """#562 §5 row 5. Same authoritative absence on a grounded asset: one edit, one durable intent."""

    async def scenario() -> tuple[RecordingNews, RecordingEditableSender]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(
                leader_title="MetaLight (02605.HK) announces interim results",
                grounded_assets=["2605"],
            ),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "new_fact",
                    "headline_zh": "MetaLight（02605.HK）公布中期业绩",
                    "assets": [{"symbol": "2605", "market_type": "equity", "role": "primary"}],
                },
            },
        )
        sender = RecordingEditableSender(order)
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "absent",
                "candidates": ["2605", "02605", "HK2605", "METALIGHT"],
                "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": [],
                "matches": [],
                "deletion_safe": True,
                "reason_zh": "Binance、Hyperliquid、OKX、Lighter、Bitget 均未发现可交易合约。",
            },
            order,
        )
        consumer = _deliverer(news, sender=sender, tradability_verifier=verifier)
        await consumer.deliver(event_id="ev-strong")
        assert order[:2] == ["prepare", "send"]
        await consumer.close()
        return news, sender

    news, sender = asyncio.run(scenario())

    assert sender.order == ["prepare", "send", "market-search", "edit"]
    assert sender.edited_cards[0]["elements"][0]["content"].startswith("未找到可交易标的\n")
    assert news.kwargs_of("settle_delivery_edit")["receipt"]["edited_at_ms"] == NOW_MS + 1_000


def test_title_only_acronym_is_kept_when_five_venue_absence_is_not_deletion_safe() -> None:
    async def scenario() -> tuple[RecordingNews, RecordingEditableSender]:
        news = _delivery_news(
            event_card=_card(leader_title="OpenAI (GPT) announces a research update", grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "neutral",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "new_fact",
                    "headline_zh": "OpenAI 发布研究更新",
                    "assets": [],
                },
            },
        )
        sender = RecordingEditableSender([])
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "absent",
                "candidates": ["GPT", "OPENAI"],
                "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": [],
                "matches": [],
                "deletion_safe": False,
                "reason_zh": "五个交易所均未命中，但标题代码缺少交易所前缀，保留消息等待人工确认。",
            }
        )
        consumer = _deliverer(news, sender=sender, tradability_verifier=verifier)
        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return news, sender

    _news, sender = asyncio.run(scenario())

    assert sender.edited_cards[0]["tradability_review"]["state"] == "absent"
    # #562 §5 row 5: the identity confidence still decides whether an absence is worth stating. An
    # ordinary name shaped like a ticker earns no claim about catalogues on the reader's card.
    assert "未找到可交易标的" not in sender.edited_cards[0]["elements"][0]["content"]


def test_single_name_is_kept_when_even_one_catalogue_check_is_incomplete() -> None:
    async def scenario() -> tuple[RecordingNews, RecordingEditableSender]:
        news = _delivery_news(
            event_card=_card(leader_title="MetaLight (02605.HK)", grounded_assets=["2605"]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "headline_zh": "MetaLight（02605.HK）公布中期业绩",
                    "assets": [{"symbol": "2605", "market_type": "equity", "role": "primary"}],
                },
            },
        )
        sender = RecordingEditableSender([])
        verifier = ScriptedTradabilityVerifier(
            {
                "state": "incomplete",
                "candidates": ["2605", "02605", "HK2605", "METALIGHT"],
                "checked_venues": ["hyperliquid", "okx", "lighter", "bitget"],
                "failed_venues": ["binance"],
                "matches": [],
                "reason_zh": "部分交易所目录查询失败，按安全规则保留消息。",
            }
        )
        consumer = _deliverer(news, sender=sender, tradability_verifier=verifier)
        await consumer.deliver(event_id="ev-strong")
        await consumer.close()
        return news, sender

    _news, sender = asyncio.run(scenario())

    assert sender.edited_cards[0]["tradability_review"]["state"] == "incomplete"
    assert "未找到可交易标的" not in sender.edited_cards[0]["elements"][0]["content"]


def test_deliverer_prices_exactly_the_assets_the_card_names() -> None:
    price = RecordingPrice(
        quotes=[
            {
                "requested_symbol": "NVDA",
                "symbol": "NVDA",
                "base_symbol": "NVDA",
                "venue": "binance.perp",
                "venue_symbol": "NVDAUSDT",
                "quote_asset": "USDT",
                "price": "217.32",
                "change_pct": 1.5,
                "change_basis": "rolling_24h",
                "instrument_class": "equity",
                "state": "fresh",
            }
        ]
    )
    news = _delivery_news(
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "verdict": {
                "direction": "bullish",
                "fact_kind": "state_change",
                "headline_zh": "英伟达",
                "assets": [
                    {"symbol": "NVDA", "market_type": "equity", "role": "primary"},
                    {"symbol": "OPENAI", "role": "mentioned"},
                ],
            },
        }
    )
    sender = RecordingSender()

    asyncio.run(_deliverer(news, price=price, sender=sender).deliver(event_id="ev-strong"))

    assert price.requested == [["NVDA"]]
    assert sender.cards[0]["elements"][0]["content"].splitlines()[-1] == "行情 NVDA $217.32 24h +1.50%（永续）"
    assert sender.presentations[0].trade_targets == (
        ReaderTradeTarget(
            ticker="NVDA",
            venue="binance.perp",
            venue_symbol="NVDAUSDT",
            base_symbol="NVDA",
            quote_asset="USDT",
        ),
    )


def test_deliverer_passes_multi_asset_returns_and_timing_as_ephemeral_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery.now_ms", lambda: NOW_MS)
    price = RecordingPrice(
        quotes=[
            {
                "requested_symbol": "BTC",
                "symbol": "BTC",
                "base_symbol": "BTC",
                "venue": "binance.perp",
                "venue_symbol": "BTCUSDT",
                "quote_asset": "USDT",
                "price": "101.10",
                "change_pct": 3.2,
                "change_basis": "rolling_24h",
                "instrument_class": "crypto",
                "state": "fresh",
            },
            {
                "requested_symbol": "ETH",
                "symbol": "ETH",
                "base_symbol": "ETH",
                "venue": "binance.spot",
                "venue_symbol": "ETHUSDT",
                "quote_asset": "USDT",
                "price": "201.00",
                "change_pct": 1.7,
                "change_basis": "rolling_24h",
                "instrument_class": "crypto",
                "state": "fresh",
            },
        ],
    )
    news = _delivery_news(
        event_card=_card(
            leader_published_at_ms=NOW_MS - 20_000,
            leader_url="https://www.bloomberg.com/news/articles/example",
            grounded_assets=["BTC", "ETH"],
        ),
        event_delivery_timing={
            "news_at_ms": NOW_MS - 20_000,
            "reaction_anchor_at_ms": NOW_MS - 20_000,
            "observed_at_ms": NOW_MS - 8_000,
        },
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "verdict": {
                "direction": "bullish",
                "fact_kind": "state_change",
                "headline_zh": "比特币与以太坊走强",
                "assets": [
                    {"symbol": "BTC", "market_type": "crypto", "role": "primary"},
                    {"symbol": "ETH", "market_type": "crypto", "role": "primary"},
                ],
            },
        },
    )
    sender = RecordingSender()
    candle_calls: list[tuple[str, str, int, int]] = []

    def candle_fetcher_for(venue: str) -> Any:
        async def fetch(venue_symbol: str, start_ms: int, end_ms: int) -> tuple[Candle, ...]:
            candle_calls.append((venue, venue_symbol, start_ms, end_ms))
            hour_price, news_price = {
                "BTCUSDT": ("99.00", "100.00"),
                "ETHUSDT": ("200.00", "199.00"),
            }[venue_symbol]
            hour_at = NOW_MS - 3_600_000
            news_at = NOW_MS - 20_000
            return (
                Candle(hour_at - 60_000, hour_at, Decimal(hour_price)),
                Candle(news_at - 60_000, news_at, Decimal(news_price)),
            )

        return fetch

    asyncio.run(
        _deliverer(
            news,
            price=price,
            sender=sender,
            candle_fetcher_for=candle_fetcher_for,
        ).deliver(event_id="ev-strong")
    )

    assert sender.presentations == [
        ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget("BTC", "binance.perp", "BTCUSDT", "BTC", "USDT"),
                ReaderTradeTarget("ETH", "binance.spot", "ETHUSDT", "ETH", "USDT"),
            ),
            market_movements=(
                ReaderMarketMovement("BTC", 110, 212, 320, "available"),
                ReaderMarketMovement("ETH", 101, 50, 170, "available"),
            ),
            news_at_ms=NOW_MS - 20_000,
            observed_at_ms=NOW_MS - 8_000,
        )
    ]
    assert price.requested_reaction_versions == []
    assert candle_calls == [
        ("binance.perp", "BTCUSDT", NOW_MS - 3_690_000, NOW_MS),
        ("binance.spot", "ETHUSDT", NOW_MS - 3_690_000, NOW_MS),
    ]


def test_delivery_price_points_try_binance_first_and_fail_over_the_whole_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery.now_ms", lambda: NOW_MS)
    price = RecordingPrice(
        quotes=[
            {
                "requested_symbol": "MSFT",
                "symbol": "MSFT",
                "base_symbol": "MSFT",
                "venue": "binance.perp",
                "venue_symbol": "MSFTUSDT",
                "quote_asset": "USDT",
                "instrument_class": "equity",
                "price": "500",
                "state": "fresh",
                "change_pct": 2.0,
            }
        ],
        instruments={
            "MSFT": (
                PriceInstrument("binance.perp", "MSFTUSDT", "MSFT", "equity", "USDT"),
                # Same ticker text, wrong asset class: never a Microsoft fallback.
                PriceInstrument("hl.spot", "@289", "MSFT", "crypto", "USDC"),
                PriceInstrument("hl.xyz", "xyz:MSFT", "MSFT", "equity"),
                PriceInstrument("okx.perp", "MSFT-USDT-SWAP", "MSFT", "equity", "USDT"),
            )
        },
    )
    news_at = NOW_MS - 20_000
    news = _delivery_news(
        event_card=_card(grounded_assets=["MSFT"], leader_published_at_ms=news_at),
        event_delivery_timing={"news_at_ms": news_at, "observed_at_ms": news_at + 1_000},
        latest_verdict=lambda **_kwargs: {
            "final_decision": "push",
            "verdict": {
                "direction": "bullish",
                "fact_kind": "state_change",
                "headline_zh": "微软事件",
                "assets": [{"symbol": "MSFT", "market_type": "equity", "role": "primary"}],
            },
        },
    )
    sender = RecordingSender()
    calls: list[tuple[str, str]] = []

    def price_fetcher_for(venue: str) -> Any:
        async def fetch(venue_symbol: str, targets: Any) -> dict[int, PricePoint]:
            calls.append((venue, venue_symbol))
            current, hour, day, event = targets
            if venue == "binance.perp":
                return {
                    current: PricePoint(current - 50, Decimal("101"), "trade"),
                    hour: PricePoint(hour - 50, Decimal("100"), "trade"),
                    day: PricePoint(day - 50, Decimal("90"), "trade"),
                }
            if venue == "hl.xyz":
                return {
                    current: PricePoint(current - 40, Decimal("101"), "trade"),
                    hour: PricePoint(hour, Decimal("100"), "candle_1m"),
                    day: PricePoint(day, Decimal("80"), "candle_1m"),
                    event: PricePoint(event - 10, Decimal("99"), "trade"),
                }
            return {}

        return fetch

    asyncio.run(
        _deliverer(
            news,
            price=price,
            sender=sender,
            price_fetcher_for=price_fetcher_for,
        ).deliver(event_id="ev-strong")
    )

    assert calls == [("binance.perp", "MSFTUSDT"), ("hl.xyz", "xyz:MSFT")]
    assert sender.presentations[0].market_movements == (ReaderMarketMovement("MSFT", 202, 100, 2625, "available"),)
    assert sender.presentations[0].trade_targets == (
        ReaderTradeTarget(
            ticker="MSFT",
            venue="hl.builder",
            venue_symbol="xyz:MSFT",
            base_symbol="MSFT",
            quote_asset="",
        ),
    )


def test_telegram_delivery_sends_before_market_enrichment_then_edits_the_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery.now_ms", lambda: NOW_MS)

    async def scenario() -> tuple[RecordingNews, RecordingEditableSender, list[str]]:
        allow_prices = asyncio.Event()
        price_started = asyncio.Event()
        order: list[str] = []
        price = RecordingPrice(
            quotes=[
                {
                    "requested_symbol": "MSFT",
                    "symbol": "MSFT",
                    "base_symbol": "MSFT",
                    "venue": "binance.perp",
                    "venue_symbol": "MSFTUSDT",
                    "quote_asset": "USDT",
                    "instrument_class": "equity",
                    "price": "500",
                    "state": "fresh",
                }
            ],
            instruments={"MSFT": (PriceInstrument("binance.perp", "MSFTUSDT", "MSFT", "equity", "USDT"),)},
        )
        news_at = NOW_MS - 20_000
        news = _delivery_news(
            event_card=_card(grounded_assets=["MSFT"], leader_published_at_ms=news_at),
            event_delivery_timing={"news_at_ms": news_at, "observed_at_ms": news_at + 1_000},
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "headline_zh": "微软事件",
                    "assets": [{"symbol": "MSFT", "market_type": "equity", "role": "primary"}],
                },
            },
        )
        sender = RecordingEditableSender(order)

        def price_fetcher_for(_venue: str) -> Any:
            async def fetch(_venue_symbol: str, targets: Any) -> dict[int, PricePoint]:
                order.append("price")
                price_started.set()
                await allow_prices.wait()
                current, hour, day, event = targets
                return {
                    current: PricePoint(current, Decimal("101"), "trade"),
                    hour: PricePoint(hour, Decimal("100"), "trade"),
                    day: PricePoint(day, Decimal("90"), "candle_1m"),
                    event: PricePoint(event, Decimal("99"), "trade"),
                }

            return fetch

        consumer = _deliverer(
            news,
            price=price,
            sender=sender,
            price_fetcher_for=price_fetcher_for,
        )
        await asyncio.wait_for(
            consumer.deliver(event_id="ev-strong"),
            timeout=0.2,
        )
        assert order[:2] == ["prepare", "send"]
        assert "edit" not in order
        assert sender.presentations[0].market_data_state == "pending"
        assert news.kwargs_of("settle_delivery")["state"] == "sent"

        await asyncio.wait_for(price_started.wait(), timeout=0.2)
        assert order == ["prepare", "send", "price"]
        allow_prices.set()
        await consumer.close()
        return news, sender, order

    news, sender, order = asyncio.run(scenario())

    assert order == ["prepare", "send", "price", "edit"]
    assert sender.edited_presentations[0].market_data_state == "ready"
    assert sender.edited_presentations[0].market_movements == (
        ReaderMarketMovement("MSFT", 202, 100, 1222, "available"),
    )
    assert sender.edited_cards[0]["elements"][0]["content"].splitlines()[-1].startswith("行情 MSFT $101")
    assert news.kwargs_of("begin_delivery_edit")["card"] == sender.edited_cards[0]
    assert news.kwargs_of("settle_delivery_edit")["receipt"]["edited_at_ms"] == NOW_MS + 1_000


def test_delivery_refuses_to_claim_when_startup_edit_reconciliation_is_unavailable() -> None:
    def unavailable(**_kwargs: Any) -> int:
        raise TransientError("edit reconciliation unavailable")

    news = _delivery_news(terminalize_interrupted_delivery_edits=unavailable)
    bus = FakeBus()
    consumer = _deliverer(news, sender=RecordingSender())

    with pytest.raises(TransientError, match="edit reconciliation unavailable"):
        asyncio.run(consumer.run(stop_event=asyncio.Event()))

    assert bus.consumed == []


def test_delivery_waits_out_startup_news_lane_contention_before_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "_DELIVERY_STARTUP_RECONCILE_RETRY_SECONDS", 0.001)
    attempts = 0

    def reconcile_after_contention(**_kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DeferError("db_admission_timeout:news_delivery_edit_reconcile")
        return 1

    news = _delivery_news(terminalize_interrupted_delivery_edits=reconcile_after_contention)
    consumer = _deliverer(news, sender=RecordingSender())
    stop_event = asyncio.Event()

    async def scenario() -> None:
        task = asyncio.create_task(consumer.run(stop_event=stop_event))
        for _ in range(100):
            if "claim_due_deliveries" in news.names():
                break
            if task.done():
                await task
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("delivery did not claim after startup contention cleared")
        stop_event.set()
        await task

    asyncio.run(scenario())

    assert attempts == 2
    assert "claim_due_deliveries" in news.names()


def test_delivery_periodically_retries_stale_edit_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery._DELIVERY_EDIT_RECONCILE_SECONDS", 0.01)
    stop_event = asyncio.Event()
    attempts = 0

    def stale_reconcile(**_kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientError("temporary edit reconciliation outage")
        stop_event.set()
        return 1

    news = _delivery_news(
        terminalize_interrupted_delivery_edits=0,
        terminalize_stale_delivery_edits=stale_reconcile,
    )
    consumer = _deliverer(news, sender=RecordingSender())

    asyncio.run(consumer.run(stop_event=stop_event))

    assert attempts == 2
    assert "claim_due_deliveries" in news.names()


def test_delivery_fails_closed_when_periodic_edit_reconciliation_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery._DELIVERY_EDIT_RECONCILE_SECONDS", 0.01)

    def invariant_failure(**_kwargs: Any) -> int:
        raise RuntimeError("edit reconciliation invariant failure")

    news = _delivery_news(
        terminalize_interrupted_delivery_edits=0,
        terminalize_stale_delivery_edits=invariant_failure,
    )
    consumer = _deliverer(news, sender=RecordingSender())

    with pytest.raises(RuntimeError, match="edit reconciliation invariant failure"):
        asyncio.run(consumer.run(stop_event=asyncio.Event()))

    # The claim loop was live beside the reconciler and the reconciler's crash still won: `run`
    # cancels its sibling and raises, which is what marks the `news_delivery` capability faulted.
    assert "claim_due_deliveries" in news.names()


def test_pending_enrichment_does_not_block_the_next_telegram_initial_send() -> None:
    async def scenario() -> list[str]:
        allow_prices = asyncio.Event()
        first_price_started = asyncio.Event()
        order: list[str] = []
        news = _delivery_news(
            begin_states=["new", "new"],
            event_card=_card(grounded_assets=["MSFT"]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "headline_zh": "微软事件",
                    "assets": [{"symbol": "MSFT", "market_type": "equity", "role": "primary"}],
                },
            },
        )

        def price_fetcher_for(_venue: str) -> Any:
            async def fetch(_venue_symbol: str, targets: Any) -> dict[int, PricePoint]:
                order.append("price")
                first_price_started.set()
                await allow_prices.wait()
                current, hour = targets[:2]
                return {
                    current: PricePoint(current, Decimal("101"), "trade"),
                    hour: PricePoint(hour, Decimal("100"), "trade"),
                }

            return fetch

        consumer = _deliverer(
            news,
            price=RecordingPrice(
                quotes=[
                    {
                        "requested_symbol": "MSFT",
                        "symbol": "MSFT",
                        "base_symbol": "MSFT",
                        "venue": "binance.perp",
                        "venue_symbol": "MSFTUSDT",
                        "quote_asset": "USDT",
                        "instrument_class": "equity",
                        "price": "500",
                        "state": "fresh",
                    }
                ],
                instruments={"MSFT": (PriceInstrument("binance.perp", "MSFTUSDT", "MSFT", "equity", "USDT"),)},
            ),
            sender=RecordingEditableSender(order),
            price_fetcher_for=price_fetcher_for,
        )
        await consumer.deliver(event_id="ev-first")
        await asyncio.wait_for(first_price_started.wait(), timeout=0.2)
        await asyncio.wait_for(
            consumer.deliver(event_id="ev-second"),
            timeout=0.2,
        )
        assert order.count("send") == 2
        assert "edit" not in order
        allow_prices.set()
        await consumer.close()
        return order

    order = asyncio.run(scenario())

    assert order.count("send") == 2
    assert order.count("edit") == 2
    assert order.index("send") < order.index("price")


def test_delivery_drain_waits_for_native_edit_before_closing_the_sender() -> None:
    async def scenario() -> tuple[list[str], BlockingEditSender]:
        order: list[str] = []
        sender = BlockingEditSender(order)
        finite = FiniteOperations(telemetry=TelemetryRegistry())
        news = _delivery_news(
            event_card=_card(grounded_assets=["MSFT"]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "headline_zh": "微软事件",
                    "assets": [{"symbol": "MSFT", "market_type": "equity", "role": "primary"}],
                },
            },
        )
        consumer = DelivererLoop(
            db=FakeWorkerDatabase(
                news,
                price=RecordingPrice(
                    quotes=[
                        {
                            "requested_symbol": "MSFT",
                            "symbol": "MSFT",
                            "base_symbol": "MSFT",
                            "venue": "binance.perp",
                            "venue_symbol": "MSFTUSDT",
                            "quote_asset": "USDT",
                            "instrument_class": "equity",
                            "price": "500",
                            "state": "fresh",
                        }
                    ]
                ),
            ),
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=0.0,
        )
        await consumer.deliver(event_id="ev-strong")
        assert await asyncio.to_thread(sender.started.wait, 1.0)
        finite.close_admission()
        drain_task = asyncio.create_task(consumer.drain())
        await asyncio.sleep(0.02)
        assert not drain_task.done()
        assert sender.closed is False
        sender.release.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert await finite.drain(timeout_seconds=1.0)
        await consumer.close_sender()
        finite.close()
        return order, sender

    order, sender = asyncio.run(scenario())

    assert order[-2:] == ["edit", "close"]
    assert sender.closed is True


def test_delivery_drain_allows_an_accepted_edit_to_submit_after_shutdown_admission_closes() -> None:
    async def scenario() -> list[str]:
        order: list[str] = []
        price_started = asyncio.Event()
        allow_price = asyncio.Event()
        sender = RecordingEditableSender(order)
        finite = FiniteOperations(telemetry=TelemetryRegistry())
        news = _delivery_news(
            event_card=_card(grounded_assets=["MSFT"]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bullish",
                    "fact_kind": "state_change",
                    "headline_zh": "微软事件",
                    "assets": [{"symbol": "MSFT", "market_type": "equity", "role": "primary"}],
                },
            },
        )

        def price_fetcher_for(_venue: str) -> Any:
            async def fetch(_venue_symbol: str, targets: Any) -> dict[int, PricePoint]:
                order.append("price")
                price_started.set()
                await allow_price.wait()
                current, hour = targets[:2]
                return {
                    current: PricePoint(current, Decimal("101"), "trade"),
                    hour: PricePoint(hour, Decimal("100"), "trade"),
                }

            return fetch

        consumer = DelivererLoop(
            db=FakeWorkerDatabase(
                news,
                price=RecordingPrice(
                    quotes=[
                        {
                            "requested_symbol": "MSFT",
                            "symbol": "MSFT",
                            "base_symbol": "MSFT",
                            "venue": "binance.perp",
                            "venue_symbol": "MSFTUSDT",
                            "quote_asset": "USDT",
                            "instrument_class": "equity",
                            "price": "500",
                            "state": "fresh",
                        }
                    ],
                    instruments={"MSFT": (PriceInstrument("binance.perp", "MSFTUSDT", "MSFT", "equity", "USDT"),)},
                ),
            ),
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=0.0,
            price_fetcher_for=price_fetcher_for,
        )
        await consumer.deliver(event_id="ev-strong")
        await asyncio.wait_for(price_started.wait(), timeout=0.2)
        finite.close_admission()
        drain_task = asyncio.create_task(consumer.drain())
        allow_price.set()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert await finite.drain(timeout_seconds=1.0)
        await consumer.close_sender()
        finite.close()
        return order

    order = asyncio.run(scenario())

    assert order == ["prepare", "send", "price", "edit"]


def _quoted_delivery_news() -> RecordingNews:
    """A pushed crypto card whose primary the Gate did ground, which is what earns a quote line.

    This used to be an OI frame. #458 stopped the OI lane from pushing, so it can no longer stand in
    for "a card that reaches delivery"; what the tests below are about — a stale quote, and one whose
    reference change expired — is the quote line itself, and it is reached the ordinary way.
    """

    return _delivery_news(
        event_card=_card(admission="candidate", grounded_assets=["DOGE"]),
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "verdict": {
                "novelty": "new_fact",
                "restates": -1,
                "direction": "bullish",
                "scope": "single_name",
                "fact_kind": "state_change",
                "confidence": 0.8,
                "headline_zh": "DOGE 现货 ETF 通过",
                "why_zh": "现货通道打开。",
                "assets": [{"symbol": "DOGE", "role": "primary", "market_type": "crypto"}],
            },
        },
    )


def test_deliverer_omits_a_stale_quote_after_requesting_the_grounded_symbol() -> None:
    price = RecordingPrice(quotes=[{"symbol": "DOGE", "price": "0.2143", "state": "stale"}])
    sender = RecordingSender()
    news = _quoted_delivery_news()

    asyncio.run(_deliverer(news, price=price, sender=sender).deliver(event_id="ev-strong"))

    assert price.requested == [["DOGE"]]
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)
    assert news.kwargs_of("settle_delivery")["state"] == "sent"  # quote state never changes eligibility


def test_deliverer_keeps_a_fresh_price_after_its_reference_change_expires() -> None:
    price = RecordingPrice(
        quotes=[
            {
                "symbol": "DOGE",
                "price": "0.2143",
                "change_pct": None,
                "change_basis": "rolling_24h",
                "reference_age_ms": 360_001,
                "instrument_class": "crypto",
                "state": "fresh",
            }
        ]
    )
    news = _quoted_delivery_news()
    sender = RecordingSender()

    asyncio.run(_deliverer(news, price=price, sender=sender).deliver(event_id="ev-strong"))

    lines = sender.cards[0]["elements"][0]["content"].splitlines()
    assert lines[-1] == "行情 DOGE $0.2143"
    assert news.kwargs_of("settle_delivery")["state"] == "sent"


def test_deliverer_does_not_price_an_ordinary_ungrounded_model_asset() -> None:
    price = RecordingPrice(quotes=[{"symbol": "DOGE", "price": "0.2143", "state": "fresh"}])
    news = _delivery_news(
        event_card=_card(admission="candidate", grounded_assets=[]),
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "program_version": "program-v4",
            "verdict": {
                "direction": "bullish",
                "fact_kind": "state_change",
                "headline_zh": "模型提到了 DOGE",
                "assets": [{"symbol": "DOGE", "role": "primary"}],
            },
        },
    )
    sender = RecordingSender()

    asyncio.run(_deliverer(news, price=price, sender=sender).deliver(event_id="ev-strong"))

    assert price.requested == []
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_delivers_when_the_price_plane_fails() -> None:
    news = _delivery_news()
    sender = RecordingSender()

    asyncio.run(
        _deliverer(news, price=RecordingPrice(error=RuntimeError("quote lane on fire")), sender=sender).deliver(
            event_id="ev-strong"
        )
    )

    assert news.kwargs_of("settle_delivery")["state"] == "sent"
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_delivers_when_quote_read_cannot_be_admitted() -> None:
    news = _delivery_news()
    sender = RecordingSender()
    db = FakeWorkerDatabase(news, admission_timeout_for={"news_delivery_quotes"}, price=RecordingPrice())
    deliverer = DelivererLoop(db=db, sender=sender, finite_operations=InlineFinite(), min_interval_seconds=0.0)

    asyncio.run(deliverer.deliver(event_id="ev-strong"))

    assert "news_delivery_quotes" in db.operations
    assert news.kwargs_of("settle_delivery")["state"] == "sent"
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_does_not_read_quotes_for_a_card_it_will_not_send() -> None:
    dropped = _delivery_news(latest_verdict=lambda *, event_id, stage: {"final_decision": "drop", "verdict": {}})
    dropped_price = RecordingPrice()
    asyncio.run(_deliverer(dropped, price=dropped_price, sender=RecordingSender()).deliver(event_id="ev-strong"))
    assert dropped_price.requested == []

    unavailable_price = RecordingPrice()
    asyncio.run(_deliverer(_delivery_news(), price=unavailable_price).deliver(event_id="ev-strong"))
    assert unavailable_price.requested == []


def test_janitor_names_semantic_work_that_exhausted_its_attempts(caplog, monkeypatch) -> None:
    """Exhausted work is visible failed work, never silently pending."""

    news = RecordingNews(expired_count=3)
    # Some earlier real-runtime tests reconfigure logging. This unit owns its logger state and restores it.
    monkeypatch.setattr(logging.getLogger("tracefold.news"), "disabled", False)
    with caplog.at_level("WARNING", logger="tracefold.news"):
        exhausted = FakeWorkerDatabase(news)
        asyncio.run(JanitorLoop(db=exhausted, cold_db=exhausted.cold_port, bus=FakeBus()).repair_semantic_wakes())
    assert any("exhausted its attempts" in r.getMessage() for r in caplog.records)

    quiet = RecordingNews()
    with caplog.at_level("WARNING", logger="tracefold.news"):
        caplog.clear()
        quiet_db = FakeWorkerDatabase(quiet)
        asyncio.run(JanitorLoop(db=quiet_db, cold_db=quiet_db.cold_port, bus=FakeBus()).repair_semantic_wakes())
    assert not [r for r in caplog.records if "exhausted" in r.getMessage()]


def test_janitor_re_wakes_stale_semantic_work_and_records_marker_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.maintenance.now_ms", lambda: NOW_MS)
    news = RecordingNews(
        pending_semantic_event_ids=["ev-stale"],
        semantic_wake_route={
            "event_id": "ev-stale",
            "wanted_revision": 2,
            "dedupe_family": "general",
            "queue_priority": "normal",
            "trace_id": "trace-event",
        },
        semantic_wake_state={"pending": 2, "oldest_pending_at_ms": NOW_MS - 120_000, "expired": 3},
    )
    telemetry = RecordingHandoffTelemetry()
    bus = FakeBus()
    db = FakeWorkerDatabase(news, admission_timeout_for={"news_semantic_wake_mark"})

    woken = asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, bus=bus, telemetry=telemetry).repair_semantic_wakes())

    assert woken == 1
    assert [message.message_id for message in bus.published] == ["event:ev-stale:2"]
    assert bus.published[0].payload == {"event_id": "ev-stale", "revision": 2}
    assert bus.published[0].trace_id == "trace-event"
    assert telemetry.states == [("event", 2, 120.0, 3)]
    assert telemetry.repairs == [("event", "marker_pending")]


def test_janitor_hands_pending_notification_work_to_the_notification_wake_only_when_one_is_composed() -> None:
    news = RecordingNews(pending_notification_event_ids=["ev-notify"])
    woken: list[tuple[str, str]] = []

    async def notification_wake(event_id: str, channel: str) -> None:
        woken.append((event_id, channel))

    db = FakeWorkerDatabase(news)
    asyncio.run(
        JanitorLoop(
            db=db, cold_db=db.cold_port, bus=FakeBus(), notification_wake=notification_wake
        ).repair_semantic_wakes()
    )
    assert woken == [("ev-notify", "news")]

    alone = RecordingNews(pending_notification_event_ids=["ev-notify"])
    alone_db = FakeWorkerDatabase(alone)
    asyncio.run(JanitorLoop(db=alone_db, cold_db=alone_db.cold_port, bus=FakeBus()).repair_semantic_wakes())
    assert "pending_notification_event_ids" not in alone.names()


def test_janitor_contains_typed_wake_transients_but_unknown_failures_escape() -> None:
    class UnavailableBus(FakeBus):
        async def publish(self, message: BusMessage) -> None:
            del message
            raise BrokerUnavailable("offline")

        async def queue_depths(self) -> dict[str, Any]:
            return {}

        prefix = ""

    news = RecordingNews(pending_semantic_event_ids=["ev-transient"], purge_learning_retention={})
    telemetry = RecordingHandoffTelemetry()
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, bus=UnavailableBus(), telemetry=telemetry).turn())

    assert telemetry.repairs == [("event", "transient")]
    assert "mark_semantic_work_published" not in news.names()
    # The judgment cache retention runs on the cold lane every turn.
    assert "news_semantic_cache_retention" in db.heavy_operations

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("repair bug")

    broken = RecordingNews(semantic_wake_state=_explode)
    broken_db = FakeWorkerDatabase(broken)
    with pytest.raises(RuntimeError, match="repair bug"):
        asyncio.run(JanitorLoop(db=broken_db, cold_db=broken_db.cold_port, bus=FakeBus()).turn())


def test_janitor_projects_the_latest_publish_failure_into_broker_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bus(FakeBus):
        prefix = ""

        def __init__(self) -> None:
            super().__init__()
            self.last_publish_failure = BrokerPublishFailure(
                error_code="news_broker_publish_failed:TimeoutError",
                at_ms=NOW_MS + 5_000,
            )

        async def broker_snapshot(self) -> dict[str, Any]:
            return {}

    first_read = True

    def clock_ms() -> int:
        nonlocal first_read
        if first_read:
            first_read = False
            return NOW_MS
        return NOW_MS + 10_000

    monkeypatch.setattr("tracefold.news.pipeline.maintenance.now_ms", clock_ms)
    news = RecordingNews(purge_learning_retention={})
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, bus=Bus()).turn())

    snapshot = news.kwargs_of("update_broker_snapshot")["snapshot"]
    assert snapshot["connected"] is True
    assert snapshot["last_publish_error_code"] == "news_broker_publish_failed:TimeoutError"
    assert snapshot["last_publish_error_at_ms"] == NOW_MS + 5_000
    assert news.kwargs_of("update_broker_snapshot")["now_ms"] == NOW_MS + 10_000


def test_janitor_refreshes_every_fixed_opennews_incident_gauge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.maintenance.now_ms", lambda: NOW_MS)
    news = RecordingNews(
        open_incident_summary=[
            {"cause_class": "authentication", "count": 2, "oldest_opened_at_ms": NOW_MS - 90_000},
            {"cause_class": "future_dynamic_cause", "count": 3, "oldest_opened_at_ms": NOW_MS - 30_000},
        ],
        purge_learning_retention={},
    )
    telemetry = RecordingHandoffTelemetry()
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, telemetry=telemetry).turn())

    assert len(telemetry.incidents) == 13
    assert len({cause for _, cause, _, _ in telemetry.incidents}) == 11
    observed = {cause: (provider, count, age) for provider, cause, count, age in telemetry.incidents}
    assert observed["authentication"] == ("opennews", 2, 90.0)
    assert observed["unknown"] == ("opennews", 3, 30.0)
    assert observed["broker_unavailable"] == ("opennews", 0, 0.0)

    deferred_news = RecordingNews(purge_learning_retention={})
    deferred_db = FakeWorkerDatabase(
        deferred_news,
        admission_timeout_for={"news_opennews_incident_summary"},
    )
    asyncio.run(JanitorLoop(db=deferred_db, cold_db=deferred_db.cold_port, telemetry=telemetry).turn())

    def _explode() -> Any:
        raise RuntimeError("incident projection bug")

    broken_news = RecordingNews(open_incident_summary=_explode)
    broken_db = FakeWorkerDatabase(broken_news)
    with pytest.raises(RuntimeError, match="incident projection bug"):
        asyncio.run(JanitorLoop(db=broken_db, cold_db=broken_db.cold_port, telemetry=telemetry).turn())


def test_janitor_runs_raw_batches_and_learning_retention_in_separate_cold_transactions() -> None:
    raw_batches = iter(
        (
            {
                "deleted_items": 500,
                "backlog_items": 1,
                "backlog_capped": False,
                "oldest_observed_at_ms": NOW_MS - 40 * 86_400_000,
            },
            {
                "deleted_items": 1,
                "backlog_items": 0,
                "backlog_capped": False,
                "oldest_observed_at_ms": None,
            },
        )
    )
    news = RecordingNews(
        purge_before=lambda **_kwargs: next(raw_batches),
        purge_learning_retention={
            "deleted_recordings": 2,
            "deleted_cases": 1,
            "deleted_artifacts": 0,
        },
    )
    db = FakeWorkerDatabase(news)
    telemetry = RecordingHandoffTelemetry()

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, telemetry=telemetry, chain_tape_enabled=True).turn())

    assert db.heavy_operations == [
        "news_expire_bands",
        "news_raw_retention",
        "news_raw_retention",
        # #553 PR-2: one bounded batch per pass, beside the purge that made those observations
        # unreadable, so alerting state for a group nobody can read any more goes with them.
        "news_market_track_retention",
        # #572 PR-1: the wallet tape's own bounded batch, on the same heavy slot and beside the other
        # sweeps rather than as a task of its own.
        "news_chain_tape_retention",
        # #706: the judgment cache and stage checkpoints keep 14 days, one bounded batch per pass.
        "news_semantic_cache_retention",
        "news_learning_retention",
    ]
    assert news.kwargs_of("chain_tape_purge_fills")["limit"] == 500
    # The judged cutoff, not the raw one: a track outlives the raw text and dies with the judged
    # window, which is when its group's last observation actually stops being readable.
    prune = news.kwargs_of("market_prune_tracks")
    assert prune == {"cutoff_ms": news.kwargs_of("purge_before")["judged_cutoff_ms"], "limit": 500}
    assert db.operations == ["news_opennews_incident_summary"]
    assert news.kwargs_of("purge_learning_retention") == {"batch_size": 500}
    assert [name for name in news.names() if name == "purge_before"] == ["purge_before", "purge_before"]
    assert telemetry.raw_retention[0]["deleted_rows"] == 501
    assert telemetry.raw_retention[0]["batches"] == 2
    assert telemetry.raw_retention[0]["backlog_rows"] == 0
    raw_timeouts = [timeout for name, timeout in db.operation_timeouts if name == "news_raw_retention"]
    assert len(raw_timeouts) == 2
    assert all(0 < timeout <= 1.0 for timeout in raw_timeouts)


def test_janitor_does_not_sweep_a_wallet_tape_that_is_not_running() -> None:
    """#572 PR-1. A disabled tape writes no fills, so a `DELETE` every sixty seconds has a known answer."""

    news = RecordingNews(purge_before={}, purge_learning_retention={})
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port).turn())

    assert "news_chain_tape_retention" not in db.heavy_operations
    assert "chain_tape_purge_fills" not in news.names()


def test_janitor_records_retention_failure_without_stopping_the_loop() -> None:
    def _fail(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("broken retention function")

    news = RecordingNews(purge_before={}, purge_learning_retention=_fail)
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, chain_tape_enabled=True).turn())

    assert db.heavy_operations == [
        "news_expire_bands",
        "news_raw_retention",
        "news_market_track_retention",
        "news_chain_tape_retention",
        "news_semantic_cache_retention",
        "news_learning_retention",
        "news_learning_retention_error",
    ]
    error = news.kwargs_of("record_learning_retention_error")
    assert error["error_code"] == "learning_retention_failed:RuntimeError"


# ---------------------------------------------------------- Receiver / Recovery
class _DurableIncidentNews(RecordingNews):
    def __init__(self) -> None:
        super().__init__()
        self.open_causes: set[str] = set()

    def open_incident(self, *, cause_class: str, **kwargs: Any) -> int:
        self.calls.append(("open_incident", {"cause_class": cause_class, **kwargs}))
        self.open_causes.add(cause_class)
        return 1

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, **kwargs: Any) -> int:
        self.calls.append(("close_open_incidents", {"cause_classes": cause_classes, **kwargs}))
        selected = set(cause_classes or self.open_causes)
        closed = len(self.open_causes & selected)
        self.open_causes -= selected
        return closed

    def update_ingest_state(self, **kwargs: Any) -> None:
        self.calls.append(("update_ingest_state", kwargs))


class _FailingPublishBus(FakeBus):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def publish(self, message: BusMessage) -> None:
        del message
        raise self.error


class _FailingOnceBus(FakeBus):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.attempts = 0

    async def publish(self, message: BusMessage) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise self.error
        await super().publish(message)


class _BlockedPublishBus(FakeBus):
    async def publish(self, message: BusMessage) -> None:
        del message
        await asyncio.Event().wait()


def test_receiver_surfaces_database_and_unknown_publish_failures() -> None:
    news = _DurableIncidentNews()
    deferred = OpenNewsReceiver(
        bus=_FailingPublishBus(BrokerUnavailable("news_broker_not_connected")),
        db=FakeWorkerDatabase(news, admission_timeout_for={"news_ingest_backpressure"}),
        ws_client=None,
        recovery=None,
    )
    unknown = OpenNewsReceiver(
        bus=_FailingPublishBus(RuntimeError("bug")),
        db=FakeWorkerDatabase(news),
        ws_client=None,
        recovery=None,
    )

    async def scenario() -> None:
        with pytest.raises(DeferError, match="news_ingest_backpressure"):
            await deferred._publish_frame({"params": {"id": 1}}, strategy_id="1018")
        with pytest.raises(RuntimeError, match="bug"):
            await unknown._publish_frame({"params": {"id": 2}}, strategy_id="1018")

    asyncio.run(scenario())
    assert not news.open_causes


class _StubWsClient:
    """The provider socket, reduced to what the Receiver loop actually calls."""

    def __init__(self) -> None:
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def receive(self) -> Any:
        await asyncio.Event().wait()  # a live socket with a quiet provider

    async def close(self) -> None:
        self.closed += 1


def test_a_receiver_that_stops_gracefully_records_a_planned_shutdown_and_no_outage() -> None:
    """The graceful path is unchanged and must stay distinguishable from a kill."""

    news = _DurableIncidentNews()
    news.responses["ingest_liveness"] = {"connected": False, "updated_at_ms": 1_700_000_000_000}
    receiver = OpenNewsReceiver(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        ws_client=_StubWsClient(),
        recovery=None,
    )

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(receiver.run(stop_event=stop))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    assert news.open_causes == {"planned_shutdown"}
    opened = news.kwargs_of("open_incident")
    assert opened["cause_class"] == "planned_shutdown" and opened["planned"] is True


def _pending_incident(incident_id: int = 1) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "cause_class": "broker_unavailable",
        "opened_at_ms": 1_000_000_000_000,
        "closed_at_ms": 2_000_000_000_000,
        "recovery_from_at_ms": None,
        "recovery_to_at_ms": None,
    }


def _history_hit(hit_id: int, published_at_ms: int) -> dict[str, Any]:
    return {
        "id": hit_id,
        "text": f"Recovery hit {hit_id}",
        "link": f"https://example.test/{hit_id}",
        "source": "Reuters",
        "newsType": "news",
        "engineType": "news",
        "ts": published_at_ms,
        "coins": [],
        "strategy": {"id": 1018, "name": "News", "sourceType": "news"},
    }


class _HistoryClient:
    def __init__(
        self,
        *,
        hits: list[dict[str, Any]] | None = None,
        strategy_error: Exception | None = None,
        hits_error: Exception | None = None,
        hits_error_page: int | None = None,
        hits_delay: float = 0.0,
        total: int | None = None,
    ) -> None:
        self.hits = hits or []
        self.strategy_error = strategy_error
        self.hits_error = hits_error
        self.hits_error_page = hits_error_page
        self.hits_delay = hits_delay
        self.total = len(self.hits) if total is None else total
        self.hits_calls = 0

    async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
        if self.strategy_error is not None:
            raise self.strategy_error
        return {"success": True, "data": [{"id": 1018, "name": "News", "enabled": True}]}

    async def get_strategy_hits(self, *, page: int, **_kwargs: Any) -> dict[str, Any]:
        self.hits_calls += 1
        if self.hits_delay:
            await asyncio.sleep(self.hits_delay)
        if self.hits_error is not None and (self.hits_error_page is None or page == self.hits_error_page):
            raise self.hits_error
        return {
            "success": True,
            "data": self.hits if page == 1 else [],
            "page": page,
            "limit": 100,
            "total": self.total,
        }


def test_recovery_typed_provider_failure_stays_pending_and_unknown_surfaces() -> None:
    incident = _pending_incident()

    for error in (OpenNewsHistoryError("opennews_history_rate_limited"), RuntimeError("bug")):
        news = RecordingNews(pending_recovery_incidents=[incident])
        client = _HistoryClient(strategy_error=error)
        recovery = RecoveryRunner(bus=FakeBus(), db=FakeWorkerDatabase(news), history_client=client)

        with pytest.raises(type(error), match=str(error)):
            asyncio.run(recovery._recover_pending())

        assert "complete_recovery" not in news.names()
        if isinstance(error, OpenNewsHistoryError):
            assert news.kwargs_of("record_recovery_error")["error_code"] == error.code
        else:
            assert "record_recovery_error" not in news.names()


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (BrokerUnavailable("news_broker_not_connected"), "news_broker_unavailable"),
        (BrokerBackpressure("news_broker_publish_rejected"), "news_broker_backpressure"),
    ],
)
def test_recovery_broker_failure_stays_pending_then_resumes(error: Exception, error_code: str) -> None:
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    bus = _FailingOnceBus(error)
    recovery = RecoveryRunner(
        bus=bus,
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(hits=[_history_hit(1, 999_999_970_000)]),
    )

    with pytest.raises(type(error), match=str(error)):
        asyncio.run(recovery._recover_pending())
    assert "complete_recovery" not in news.names()
    assert news.kwargs_of("record_recovery_error")["error_code"] == error_code

    assert asyncio.run(recovery._recover_pending()) == "success"
    assert news.kwargs_of("complete_recovery")["status"] == "recovered"
    assert [message.message_id for message in bus.published] == ["raw:1"]


def test_recovery_explicit_no_history_after_progress_is_partial_not_unavailable() -> None:
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    recovery = RecoveryRunner(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(
            hits=[_history_hit(index, 1_500_000_000_000) for index in range(1, 101)],
            hits_error=OpenNewsHistoryError("opennews_history_unavailable"),
            hits_error_page=2,
            total=101,
        ),
    )

    assert asyncio.run(recovery._recover_pending()) == "partial"
    completed = news.kwargs_of("complete_recovery")
    assert completed["status"] == "partial"
    assert completed["recovered_count"] == 100
    assert completed["error_code"] == "opennews_history_unavailable"


def test_recovery_requires_the_production_history_client() -> None:
    with pytest.raises(ValueError, match="opennews_history_client_required"):
        RecoveryRunner(bus=FakeBus(), db=FakeWorkerDatabase(RecordingNews()), history_client=None)


def test_recovery_message_budget_resumes_without_replaying_the_page_prefix() -> None:
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    bus = FakeBus()
    client = _HistoryClient(hits=[_history_hit(1, 1_500_000_000_000), _history_hit(2, 999_999_970_000)])
    recovery = RecoveryRunner(
        bus=bus,
        db=FakeWorkerDatabase(news),
        history_client=client,
        max_published_messages=1,
    )

    with pytest.raises(RuntimeError, match="opennews_recovery_published_messages_budget"):
        asyncio.run(recovery._recover_pending())
    assert [message.message_id for message in bus.published] == ["raw:1"]
    assert news.kwargs_of("record_recovery_error")["error_code"] == "opennews_recovery_published_messages_budget"
    assert "complete_recovery" not in news.names()

    assert asyncio.run(recovery._recover_pending()) == "success"
    assert [message.message_id for message in bus.published] == ["raw:1", "raw:2"]
    completed = news.kwargs_of("complete_recovery")
    assert completed["status"] == "recovered" and completed["recovered_count"] == 2
    assert all(message.routing_key == RK_RAW_RECOVERY.format(strategy_id="1018") for message in bus.published)


def test_recovery_indexes_raw_params_by_the_normalized_provider_id() -> None:
    hit = _history_hit(42, 999_999_970_000)
    hit["id"] = " 42 "
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    bus = FakeBus()
    recovery = RecoveryRunner(
        bus=bus,
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(hits=[hit]),
    )

    assert asyncio.run(recovery._recover_pending()) == "success"
    assert [message.message_id for message in bus.published] == ["raw:42"]


def test_recovery_provider_call_and_wall_budgets_leave_incident_pending() -> None:
    cases = (
        (
            _HistoryClient(
                hits=[_history_hit(index, 1_500_000_000_000) for index in range(1, 101)],
                total=200,
            ),
            {"max_provider_calls": 2},
            "provider_calls",
        ),
        (_HistoryClient(hits_delay=0.05), {"max_wall_seconds": 0.005}, "wall_time"),
    )
    for client, kwargs, budget_name in cases:
        news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
        recovery = RecoveryRunner(
            bus=FakeBus(),
            db=FakeWorkerDatabase(news),
            history_client=client,
            **kwargs,
        )
        with pytest.raises(RuntimeError, match=f"opennews_recovery_{budget_name}_budget"):
            asyncio.run(recovery._recover_pending())
        assert "complete_recovery" not in news.names()
        if budget_name == "wall_time":
            assert "record_recovery_error" not in news.names()
        else:
            assert news.kwargs_of("record_recovery_error")["error_code"] == (f"opennews_recovery_{budget_name}_budget")


def test_recovery_wall_budget_checkpoints_an_out_of_window_cpu_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 0.0}

    def _tick() -> float:
        clock["now"] += 0.01
        return clock["now"]

    monkeypatch.setattr(recovery_module, "time", SimpleNamespace(perf_counter=_tick))
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    recovery = RecoveryRunner(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(hits=[_history_hit(index, 999_999_000_000) for index in range(100)]),
        max_wall_seconds=0.2,
    )

    with pytest.raises(RuntimeError, match="opennews_recovery_wall_time_budget"):
        asyncio.run(recovery._recover_pending())

    assert "complete_recovery" not in news.names()


@pytest.mark.parametrize(
    "error_type",
    [DeferError, TransientError],
)
def test_recovery_persists_a_known_incident_db_error_after_the_database_recovers(error_type: type[Exception]) -> None:
    class _RecoveryDatabase(FakeWorkerDatabase):
        fail_complete = True
        fail_error_record = True

        async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
            if name == "news_recovery_complete" and self.fail_complete:
                raise error_type(f"db_failure:{name}")
            if name == "news_recovery_error" and self.fail_error_record:
                raise error_type(f"db_failure:{name}")
            return await super().tx(name, fn, timeout_seconds=timeout_seconds)

    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    db = _RecoveryDatabase(news)
    recovery = RecoveryRunner(bus=FakeBus(), db=db, history_client=_HistoryClient())

    with pytest.raises(error_type, match="db_failure:news_recovery_complete"):
        asyncio.run(recovery._recover_pending())
    assert "record_recovery_error" not in news.names()

    db.fail_error_record = False
    with pytest.raises(DeferError, match="news_recovery_database_error_recorded"):
        asyncio.run(recovery._recover_pending())
    assert news.kwargs_of("record_recovery_error")["error_code"] == "news_recovery_database_transient"

    db.fail_complete = False
    assert asyncio.run(recovery._recover_pending()) == "success"
    assert news.kwargs_of("complete_recovery")["status"] == "recovered"


def test_recovery_empty_strategy_list_stays_pending() -> None:
    class _NoStrategiesHistory:
        async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
            return {"success": True, "data": []}

        async def get_strategy_hits(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("no enabled strategy may fetch hits")

        async def close(self) -> None:
            pass

    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    recovery = RecoveryRunner(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        history_client=_NoStrategiesHistory(),
    )

    with pytest.raises(OpenNewsHistoryError, match="opennews_history_strategy_list_empty"):
        asyncio.run(recovery._recover_pending())

    assert "complete_recovery" not in news.names()
    assert news.kwargs_of("record_recovery_error")["error_code"] == "opennews_history_strategy_list_empty"


def test_recovery_wall_budget_stops_no_history_terminal_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(recovery_module, "time", SimpleNamespace(perf_counter=lambda: clock["now"]))

    class _NoHistory:
        async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
            raise OpenNewsHistoryError("opennews_history_unavailable")

        async def get_strategy_hits(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("explicit list failure may not fetch hits")

        async def close(self) -> None:
            pass

    def _complete(**_kwargs: Any) -> bool:
        clock["now"] = 1.0
        return True

    news = RecordingNews(
        pending_recovery_incidents=[_pending_incident(1), _pending_incident(2)],
        complete_recovery=_complete,
    )
    bus = FakeBus()
    recovery = RecoveryRunner(
        bus=bus,
        db=FakeWorkerDatabase(news),
        history_client=_NoHistory(),
        max_wall_seconds=0.5,
    )

    with pytest.raises(RuntimeError, match="opennews_recovery_wall_time_budget"):
        asyncio.run(recovery._recover_pending())

    completed = [kwargs for name, kwargs in news.calls if name == "complete_recovery"]
    assert len(completed) == 1 and completed[0]["status"] == "unavailable"
    assert bus.published == []


def test_recovery_wall_budget_cancels_a_stalled_broker_publish() -> None:
    news = RecordingNews(pending_recovery_incidents=[_pending_incident()])
    recovery = RecoveryRunner(
        bus=_BlockedPublishBus(),
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(hits=[_history_hit(1, 1_500_000_000_000)]),
        max_wall_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="opennews_recovery_wall_time_budget"):
        asyncio.run(asyncio.wait_for(recovery._recover_pending(), timeout=0.2))

    assert "complete_recovery" not in news.names()


def test_recovery_periodically_finds_pending_work_without_a_request() -> None:
    pending: list[dict[str, Any]] = []
    news = RecordingNews(pending_recovery_incidents=lambda: list(pending))
    recovery = RecoveryRunner(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        history_client=_HistoryClient(),
        scan_interval_seconds=0.005,
    )
    stop = asyncio.Event()

    async def scenario() -> None:
        task = asyncio.create_task(recovery.run(stop_event=stop))
        for _ in range(100):
            if news.names().count("pending_recovery_incidents") >= 1:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("startup recovery scan did not run")
        pending.append(_pending_incident())
        for _ in range(100):
            if "complete_recovery" in news.names():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("periodic recovery scan did not run")
        stop.set()
        await task

    asyncio.run(scenario())
    assert news.kwargs_of("complete_recovery")["status"] == "recovered"


# ---------------------------------------------------------------------------- market lane (#553)
_OI_TITLE = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
_LIQUIDATION_TITLE = "SOL Large Short Liquidation 202.71K at $137.01"
_WALLET_TITLE = "js-2 Close Short SOL $482,113.55 , Price $137.01 , PNL -$8,204.10"


def _market_raw(
    *,
    strategy_id: int,
    strategy_name: str,
    source_type: str,
    text: str,
    record_id: int = 900_001,
    ingest_mode: str = "live",
    provider_source: str = "binance",
    extra_params: dict[str, Any] | None = None,
) -> BusMessage:
    params: dict[str, Any] = {
        "id": record_id,
        "engineType": "market",
        "text": text,
        "source": provider_source,
        "ts": NOW_MS,
        "strategy": {"id": strategy_id, "name": strategy_name, "sourceType": source_type},
    }
    params.update(extra_params or {})
    return _message(
        "raw",
        {
            "params": params,
            "strategy_id": str(strategy_id),
            "ingest_mode": ingest_mode,
            "observed_at_ms": NOW_MS,
        },
        routing_key=RK_RAW_LIVE.format(strategy_id=str(strategy_id)),
    )


def _market_deduper(news: RecordingNews, bus: FakeBus) -> DeduperConsumer:
    return DeduperConsumer(bus=bus, db=FakeWorkerDatabase(news), watchlist_symbols=frozenset({"SOL", "TRUMP"}))


def _admit_market(raw: BusMessage) -> tuple[RecordingNews, FakeBus]:
    news = RecordingNews(upsert_item=True)
    bus = FakeBus()
    asyncio.run(_market_deduper(news, bus).handle(raw))
    return news, bus


def test_an_oi_frame_is_stored_with_its_typed_fact_and_opens_no_event() -> None:
    """The whole market design in one test: Item and ledger row in one transaction, nothing else.

    No Gate, no title dedupe, no storyline, no evidence snapshot, no verdict and no Event -- and so
    nothing published, because there is no Triage message for a measurement to carry.
    """

    news, bus = _admit_market(
        _market_raw(strategy_id=1019, strategy_name="OI Event Monitor", source_type="market", text=_OI_TITLE)
    )

    item = news.kwargs_of("upsert_item")
    assert (item["market_kind"], item["market_parse_status"], item["market_parse_error"]) == ("oi", "parsed", None)
    assert item["market_source_strategy_id"] == "1019"
    signal = news.kwargs_of("insert_oi_signal")
    assert (signal["symbol"], signal["direction"], signal["oi_change_bps"]) == ("TRUMP", "rise", 455)
    assert signal["source_venue"] == "binance"
    assert signal["measurement_definition"] == "oi_signal_v1|opennews_oi_source_v1|300000"
    # No live writer can mark a fact as reconstructed: the column defaults to false and only the
    # migration ever sets it.
    assert "historical" not in signal
    for editorial in ("insert_event", "add_member", "append_evidence_snapshot", "find_band_candidates"):
        assert editorial not in news.names(), editorial
    assert bus.published == []


def test_a_recovery_market_frame_is_stored_exactly_like_a_live_one() -> None:
    """#553. Recovery frames never reached Triage, so the ledger simply had no row for them."""

    news, bus = _admit_market(
        _market_raw(
            strategy_id=1019,
            strategy_name="OI Event Monitor",
            source_type="market",
            text=_OI_TITLE,
            ingest_mode="recovery",
        )
    )

    assert news.kwargs_of("upsert_item")["ingest_mode"] == "recovery"
    assert news.kwargs_of("insert_oi_signal")["symbol"] == "TRUMP"
    assert bus.published == []


@pytest.mark.parametrize("strategy_id", [2000, 2083])
def test_both_liquidation_strategies_write_the_typed_fact_with_their_own_source_id(strategy_id: int) -> None:
    """2083 is where every measured liquidation actually came from, and it was `unsupported` (#553)."""

    news, _ = _admit_market(
        _market_raw(
            strategy_id=strategy_id,
            strategy_name="renamed by the provider",
            source_type="market",
            text=_LIQUIDATION_TITLE,
            provider_source="okx",
        )
    )

    assert news.kwargs_of("upsert_item")["market_kind"] == "liquidation"
    fact = news.kwargs_of("insert_market_liquidation")["fact"]
    assert (fact.symbol, fact.source_venue, fact.source_strategy_id) == ("SOL", "okx", str(strategy_id))
    assert fact.liquidated_position_side == "short"


def test_a_wallet_report_is_parsed_into_an_account_action_with_its_address_and_metrics_kept() -> None:
    news, _ = _admit_market(
        _market_raw(
            strategy_id=2026,
            strategy_name="聪明钱监控",
            source_type="wallet",
            text=_WALLET_TITLE,
            provider_source="",
            extra_params={
                "relatedAddress": "0x" + "1" * 40,
                "strategy": {
                    "id": 2026,
                    "name": "聪明钱监控",
                    "sourceType": "wallet",
                    "metrics": {"position_value": {"value": 482113.55, "unit": "USD"}},
                },
            },
        )
    )

    item = news.kwargs_of("upsert_item")
    assert (item["market_kind"], item["market_parse_status"]) == ("smart_money", "parsed")
    payload = json.loads(item["provider_params_json"])
    assert payload["relatedAddress"] == "0x" + "1" * 40
    assert payload["strategy"]["metrics"]["position_value"]["value"] == 482113.55
    fact = news.kwargs_of("insert_market_smart_money")["fact"]
    assert (fact.trader_label, fact.action, fact.position_side, fact.symbol) == ("js-2", "close", "short", "SOL")
    assert fact.account_address == "0x" + "1" * 40
    assert str(fact.pnl_usd) == "-8204.10"
    assert fact.source_venue is None


@pytest.mark.parametrize(
    ("strategy_id", "source_type", "text", "kind", "reason"),
    [
        (1019, "market", "TRUMP open interest is up a lot today", "oi", "oi_template_unmatched"),
        (2083, "market", "SOL liquidated somewhere", "liquidation", "liquidation_template_unmatched"),
        (2026, "wallet", "Withdraw USDC", "smart_money", "smart_money_template_unmatched"),
        (9999, "wallet", "Some new market monitor said something", "unknown_market", "unknown_market_source"),
    ],
)
def test_a_template_this_code_cannot_prove_is_stored_as_a_raw_card_with_its_reason(
    strategy_id: int,
    source_type: str,
    text: str,
    kind: str,
    reason: str,
) -> None:
    """`Withdraw USDC` is real account activity. Refusing it would delete a fact to protect a parser."""

    news, bus = _admit_market(
        _market_raw(strategy_id=strategy_id, strategy_name="whatever", source_type=source_type, text=text)
    )

    item = news.kwargs_of("upsert_item")
    assert (item["market_kind"], item["market_parse_status"], item["market_parse_error"]) == (kind, "raw", reason)
    for writer in ("insert_oi_signal", "insert_market_liquidation", "insert_market_smart_money"):
        assert writer not in news.names(), writer
    assert "insert_event" not in news.names()
    assert bus.published == []


def test_an_ordinary_news_frame_still_opens_an_event_and_never_reaches_the_market_writers() -> None:
    news = RecordingNews(
        upsert_item=True,
        fact_membership=None,
        find_exact_event=None,
        find_artifact_event=None,
        find_band_candidates=[],
    )
    bus = FakeBus()
    raw = _message(
        "raw",
        {
            "params": {
                "id": 900_100,
                "engineType": "news",
                "score": 90,
                "text": "SEC approves a spot ETF for SOL",
                "source": "opennews",
                "ts": NOW_MS,
                "strategy": {"id": 1018, "name": "News Score > 70", "sourceType": "news"},
            },
            "strategy_id": "1018",
            "ingest_mode": "live",
            "observed_at_ms": NOW_MS,
        },
        routing_key=RK_RAW_LIVE.format(strategy_id="1018"),
    )

    asyncio.run(_market_deduper(news, bus).handle(raw))

    # The editorial branch never names a market column, so the Item's market identity stays absent.
    assert news.kwargs_of("upsert_item").get("market_kind") is None
    assert news.kwargs_of("insert_event")["event_kind"] == "news"
    for writer in ("insert_oi_signal", "insert_market_liquidation", "insert_market_smart_money"):
        assert writer not in news.names(), writer


def test_a_progression_review_that_has_not_answered_cannot_hold_the_first_delivery() -> None:
    """#651 §6.3: novelty decides before the send, and `ProgressionVerifier` decides nothing about it.

    The restatement guard now drops a repeat whichever way the model read its direction, which makes
    novelty a pre-delivery decision in full. The one model check that runs *after* the send has to stay
    there: this verifier is entered and never answers, and the receipt is written anyway. What an
    unanswered review may cost is a badge that stays `pending`; what it may never cost is the card.
    """

    async def scenario() -> tuple[RecordingNews, RecordingEditableSender, list[str]]:
        order: list[str] = []
        news = _delivery_news(
            event_card=_card(grounded_assets=[]),
            latest_verdict=lambda **_kwargs: {
                "final_decision": "push",
                "verdict": {
                    "direction": "bearish",
                    "fact_kind": "state_change",
                    "scope": "single_name",
                    "novelty": "progression",
                    "headline_zh": "美光台湾工会初步投票支持罢工比例达 80%",
                    "why_zh": "工会行动从劳资协商进入有明确门槛的罢工程序。",
                    "assets": [],
                },
                "trace": {
                    "told": [
                        {
                            "i": 0,
                            "event_id": "ev-parent",
                            "tier": "storyline",
                            "similarity": 0.31,
                            "headline_zh": "美光工会此前启动劳资协商",
                            "symbols": ["MU"],
                            "at_ms": NOW_MS - 150_000,
                        }
                    ]
                },
            },
            delivery=lambda *, event_id, kind: (
                {
                    "event_id": event_id,
                    "kind": kind,
                    "state": "sent",
                    "delete_state": None,
                    "receipt": {
                        "provider": "telegram",
                        "message_id": 41,
                        "pushed_at_ms": NOW_MS - 150_000,
                        "target_sha256": "a" * 64,
                    },
                }
                if event_id == "ev-parent" and kind == "first"
                else None
            ),
        )
        sender = RecordingEditableSender(order)
        verifier = BlockingProgressionVerifier(order)
        consumer = _deliverer(news, sender=sender, progression_verifier=verifier)

        await asyncio.wait_for(consumer.deliver(event_id="ev-strong"), timeout=0.2)
        # The receipt exists before the review is even entered, and it does not change afterwards.
        assert news.kwargs_of("settle_delivery")["state"] == "sent"
        await asyncio.wait_for(verifier.started.wait(), timeout=0.2)
        assert order == ["prepare", "send", "review"]
        assert news.kwargs_of("settle_delivery")["state"] == "sent"
        assert sender.presentations[0].progression_review_state == "pending"
        assert sender.edited_presentations == []
        # Released only so the loop can be closed; the assertions above all hold while it is blocked.
        verifier.release.set()
        await consumer.close()
        return news, sender, order

    news, _sender, order = asyncio.run(scenario())

    assert order.index("send") < order.index("review")
    assert news.kwargs_of("settle_delivery")["state"] == "sent"
