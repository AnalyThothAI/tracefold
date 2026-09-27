"""Real News producer -> outbox -> App mapping -> Trading reader/compiler; only storage and market I/O are faked."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from tests.trading.news_public_updates import first_report, next_update
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analysis import FrameReader, LegacyCatalystPayload, catalyst_assets, public_update
from tracefold.news.storage.trade_projection import TradeProjectionStorage
from tracefold.news.updates.contracts import PublicUpdate
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
from tracefold.trading.engine.plans import AnalysisProposal, compile_proposal
from tracefold.trading.engine.target import SourceAsset


class _Market:
    def __init__(self, close: str) -> None:
        self.close = close

    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        rows: tuple[dict[str, Any], ...] = ()
        if request.dataset == "instrument_rules":
            rows = ({"native_symbol": request.native_symbol, "trading_status": "TRADING", "event_at_ms": 960_000},)
        if request.dataset == "perp_bars":
            assert request.start_ms is not None and request.end_ms == 960_000
            rows = tuple(
                {
                    "event_at_ms": (index + 1) * 60_000,
                    "received_at_ms": 1_000_000,
                    "high": "103" if index == 15 else "101",
                    "low": "97" if index == 15 else "99",
                    "close": self.close if index == 15 else "100",
                    "quote_volume": "1000",
                    "taker_buy_quote_volume": "500",
                }
                for index in range(16)
            )
        return MarketDataResult(
            status="ok" if rows else "missing",
            payload=rows,
            schema_version="fixture_v1",
            source_version="fixture_v1",
            unit_definition=request.unit_definition,
            source_identity=request.source_identity,
            event_start_ms=rows[0]["event_at_ms"] if rows else None,
            event_end_ms=rows[-1]["event_at_ms"] if rows else None,
            received_at_ms=1_000_000 if rows else None,
            missing_reasons=() if rows else ("fixture_optional_missing",),
            request_receipts=(),
        )


class _Outbox:
    """The News outbox connection: captures the exact frozen row the producer's SQL writes."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        assert "INSERT INTO news_trade_events" in sql
        kind, key, revision, digest, serialized, recorded = params
        # PostgreSQL returns the jsonb column as a decoded object, never the producer's string.
        self.rows.append(
            {
                "event_id": len(self.rows) + 1,
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


def _enqueue(update: PublicUpdate) -> dict[str, Any]:
    """The News outbox mapping of one adopted public update, through the real outbox writer."""

    storage = TradeProjectionStorage()
    storage.conn = _Outbox()
    assert storage.enqueue_trade_event(
        kind="catalyst" if update.kind == "catalyst_delta" else "source_update",
        source_fact_key=update.event_id,
        source_revision=update.content_revision,
        payload=update.model_dump(mode="json"),
        source_recorded_at_ms=update.semantic_completed_at_ms,
    )
    (row,) = storage.conn.rows
    return dict(row)


def _prepare(source, close, tmp_path, monkeypatch, *, amendments: tuple[dict[str, Any], ...] = ()):
    monkeypatch.setattr("tracefold.app.trading_analysis._clock_ms", lambda: 1_000_000)
    files = AnalysisFiles(tmp_path)

    async def amendments_at(cutoff_ms: int) -> tuple[dict[str, Any], ...]:
        assert cutoff_ms == 1_000_000
        return amendments

    prepared = asyncio.run(
        FrameReader(_Market(close), files).prepare(
            case={
                "case_id": "b" * 64,
                "trigger_id": "c" * 64,
                "root_expires_at_ms": 1_530_000,
                "created_at_ms": 940_000,
                "target_selection": {
                    "reason": "selected",
                    "asset_id": "crypto:SOL",
                    "instrument": {
                        "native_symbol": "SOLUSDT",
                        "environment": "live",
                        "mapping_semantics_digest": "a" * 64,
                    },
                },
            },
            source_fact=source,
            source_first_visible_at_ms=930_000,
            source_amendments_at=amendments_at,
        )
    )
    snapshot = files.read(prepared.evidence_ref)
    assert snapshot["source_fact"] == source
    assert json.loads(files.read(prepared.brief_ref)["brief_json"])["source_fact"] == source
    assert snapshot["source_amendments"] == list(amendments)
    return prepared


def _produced_catalyst() -> dict[str, Any]:
    """Exercise the actual News assembly, projection and outbox writer, not a hand-authored Trading fixture."""

    _, catalyst = first_report(event_id="event-sol", first_available_at_ms=920_000, completed_at_ms=925_000)
    row = _enqueue(catalyst)
    assert row["kind"] == "catalyst"
    assert public_update(row) == catalyst
    assert catalyst_assets(public_update(row)) == (SourceAsset("SOL", "crypto", "primary"),)
    payload = row["payload"]
    assert payload["schema_version"] == "news_public_update_v1" and payload["kind"] == "catalyst_delta"
    assert not {"headline", "why", "headline_zh", "why_zh", "title"}.intersection(payload)
    return dict(payload)


@pytest.mark.parametrize("close,side", [("102", "long"), ("98", "short"), ("100", None)])
def test_news_public_catalyst_reaches_citable_evidence_and_final_decision(close, side, tmp_path, monkeypatch) -> None:
    source = _produced_catalyst()
    prepared = _prepare(source, close, tmp_path, monkeypatch)
    evidence = prepared.brief.evidence_catalog["source"]
    assert evidence["status"] == "ok"
    assert evidence["values"] == {"text": source["text"]}
    assert evidence["unit_definition"] == {"text": "text"}
    # Recorded when the semantic fact completed, not when Trading relayed it.
    assert evidence["event_at_ms"] == 925_000
    assert "SOL protocol sets the swap fee to 25 bps." in source["text"]
    assert "asset=SOL; market=crypto; role=primary" in source["text"]
    assert "source" in json.loads(prepared.brief.text)["citable_evidence_ids"]
    selected = next(
        item
        for item in prepared.plans
        if item.side == (side or "long")
        and item.kind == ("closed_bar_cross_v1" if side is None else "immediate_entry_v1")
    )
    assessment = AnalysisProposal(
        selected_plan_id=selected.plan_id,
        supporting_evidence=("source", "market:perp_bars"),
        public_rationale="Recorded source and code-owned price condition.",
    )
    decision = compile_proposal(
        proposal=assessment,
        plans=prepared.plans,
        evidence_catalog=prepared.brief.evidence_catalog,
        judgment_refs=frozenset(),
        now_ms=1_000_000,
    )
    assert decision.action == ("WATCH" if side is None else "TRADE")
    assert decision.side == (side or "long")
    assert (decision.watch_condition is not None) == (side is None)


def test_recorded_correction_is_visible_to_analysis_without_changing_the_plan_menu(tmp_path, monkeypatch) -> None:
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
    row = _enqueue(correction)
    assert row["kind"] == "source_update" and public_update(row) == correction
    amendment = {
        "update_id": correction.update_id,
        "content_revision": correction.content_revision,
        "affected_claim_refs": list(correction.affected_claim_refs),
        "retired_claim_refs": list(correction.retired_claim_refs),
        "payload": row["payload"],
        "received_at_ms": 999_000,
    }
    source = _produced_catalyst()
    plain = _prepare(source, "102", tmp_path / "plain", monkeypatch)
    amended = _prepare(source, "102", tmp_path / "amended", monkeypatch, amendments=(amendment,))
    brief = json.loads(amended.brief.text)
    assert brief["source_amendments"] == [amendment]
    assert brief["source_amendments"][0]["retired_claim_refs"] == list(catalyst.claim_refs)
    # Visibility only: the same citable source and the same plans. The refusal is the last entry check.
    assert [plan.plan_id for plan in amended.plans] == [plan.plan_id for plan in plain.plans]
    assert amended.brief.evidence_catalog["source"] == {
        **plain.brief.evidence_catalog["source"],
        "source_ref": amended.brief.evidence_catalog["source"]["source_ref"],
    }


@pytest.mark.parametrize(
    "text",
    [
        {"text": " \t\n"},
        {"text": ""},
        {"text": 123},
        {"text": None, "headline": "A retired alias cannot rescue the public text"},
    ],
)
def test_blank_public_text_is_missing_in_both_evidence_and_candidates(text, tmp_path, monkeypatch) -> None:
    source = {**_produced_catalyst(), **text}
    before = deepcopy(source)
    prepared = _prepare(source, "100", tmp_path, monkeypatch)
    evidence = prepared.brief.evidence_catalog["source"]
    assert evidence["status"] == "missing" and evidence["values"] == {}
    assert "source" not in json.loads(prepared.brief.text)["citable_evidence_ids"]
    assert prepared.plans == ()
    assert source == before


@pytest.mark.parametrize(
    "legacy",
    [
        {"kind": "catalyst", "headline": "Old headline", "why": "Old why", "source_recorded_at_ms": 930_000},
        {"kind": "catalyst", "headline": "", "why": None, "oi_value_usd": 1000},
    ],
)
def test_retired_headline_why_catalyst_has_no_reading_path(legacy, tmp_path, monkeypatch) -> None:
    row = {"kind": "catalyst", "source_fact_key": "event-old", "source_revision": "1:" + "e" * 64, "payload": legacy}
    with pytest.raises(LegacyCatalystPayload, match="legacy_catalyst_payload"):
        public_update(row)
    prepared = _prepare(legacy, "102", tmp_path, monkeypatch)
    assert prepared.brief.evidence_catalog["source"]["status"] == "missing"
    assert prepared.plans == ()


def test_payload_must_name_its_outbox_identity() -> None:
    _, catalyst = first_report(event_id="event-sol", first_available_at_ms=920_000, completed_at_ms=925_000)
    row = _enqueue(catalyst)
    for field, value in (("source_fact_key", "another-event"), ("source_revision", "another-revision")):
        with pytest.raises(ValueError, match="trade_event_public_identity_mismatch"):
            public_update({**row, field: value})
    with pytest.raises(ValueError, match="trade_event_public_identity_mismatch"):
        public_update({**row, "kind": "source_update"})
    with pytest.raises(ValueError):
        public_update({**row, "payload": {**row["payload"], "text": "rewritten", "update_id": "public:forged"}})


def test_oi_zero_values_remain_citable_without_optional_market_frames(tmp_path, monkeypatch) -> None:
    values = {
        "oi_change_bps": 0,
        "oi_value_usd": 0,
        "measurement_definition": "exchange-oi-v1",
        "measurement_window_ms": 60_000,
    }
    prepared = _prepare({"kind": "oi", "source_recorded_at_ms": 930_000, **values}, "102", tmp_path, monkeypatch)
    evidence = prepared.brief.evidence_catalog["source"]
    assert evidence["status"] == "ok" and evidence["values"] == values
    assert any(item.kind == "immediate_entry_v1" for item in prepared.plans)
