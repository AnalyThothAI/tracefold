"""Generate and verify the behavior/structure baseline for refactor epic #162.

This is test support rather than an application caller: it deliberately reaches the
concrete owner seams and records their observable values. Stable value and port contracts
remain available from the business package roots.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.support.news_judgment import trade_relevance
from tracefold.app.workers.task_contract import worker_task_names
from tracefold.integrations import rabbitmq as rabbitmq_module
from tracefold.news.agents.program_compiler import COMPILER_ID
from tracefold.news.agents.program_compiler_security import (
    COMPILER_CORPUS_SCHEMA,
    COMPILER_ENDPOINT_IDENTITY_SCHEMA,
    COMPILER_INPUT_SCHEMA,
    COMPILER_RECEIPT_CHAIN_SCHEMA,
    COMPILER_RECEIPT_SCHEMA,
    COMPILER_ROLE_BINDING_SCHEMA,
    COMPILER_RUNNER_RECEIPTS_SCHEMA,
)
from tracefold.news.agents.program_metric import METRIC_ID
from tracefold.news.agents.semantic_program import (
    PROGRAM_ADAPTER_SHA256,
    PROGRAM_ASSEMBLER_SHA256,
    PROGRAM_DEPENDENCY_LOCK_SHA256,
    PROGRAM_FACTORY_ID,
    PROGRAM_INPUT_CONTRACT_SHA256,
    PROGRAM_LEARNING_EPOCH,
    PROGRAM_NORMALIZER_SHA256,
    PROGRAM_RENDERER_SHA256,
    PROGRAM_SCHEMA_VERSION,
    PROGRAM_TOPOLOGY_SHA256,
    PROGRAM_VERSION,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.bus import MAX_TRANSIENT_ATTEMPTS, RETRY_TTL_MS
from tracefold.news.delivery import render_first_card
from tracefold.news.eval.replay import replay_hits
from tracefold.news.models import TRIAGE_POLICY_VERSION, ReaderReceipt, TriageVerdict
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.news.review import REVIEW_RUBRIC_VERSION
from tracefold.news.semantic_contract import (
    EditorialEnvelope,
    ProgramTrace,
    ProgramUsage,
    SemanticJudgment,
    TriageContext,
)
from tracefold.news.triage_rules import GateFacts
from tracefold.news.triage_rules import decide as news_decide
from tracefold.platform.config.models import Settings
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import news_candidate, oi_candidate
from tracefold.trading.candidate.routing import resolve_instrument
from tracefold.trading.contracts import (
    ACTIVE_ORDER_STATES,
    TERMINAL_ORDER_STATES,
    TRADING_MANIFEST_VERSION,
    TRADING_POLICY_VERSION,
    TRADING_PROGRAM_VERSION,
    Bar,
    InstrumentRef,
    MarketContext,
    NewsCandidateRow,
    NewsTradeCandidate,
    OiCandidateRow,
    OiTradeCandidate,
    OrderState,
    PreparedOrder,
    RiskRejection,
    TradeDecision,
    TradingCaseManifest,
)
from tracefold.trading.decision.policy import decide as trading_decide
from tracefold.trading.decision.policy import side_to_order_side
from tracefold.trading.decision.regime import assess, pre_move_bps
from tracefold.trading.execution.order import SizedOrder, build_payload, size_order
from tracefold.trading.execution.paper import PaperAdapter, PaperFaults
from tracefold.trading.pipeline.root import build_pipeline
from tracefold.trading.pipeline.runtime import TradingConfig

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "generated" / "refactor-baseline-9441ce99.json"
BASELINE_REVISION = "9441ce995bea872805b9611b8b7a860f5d1d1385"
SCHEMA_VERSION = "tracefold_refactor_baseline_v1"
NOW_MS = 1_787_000_000_000

# Intentional post-#160 drift, declared leaf by leaf *with the value it drifted to*. The baseline is frozen at
# `BASELINE_REVISION` on purpose: it is epic #162's proof that a refactor changed no behavior, so regenerating
# it in place would destroy the reference point rather than record the change. A semantic PR that *means* to
# move a contract declares exactly the leaves it moves and leaves every other leaf guarded.
#
# Declaring the *value*, not just the path, is what keeps this a guard. Against a frozen baseline a leaf that
# has drifted once can never drift back, so exempting a path by name would exempt it forever: the next PR to
# touch `openapi.json` or the Program root would sail through silently under a note written for #173. Pinning
# the expected value means the next such PR has to come here and say so — and a leaf that returns to the
# baseline value fails as a stale entry rather than lingering.
#
# #173 added the `product_progress` TradeChannel. It is publicly projected through `news_review_v3`, so both
# generated contract artefacts move with it, and the code-owned stable Program root is re-issued.
INTENTIONAL_DRIFT: dict[str, tuple[str, str]] = {
    "generated_artifacts_sha256.docs/generated/openapi.json": (
        "issue_173_product_progress_channel",
        "b0e424cf22fe9b12a6d5e5e8f59098315bb0c2b7f77e0775b27b014120efa23a",
    ),
    "generated_artifacts_sha256.web/src/lib/types/openapi.ts": (
        "issue_173_product_progress_channel",
        "1fd00735c168b23be86806c96d4e062a484ff42756e29dc52fcec0fe9211290a",
    ),
    "program_learning.program_sha256": (
        "issue_173_product_recall_baseline_root",
        "9334eae481e2d0cdcc3b982d25aa8def22538cadb1a57549074b56fb2a96d1ba",
    ),
}


def _changed_leaves(expected: Any, current: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every leaf path whose value differs, with the value it now holds."""

    if isinstance(expected, dict) and isinstance(current, dict):
        for key in sorted(set(expected) | set(current)):
            child = f"{path}.{key}" if path else key
            yield from _changed_leaves(expected.get(key, _MISSING), current.get(key, _MISSING), child)
    elif isinstance(expected, list) and isinstance(current, list) and len(expected) == len(current):
        for index, (left, right) in enumerate(zip(expected, current, strict=True)):
            yield from _changed_leaves(left, right, f"{path}[{index}]")
    elif expected != current:
        yield path, current


_MISSING = object()


def _sha_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _rabbitmq_contract() -> dict[str, Any]:
    declared = rabbitmq_module.topology()
    queues = [
        {
            "name": spec.name,
            "durable": True,
            "arguments": dict(spec.arguments),
            "bindings": (
                [{"exchange": declared.exchange, "routing_key": key} for key in spec.bindings]
                if spec.bindings
                else [{"exchange": declared.dlx, "routing_key": "#"}]
            ),
        }
        for spec in declared.queues
    ]
    queues.append(
        {
            "name": declared.retry_queue,
            "durable": True,
            "arguments": {
                "x-queue-type": "quorum",
                "x-message-ttl": RETRY_TTL_MS,
                "x-dead-letter-exchange": declared.exchange,
            },
            "bindings": [{"exchange": declared.retry_exchange, "routing_key": "#"}],
        }
    )
    return {
        "exchange_declarations": [
            {"name": declared.exchange, "type": "topic", "durable": True},
            {"name": declared.dlx, "type": "fanout", "durable": True},
            {"name": declared.retry_exchange, "type": "fanout", "durable": True},
        ],
        "queue_declarations": sorted(queues, key=lambda queue: queue["name"]),
        "publisher": {
            "confirms": True,
            "content_type": "application/json",
            "delivery_mode": "PERSISTENT",
            "priority_preserved": True,
            "publish_timeout_seconds": 10,
        },
        "retry_ttl_ms": RETRY_TTL_MS,
        "max_transient_attempts": MAX_TRANSIENT_ATTEMPTS,
        "settlement_contract": {
            "success": "ack",
            "defer_after_republish": "ack_unincremented",
            "transient_after_republish": "ack_incremented",
            "transient_exhausted": "reject_no_requeue_to_dlx",
            "permanent": "reject_no_requeue_to_dlx",
        },
        "integration_evidence": [
            "tests/integration/test_news_bus_rabbitmq.py::test_declarations_confirms_and_publisher_properties_match_the_runtime_contract",
            "tests/integration/test_news_bus_rabbitmq.py::test_topology_is_three_queues_one_dlq_one_retry_lane",
            "tests/integration/test_news_bus_rabbitmq.py::test_consume_handles_up_to_prefetch_messages_concurrently",
            "tests/integration/test_news_bus_rabbitmq.py::test_transient_is_counted_defer_is_not_and_permanent_dead_letters",
        ],
    }


def _runner_declarations() -> dict[str, Any]:
    enabled_news = NewsPipeline(
        receiver=object(),  # type: ignore[arg-type]
        recovery=object(),  # type: ignore[arg-type]
        deduper=object(),  # type: ignore[arg-type]
        triage=object(),  # type: ignore[arg-type]
        deliverer=object(),  # type: ignore[arg-type]
        janitor=object(),  # type: ignore[arg-type]
        instruments=object(),  # type: ignore[arg-type]
        quotes=object(),  # type: ignore[arg-type]
        reactions=object(),  # type: ignore[arg-type]
    )
    trading = build_pipeline(
        db=object(),
        config=TradingConfig(),
        bars=lambda _venue: None,
        candidate_projection=lambda *_: ((), ()),
        instrument_projection=lambda *_: (),
    )
    return {
        "ordered_task_names": list(worker_task_names(news_pipeline=enabled_news, trading_pipeline=trading)),
        "prefetch_resolved_defaults": {
            "news.raw": 1,
            "news.triage": Settings().news.triage.concurrency,
            "news.deliver": 1,
        },
        "integration_evidence": [
            "tests/architecture/test_worker_task_contract.py::test_enabled_worker_task_names_are_the_runtime_declarations",
            "tests/integration/test_workers_runtime_v2.py",
        ],
    }


def _program_contract() -> dict[str, Any]:
    artifact = load_stable_program_artifact()
    return {
        "schema_version": PROGRAM_SCHEMA_VERSION,
        "factory_id": PROGRAM_FACTORY_ID,
        "program_version": PROGRAM_VERSION,
        "learning_epoch": PROGRAM_LEARNING_EPOCH,
        "program_sha256": artifact.program_sha256,
        "dependency_lock_sha256": PROGRAM_DEPENDENCY_LOCK_SHA256,
        "topology_sha256": PROGRAM_TOPOLOGY_SHA256,
        "adapter_sha256": PROGRAM_ADAPTER_SHA256,
        "normalizer_sha256": PROGRAM_NORMALIZER_SHA256,
        "assembler_sha256": PROGRAM_ASSEMBLER_SHA256,
        "input_contract_sha256": PROGRAM_INPUT_CONTRACT_SHA256,
        "renderer_sha256": PROGRAM_RENDERER_SHA256,
        "policy_version": TRIAGE_POLICY_VERSION,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "metric_id": METRIC_ID,
        "compiler_id": COMPILER_ID,
        "compiler_schemas": sorted(
            (
                COMPILER_INPUT_SCHEMA,
                COMPILER_CORPUS_SCHEMA,
                COMPILER_ENDPOINT_IDENTITY_SCHEMA,
                COMPILER_ROLE_BINDING_SCHEMA,
                COMPILER_RECEIPT_SCHEMA,
                COMPILER_RECEIPT_CHAIN_SCHEMA,
                COMPILER_RUNNER_RECEIPTS_SCHEMA,
            )
        ),
    }


def _projection_contract() -> dict[str, Any]:
    schemas = {
        "news": NewsTradeCandidate.model_json_schema(),
        "oi": OiTradeCandidate.model_json_schema(),
        "manifest": TradingCaseManifest.model_json_schema(),
    }
    return {
        "manifest_version": TRADING_MANIFEST_VERSION,
        "policy_version": TRADING_POLICY_VERSION,
        "program_version": TRADING_PROGRAM_VERSION,
        "schema_sha256": {name: _canonical_sha(schema) for name, schema in schemas.items()},
        "point_in_time_reads": {
            "oi": {
                "fields": sorted(_oi_row()),
                "after_created_at_ms": "exclusive",
                "until_created_at_ms": "inclusive",
                "order": ["verdict_created_at_ms", "event_id"],
                "generation": {
                    "learning_epoch": "program_v6",
                    "program_version": "news_oi_signal_v1",
                    "policy_version": "news_triage_policy_v10",
                    "editorial_origin": "telemetry_deterministic",
                },
            },
            "news": {
                "fields": sorted(_news_row()),
                "after_created_at_ms": "exclusive",
                "until_created_at_ms": "inclusive",
                "order": ["verdict_created_at_ms", "event_id"],
                "generation": {
                    "learning_epoch": "program_v6",
                    "program_version": "news_semantic_program_v4",
                    "policy_version": "news_triage_policy_v10",
                    "editorial_origin": "model",
                },
            },
            "instrument": {
                "venues": ["binance.perp", "hl.perp"],
                "status": "trading",
                "instrument_class": "crypto",
                "order": ["venue", "quote_preference", "symbol_length", "venue_symbol"],
            },
        },
        "integration_evidence": [
            "tests/integration/test_trading_ledger.py::test_oi_projection_exposes_only_post_epoch_v10_judgments",
            "tests/integration/test_trading_ledger.py::test_model_projection_requires_v4_model_editorial_in_the_v6_epoch",
            "tests/integration/test_trading_ledger.py::test_news_to_trading_projection_freezes_fields_boundaries_order_and_content_identity",
            "tests/integration/test_trading_ledger.py::test_a_qualifying_frame_becomes_one_paper_order_with_no_model_call",
        ],
    }


def _news_replay_snapshot() -> dict[str, Any]:
    hits = json.loads((ROOT / "tests" / "fixtures" / "news_v3_hits_sample.json").read_text(encoding="utf-8"))
    report = replay_hits(hits, watchlist_symbols=frozenset({"BTC", "ETH", "NVDA"}))
    return {
        "report_sha256": _canonical_sha(report),
        "counts": report["counts"],
        "candidate_share_of_items": report["candidate_share_of_items"],
        "storylines": report["storylines"],
    }


def _news_event() -> dict[str, Any]:
    return {
        "event_id": "event-baseline-doge",
        "evidence_version": 3,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-doge-1",
        "reporting_origin": "Reuters",
        "provenance": ["strategy:baseline"],
        "engine_type": "news",
        "leader_title": "Dogecoin gains direct payment access after a new integration",
        "raw_first_line": "Dogecoin gains direct payment access",
        "leader_description": "The integration makes DOGE available to a new payment network.",
        "leader_url": "https://example.invalid/doge",
        "leader_published_at_ms": NOW_MS,
        "opened_at_ms": NOW_MS,
        "member_count": 2,
        "family": "general",
        "provider_score_max": 88,
        "provider_metadata": {"coins": [{"symbol": "DOGE", "grade": "A"}]},
        "queue_priority": "high",
        "comparison_title": "dogecoin gains direct payment access after a new integration",
        "asset_class": "crypto",
        "grounded_assets": ["DOGE"],
        "macro_lexicon": False,
        "pr_template": False,
        "admission": "candidate",
        "storyline_key": "asset:DOGE",
    }


def _news_flow_snapshot() -> dict[str, Any]:
    event = _news_event()
    context = TriageContext.from_card(
        event,
        watchlist=("BTC",),
        told_rows=(),
        now_ms=NOW_MS + 4_000,
        queue_lag_ms=4_000,
    )
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="partnership",
        assets=[{"symbol": "DOGE", "market_type": "spot", "role": "primary"}],
        direction="bullish",
        scope="single_name",
        magnitude=2,
        actionable=True,
        confidence=0.83,
        decision="push",
        audience="crypto",
        headline_zh="狗狗币新增直接支付入口",
        title_zh="",
        why_zh="新集成扩大可用渠道，可能带来新增交易与支付需求。",
    )
    editorial = EditorialEnvelope.issue(editorial_origin="model", relevance=trade_relevance())
    artifact = load_stable_program_artifact()
    trace = ProgramTrace(
        program_version=PROGRAM_VERSION,
        program_sha256=artifact.program_sha256,
        context_sha256=context.selected_context_sha256(),
        factory_id=PROGRAM_FACTORY_ID,
        topology_sha256=PROGRAM_TOPOLOGY_SHA256,
        adapter_sha256=PROGRAM_ADAPTER_SHA256,
        assembler_sha256=PROGRAM_ASSEMBLER_SHA256,
        verdict_sha256=canonical_sha(verdict.model_dump(mode="json")),
        editorial_sha256=editorial.editorial_sha256,
        answering_route="primary",
    )
    semantic = SemanticJudgment(
        verdict=verdict,
        editorial=editorial,
        program_version=PROGRAM_VERSION,
        program_sha256=artifact.program_sha256,
        trace=trace,
        usage=ProgramUsage(
            wall_latency_ms=0,
            call_count=0,
            physical_call_count=0,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            total_tokens=0,
        ),
    )
    scored = semantic.scored()
    decision = news_decide(
        scored,
        GateFacts(
            grounded_assets=("DOGE",),
            watchlist_symbols=frozenset({"BTC"}),
            admission="candidate",
            source_age_s=4,
        ),
        None,
    )
    card = render_first_card(
        event=event,
        verdict=verdict.model_dump(mode="json"),
        decision=decision.final,
        grounded_assets=("DOGE",),
    )
    receipt = ReaderReceipt.from_delivery({"state": "sent", "settled_at_ms": NOW_MS + 5_000, "card": card})
    flow = {
        "triage_context": context.model_dump(mode="json"),
        "model_visible_event_semantics": context.event_semantics_payload(),
        "scored_judgment": scored.model_dump(mode="json"),
        "editorial_envelope": editorial.model_dump(mode="json"),
        "verdict": verdict.model_dump(mode="json"),
        "decision": asdict(decision),
        "card": card,
        "delivery_receipt": receipt.model_dump(mode="json"),
    }
    return {"flow": flow, "snapshot_sha256": _canonical_sha(flow)}


def _oi_row() -> dict[str, Any]:
    return {
        "event_id": "event-oi-doge",
        "verdict_created_at_ms": NOW_MS,
        "observed_at_ms": NOW_MS,
        "symbol": "DOGE",
        "venue": "hyperliquid",
        "direction": "rise",
        "oi_change_bps": 320,
        "oi_value_usd": 73_010_000,
        "whale_long_profit_bps": 9_900,
        "whale_oi_ratio_bps": 21_097,
        "rank_in_window": 1,
        "metric_version": "oi_signal_v1",
        "learning_epoch": "program_v6",
        "program_version": "news_oi_signal_v1",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
        "final_decision": "push",
        "ingest_mode": "live",
    }


def _news_row() -> dict[str, Any]:
    return {
        "event_id": "event-news-doge",
        "verdict_created_at_ms": NOW_MS - 60_000,
        "opened_at_ms": NOW_MS - 61_000,
        "evidence_version": 3,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-doge-1",
        "comparison_fingerprint": "fp-doge",
        "source_artifact_id": "x:123",
        "source_published_at_ms": NOW_MS - 62_000,
        "final_decision": "push",
        "verdict": {
            "novelty": "new_fact",
            "event_type": "partnership",
            "assets": [{"symbol": "DOGE", "role": "primary"}],
            "direction": "bullish",
            "scope": "single_name",
            "magnitude": 2,
            "headline_zh": "狗狗币新增直接支付入口",
            "why_zh": "新集成扩大可用渠道。",
        },
        "grounded_assets": ["DOGE"],
        "asset_class": "crypto",
        "learning_epoch": "program_v6",
        "program_version": "news_semantic_program_v4",
        "program_sha256": "1" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "model",
        "editorial_sha256": "2" * 64,
        "scored_judgment_sha256": "3" * 64,
        "runtime_manifest_sha": "4" * 64,
        "ingest_mode": "live",
    }


async def _trading_faults(order: PreparedOrder) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    for fault in ("ack", "reject", "ambiguous", "ambiguous_lost"):
        adapter = PaperAdapter(faults=PaperFaults(script=[fault]))
        receipt = await adapter.submit(order.model_copy(update={"order_id": f"order-{fault}"}))
        current_order = order.model_copy(update={"order_id": f"order-{fault}"})
        observation = await adapter.observe(current_order)
        entry: dict[str, Any] = {
            "attempts": adapter.attempts,
            "ledger_state": receipt.state,
            "receipt": receipt.model_dump(mode="json"),
            "remote_present": observation is not None,
        }
        if receipt.state == "AMBIGUOUS":
            entry["reconciliation_result"] = (
                {"state": "OPEN", "reason": "resolved_by_read"}
                if observation is not None
                else {"state": "REJECTED", "reason": "proven_absent"}
            )
        snapshots[fault] = entry
    return snapshots


def _trading_manifest() -> tuple[OiTradeCandidate, NewsTradeCandidate, TradingCaseManifest]:
    blacklist = Blacklist.from_rows([])
    oi = oi_candidate(OiCandidateRow(**_oi_row()), now_ms=NOW_MS, blacklist=blacklist)
    projected_news = news_candidate(NewsCandidateRow(**_news_row()), now_ms=NOW_MS, blacklist=blacklist)
    assert isinstance(oi, OiTradeCandidate)
    assert isinstance(projected_news, NewsTradeCandidate)
    instrument = resolve_instrument(
        [
            {
                "venue": "hl.perp",
                "venue_symbol": "DOGE",
                "base_symbol": "DOGE",
                "instrument_class": "crypto",
                "quote_asset": "USDC",
                "status": "trading",
                "last_seen_ms": NOW_MS,
            },
            {
                "venue": "binance.perp",
                "venue_symbol": "DOGEUSDT",
                "base_symbol": "DOGE",
                "instrument_class": "crypto",
                "quote_asset": "USDT",
                "status": "trading",
                "last_seen_ms": NOW_MS,
            },
        ],
        priority=("binance", "hyperliquid"),
        observed_at_ms=NOW_MS,
    )
    assert isinstance(instrument, InstrumentRef)
    bars = (
        Bar(open_at_ms=NOW_MS - 3_900_000, close_at_ms=NOW_MS - 3_600_000, close=Decimal("100")),
        Bar(open_at_ms=NOW_MS - 300_000, close_at_ms=NOW_MS, close=Decimal("103")),
    )
    move = pre_move_bps(bars, anchor_at_ms=NOW_MS)
    regime = assess(oi_direction=oi.oi_direction, move=move)
    manifest = TradingCaseManifest(
        case_kind="news_oi",
        underlying_key="crypto:DOGE",
        base_symbol="DOGE",
        cutoff_ms=NOW_MS,
        oi=oi,
        news=projected_news,
        regime=regime,
        instrument=instrument,
        mark_price=Decimal("103"),
        pre_move_bps=move,
    )
    return oi, projected_news, manifest


def _trading_order(
    manifest: TradingCaseManifest,
    oi: OiTradeCandidate,
) -> tuple[TradeDecision, Any, PreparedOrder]:
    model_decision = TradeDecision(
        decision="long",
        directness="direct",
        surprise=3,
        price_in=0,
        alignment="aligned",
        horizon="hours",
        reason_code="new_distribution_channel",
        thesis_zh="支付入口可能带来新增需求。",
        invalidation_zh="新增使用未出现。",
    )
    outcome = trading_decide(
        case_kind="news_oi",
        mode="paper",
        regime=manifest.regime.regime,
        decision=model_decision,
        whale_long_profit_bps=oi.whale_long_profit_bps,
        oi_value_usd=oi.oi_value_usd,
    )
    side = side_to_order_side(outcome.decision)
    assert side is not None
    sized = size_order(
        side=side,
        market=MarketContext(
            instrument=manifest.instrument,
            mark_price=manifest.mark_price,
            observed_at_ms=manifest.cutoff_ms,
            pre_move_bps=manifest.pre_move_bps,
            pre_move_lookback_ms=3_600_000,
            spread_bps=None,
            spread_available=False,
        ),
        mode="paper",
    )
    assert isinstance(sized, SizedOrder) and not isinstance(sized, RiskRejection)
    payload = build_payload(
        instrument_exchange_id=manifest.instrument.exchange_id,
        provider_symbol=manifest.instrument.provider_symbol,
        side=side,
        quantity=sized.quantity,
        stop_price=sized.stop_price,
        take_profit_price=sized.take_profit_price,
        hedged=False,
    )
    order = PreparedOrder(
        order_id="order-baseline",
        case_id="case-baseline",
        underlying_key=manifest.underlying_key,
        instrument=manifest.instrument,
        mode="paper",
        side=side,
        notional_usd=sized.notional_usd,
        quantity=sized.quantity,
        entry_reference=sized.entry_reference,
        stop_price=sized.stop_price,
        take_profit_price=sized.take_profit_price,
        must_close_after_ms=1_800_000,
        payload=payload,
    )
    return model_decision, outcome, order


def _trading_flow_snapshot() -> dict[str, Any]:
    oi, projected_news, manifest = _trading_manifest()
    model_decision, outcome, order = _trading_order(manifest, oi)
    faults = asyncio.run(_trading_faults(order))
    flow = {
        "news_projection": projected_news.model_dump(mode="json"),
        "oi_projection": oi.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "manifest_sha256": manifest.digest(),
        "regime": manifest.regime.model_dump(mode="json"),
        "trade_decision": model_decision.model_dump(mode="json"),
        "policy_outcome": outcome.model_dump(mode="json"),
        "prepared_order": order.model_dump(mode="json"),
        "faults_and_reconciliation": faults,
        "active_states": sorted(state.value for state in ACTIVE_ORDER_STATES),
        "terminal_states": sorted(state.value for state in TERMINAL_ORDER_STATES),
        "all_states": sorted(state.value for state in OrderState),
        "integration_evidence": [
            "tests/integration/test_trading_ledger.py::test_an_ambiguous_attempt_is_resolved_by_reading_and_never_by_resending",
            "tests/integration/test_trading_ledger.py::test_an_attempt_that_never_landed_is_proven_absent_and_frees_the_underlying",
        ],
    }
    return {"flow": flow, "snapshot_sha256": _canonical_sha(flow)}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _declared_exports(tree: ast.AST) -> list[str] | None:
    for node in getattr(tree, "body", ()):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                value = ast.literal_eval(node.value) if node.value is not None else []
                return sorted(str(item) for item in value)
    return None


def _historical_structure() -> dict[str, Any]:
    prefix = "src/tracefold/"
    paths = [
        path
        for path in _git("ls-tree", "-r", "--name-only", BASELINE_REVISION, "src/tracefold").splitlines()
        if path.endswith(".py")
    ]
    modules: dict[str, Any] = {}
    package_exports: dict[str, list[str]] = {}
    for relative in sorted(paths):
        source = _git("show", f"{BASELINE_REVISION}:{relative}")
        tree = ast.parse(source, filename=relative)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names if alias.name.startswith("tracefold"))
            elif isinstance(node, ast.ImportFrom):
                rendered = f"{'.' * node.level}{node.module or ''}"
                if node.level or rendered.startswith("tracefold"):
                    imports.add(rendered)
        modules[relative] = {
            "lines": len(source.splitlines()),
            "tracefold_imports": sorted(imports),
        }
        if relative.endswith("/__init__.py"):
            exports = _declared_exports(tree)
            if exports is not None:
                package = relative.removeprefix(prefix).removesuffix("/__init__.py").replace("/", ".")
                package_exports[f"tracefold.{package}" if package else "tracefold"] = exports
    return {
        "module_count": len(modules),
        "modules": modules,
        "package_exports": package_exports,
        "package_export_counts": {name: len(exports) for name, exports in package_exports.items()},
    }


def capture_behavior() -> dict[str, Any]:
    generated = {
        path: _sha_bytes(ROOT / path)
        for path in (
            "docs/generated/cli-help.md",
            "docs/generated/db-schema.md",
            "docs/generated/openapi.json",
            "web/src/lib/types/openapi.ts",
        )
    }
    immutable_inputs = {
        path: _sha_bytes(ROOT / path) for path in ("Dockerfile", "compose.yaml", "uv.lock", "web/package-lock.json")
    }
    return {
        "generated_artifacts_sha256": generated,
        "infrastructure_and_dependency_sha256": immutable_inputs,
        "migration_head": latest_migration_version(),
        "rabbitmq": _rabbitmq_contract(),
        "workers": _runner_declarations(),
        "program_learning": _program_contract(),
        "news_to_trading": _projection_contract(),
        "representative_news_replay": _news_replay_snapshot(),
        "representative_news_flow": _news_flow_snapshot(),
        "representative_trading_flow": _trading_flow_snapshot(),
    }


def assert_matches_baseline() -> None:
    expected = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    current = _jsonable(capture_behavior())
    if (
        expected.get("schema_version") != SCHEMA_VERSION
        or expected.get("baseline_revision") != BASELINE_REVISION
        or not expected.get("historical_structure")
    ):
        raise AssertionError("Issue #162 baseline identity is not the frozen one; do not regenerate it in place.")

    changed = dict(_changed_leaves(expected.get("behavior_and_runtime_contracts"), current))
    undeclared = sorted(path for path in changed if path not in INTENTIONAL_DRIFT)
    if undeclared:
        raise AssertionError(
            "Issue #162 behavior/runtime baseline drifted on leaves nobody declared: "
            + ", ".join(undeclared)
            + ". Either the change was unintended, or declare each leaf in INTENTIONAL_DRIFT with its reason "
            "and the exact value it drifted to."
        )
    stale = sorted(path for path in INTENTIONAL_DRIFT if path not in changed)
    if stale:
        raise AssertionError(
            "INTENTIONAL_DRIFT still declares leaves that now match the frozen baseline: "
            + ", ".join(stale)
            + ". Remove the stale entries; an exemption that outlives its cause silently widens the guard."
        )
    moved_again = sorted(
        f"{path} is {changed[path]!r}, declared {declared!r} for {reason}"
        for path, (reason, declared) in INTENTIONAL_DRIFT.items()
        if changed.get(path) != declared
    )
    if moved_again:
        raise AssertionError(
            "INTENTIONAL_DRIFT leaves moved past their declared values: "
            + "; ".join(moved_again)
            + ". A declared exemption covers one known value, never 'this leaf may now change freely'."
        )


def _write() -> None:
    current = {
        "schema_version": SCHEMA_VERSION,
        "baseline_revision": BASELINE_REVISION,
        "behavior_and_runtime_contracts": capture_behavior(),
        "historical_structure": _historical_structure(),
    }
    OUTPUT.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare current behavior/runtime contracts with the frozen #162 baseline",
    )
    args = parser.parse_args()
    if args.check:
        try:
            assert_matches_baseline()
        except AssertionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    _write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
