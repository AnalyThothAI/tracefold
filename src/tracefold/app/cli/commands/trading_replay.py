"""App-owned News -> Trading composition for the bounded OI BAR replay."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from psycopg import Error as PostgresError

from tracefold.app.cli.replay_artifacts import publish_replay_artifact, verify_replay_artifact
from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import (
    CANDIDATE_GATE_VERSION,
    trading_config_from_settings,
    trading_settings_gate,
    trading_settings_strategies,
)
from tracefold.integrations.nautilus.config import installed_nautilus_wheel_identity
from tracefold.integrations.nautilus.replay import run_bar_episode
from tracefold.integrations.venues import (
    VenueBar,
    VenueExpectedError,
    fetch_binance_bars,
    fetch_hyperliquid_bars,
)
from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import oi_candidate
from tracefold.trading.candidate.routing import resolve_instrument
from tracefold.trading.contracts import OiTradeCandidate, canonical_sha256
from tracefold.trading.decision.regime import RegimePolicy
from tracefold.trading.execution_policy import EXECUTION_POLICY_SHA256
from tracefold.trading.intent import INTENT_POLICY_SHA256, capability_instrument_id
from tracefold.trading.replay import (
    DirectionalReplayPlan,
    ReplayArtifactV1,
    ReplayBarV1,
    ReplayMarketSlice,
    ReplayReceiptV1,
    ReplaySpecV1,
    ReplayTerminalOutcomeV1,
    evaluate_replay_market_slices,
    plan_replay_source,
    replay_strategy_identity,
    summarize_replay_outcomes,
    unresolved_replay_instrument,
)
from tracefold.trading.research.oi_replay import OiReplayOutcome, replay_oi_facts
from tracefold.trading.strategy.root import TradingStrategy

REPLAY_ROW_LIMIT = 20_000


def handle_oi_replay(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    from tracefold.app.workers.wiring.news_to_trading import to_oi_candidate_row

    days = int(getattr(args, "days", 7) or 7)
    if days <= 0:
        return 2, {"ok": False, "error": "replay_days_invalid"}
    if str(getattr(args, "fidelity", "bar_v1") or "") != "bar_v1":
        return 2, {"ok": False, "error": "replay_fidelity_invalid"}
    start_ms = now_ms - days * 86_400_000
    requested_venues = tuple(
        sorted(
            {
                venue.strip()
                for venue in str(getattr(args, "venues", "binance.perp,hl.perp") or "").split(",")
                if venue.strip()
            }
        )
    )
    if not requested_venues or any(venue not in {"binance.perp", "hl.perp"} for venue in requested_venues):
        return 2, {"ok": False, "error": "replay_venues_invalid"}
    strategy_id = str(getattr(args, "strategy", "oi_smart_money_momentum_v1") or "")
    strategy = _strategy(settings, strategy_id)
    if strategy is None or "oi" not in strategy.trigger_kinds:
        return 2, {"ok": False, "error": "replay_strategy_invalid"}

    gate = trading_settings_gate(settings)
    runtime_config = trading_config_from_settings(settings)
    try:
        with repositories(settings, role="workers") as repos, repos.transaction():
            repos.trading.blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        with repositories(settings, role="serve") as repos, repos.transaction():
            repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot, blacklist = repos.trading.replay_authority_snapshot(now_ms=now_ms)
            fact_rows = repos.news.trade_candidate_oi_rows(
                metric_version=NEWS_OI_METRIC_VERSION,
                after_created_at_ms=start_ms,
                until_created_at_ms=now_ms,
                limit=REPLAY_ROW_LIMIT,
            )
            facts = [to_oi_candidate_row(row) for row in fact_rows]
            if len(facts) >= REPLAY_ROW_LIMIT:
                return 1, {"ok": False, "error": "replay_source_truncated"}
            report = replay_oi_facts(
                facts,
                gate=gate,
                strategy=cast(Any, strategy),
                blacklist=Blacklist.from_rows([]),
                now_ms=now_ms,
            )
            parsed = _parsed_sources(facts)
            plans, immediate, research_rows = _plans(
                repos,
                report.outcomes,
                parsed,
                strategy=strategy,
                requested_venues=requested_venues,
            )
    except RuntimeError as exc:
        return 1, {"ok": False, "error": str(exc)}
    except (OSError, PostgresError):
        return 1, {"ok": False, "error": "replay_authority_unavailable"}

    market_slices = asyncio.run(_fetch_market_slices(plans, now_ms=now_ms, regime_policy=runtime_config.regime))
    outcomes = immediate + evaluate_replay_market_slices(
        market_slices,
        strategy=strategy,
        snapshot=snapshot,
        blacklist=blacklist,
        run_episode=run_bar_episode,
        regime_policy=runtime_config.regime,
        target_notional=runtime_config.fixed_notional_usd,
    )
    outcomes.sort(key=lambda row: (row.source_identity, row.strategy_identity, row.scenario_venue or ""))
    if len(outcomes) != len(facts) or len({row.source_identity for row in outcomes}) != len(facts):
        return 1, {"ok": False, "error": "replay_terminal_accounting_invalid"}

    identity = runtime_identity()
    strategy_identity = replay_strategy_identity(strategy)
    market_rows = [item.artifact_row() for item in market_slices]
    scenario_rows = [
        {
            "source_identity": plan.source.source_key,
            "venue": plan.venue,
            "instrument_id": plan.instrument_id,
        }
        for plan in plans
    ]
    source_payload = [dict(row) for row in facts]
    research_payload = sorted(
        {canonical_sha256(row): row for row in research_rows}.values(),
        key=lambda row: (str(row["venue"]), str(row["venue_symbol"]), str(row["base_symbol"])),
    )
    spec = ReplaySpecV1(
        start_ms=start_ms,
        end_ms=now_ms,
        source_query_contract_sha256=canonical_sha256(
            {
                "projection": "news_trade_projection_v8",
                "metric_version": NEWS_OI_METRIC_VERSION,
                "after_created_at_ms": start_ms,
                "until_created_at_ms": now_ms,
                "limit": REPLAY_ROW_LIMIT,
                "order": "verdict_created_at_ms_desc_event_id_desc",
            }
        ),
        source_facts_sha256=canonical_sha256(source_payload),
        market_slice_sha256=canonical_sha256(market_rows),
        research_universe_sha256=canonical_sha256(research_payload),
        execution_capability_snapshot_sha256=snapshot.snapshot_sha256,
        replay_scenarios_sha256=canonical_sha256(scenario_rows),
        blacklist_snapshot_sha256=blacklist.snapshot_sha256,
        candidate_gate_version=CANDIDATE_GATE_VERSION,
        candidate_gate_config_sha256=gate.digest,
        regime_lookback_ms=runtime_config.regime.lookback_ms,
        regime_min_price_move_bps=runtime_config.regime.min_price_move_bps,
        regime_max_price_move_bps=runtime_config.regime.max_price_move_bps,
        regime_bar_gap_tolerance_ms=runtime_config.regime.bar_gap_tolerance_ms,
        target_notional_usd=runtime_config.fixed_notional_usd,
        strategy_identities=[
            {
                "strategy_id": str(strategy.strategy_id),
                "strategy_version": strategy.strategy_version,
                "strategy_config_sha256": strategy.config_digest,
                "strategy_identity": strategy_identity,
            }
        ],
        intent_policy_sha256=INTENT_POLICY_SHA256,
        execution_policy_sha256=EXECUTION_POLICY_SHA256,
        app_revision=identity.runtime_revision,
        app_image_digest=identity.image_digest,
        nautilus_wheel_identity=installed_nautilus_wheel_identity(),
        venue_scenarios=[{"venue": venue, "mode": "source_native"} for venue in requested_venues],
        fee_model={"version": "maker_taker_v1", "maker_bps": "5", "taker_bps": "5"},
        funding_model={"version": "unavailable_v1", "funding": "null"},
        fill_model={
            "version": "nautilus_bar_ohlcv_v2",
            "entry_policy_quote": "containing_bar_open_at_decision_proxy",
            "entry": "first_bar_close_after_decision_then_engine_market_fill",
            "stop_touch": "bar_low_then_engine_market_fill",
            "gap": "engine_bar_execution",
            "holding": "first_bar_at_or_after_180s_then_engine_market_fill",
        },
        slippage_model={"version": "engine_bar_execution_v1", "configured_bps": "0"},
        latency_model={"version": "bar_observation_v1", "configured_ms": "0"},
    )
    summary = summarize_replay_outcomes(outcomes)
    artifact = ReplayArtifactV1(
        run_id=spec.run_id,
        spec=spec,
        blacklist_snapshot_payload=blacklist,
        source_facts=source_payload,
        market_slices=market_rows,
        outcomes=outcomes,
        summary=summary,
    )
    try:
        existing = _existing_receipt(settings, spec.run_id)
    except (OSError, PostgresError):
        return 1, {"ok": False, "error": "replay_receipt_read_failed"}
    if existing is not None:
        try:
            verify_replay_artifact(Path(existing.artifact_path), expected_sha256=existing.artifact_sha256)
        except RuntimeError as exc:
            return 1, {"ok": False, "error": str(exc)}
        return 0, _success(existing, summary, reused=True)

    try:
        artifact_path, artifact_sha256 = publish_replay_artifact(
            Path(str(getattr(args, "out", "artifacts/trading-replay"))).resolve(),
            artifact,
        )
    except (OSError, RuntimeError) as exc:
        return 1, {"ok": False, "error": str(exc) or "replay_artifact_publish_failed"}
    receipt = ReplayReceiptV1(
        run_id=spec.run_id,
        spec_sha256=spec.run_id,
        created_at_ms=now_ms,
        artifact_path=str(artifact_path),
        artifact_sha256=artifact_sha256,
        source_count=len(facts),
        directional_count=sum(row.decision == "DIRECTIONAL" for row in outcomes),
        terminal_outcome_count=len(outcomes),
    )
    try:
        with repositories(settings, role="workers") as repos, repos.transaction():
            inserted = repos.trading.insert_replay_receipt(receipt)
            stored = receipt if inserted else ReplayReceiptV1.model_validate(repos.trading.replay_receipt(spec.run_id))
            if stored.artifact_sha256 != artifact_sha256 or stored.artifact_path != str(artifact_path):
                raise RuntimeError("replay_receipt_conflict")
    except (RuntimeError, ValueError) as exc:
        return 1, {"ok": False, "error": str(exc)}
    except (OSError, PostgresError):
        return 1, {"ok": False, "error": "replay_receipt_write_failed"}
    return 0, _success(receipt, summary, reused=False)


def _strategy(settings: Any, strategy_id: str) -> TradingStrategy | None:
    return next(
        (strategy for strategy in trading_settings_strategies(settings) if strategy.strategy_id == strategy_id),
        None,
    )


def _parsed_sources(facts: list[Any]) -> dict[str, OiTradeCandidate]:
    parsed: dict[str, OiTradeCandidate] = {}
    for row in facts:
        candidate = oi_candidate(row)
        if isinstance(candidate, OiTradeCandidate):
            parsed[candidate.source_key] = candidate
    return parsed


def _plans(
    repos: Any,
    funnel_outcomes: list[OiReplayOutcome],
    parsed: dict[str, OiTradeCandidate],
    *,
    strategy: TradingStrategy,
    requested_venues: tuple[str, ...],
) -> tuple[list[DirectionalReplayPlan], list[ReplayTerminalOutcomeV1], list[dict[str, Any]]]:
    from tracefold.app.workers.wiring.news_to_trading import news_trade_instruments

    plans: list[DirectionalReplayPlan] = []
    immediate: list[ReplayTerminalOutcomeV1] = []
    research_rows: list[dict[str, Any]] = []
    for outcome in funnel_outcomes:
        planned = plan_replay_source(
            outcome,
            parsed,
            strategy=strategy,
            requested_venues=requested_venues,
        )
        if isinstance(planned, ReplayTerminalOutcomeV1):
            immediate.append(planned)
            continue
        rows = list(
            news_trade_instruments(
                repos,
                planned.source.base_symbol,
                (planned.venue,),
                observed_at_ms=planned.source.observed_at_ms,
            )
        )
        research_rows.extend(dict(row) for row in rows)
        exchange = "binance" if planned.venue == "binance.perp" else "hyperliquid"
        instrument = resolve_instrument(
            rows,
            priority=(exchange,),
            observed_at_ms=planned.source.observed_at_ms,
        )
        if instrument is None:
            immediate.append(unresolved_replay_instrument(planned, strategy=strategy))
            continue
        replay_id = (
            capability_instrument_id(instrument)
            if planned.venue == "binance.perp"
            else f"{instrument.provider_symbol}-PERP.HYPERLIQUID"
        )
        plans.append(
            DirectionalReplayPlan(
                source=planned.source,
                instrument=instrument,
                venue=planned.venue,
                instrument_id=replay_id,
            )
        )
    return plans, immediate, research_rows


async def _fetch_market_slices(
    plans: list[DirectionalReplayPlan],
    *,
    now_ms: int,
    regime_policy: RegimePolicy,
) -> list[ReplayMarketSlice]:
    semaphore = asyncio.Semaphore(8)

    async def fetch(plan: DirectionalReplayPlan) -> ReplayMarketSlice:
        start_ms = plan.source.observed_at_ms - regime_policy.lookback_ms - regime_policy.bar_gap_tolerance_ms
        end_ms = max(plan.source.observed_at_ms, plan.source.verdict_created_at_ms) + 1_200_000
        try:
            async with semaphore:
                fetched = (
                    await fetch_binance_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    if plan.venue == "binance.perp"
                    else await fetch_hyperliquid_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                )
        except VenueExpectedError:
            return ReplayMarketSlice(plan, [], "market_history_missing", start_ms, end_ms)
        bars = [_to_replay_bar(plan, bar) for bar in fetched if bar.close_at_ms <= min(end_ms, now_ms)]
        reason = None if bars else "market_history_missing"
        return ReplayMarketSlice(plan, bars, reason, start_ms, end_ms)

    return list(await asyncio.gather(*(fetch(plan) for plan in plans)))


def _to_replay_bar(plan: DirectionalReplayPlan, bar: VenueBar) -> ReplayBarV1:
    return ReplayBarV1(
        venue=plan.venue,
        instrument_id=plan.instrument_id,
        open_at_ms=bar.open_at_ms,
        close_at_ms=bar.close_at_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _existing_receipt(settings: Any, run_id: str) -> ReplayReceiptV1 | None:
    with repositories(settings, role="serve") as repos:
        row = repos.trading.replay_receipt(run_id)
    return None if row is None else ReplayReceiptV1.model_validate(row)


def _success(receipt: ReplayReceiptV1, summary: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "terminal": "OI_BAR_REPLAY_ATTRIBUTED",
            "run_id": receipt.run_id,
            "artifact_path": receipt.artifact_path,
            "artifact_sha256": receipt.artifact_sha256,
            "reused": reused,
            "summary": summary,
        },
    }


__all__ = ["REPLAY_ROW_LIMIT", "handle_oi_replay"]
