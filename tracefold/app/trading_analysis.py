"""LIVE Case intake, one frozen forecast, six policies, and cold paired labels."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx

from tracefold.app.trading_assessor import TradingAssessor
from tracefold.app.trading_case_prepare import CasePreparer, prune_snapshots
from tracefold.app.trading_intake import (
    _assets,
    _configured_universe,
    _select_from_connection,
    catalyst_assets,
    public_update,
)
from tracefold.platform.postgres.runtime_processes import AnalysisProcessDetail, RuntimeProcesses
from tracefold.trading.engine.case_view import CaseView, case_view_from_record
from tracefold.trading.engine.forecast import POLICY_VERSION, PolicyConfig, all_policy_decisions
from tracefold.trading.engine.marketdata import MarketDataPort, analysis_market_request
from tracefold.trading.engine.paper import BAR_MS, GEOMETRY_VERSION, Bar, LegGeometry, PaperLeg, both_legs
from tracefold.trading.executor.core import SignalV4

_LOG = logging.getLogger(__name__)
_PAPER_BUFFER_MS = 120_000


def _clock_ms() -> int:
    return int(time.time() * 1000)


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class AnalysisRunner:
    def __init__(
        self,
        *,
        settings: Any,
        market_data: MarketDataPort,
        assessor: TradingAssessor | None,
        program_sha: str,
        raw_root: Path,
        fault_code: str | None = None,
    ) -> None:
        self.instance_id = str(uuid4())
        self._runtime_started = False
        self.settings = settings
        self.market_data = market_data
        self._db_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="analysis-db")
        self.preparer = CasePreparer(market_data, raw_root, self._db_pool)
        self.assessor = assessor
        self.program_sha = program_sha
        self.fault_code = fault_code
        self._universe = _configured_universe(settings)
        self._lease_ms = (settings.trading.analysis.model_timeout_seconds + 30) * 1_000
        self._config_digest = _sha(
            {
                "analysis": settings.trading.analysis.model_dump(mode="json"),
                "account_slot": settings.trading.execution.account_slot,
            }
        )
        self._active: set[asyncio.Task[bool]] = set()
        self._label_task: asyncio.Task[int] | None = None

    def _db(self, fn: Any, *, transaction: bool = False) -> Any:
        from tracefold.app.repository_session import repositories

        with repositories(self.settings, application_name="tracefold_analysis") as repos:
            if transaction:
                with repos.transaction():
                    return fn(repos)
            return fn(repos)

    async def _db_async(self, fn: Any, *, transaction: bool = False) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self._db_pool, partial(self._db, fn, transaction=transaction)
        )

    async def relay_once(self, *, batch_size: int = 64) -> int:
        events = await self._db_async(lambda repos: repos.news.unacknowledged_trade_events(limit=batch_size))
        for event in events:
            try:
                if event["kind"] == "source_update":
                    update = public_update(event)
                    disposition = await self._db_async(
                        lambda repos, update=update, event=event: repos.trading.receive_source_update(
                            update_id=update.update_id,
                            source_fact_key=event["source_fact_key"],
                            content_revision=event["source_revision"],
                            affected_claim_refs=update.affected_claim_refs,
                            retired_claim_refs=update.retired_claim_refs,
                            payload=event["payload"],
                            payload_sha256=event["payload_sha256"],
                            now_ms=_clock_ms(),
                        ),
                        transaction=True,
                    )
                else:
                    kind = event["kind"]
                    if kind not in ("oi", "catalyst"):
                        raise ValueError("trade_event_kind_invalid")
                    assets = (
                        catalyst_assets(public_update(event)) if kind == "catalyst" else _assets(dict(event["payload"]))
                    )
                    selection = await _select_from_connection(
                        self.market_data,
                        kind,
                        assets,
                        environment="live",
                        universe=self._universe,
                        verified_routes=self.settings.trading.analysis.verified_routes,
                    )
                    _, _, disposition = await self._db_async(
                        lambda repos, kind=kind, event=event, selection=selection: repos.trading.accept_trigger(
                            kind=kind,
                            source_fact_key=event["source_fact_key"],
                            source_revision=event["source_revision"],
                            payload_sha256=event["payload_sha256"],
                            payload=event["payload"],
                            selection=selection,
                            now_ms=_clock_ms(),
                            root_ttl_ms=self.settings.trading.analysis.root_ttl_seconds * 1_000,
                        ),
                        transaction=True,
                    )
                if disposition == "source_conflict":
                    _LOG.warning("trade_event_source_conflict", extra={"source_fact_key": event["source_fact_key"]})
                await self._db_async(
                    lambda repos, event=event: repos.news.acknowledge_trade_event(
                        event_id=event["event_id"], payload_sha256=event["payload_sha256"], now_ms=_clock_ms()
                    ),
                    transaction=True,
                )
            except TimeoutError:
                continue
            except (KeyError, TypeError, ValueError):
                await self._db_async(
                    lambda repos, event=event: repos.news.reject_trade_event(
                        event_id=event["event_id"],
                        payload_sha256=event["payload_sha256"],
                        reason="trade_event_payload_invalid",
                    ),
                    transaction=True,
                )
        return len(events)

    async def _publication_reason(self, case: dict[str, Any]) -> str | None:
        execution = self.settings.trading.execution
        if not self.settings.trading.analysis.publish_signals:
            return "publish_disabled"
        if not execution.enabled or execution.binance.environment != "DEMO":
            return "runtime_unavailable"
        running = await self._db_async(
            lambda repos: RuntimeProcesses(
                repos.conn,
                kind="executor",
                key=execution.account_slot,
            ).is_running(now_ms=_clock_ms())
        )
        if not running:
            return "runtime_unavailable"
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            try:
                response = await client.get("https://demo-fapi.binance.com/fapi/v1/exchangeInfo")
                response.raise_for_status()
                symbols = response.json()["symbols"]
            except (httpx.HTTPError, KeyError, ValueError):
                return "runtime_unavailable"
        if not any(
            row.get("symbol") == case["native_symbol"]
            and row.get("baseAsset") == str(case["asset_id"]).split(":", 1)[-1]
            and row.get("quoteAsset") == "USDT"
            and row.get("contractType") == "PERPETUAL"
            and row.get("status") == "TRADING"
            for row in symbols
        ):
            return "execution_venue_unlisted"
        return None

    def _signal(self, case: dict[str, Any], view: CaseView, action: str, now_ms: int) -> SignalV4:
        policy_id = self.settings.trading.analysis.active_policy
        decision_id = _sha((case["case_id"], self.program_sha, policy_id, POLICY_VERSION))
        return SignalV4(
            signal_id=_sha((decision_id, "signal_v4")),
            decision_id=decision_id,
            case_id=str(case["case_id"]),
            account_slot=self.settings.trading.execution.account_slot,
            entry_scope_id=_sha((case["trigger_id"], case["asset_id"])),
            asset_id=str(case["asset_id"]),
            native_symbol=str(case["native_symbol"]),
            mapping_semantics_digest=str(case["mapping_digest"]),
            side=cast(Any, action),
            reference_price=Decimal(str(case["reference_price"])),
            max_drift_bps=50,
            stop_bps=view.geometry.stop_bps,
            tp_bps=view.geometry.tp_bps,
            max_hold_s=view.geometry.max_hold_ms // 1_000,
            policy_id=policy_id,
            policy_version=POLICY_VERSION,
            geometry_version=view.geometry.version,
            decided_at_ns=now_ms * 1_000_000,
            expires_at_ns=(now_ms + 300_000) * 1_000_000,
        )

    async def analyze_one(self, case: dict[str, Any] | None = None) -> bool:
        if case is None:
            case = await self._db_async(
                lambda repos: repos.trading.claim_case(now_ms=_clock_ms(), lease_ms=self._lease_ms),
                transaction=True,
            )
        if case is None:
            return False
        case_id = str(case["case_id"])
        token = str(case["claim_token"])
        frozen_view = case.get("view") is not None
        try:
            if case.get("view") is not None:
                view = case_view_from_record(dict(case["view"]))
                reference_price = Decimal(str(case["reference_price"]))
            else:
                source = await self._db_async(lambda repos: repos.trading.case_trigger(case_id))
                if source is None:
                    raise ValueError("analysis_trigger_missing")
                baselines = await self._db_async(
                    lambda repos: repos.trading.pit_base_rates(
                        trigger_kind=case["trigger_kind"], known_at_ms=_clock_ms()
                    )
                )
                recent_context = await self._db_async(
                    lambda repos: repos.trading.recent_asset_context(
                        asset_id=case["asset_id"],
                        known_at_ms=_clock_ms(),
                        exclude_trigger_id=case["trigger_id"],
                    )
                )
                prepared = await self.preparer.prepare(
                    case=case,
                    source_fact=dict(source["payload"]),
                    base_rates=baselines,
                    recent_context=tuple(recent_context),
                )
                view = prepared.view
                reference_price = prepared.reference_price
                frozen = await self._db_async(
                    lambda repos: repos.trading.freeze_case(
                        case_id=case_id,
                        claim_token=token,
                        now_ms=_clock_ms(),
                        view=asdict(view),
                        raw_snapshot_ref=prepared.raw_snapshot_ref,
                        geometry_version=view.geometry.version,
                        stop_bps=view.geometry.stop_bps,
                        tp_bps=view.geometry.tp_bps,
                        half_spread_bps=view.half_spread_bps,
                        reference_price=reference_price,
                    ),
                    transaction=True,
                )
                if not frozen:
                    return True
                frozen_view = True
            started = _clock_ms()
            result = None if self.assessor is None else await self.assessor.assess(view)
            ended = _clock_ms()
            forecast = None if result is None else result.forecast
            status = (
                "provider"
                if result is None
                else "ok"
                if result.status == "complete"
                else result.error_code or "provider"
            )
            decisions = all_policy_decisions(
                view.features,
                forecast,
                PolicyConfig(view.geometry.stop_bps, view.geometry.tp_bps, view.half_spread_bps),
            )
            live_action = next(
                item for item in decisions if item.policy_id == self.settings.trading.analysis.active_policy
            )
            publication = "abstained" if live_action.action == "abstain" else await self._publication_reason(case)
            now_ms = _clock_ms()

            def settle(repos: Any) -> bool:
                if not repos.trading.claim_is_current(case_id=case_id, claim_token=token, now_ms=now_ms):
                    return False
                repos.trading.record_forecast(
                    case_id=case_id,
                    program_sha=self.program_sha,
                    route=self.settings.trading.analysis.model_name or "",
                    status=status,
                    forecast=forecast,
                    notes=() if result is None else result.notes,
                    usage={} if result is None else result.usage,
                    started_at_ms=started,
                    ended_at_ms=ended,
                )
                repos.trading.record_policy_actions(
                    case_id=case_id,
                    program_sha=self.program_sha,
                    decisions=decisions,
                    now_ms=now_ms,
                )
                if live_action.action != "abstain":
                    signal_id = None
                    publish_status = publication
                    if publication is None:
                        publish_status = repos.trading.publication_source_status(case_id=case_id, now_ms=now_ms)
                        if publish_status is None and now_ms >= int(case["root_expires_at_ms"]):
                            publish_status = "signal_expired"
                        if publish_status is None and not RuntimeProcesses(
                            repos.conn, kind="executor", key=self.settings.trading.execution.account_slot
                        ).is_running(now_ms=now_ms):
                            publish_status = "runtime_unavailable"
                        if publish_status is None:
                            signal = self._signal(
                                {**case, "reference_price": reference_price}, view, live_action.action, now_ms
                            )
                            repos.trading.append_signal(signal)
                            signal_id = signal.signal_id
                            publish_status = "published"
                    repos.trading.set_publication(
                        case_id=case_id,
                        program_sha=self.program_sha,
                        policy_id=live_action.policy_id,
                        policy_version=POLICY_VERSION,
                        publish_status=publish_status,
                        signal_id=signal_id,
                    )
                return bool(
                    repos.trading.finish_case(
                        case_id=case_id,
                        claim_token=token,
                        status="complete",
                        failure_code=None,
                        now_ms=now_ms,
                    )
                )

            await self._db_async(settle, transaction=True)
        except Exception as exc:
            _LOG.exception("analysis_case_failed", extra={"case_id": case_id})
            code = "data_missing" if isinstance(exc, (ValueError, KeyError)) else "provider"

            def fail(repos: Any) -> None:
                now_ms = _clock_ms()
                if not repos.trading.claim_is_current(case_id=case_id, claim_token=token, now_ms=now_ms):
                    return
                if not frozen_view:
                    legs = (
                        PaperLeg(
                            "long", "missing", None, "case_preparation_failed", None, None, None, None, None, None, None
                        ),
                        PaperLeg(
                            "short",
                            "missing",
                            None,
                            "case_preparation_failed",
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        ),
                    )
                    repos.trading.record_paper_legs(
                        case_id=case_id, legs=legs, geometry_version=GEOMETRY_VERSION, now_ms=now_ms
                    )
                repos.trading.finish_case(
                    case_id=case_id,
                    claim_token=token,
                    status="failed",
                    failure_code=code,
                    now_ms=now_ms,
                )

            await self._db_async(
                fail,
                transaction=True,
            )
        return True

    async def label_once(self, *, limit: int = 4) -> int:
        now_ms = _clock_ms()
        rows = await self._db_async(
            lambda repos: repos.trading.label_candidates(now_ms=now_ms, buffer_ms=_PAPER_BUFFER_MS, limit=limit)
        )
        for case in rows:
            decided = int(case["decided_at_ms"])
            end = (decided // BAR_MS + 1) * BAR_MS + 14_400_000
            request = analysis_market_request(
                dataset="perp_bars",
                native_symbol=str(case["native_symbol"]),
                end_ms=end,
                window_minutes=242,
                deadline_at_monotonic=time.monotonic() + 8,
            )
            try:
                answer = await self.market_data.fetch(request)
                if answer.status not in ("ok", "partial") or answer.source_identity != "binance_public_v1":
                    raise ValueError("paper_market_data_missing")
                bars = tuple(
                    Bar(
                        int(row["event_at_ms"]),
                        Decimal(str(row["high"])),
                        Decimal(str(row["low"])),
                        Decimal(str(row["close"])),
                    )
                    for row in answer.payload
                    if int(row["event_at_ms"]) <= end
                )
            except (ValueError, KeyError, TypeError):
                bars = ()
            legs = both_legs(
                decided_at_ms=decided,
                bars=bars,
                leg_geometry=LegGeometry(int(case["stop_bps"]), int(case["tp_bps"])),
                half_spread_bps=Decimal(str(case["half_spread_bps"])),
            )
            await self._db_async(
                lambda repos, case=case, legs=legs: repos.trading.record_paper_legs(
                    case_id=case["case_id"],
                    legs=legs,
                    geometry_version=case["geometry_version"],
                    now_ms=_clock_ms(),
                ),
                transaction=True,
            )
        return len(rows)

    def _heartbeat(self, repos: Any, now_ms: int) -> None:
        runtime = RuntimeProcesses(repos.conn, kind="analysis", key=self.settings.trading.execution.account_slot)
        if not self._runtime_started:
            if not runtime.begin(instance_id=self.instance_id, started_at_ms=now_ms, now_ms=now_ms):
                raise RuntimeError("analysis_account_slot_already_owned")
            runtime.transition(instance_id=self.instance_id, lifecycle_state="running", now_ms=now_ms)
            self._runtime_started = True
        detail = AnalysisProcessDetail(
            active_policy=self.settings.trading.analysis.active_policy,
            program_sha=self.program_sha,
            model_name=self.settings.trading.analysis.model_name,
            model_configured=self.assessor is not None,
            publish_signals=self.settings.trading.analysis.publish_signals,
            config_digest=self._config_digest,
        )
        runtime.heartbeat(
            instance_id=self.instance_id,
            now_ms=now_ms,
            fault_code=self.fault_code,
            detail=detail.model_dump(mode="json"),
        )

    def _stop_runtime(self, repos: Any) -> None:
        RuntimeProcesses(repos.conn, kind="analysis", key=self.settings.trading.execution.account_slot).transition(
            instance_id=self.instance_id,
            lifecycle_state="stopped",
            now_ms=_clock_ms(),
        )

    async def run(self, stop: asyncio.Event) -> None:
        next_heartbeat = 0
        next_prune = 0
        try:
            while not stop.is_set():
                try:
                    now_ms = _clock_ms()
                    if now_ms >= next_prune:
                        await asyncio.get_running_loop().run_in_executor(
                            self._db_pool, partial(prune_snapshots, self.preparer.raw_root, now_s=now_ms / 1_000)
                        )
                        next_prune = now_ms + 86_400_000
                    if now_ms >= next_heartbeat:
                        await self._db_async(
                            lambda repos, now_ms=now_ms: self._heartbeat(repos, now_ms), transaction=True
                        )
                        next_heartbeat = now_ms + 5_000
                    await self.relay_once()
                    while len(self._active) < self.settings.trading.analysis.max_active_cases:
                        case = await self._db_async(
                            lambda repos: repos.trading.claim_case(now_ms=_clock_ms(), lease_ms=self._lease_ms),
                            transaction=True,
                        )
                        if case is None:
                            break
                        task = asyncio.create_task(self.analyze_one(case))
                        self._active.add(task)
                        task.add_done_callback(self._active.discard)
                    if self._label_task is None or self._label_task.done():
                        if self._label_task is not None:
                            self._label_task.result()
                        self._label_task = asyncio.create_task(self.label_once())
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and str(exc) in (
                        "runtime_process_identity_lost",
                        "analysis_account_slot_already_owned",
                    ):
                        raise
                    _LOG.exception("analysis_runner_cycle_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
        finally:
            if self._active:
                await asyncio.gather(*self._active, return_exceptions=True)
            if self._label_task is not None:
                await asyncio.gather(self._label_task, return_exceptions=True)
            if self._runtime_started:
                await self._db_async(self._stop_runtime, transaction=True)
            self._db_pool.shutdown(wait=True)
