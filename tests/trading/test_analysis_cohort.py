from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.export_trading_analysis_cohort import (
    _rule_receipt_archives_complete,
    _rule_shadow_receipt,
    _rule_watch_path,
)
from scripts.trading_analysis_cohort import (
    PURGE_MS,
    _holdout_comparison,
    _model_cost,
    _rule_action,
    _rule_decision,
    _split,
    evaluate,
)
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.trading.engine.strategy import STRATEGY_VERSION


def _case(case_id: str, root: str, at_ms: int, *, group: str | None = None) -> dict[str, object]:
    return {
        "case_id": case_id,
        "root_trigger_id": root,
        "source_group_id": group or root,
        "asset_id": "crypto:SOL",
        "created_at_ms": at_ms,
        "run_kind": "initial",
        "state": "DONE",
        "decision_action": "NO_TRADE",
        "decision_policy_version": "v3",
    }


def test_time_purge_and_source_group_keep_complete_roots_together() -> None:
    cutoff = 100_000_000
    assert _split(cutoff - PURGE_MS, cutoff) == "development"
    assert _split(cutoff - PURGE_MS + 1, cutoff) == "purged"
    assert _split(cutoff, cutoff) == "holdout"
    cases = [
        _case("a", "root-a", cutoff - PURGE_MS, group="shared"),
        _case("b", "root-b", cutoff, group="shared"),
        _case("c", "root-c", cutoff + 1),
    ]
    cases.append({**_case("child", "root-c", cutoff + 2), "run_kind": "conditional"})
    cases[-1]["attempts"] = [{"model_name": "fixture-model", "prompt_sha": "fixture-prompt"}]
    report = evaluate(cases, expected_roots=3, cutoff_ms=cutoff, invalid_outputs=[], expected_invalid=0)
    assert report["model_identities"] == [{"model_name": "fixture-model", "prompt_sha": "fixture-prompt"}]
    assert report["decision_policy_versions"] == ["v3"]
    assert report["denominator"] == {"root_triggers": 3, "cases": 4}
    assert report["split_roots"] == {"cross_split_excluded": 2, "holdout": 1}
    assert report["funnel"]["conditional_cases"] == 1
    assert report["arms"]["holdout"]["dspy"]["net_evaluable"] == 0
    assert report["arms"]["holdout"]["dspy"]["net_unknown"] == 0


def test_net_requires_contemporary_strategy_receipt_and_applies_capital() -> None:
    cutoff = 100_000_000
    first = _case("a", "root-a", cutoff)
    first["decision_action"] = "TRADE"
    first["arm_evaluations"] = {
        "dspy": {
            "status": "simulated",
            "strategy_version": STRATEGY_VERSION,
            "entry_at_ms": cutoff + 1000,
            "exit_at_ms": cutoff + 2000,
            "net_bps": "100",
            "net_components_bps": {
                "gross": "130",
                "entry_cost": "5",
                "exit_cost": "5",
                "fees": "20",
                "funding_cashflow": "0",
            },
            "entry_quote_ref": "entry",
            "exit_quote_ref": "exit",
            "instrument_rules_ref": "rules",
            "mark_path_ref": "marks",
            "funding_ref": "funding",
            "fee_ref": "fees",
            "order_latency_ms": 100,
            "requested_notional_usdt": "100",
            "simulated_notional_usdt": "100",
            "quantity_base": "1",
            "entry_price": "100",
            "market_step_size": "0.01",
            "stop_bps": "100",
        }
    }
    second = _case("b", "root-b", cutoff + 100)
    second["decision_action"] = "TRADE"
    report = evaluate(
        [first, second],
        expected_roots=2,
        cutoff_ms=cutoff,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("1000"),
    )
    summary = report["arms"]["holdout"]["dspy"]
    assert summary["net_evaluable"] == 1
    assert summary["net_unknown"] == 1
    assert summary["net_unknown_reasons"] == {"receipt_missing": 1}
    assert summary["portfolio_complete"] is False
    assert summary["ending_equity_usdt"] is None
    assert summary["account_drawdown_usdt"] is None
    first["arm_evaluations"]["dspy"]["equity_marks"] = [
        {"at_ms": cutoff + 1000, "liquidation_net_bps": "-100"},
        {"at_ms": cutoff + 1500, "liquidation_net_bps": "-300"},
    ]
    marked = evaluate(
        [first],
        expected_roots=1,
        cutoff_ms=cutoff,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("1000"),
    )
    assert marked["arms"]["holdout"]["dspy"]["account_drawdown_usdt"] == "3"
    assert marked["arms"]["holdout"]["dspy"]["ending_equity_usdt"] == "1001"
    first["arm_evaluations"]["dspy"]["instrument_rules_ref"] = None
    unknown = evaluate([first, second], expected_roots=2, cutoff_ms=cutoff, invalid_outputs=[], expected_invalid=0)
    assert unknown["arms"]["holdout"]["dspy"]["net_unknown"] == 2
    assert unknown["arms"]["holdout"]["dspy"]["net_unknown_reasons"] == {
        "receipt_invalid": 1,
        "receipt_missing": 1,
    }


def test_no_trade_is_zero_cashflow_but_missing_rule_path_is_unknown() -> None:
    at_ms = 100_000_000
    root = _case("initial", "root", at_ms)
    report = evaluate([root], expected_roots=1, cutoff_ms=at_ms, invalid_outputs=[], expected_invalid=0)
    assert report["arms"]["holdout"]["dspy"]["ending_equity_usdt"] == "1000"
    rule = report["arms"]["holdout"]["rule"]
    assert rule["portfolio_complete"] is False
    assert rule["net_unknown_reasons"] == {"rule_decision_unavailable": 1}
    assert rule["ending_equity_usdt"] is None


def test_legacy_model_and_timer_are_not_new_dspy_arm_evidence() -> None:
    at_ms = 100_000_000
    old = _case("old", "root-old", at_ms)
    old["decision_policy_version"] = "v2"
    timed = _case("timer-root", "root-timer", at_ms + 1)
    child = {**_case("timer-child", "root-timer", at_ms + 2), "run_kind": "recheck", "recheck_seq": 1}
    report = evaluate([old, timed, child], expected_roots=2, cutoff_ms=at_ms, invalid_outputs=[], expected_invalid=0)
    dspy = report["arms"]["holdout"]["dspy"]
    assert dspy["portfolio_complete"] is False
    assert dspy["net_unknown_reasons"] == {
        "legacy_model_contract_uncomparable": 1,
        "legacy_timer_recheck_uncomparable": 1,
    }
    assert dspy["ending_equity_usdt"] is None


def test_terminal_failure_and_expired_watch_have_zero_cashflow_but_open_states_are_unknown() -> None:
    at_ms = 100_000_000
    failed = _case("failed", "root-failed", at_ms)
    failed.update(state="FAILED", decision_action=None, decision_policy_version=None)
    watch = _case("watch", "root-watch", at_ms + 1)
    watch["decision_action"] = "WATCH"
    excluded = _case("excluded", "root-excluded", at_ms + 2)
    excluded.update(state="EXCLUDED", decision_action=None, decision_policy_version=None)
    expired = _case("expired", "root-expired", at_ms + 3)
    expired.update(decision_action="WATCH", watch_status="expired")
    pending = _case("pending", "root-pending", at_ms + 4)
    pending.update(state="PENDING", decision_action=None, decision_policy_version=None)
    report = evaluate(
        [failed, watch, excluded, expired, pending],
        expected_roots=5,
        cutoff_ms=at_ms,
        invalid_outputs=[],
        expected_invalid=0,
    )
    dspy = report["arms"]["holdout"]["dspy"]
    assert dspy["net_unknown_reasons"] == {
        "model_decision_unavailable": 1,
        "watch_outcome_unverified": 1,
    }
    assert dspy["technical_no_trade"] == 1
    assert dspy["expired_watch_no_trade"] == 1
    assert dspy["portfolio_complete"] is False
    assert dspy["ending_equity_usdt"] is None
    assert report["funnel"]["watch_statuses"] == {"expired": 1, "missing": 1}
    assert report["funnel"]["initial_failures"] == 1
    assert report["funnel"]["initial_unsettled"] == 1
    assert report["arms"]["holdout"]["rule"]["decisions"]["NO_TRADE"] == 1
    settled = evaluate(
        [failed, expired, excluded], expected_roots=3, cutoff_ms=at_ms, invalid_outputs=[], expected_invalid=0
    )
    assert settled["arms"]["holdout"]["dspy"]["ending_equity_usdt"] == "1000"


def test_holdout_conclusion_requires_complete_net_drawdown_and_currency_assumption() -> None:
    rule = {
        "portfolio_complete": True,
        "account_drawdown_usdt": "2",
        "net_evaluable": 1,
        "ending_equity_usdt": "1001",
    }
    dspy = {
        **rule,
        "ending_equity_usdt": "1002",
        "model_cost_known_microusd": 2_000_000,
        "model_cost_unknown_calls": 0,
    }
    missing_fx = _holdout_comparison(root_count=3, rule=rule, dspy=dspy, model_usd_to_usdt_rate=None)
    assert missing_fx["research_conclusion"] == "evidence_insufficient"
    assert missing_fx["incomplete_reasons"] == ["model_currency_conversion_missing"]
    priced = _holdout_comparison(root_count=3, rule=rule, dspy=dspy, model_usd_to_usdt_rate=Decimal("1"))
    assert priced["after_model_delta_usdt"] == "-1"
    assert priced["research_conclusion"] == "no_observed_advantage_or_worse"
    dspy["model_cost_known_microusd"] = 100_000
    positive = _holdout_comparison(root_count=3, rule=rule, dspy=dspy, model_usd_to_usdt_rate=Decimal("1"))
    assert positive["after_model_delta_usdt"] == "0.9"
    assert positive["research_conclusion"] == "supports_continued_research"
    dspy["model_cost_unknown_calls"] = 1
    assert (
        _holdout_comparison(root_count=3, rule=rule, dspy=dspy, model_usd_to_usdt_rate=Decimal("1"))[
            "research_conclusion"
        ]
        == "evidence_insufficient"
    )


def test_capital_limit_does_not_resize_a_validated_execution_receipt() -> None:
    cutoff = 100_000_000
    first = _case("a", "root-a", cutoff)
    first["decision_action"] = "TRADE"
    first["arm_evaluations"] = {
        "dspy": {
            "status": "simulated",
            "strategy_version": STRATEGY_VERSION,
            "entry_at_ms": cutoff + 1000,
            "exit_at_ms": cutoff + 2000,
            "net_bps": "100",
            "net_components_bps": {
                "gross": "100",
                "entry_cost": "0",
                "exit_cost": "0",
                "fees": "0",
                "funding_cashflow": "0",
            },
            "entry_quote_ref": "entry",
            "exit_quote_ref": "exit",
            "instrument_rules_ref": "rules",
            "mark_path_ref": "marks",
            "funding_ref": "funding",
            "fee_ref": "fees",
            "order_latency_ms": 100,
            "requested_notional_usdt": "100",
            "simulated_notional_usdt": "100",
            "quantity_base": "1",
            "entry_price": "100",
            "market_step_size": "0.01",
            "stop_bps": "100",
        }
    }
    report = evaluate(
        [first],
        expected_roots=1,
        cutoff_ms=cutoff,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("500"),
    )
    summary = report["arms"]["holdout"]["dspy"]
    assert summary["net_evaluable"] == 0
    assert summary["capital_rejected"] == 1


def test_wrong_historical_denominator_fails_before_metrics() -> None:
    row = _case("a", "r", 1)
    with pytest.raises(ValueError, match="root_denominator_mismatch"):
        evaluate([row], expected_roots=531, cutoff_ms=10, invalid_outputs=[], expected_invalid=22)


def _watch_root(at_ms: int) -> dict[str, object]:
    root = _case("initial", "root", at_ms)
    root.update(
        mapping_semantics_digest="a" * 64,
        root_expires_at_ms=at_ms + 300_000,
        evidence={
            "source_fact": {"kind": "catalyst", "headline_zh": "Fixture event"},
            "source_first_visible_at_ms": at_ms - 1_000,
            "market": {
                "perp_bars": {
                    "status": "ok",
                    "payload": [
                        {"event_at_ms": at_ms - (15 - index) * 60_000, "high": "101", "low": "99", "close": "100"}
                        for index in range(16)
                    ],
                }
            },
        },
    )
    return root


def test_rule_arm_crosses_independently_of_dspy_child_case() -> None:
    at_ms = 100_000_000
    root = _watch_root(at_ms)
    root["rule_watch_bars"] = [{"event_at_ms": at_ms + 60_000, "received_at_ms": at_ms + 61_000, "close": "102"}]
    assert _rule_action([root]) == "TRADE"
    report = evaluate([root], expected_roots=1, cutoff_ms=at_ms, invalid_outputs=[], expected_invalid=0)
    assert report["arms"]["holdout"]["rule"]["decisions"] == {"TRADE": 1}
    assert report["arms"]["holdout"]["rule"]["net_unknown"] == 1
    assert report["arms"]["holdout"]["dspy"]["decisions"] == {"NO_TRADE": 1}


def test_initial_rule_breakout_needs_on_time_bar_receipt() -> None:
    at_ms = 100_000_000
    root = _watch_root(at_ms)
    root["evidence"]["market"]["perp_bars"]["payload"][-1].update(
        high="103", close="102", received_at_ms=at_ms + 121_000
    )
    assert _rule_action([root]) == "NO_TRADE"
    root["evidence"]["market"]["perp_bars"]["payload"][-1]["received_at_ms"] = at_ms + 1_000
    assert _rule_action([root]) == "TRADE"


def test_rule_arm_does_not_trade_a_late_first_cross_or_guess_across_a_gap() -> None:
    at_ms = 100_000_000
    root = _watch_root(at_ms)
    root["rule_watch_bars"] = [
        {"event_at_ms": at_ms + 60_000, "received_at_ms": at_ms + 181_000, "close": "102"},
        {"event_at_ms": at_ms + 120_000, "received_at_ms": at_ms + 121_000, "close": "98"},
    ]
    assert _rule_action([root]) == "NO_TRADE"
    root["rule_watch_bars"] = [{"event_at_ms": at_ms + 120_000, "received_at_ms": at_ms + 121_000, "close": "102"}]
    assert _rule_action([root]) is None


def test_rule_arm_complete_path_ends_at_non_aligned_root_expiry() -> None:
    at_ms = 100_000_000
    root = _watch_root(at_ms)
    root["root_expires_at_ms"] = at_ms + 90_000
    root["rule_watch_bars"] = [
        {"event_at_ms": at_ms + 60_000, "received_at_ms": at_ms + 61_000, "close": "100"},
        {"event_at_ms": at_ms + 120_000, "received_at_ms": at_ms + 121_000, "close": "100"},
    ]
    root["rule_watch_status"] = "complete"
    assert _rule_action([root]) == "NO_TRADE"


def test_exported_rule_path_requires_contiguous_archived_bars() -> None:
    evidence = {"entry_reference": {"closed_at_ms": 60_000}}
    tape = {
        "version": "root_research_tape_v2",
        "closed_bars": [
            {"event_at_ms": 120_000, "received_at_ms": 120_100, "close": "100", "snapshot_ref": "a"},
            {"event_at_ms": 180_000, "received_at_ms": 180_100, "close": "101", "snapshot_ref": "b"},
        ],
    }
    path, status = _rule_watch_path(evidence, tape, 150_000)
    assert status == "complete" and [row["event_at_ms"] for row in path] == [120_000]
    tape["closed_bars"] = tape["closed_bars"][1:]
    assert _rule_watch_path(evidence, tape, 150_000)[1] == "partial"


def test_rule_arm_replays_frozen_quote_mark_funding_and_contract_rules(tmp_path) -> None:
    at_ms = 100_020_000
    root = _watch_root(at_ms)
    root["rule_watch_bars"] = [{"event_at_ms": at_ms + 60_000, "received_at_ms": at_ms + 61_000, "close": "102"}]
    files = AnalysisFiles(tmp_path)
    root["evidence_ref"] = "e" * 64
    root["target_selection"] = {
        "instrument": {
            "native_symbol": "SOLUSDT",
            "environment": "live",
            "mapping_semantics_digest": "a" * 64,
            "units_per_contract": "1",
        }
    }
    root["evidence"]["data_environment"] = "live"
    root["evidence"]["market"]["instrument_rules"] = {
        "status": "ok",
        "unit_definition": "binance_usdm_contract_rules_v1",
        "received_at_ms": at_ms,
        "payload": [
            {
                "native_symbol": "SOLUSDT",
                "trading_status": "TRADING",
                "contract_type": "PERPETUAL",
                "quote_asset": "USDT",
                "settlement_asset": "USDT",
                "price_tick_size": "0.01",
                "market_min_quantity": "0.01",
                "market_max_quantity": "1000",
                "market_step_size": "0.01",
                "minimum_notional": "5",
            }
        ],
    }
    research_end = root["root_expires_at_ms"] + 14_400_000 + 120_000
    entry = {
        "status": "ok",
        "environment": "live",
        "native_symbol": "SOLUSDT",
        "mapping_semantics_digest": "a" * 64,
        "units_per_contract": "1",
        "received_at_ms": at_ms + 62_000,
        "bid": "101.9",
        "ask": "102",
        "bid_quantity": "10",
        "ask_quantity": "10",
    }
    exit_quote = {**entry, "received_at_ms": at_ms + 121_000, "bid": "95", "ask": "95.1"}
    for quote in (entry, exit_quote):
        quote["quote_ref"] = files.write(
            {
                "status": "ok",
                "environment": "live",
                "native_symbol": "SOLUSDT",
                "mapping_semantics_digest": "a" * 64,
                "units_per_contract": "1",
                "payload": [
                    {key: quote[key] for key in ("received_at_ms", "bid", "ask", "bid_quantity", "ask_quantity")}
                ],
            }
        )
    mark = {
        "event_at_ms": at_ms + 120_000,
        "received_at_ms": at_ms + 120_100,
        "high": "103",
        "low": "90",
        "close": "100",
    }
    mark["snapshot_ref"] = files.write({"status": "ok", "payload": [mark]})
    funding_ref = files.write({"status": "ok", "payload": []})
    tape = {
        "version": "root_research_tape_v2",
        "case_id": "initial",
        "native_symbol": "SOLUSDT",
        "environment": "live",
        "mapping_semantics_digest": "a" * 64,
        "root_accepted_at_ms": at_ms,
        "quotes": [entry, exit_quote],
        "mark_bars": [mark],
        "funding_history": {
            "status": "ok",
            "payload": [],
            "snapshot_ref": funding_ref,
            "scan_received_at_ms": research_end + 120_000,
        },
    }
    root["root_market_tape_ref"] = files.write(tape)
    receipt = _rule_shadow_receipt(
        root,
        tape,
        _rule_decision([root]),
        risk_usdt=Decimal("10"),
        fee_bps_per_side=Decimal("5"),
        max_spread_fraction_of_stop=Decimal("0.25"),
    )
    assert receipt["status"] == "simulated"
    assert receipt["entry_quote_ref"] == entry["quote_ref"]
    assert receipt["exit_quote_ref"] == exit_quote["quote_ref"]
    missing = []
    assert _rule_receipt_archives_complete(files, root, tape, receipt, missing)
    assert missing == []
    assert Decimal(receipt["net_bps"]) < 0
    root["arm_evaluations"] = {"rule": receipt}
    report = evaluate(
        [root],
        expected_roots=1,
        cutoff_ms=at_ms,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("10000"),
    )
    assert report["arms"]["holdout"]["rule"]["net_evaluable"] == 1
    tape["quotes"][1]["ask"] = "94"
    assert not _rule_receipt_archives_complete(files, root, tape, receipt, missing)


def test_model_cost_includes_known_subtotal_with_unknown_physical_calls() -> None:
    root = _case("initial", "root", 1)
    root["attempts"] = [
        {"cost_microusd": None, "known_cost_microusd": 200, "unknown_cost_calls": 1},
        {"cost_microusd": 150, "known_cost_microusd": 150, "unknown_cost_calls": 0},
    ]
    assert _model_cost([root]) == (350, 1)
