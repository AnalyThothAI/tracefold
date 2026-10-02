"""Timely recovery uses the live Gate; frame accounting failures do not reconnect."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.news.test_news_v3_consumers import FakeBus, FakeWorkerDatabase, RecordingNews
from tracefold.news.bus import DeferError, TransientError
from tracefold.news.events.gate import RECOVERY_LIVE_MAX_AGE_MS, GateInput, evaluate_gate
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.storage.semantic_input import item_evidence


@pytest.mark.parametrize("engine", ["news", "listing"])
@pytest.mark.parametrize("age", [18_000, RECOVERY_LIVE_MAX_AGE_MS, RECOVERY_LIVE_MAX_AGE_MS + 1, None, -1])
def test_recovery_gate_fails_closed_only_outside_the_live_window(engine: Any, age: int | None) -> None:
    gate = evaluate_gate(GateInput("Exchange lists a new token", engine, 90, (), "recovery", source_age_ms=age))
    history = age is None or age > RECOVERY_LIVE_MAX_AGE_MS
    assert gate.admission == (
        "recovery" if history else "listing_deterministic" if engine == "listing" else "candidate"
    )
    assert gate.reasons == (("recovered_after_live_window",) if history else ())


@pytest.mark.parametrize("age", [None, RECOVERY_LIVE_MAX_AGE_MS + 1, 30 * 86_400_000])
def test_live_gate_never_applies_the_recovery_age_limit(age: int | None) -> None:
    assert evaluate_gate(GateInput("News", "news", None, (), "live", source_age_ms=age)).admission == "candidate"


@pytest.mark.parametrize(
    "error", [DeferError("db_admission_timeout:news_ingest_frame"), TransientError("db_overrun:news_ingest_frame")]
)
def test_published_frame_accounting_retries_on_the_next_frame(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0

    def record(**_kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return 1

    requested = []
    news = RecordingNews(record_published_frame=record)
    bus = FakeBus()

    class Recovery:
        def request(self) -> None:
            requested.append(True)

    receiver = OpenNewsReceiver(bus=bus, db=FakeWorkerDatabase(news), ws_client=None, recovery=Recovery())
    receiver._broker_incident_open = True

    async def scenario() -> None:
        await receiver._publish_frame({"params": {"id": 1}}, strategy_id="1018")
        assert receiver._broker_incident_open
        assert receiver._last_recorded_frame_ms is None
        await receiver._publish_frame({"params": {"id": 2}}, strategy_id="1018")

    asyncio.run(scenario())
    assert len(bus.published) == attempts == 2
    assert not receiver._broker_incident_open
    assert requested == [True]
    assert "news_ingest_frame_deferred" in caplog.text


@pytest.mark.parametrize("mode", ["live", "recovery"])
@pytest.mark.parametrize("published", [None, 1_800_000_000_000 - 18_000, 1_800_000_000_000 + 1])
def test_recovered_source_first_available_clock(mode: str, published: int | None) -> None:
    observed = 1_800_000_000_000
    evidence = item_evidence(
        {
            "item_id": "item",
            "source_item_key": "record",
            "source_id": "opennews",
            "evidence_text": "Agency announces new tariffs.",
            "observed_at_ms": observed,
            "published_at_ms": published,
            "first_ingest_mode": mode,
        }
    )
    assert evidence is not None
    expected = min(observed, published) if mode == "recovery" and published is not None else observed
    assert evidence.source.first_available_at_ms == expected
