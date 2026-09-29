"""News public outbox facts enter Trading's compact frozen input without private fields."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from tests.trading.news_public_updates import first_report, next_update
from tracefold.app.trading_intake import catalyst_assets, public_update
from tracefold.news.storage.trade_projection import TradeProjectionStorage
from tracefold.trading.engine.case_view import BaseRates, build_case_view
from tracefold.trading.engine.paper import LegGeometry
from tracefold.trading.engine.target import SourceAsset


class _Outbox:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> _Outbox:
        assert "INSERT INTO news_trade_events" in sql
        kind, key, revision, digest, serialized, recorded = params
        self.rows.append(
            {
                "kind": kind,
                "source_fact_key": key,
                "source_revision": revision,
                "payload_sha256": digest,
                "payload": json.loads(serialized),
                "source_recorded_at_ms": recorded,
            }
        )
        return self

    def fetchone(self) -> dict[str, str]:
        return {"payload_sha256": self.rows[-1]["payload_sha256"]}


def _enqueue(update: Any) -> dict[str, Any]:
    storage = TradeProjectionStorage()
    storage.conn = _Outbox()
    assert storage.enqueue_trade_event(
        kind="catalyst" if update.kind == "catalyst_delta" else "source_update",
        source_fact_key=update.event_id,
        source_revision=update.content_revision,
        payload=update.model_dump(mode="json"),
        source_recorded_at_ms=update.semantic_completed_at_ms,
    )
    return storage.conn.rows[0]


def test_public_catalyst_and_amendment_become_compact_pit_context() -> None:
    head, catalyst = first_report(event_id="event-sol", first_available_at_ms=920_000, completed_at_ms=925_000)
    _, correction = next_update(
        head,
        "Correction: the SOL swap fee was set to 20 bps, not 25 bps.",
        previous_ref=head.claims[0].ref,
        relation="corrects",
        change_kind="correction",
        quantity="20",
        revision=2,
        first_available_at_ms=926_000,
        completed_at_ms=927_000,
    )
    source = _enqueue(catalyst)
    amendment = _enqueue(correction)
    assert public_update(source) == catalyst
    assert public_update(amendment) == correction
    assert catalyst_assets(catalyst) == (SourceAsset("SOL", "crypto", "primary"),)
    assert not {"headline", "why", "headline_zh", "why_zh"}.intersection(source["payload"])
    context = (
        {
            "kind": "catalyst",
            "payload": source["payload"],
            "amendments": [{"payload": amendment["payload"]}],
        },
    )
    empty = BaseRates("long", 0, None), BaseRates("short", 0, None)
    view = build_case_view(
        case_id="b" * 64,
        asset_id="crypto:SOL",
        trigger_kind="catalyst",
        decided_at_ms=1_000_000,
        source_fact=source["payload"],
        features={"perp_return_15m_bps": "12"},
        geometry=LegGeometry(200, 400),
        half_spread_bps=Decimal("2"),
        base_rates=empty,
        recent_context=context,
    )
    prompt = view.prompt_json()
    assert "SOL protocol sets the swap fee to 25 bps" in prompt
    assert "[digest]" in prompt
    assert "correction" in prompt.lower()
    assert '"mode":"decision"' in prompt
    assert '"phase":"announced"' in prompt
    assert '"content_kind":"official_measure"' in prompt
    assert "headline" not in prompt
    assert "event-sol" not in prompt
    assert "1000000" not in prompt


@pytest.mark.parametrize(
    "legacy",
    [
        {"kind": "catalyst", "headline": "Old headline", "why": "Old why"},
        {"kind": "catalyst", "text": "", "oi_value_usd": 1000},
    ],
)
def test_retired_catalyst_payload_is_rejected(legacy: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        public_update({"kind": "catalyst", "source_fact_key": "old", "source_revision": "v1", "payload": legacy})
