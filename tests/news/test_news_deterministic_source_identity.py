from __future__ import annotations

import pytest

from tracefold.news import liquidations, oi_signals
from tracefold.news.source_contracts import SOURCE_CONTRACT_CLASSIFIER_VERSION


def test_deterministic_program_identities_commit_to_the_source_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    oi_sha = oi_signals.program_sha256()
    liquidation_sha = liquidations.program_sha256()

    monkeypatch.setattr(oi_signals, "SOURCE_CONTRACT_CLASSIFIER_VERSION", "opennews_source_classifier_v2")
    monkeypatch.setattr(liquidations, "SOURCE_CONTRACT_CLASSIFIER_VERSION", "opennews_source_classifier_v2")

    assert oi_signals.program_sha256() != oi_sha
    assert liquidations.program_sha256() != liquidation_sha
    assert oi_signals.PARSER_VERSION == "oi_signal_parser_v1"
    assert liquidations.PARSER_VERSION == "liquidation_parser_v1"


def test_success_and_failure_traces_name_the_source_classifier() -> None:
    signal = oi_signals.parse_oi_signal(
        "BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    )
    fact = liquidations.parse_liquidation(
        "BTC Large Short Liquidation 10K at $150",
        item_id="a" * 64,
        fact_id="whole",
        provider_source="binance",
        event_at_ms=1_000,
        received_at_ms=2_000,
    )
    assert signal is not None and fact is not None

    oi_trace = oi_signals.oi_judgment_trace(
        oi_signals.evaluate_oi(signal, earlier_eligible_count=0),
        policy=oi_signals.DEFAULT_OI_POLICY,
    )
    liquidation_trace = liquidations.trace(fact)
    _, oi_failure = oi_signals.oi_parse_failure("bad frame", provider_source="binance")
    _, liquidation_failure = liquidations.parse_failure("bad frame", provider_source="binance")

    for trace in (oi_trace, liquidation_trace, oi_failure, liquidation_failure):
        assert trace["source_classifier_version"] == SOURCE_CONTRACT_CLASSIFIER_VERSION
