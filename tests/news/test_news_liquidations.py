from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.news.events.gate import GateInput, evaluate_gate
from tracefold.news.liquidations import (
    ADMISSION_POLICY_VERSION,
    PROGRAM_VERSION,
    READER_CONTRACT_VERSION,
    TRIAGE_POLICY_VERSION,
    judge,
    parse_failure,
    parse_liquidation,
    program_sha256,
)
from tracefold.news.source_contracts import classify_source_contract, source_contract_admission


def _parse(title: str, *, venue: str = "binance"):
    return parse_liquidation(
        title,
        item_id="a" * 64,
        fact_id="whole",
        provider_source=venue,
        event_at_ms=1_000,
        received_at_ms=2_000,
    )


@pytest.mark.parametrize(
    ("text", "notional"),
    [
        ("SPCX Large Short Liquidation 202.71K at $137.01", Decimal("202710")),
        ("BTC Large Long Liquidation 1.25M at $123.45", Decimal("1250000")),
        ("ETH Large Short Liquidation 2B at $4.50", Decimal("2000000000")),
        ("SOL Large Long Liquidation 99 at $1", Decimal("99")),
    ],
)
def test_exact_template_and_decimal_units(text: str, notional: Decimal) -> None:
    fact = _parse(text)
    assert fact is not None
    assert fact.notional_usd == notional
    assert fact.price > 0


def test_provider_position_side_is_normalized_to_the_forced_order_side() -> None:
    short = _parse("SOL Large Short Liquidation 10K at $150")
    long = _parse("SOL Large Long Liquidation 10K at $150", venue="hyperliquid")
    assert short is not None and (short.liquidated_position_side, short.forced_order_side) == ("short", "buy")
    assert long is not None and (long.liquidated_position_side, long.forced_order_side) == ("long", "sell")


def test_source_contract_records_every_semantic_gap_and_stays_incomplete() -> None:
    fact = _parse("SOL Large Short Liquidation 10K at $150")
    assert fact is not None
    assert fact.provider_record_identity
    assert fact.symbol_contract_identity == "unresolved:binance:SOL"
    assert fact.position_side_semantics
    assert fact.quantity_semantics == "not_provided"
    assert fact.notional_semantics == "provider_reported_usd_notional"
    assert fact.price_semantics == "provider_reported_unspecified_price"
    assert fact.completeness_assumption and fact.throttle_assumption
    assert fact.source_contract_complete is False


@pytest.mark.parametrize(
    "text",
    [
        "SOL Liquidation 10K at $150",
        "SOL Large Buy Liquidation 10K at $150",
        "SOL Large Short Liquidation about 10K at $150",
        "SOL Large Short Liquidation 10T at $150",
        "SOL Large Short Liquidation 10K at mark $150",
        "SOL Large Short Liquidation -10K at $150",
        "XYZ- Large Short Liquidation 10K at $150",
    ],
)
def test_ambiguous_or_malformed_prose_fails_closed(text: str) -> None:
    assert _parse(text) is None


def test_unknown_venue_or_timestamp_order_fails_closed() -> None:
    assert _parse("SOL Large Short Liquidation 10K at $150", venue="other") is None
    assert (
        parse_liquidation(
            "SOL Large Short Liquidation 10K at $150",
            item_id="a" * 64,
            fact_id="whole",
            provider_source="binance",
            event_at_ms=2_000,
            received_at_ms=1_000,
        )
        is None
    )


def test_reader_judgment_stays_direction_neutral_and_owns_one_action() -> None:
    fact = _parse("SPCX Large Short Liquidation 202.71K at $137.01")
    assert fact is not None
    judgment = judge(fact)
    assert judgment.verdict.direction == "neutral"
    assert "不代表后续方向" in judgment.verdict.why_zh
    assert (judgment.decision.final, judgment.decision.override_rule) == ("push", "liquidation_fact_only")
    assert {"event_type", "actionable", "decision", "title_zh"}.isdisjoint(judgment.verdict.model_dump(mode="json"))
    assert judgment.judgment_sha256 == judge(fact).judgment_sha256


def test_parse_failure_is_a_typed_fail_closed_judgment() -> None:
    judgment, failure = parse_failure("bad liquidation", provider_source="binance")
    assert judgment.fact is None
    assert (judgment.decision.final, judgment.decision.override_rule) == ("drop", "liquidation_parse_failed")
    assert judgment.rule == "liquidation_parse_failed"
    assert "rule" not in failure


def test_program_identity_is_stable() -> None:
    assert program_sha256() == program_sha256()
    assert len(program_sha256()) == 64
    assert ADMISSION_POLICY_VERSION == "news_liquidation_admission_v1"
    assert (PROGRAM_VERSION, READER_CONTRACT_VERSION, TRIAGE_POLICY_VERSION) == (
        "news_liquidation_fact_v2",
        "liquidation_card_v2",
        "news_liquidation_policy_v2",
    )


def test_liquidation_admission_is_composed_after_unchanged_generic_gate_policy() -> None:
    generic = evaluate_gate(
        GateInput(
            title="SOL Large Short Liquidation 10K at $150",
            engine_type="market",
            provider_score=90,
            coins=(),
            ingest_mode="live",
            watchlist_symbols=frozenset(),
        )
    )
    assert generic.admission == "candidate"
    contract = classify_source_contract(
        {
            "score": 90,
            "strategies": [{"id": "2000", "name": "实时清算", "source_type": "market", "engine_type": "market"}],
        }
    )
    assert (
        source_contract_admission(contract, generic_admission=generic.admission, ingest_mode="live")
        == "liquidation_deterministic"
    )
    assert (
        source_contract_admission(contract, generic_admission=generic.admission, ingest_mode="recovery") == "recovery"
    )
