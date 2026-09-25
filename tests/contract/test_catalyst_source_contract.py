"""Real producer/reader/compiler contract; only external storage and market I/O are faked."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analysis import FrameReader
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.trading.engine.contracts import AgentAssessment
from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult
from tracefold.trading.engine.policy import compile_assessment


class _Market:
    def __init__(self, close: str) -> None:
        self.close = close

    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        rows: tuple[dict[str, Any], ...] = ()
        if request.dataset == "perp_bars":
            assert request.start_ms == 0 and request.end_ms == 960_000
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


def _prepare(source, close, tmp_path, monkeypatch):
    monkeypatch.setattr("tracefold.app.trading_analysis._clock_ms", lambda: 1_000_000)
    files = AnalysisFiles(tmp_path)
    prepared = asyncio.run(
        FrameReader(_Market(close), files).prepare(
            case={
                "case_id": "b" * 64,
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
        )
    )
    assert files.read(prepared.evidence_ref)["source_fact"] == source
    assert json.loads(files.read(prepared.brief_ref)["brief_json"])["source_fact"] == source
    return prepared


def _produced_catalyst():
    """Exercise the actual News outbox mapping, not a hand-authored Trading fixture."""
    news = Mock()
    news.reader_history_revision.return_value = (0, 0, "")
    news.latest_evidence_identity.return_value = (1, "e" * 64)
    consumer = TriageConsumer(
        bus=Mock(),
        db=Mock(),
        judge=None,
        program_version="fixture",
        program_sha256="1" * 64,
        watchlist_symbols=frozenset(),
        watchlist=[],
        concurrency=1,
        circuit_failures=2,
        circuit_open_seconds=1,
        runtime_manifest={"manifest_sha": "a" * 64},
    )
    verdict = {
        "headline_zh": "项目宣布一项变更",
        "why_zh": "来源为项目公告，执行情况尚待确认。",
        "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
        "direction": "unclear",
        "novelty": "new_fact",
        "fact_kind": "state_change",
    }
    settlement = SimpleNamespace(
        circuit_incident=None,
        final_key="fixture-source",
        history=SimpleNamespace(ledger_revision=(0, 0, "")),
        event_id="c" * 64,
        evidence_version=1,
        evidence_sha256="e" * 64,
        focus_fact_id="f" * 64,
        stamp=930_000,
        allow_stale=False,
        judgment=SimpleNamespace(judgment_contract_version="news_judgment_v3"),
        policy_version="fixture",
        origin="model",
        runtime_manifest_sha="a" * 64,
        model_name="fixture",
        program_version="fixture",
        program_sha256="1" * 64,
        degraded=False,
        error_code=None,
        card={"leader_published_at_ms": 920_000, "opened_at_ms": 925_000, "ingest_mode": "live"},
    )
    prepared = SimpleNamespace(
        decision=SimpleNamespace(rule_baseline="drop", final="drop", override_rule=None, throttled_by=None),
        verdict=verdict,
        verdict_json=json.dumps(verdict),
        model_editorial=None,
        model_editorial_json=None,
        judgment_sha256="d" * 64,
        trace={},
        trace_json="{}",
        context_line="fixture",
    )
    consumer._persist_prepared_settlement(SimpleNamespace(news=news), s=settlement, prepared=prepared)
    news.enqueue_trade_event.assert_called_once()
    payload = news.enqueue_trade_event.call_args.kwargs["payload"]
    assert payload["headline"] == verdict["headline_zh"]
    assert payload["why"] == verdict["why_zh"]
    assert not {"headline_zh", "title", "why_zh"}.intersection(payload)
    return payload


@pytest.mark.parametrize("close,side", [("102", "long"), ("98", "short"), ("100", None)])
def test_news_public_catalyst_reaches_citable_evidence_and_final_decision(close, side, tmp_path, monkeypatch) -> None:
    source = _produced_catalyst()
    prepared = _prepare(source, close, tmp_path, monkeypatch)
    evidence = prepared.brief.evidence_catalog["source"]
    assert evidence["status"] == "ok"
    assert evidence["values"] == {"headline": source["headline"], "why": source["why"]}
    assert evidence["unit_definition"] == {"headline": "text", "why": "text"}
    assert "source" in json.loads(prepared.brief.text)["citable_evidence_ids"]
    selected = next((item for item in prepared.candidates if item.side == side), None)
    assessment = AgentAssessment(
        action="WATCH" if side is None else "TRADE",
        entry_candidate_id=None if selected is None else selected.candidate_id,
        supporting_evidence=("source", "market:perp_bars"),
        public_rationale="Recorded source and code-owned price condition.",
    )
    decision = compile_assessment(
        assessment=assessment,
        candidates=prepared.candidates,
        evidence_catalog=prepared.brief.evidence_catalog,
        watch_expires_at_ms=1_530_000,
    )
    assert decision.action == assessment.action
    assert decision.side == side
    assert (decision.watch_condition is not None) == (side is None)


@pytest.mark.parametrize(
    "text",
    [
        {"headline_zh": "Internal only", "title": "Provider only", "why_zh": "Internal only"},
        {"headline": " \t", "why": "\n"},
        {"headline": 123, "why": True},
        {"headline": "", "why": None, "oi_value_usd": 1000},
    ],
)
def test_non_public_text_is_missing_in_both_evidence_and_candidates(text, tmp_path, monkeypatch) -> None:
    source = {"kind": "catalyst", "source_recorded_at_ms": 930_000, **text}
    before = deepcopy(source)
    prepared = _prepare(source, "100", tmp_path, monkeypatch)
    evidence = prepared.brief.evidence_catalog["source"]
    assert evidence["status"] == "missing" and evidence["values"] == {}
    assert "source" not in json.loads(prepared.brief.text)["citable_evidence_ids"]
    assert all(not item.entry_ready and not item.watch_eligible for item in prepared.candidates)
    assert source == before


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
    assert prepared.candidates[0].entry_ready
