"""Analysis harness: reliable relay, frozen evidence and one bounded Agent call."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analyst import TradeAnalyst
from tracefold.platform.market_identity import (
    AssetId,
    AssetRegistry,
    InstrumentRef,
    UniversePolicy,
    VerifiedAlias,
)
from tracefold.trading.engine.brief import AnalystBrief, build_brief
from tracefold.trading.engine.contracts import Candidate, ExitPlan
from tracefold.trading.engine.features import PROFILE_VERSION, extract_features
from tracefold.trading.engine.marketdata import Dataset, MarketDataPort, MarketDataRequest, MarketDataResult
from tracefold.trading.engine.outcomes import price_path_label
from tracefold.trading.engine.policy import InvalidAssessment, compile_assessment, decision_identity
from tracefold.trading.engine.target import SourceAsset, TargetSelection, select_target
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV1,
    SignalExitPlanV1,
    TradeSignalV2,
    market_key,
)
from tracefold.trading.storage.execution_stream import PreparedTradeSignal, prepare_trade_signal_v2

_BAR_MS = 60_000
_PROFILE_BARS = 241
_LOG = logging.getLogger(__name__)


def _clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class PreparedAnalysis:
    evidence_ref: str
    brief_ref: str
    brief: AnalystBrief
    candidates: tuple[Candidate, ...]
    reference_price: Decimal
    reference_at_ms: int


class FrameReader:
    """Trading owns coverage, features and frozen snapshots; the port fetches raw data."""

    def __init__(self, market_data: MarketDataPort, files: AnalysisFiles) -> None:
        self.market_data = market_data
        self.files = files

    async def prepare(
        self,
        *,
        case: dict[str, Any],
        source_fact: dict[str, Any],
        source_history: tuple[dict[str, Any], ...] = (),
    ) -> PreparedAnalysis:
        selection = dict(case["target_selection"])
        instrument = selection.get("instrument")
        if selection.get("reason") != "selected" or not isinstance(instrument, dict):
            raise ValueError("analysis_target_not_selected")
        native = str(instrument["native_symbol"])
        now = _clock_ms()
        end = now // _BAR_MS * _BAR_MS
        start = end - _PROFILE_BARS * _BAR_MS
        deadline = time.monotonic() + 5.0
        environment = str(instrument["environment"])

        def request(dataset: Dataset, symbol: str, *, spot: bool = False) -> MarketDataRequest:
            bars = dataset.endswith("bars")
            return MarketDataRequest(
                dataset=dataset,
                native_symbol=symbol,
                venue="binance.usdm",
                environment=environment,
                product="spot" if spot else "perpetual",
                source_identity="binance_public_v1",
                unit_definition="native_quote_v1",
                start_ms=start if bars else None,
                end_ms=end if bars else None,
                interval_ms=_BAR_MS if bars else None,
                max_age_ms=None if bars else 90_000,
                deadline_at_monotonic=deadline,
            )

        requests = {
            "perp_bars": request("perp_bars", native),
            "spot_bars": request("spot_bars", native, spot=True),
            "open_interest": request("open_interest", native),
            "funding_basis": request("funding_basis", native),
            "market_bars": request("market_bars", "BTCUSDT"),
        }
        answers = await asyncio.gather(
            *(self.market_data.fetch(item) for item in requests.values()),
            return_exceptions=True,
        )
        results: dict[str, MarketDataResult] = {}
        for (name, item), answer in zip(requests.items(), answers, strict=True):
            if isinstance(answer, asyncio.CancelledError):
                raise answer
            if isinstance(answer, BaseException):
                result = MarketDataResult(
                    status="error",
                    payload=(),
                    schema_version="binance_market_v1",
                    source_version="binance_public_v1",
                    unit_definition=item.unit_definition,
                    source_identity=item.source_identity,
                    event_start_ms=None,
                    event_end_ms=None,
                    received_at_ms=None,
                    missing_reasons=(type(answer).__name__,),
                    request_receipts=(),
                )
            else:
                result = answer
            results[name] = result
        knowledge_cutoff = _clock_ms()
        if any(
            result.received_at_ms is not None and result.received_at_ms > knowledge_cutoff
            for result in results.values()
        ):
            raise ValueError("market_data_received_after_cutoff")
        if results["perp_bars"].status != "ok":
            raise ValueError("required_perp_price_unavailable")
        last_bar = results["perp_bars"].payload[-1]
        reference_price = Decimal(str(last_bar["close"]))
        reference_at_ms = int(last_bar["event_at_ms"])
        if reference_price <= 0 or now - reference_at_ms > 120_000:
            raise ValueError("entry_reference_price_stale")
        if source_fact.get("kind") == "oi" and source_fact.get("oi_value_usd") is None:
            raise ValueError("required_source_oi_unavailable")
        if source_fact.get("kind") == "oi" and results["open_interest"].status != "ok":
            raise ValueError("required_market_oi_unavailable")
        features = extract_features(results, source_fact)
        sigma = features.get("perp_volatility_1m_bps")
        stop = max(100, min(1000, int((sigma or 10) * 16)))
        sides: tuple[Literal["long", "short"], ...] = ("long", "short")
        candidates = tuple(
            Candidate(
                candidate_id=f"{selection['asset_id']}:{side}:v1",
                asset_id=str(selection["asset_id"]),
                instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
                side=side,
                exit_plan=ExitPlan(
                    stop_distance_bps=stop, take_profit_bps=min(2000, stop * 2), max_holding_seconds=4 * 3_600
                ),
                required_evidence_refs=("source", "market:perp_bars"),
            )
            for side in sides
        )
        snapshot = {
            "snapshot_version": "evidence_snapshot_v1",
            "profile_version": PROFILE_VERSION,
            "case_id": case["case_id"],
            "knowledge_cutoff_ms": knowledge_cutoff,
            "source_fact": source_fact,
            "same_asset_source_history": source_history,
            "market": {
                name: {
                    "status": result.status,
                    "payload": result.payload,
                    "source_identity": result.source_identity,
                    "source_version": result.source_version,
                    "unit_definition": result.unit_definition,
                    "event_start_ms": result.event_start_ms,
                    "event_end_ms": result.event_end_ms,
                    "received_at_ms": result.received_at_ms,
                    "missing_reasons": result.missing_reasons,
                    "request_receipts": result.request_receipts,
                }
                for name, result in results.items()
            },
            "features": features,
            "entry_reference": {"price": str(reference_price), "closed_at_ms": reference_at_ms},
        }
        evidence_ref = self.files.write(snapshot)
        brief_evidence: dict[str, dict[str, Any]] = {"source": {"status": "ok", "source_ref": evidence_ref}}
        brief_evidence.update(
            {
                f"market:{name}": {
                    "status": result.status,
                    "source": result.source_identity,
                    "event_end_ms": result.event_end_ms,
                    "received_at_ms": result.received_at_ms,
                    "missing_reasons": result.missing_reasons,
                }
                for name, result in results.items()
            }
        )
        brief = build_brief(
            target_asset_id=str(selection["asset_id"]),
            instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
            source_fact=source_fact,
            source_history=source_history,
            evidence=brief_evidence,
            features=features,
            candidates=candidates,
        )
        brief_ref = self.files.write({"brief_json": brief.text})
        return PreparedAnalysis(evidence_ref, brief_ref, brief, candidates, reference_price, reference_at_ms)


def _assets(payload: dict[str, Any]) -> tuple[SourceAsset, ...]:
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("trade_event_assets_invalid")
    if len(raw_assets) > 8:
        raise ValueError("trade_event_assets_oversized")
    return tuple(SourceAsset(str(item["symbol"]), str(item["market_type"]), str(item["role"])) for item in raw_assets)


def _registry_from_rows(
    rows: list[dict[str, Any]],
    *,
    environment: str,
    universe: UniversePolicy,
    verified_routes: list[Any],
) -> AssetRegistry:
    instruments: list[InstrumentRef] = []
    verified = {route.native_symbol: route for route in verified_routes}
    aliases = []
    for row in rows:
        if row["venue"] != "binance.perp" or row["instrument_class"] != "crypto":
            continue
        native = str(row["venue_symbol"])
        base = str(row["base_symbol"])
        quote = str(row["quote_asset"])
        reviewed = verified.get(native)
        if reviewed is None and (not base or base[0].isdigit() or native != base + quote):
            # A multiplier or a nonstandard native spelling needs reviewed
            # asset and unit semantics before any executable target exists.
            continue
        try:
            asset_id = (
                AssetId("crypto", reviewed.asset_id.split(":", 1)[1])
                if reviewed is not None
                else AssetId("crypto", base)
            )
        except ValueError:
            continue
        instruments.append(
            InstrumentRef(
                venue="binance.usdm",
                environment=environment,
                product="perpetual",
                native_symbol=native,
                asset_id=asset_id,
                quote_asset=quote,
                settlement_asset=quote,
                units_per_contract=reviewed.units_per_contract if reviewed is not None else Decimal(1),
                price_unit="native_quote",
                quantity_unit="native_base",
            )
        )
        if reviewed is not None:
            aliases.append(
                VerifiedAlias(
                    "crypto",
                    reviewed.source_symbol,
                    asset_id,
                    reviewed.units_per_contract,
                    reviewed.evidence_ref,
                    native,
                )
            )
    snapshot = str(max((int(row["observed_at_ms"]) for row in rows), default=0))
    return AssetRegistry(
        snapshot_ref=f"news_native_catalogue:{snapshot}",
        instruments=tuple(instruments),
        aliases=tuple(aliases),
        known_assets=universe.excluded_asset_ids,
    )


def _select_from_public_projection(
    repos: Any,
    event: dict[str, Any],
    *,
    environment: str,
    universe: UniversePolicy,
    verified_routes: list[Any],
) -> TargetSelection:
    payload = dict(event["payload"])
    assets = _assets(payload)
    symbols = {asset.symbol for asset in assets if asset.role == "primary"}
    for route in verified_routes:
        if route.source_symbol in symbols:
            symbols.add(route.native_symbol[:-4])
    rows_by_native: dict[str, dict[str, Any]] = {}
    for symbol in sorted(symbols):
        for row in repos.news.trade_candidate_instrument(
            base_symbol=symbol,
            venues=("binance.perp",),
            observed_at_ms=int(event["source_recorded_at_ms"]),
        ):
            rows_by_native[str(row["venue_symbol"])] = row
    registry = _registry_from_rows(
        list(rows_by_native.values()), environment=environment, universe=universe, verified_routes=verified_routes
    )
    return select_target(
        kind=event["kind"], assets=assets, registry=registry, universe=universe, execution_environment=environment
    )


def _configured_universe(settings: Any) -> UniversePolicy:
    exclusions = []
    for value in settings.trading.analysis.excluded_asset_ids:
        category, symbol = value.split(":", 1)
        exclusions.append(AssetId(category, symbol))
    return UniversePolicy(version="universe_v1", excluded_asset_ids=frozenset(exclusions))


class AnalysisRunner:
    def __init__(
        self,
        *,
        settings: Any,
        market_data: MarketDataPort,
        analyst: TradeAnalyst | None,
        files_root: Path,
        max_active_cases: int = 8,
    ) -> None:
        self.settings = settings
        self.files = AnalysisFiles(files_root)
        self.reader = FrameReader(market_data, self.files)
        self.analyst = analyst
        self._max_active_cases = max_active_cases
        self._root_ttl_ms = settings.trading.analysis.root_ttl_seconds * 1000
        self._lease_ms = (settings.trading.analysis.model_timeout_seconds + 20) * 1000
        self._universe = _configured_universe(settings)
        execution = settings.trading.execution
        self._runtime_id = f"{execution.account_slot}:{execution.mode}"
        self._config_digest = hashlib.sha256(
            json.dumps(
                {
                    "analysis": settings.trading.analysis.model_dump(mode="json"),
                    "account_slot": execution.account_slot,
                    "runtime_mode": execution.mode,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if settings.trading.analysis.publish_signals and settings.trading.execution.mode not in ("paper", "live"):
            raise ValueError("analysis_publication_requires_runtime_mode")
        self._active: set[asyncio.Task[bool]] = set()
        self._label_task: asyncio.Task[int] | None = None
        self._db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="analysis-db")

    def _db(self, fn: Any, *, transaction: bool = False) -> Any:
        from tracefold.app.repository_session import repositories

        with repositories(self.settings, application_name="tracefold_analysis") as repos:
            if transaction:
                with repos.transaction():
                    return fn(repos)
            return fn(repos)

    async def _db_async(self, fn: Any, *, transaction: bool = False) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._db_executor,
            partial(self._db, fn, transaction=transaction),
        )

    async def relay_once(self, *, batch_size: int = 64) -> int:
        events = await self._db_async(lambda repos: repos.news.unacknowledged_trade_events(limit=batch_size))
        environment = "demo" if self.settings.trading.execution.mode == "paper" else "live"
        for event in events:
            try:
                selection = await self._db_async(
                    lambda repos, event=event: _select_from_public_projection(
                        repos,
                        event,
                        environment=environment,
                        universe=self._universe,
                        verified_routes=self.settings.trading.analysis.verified_routes,
                    ),
                )
            except (KeyError, TypeError, ValueError):
                await self._db_async(
                    lambda repos, event=event: repos.news.reject_trade_event(
                        event_id=event["event_id"],
                        payload_sha256=event["payload_sha256"],
                        reason="trade_event_payload_invalid",
                    ),
                    transaction=True,
                )
                continue
            try:
                await self._db_async(
                    lambda repos, event=event, selection=selection: repos.trading.accept_trigger(
                        kind=event["kind"],
                        source_fact_key=event["source_fact_key"],
                        source_revision=event["source_revision"],
                        payload_sha256=event["payload_sha256"],
                        payload=event["payload"],
                        selection=selection,
                        now_ms=_clock_ms(),
                        root_ttl_ms=self._root_ttl_ms,
                    ),
                    transaction=True,
                )
            except (KeyError, TypeError, ValueError):
                await self._db_async(
                    lambda repos, event=event: repos.news.reject_trade_event(
                        event_id=event["event_id"],
                        payload_sha256=event["payload_sha256"],
                        reason="trade_event_payload_invalid",
                    ),
                    transaction=True,
                )
                continue
            # A commit before this acknowledgement is intentionally replayable.
            await self._db_async(
                lambda repos, event=event: repos.news.acknowledge_trade_event(
                    event_id=event["event_id"], payload_sha256=event["payload_sha256"], now_ms=_clock_ms()
                ),
                transaction=True,
            )
        return len(events)

    async def _claim(self) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            await self._db_async(
                lambda repos: repos.trading.claim_analysis_case(now_ms=_clock_ms(), lease_ms=self._lease_ms),
                transaction=True,
            ),
        )

    async def analyze_one(self, case: dict[str, Any] | None = None) -> bool:
        case = case if case is not None else await self._claim()
        if case is None:
            return False
        source = await self._db_async(lambda repos: repos.trading.analysis_trigger(case["trigger_id"]))
        if source is None:
            raise RuntimeError("analysis_trigger_missing")
        source_history = tuple(
            await self._db_async(
                lambda repos: repos.trading.recent_asset_source_context(
                    asset_id=str(case["target_asset_id"]),
                    known_at_ms=int(case["created_at_ms"]),
                    exclude_trigger_id=str(case["trigger_id"]),
                ),
            )
        )
        status = "unavailable"
        evidence_ref = None
        assessment_ref = None
        brief_ref = None
        receipt = None
        decision = None
        prepared_signal: PreparedTradeSignal | None = None
        publish_block_reason: str | None = None
        try:
            prepared = await self.reader.prepare(
                case=case,
                source_fact=source["payload"],
                source_history=source_history,
            )
            evidence_ref = prepared.evidence_ref
            brief_ref = prepared.brief_ref
            if self.analyst is None:
                status = "policy_unconfigured"
            else:
                receipt = await self.analyst.assess(prepared.brief)
                if receipt.assessment is None:
                    status = receipt.error_code or "model_unavailable"
                else:
                    compiled = compile_assessment(
                        assessment=receipt.assessment,
                        brief_sha=prepared.brief.sha,
                        candidate_menu_sha=prepared.brief.candidate_menu_sha,
                        candidates=prepared.candidates,
                        evidence_refs=prepared.brief.evidence_refs,
                    )
                    decision = compiled.model_dump(mode="json")
                    status = "analyzed"
                    if compiled.action == "TRADE" and self.settings.trading.analysis.publish_signals:
                        try:
                            prepared_signal = self._prepare_signal(case, prepared, decision)
                        except ValueError as exc:
                            # The analysis remains valid, but an invalid or expired
                            # execution envelope must be visible as a publication refusal.
                            publish_block_reason = str(exc)
        except InvalidAssessment:
            status = "invalid_assessment"
        except (ValueError, TimeoutError):
            status = "evidence_unavailable"
        if receipt is not None:
            request_ref = self.files.write(receipt.request_payload) if receipt.request_payload is not None else None
            response_ref = self.files.write(receipt.response_payload) if receipt.response_payload is not None else None
            assessment_ref = self.files.write(
                {
                    "case_id": case["case_id"],
                    "claim_token": case["claim_token"],
                    "claim_attempt": case["claim_attempt"],
                    "brief_ref": brief_ref,
                    "brief_sha": receipt.brief_sha,
                    "menu_sha": receipt.menu_sha,
                    "prompt_sha": receipt.prompt_sha,
                    "model": receipt.model,
                    "profile_version": PROFILE_VERSION,
                    "started_at_ms": receipt.started_at_ms,
                    "ended_at_ms": receipt.ended_at_ms,
                    "provider_status": receipt.status,
                    "validation_status": status,
                    "input_tokens": receipt.input_tokens,
                    "output_tokens": receipt.output_tokens,
                    "cost_microusd": receipt.cost_microusd,
                    "request_ref": request_ref,
                    "response_ref": response_ref,
                    "assessment": None if receipt.assessment is None else receipt.assessment.model_dump(mode="json"),
                    "error_code": receipt.error_code,
                }
            )
        await self._db_async(
            lambda repos: repos.trading.finish_analysis_case(
                case_id=case["case_id"],
                claim_token=case["claim_token"],
                now_ms=_clock_ms(),
                analysis_status=status,
                evidence_ref=evidence_ref,
                decision=decision,
                assessment_ref=assessment_ref,
                prepared_signal=prepared_signal,
                publish_block_reason=publish_block_reason,
                max_watch_rechecks=self.settings.trading.analysis.max_watch_rechecks,
            ),
            transaction=True,
        )
        return True

    def _prepare_signal(
        self,
        case: dict[str, Any],
        prepared: PreparedAnalysis,
        decision: dict[str, Any],
    ) -> PreparedTradeSignal | None:
        plan = decision.get("exit_plan")
        if not isinstance(plan, dict) or decision.get("side") not in ("long", "short"):
            raise ValueError("analysis_trade_plan_invalid")
        instrument = case["target_selection"]["instrument"]
        native = str(instrument["native_symbol"])
        if not native.endswith("USDT") or len(native) <= 4:
            raise ValueError("analysis_native_market_unsupported")
        now_ns = _clock_ms() * 1_000_000
        expiry_ns = min(int(case["root_expires_at_ms"]) * 1_000_000, now_ns + 120_000_000_000)
        if expiry_ns <= now_ns:
            raise ValueError("analysis_signal_expired")
        decision_id = decision_identity(str(case["case_id"]), decision)
        signal_id = hashlib.sha256(
            f"{case['case_id']}:{decision_id}:signal_v2".encode(),
        ).hexdigest()
        mode = self.settings.trading.execution.mode
        signal = TradeSignalV2(
            seq=1,
            signal_id=signal_id,
            case_id=str(case["case_id"]),
            decision_id=decision_id,
            account_slot=self.settings.trading.execution.account_slot,
            runtime_mode=mode,
            entry_scope_id=str(case["entry_scope_id"]),
            asset_id=str(case["target_asset_id"]),
            market_key=market_key(native[:-4]),
            native_symbol=native,
            mapping_semantics_digest=str(case["mapping_semantics_digest"]),
            direction=decision["side"],
            observed_at_ns=now_ns,
            expires_at_ns=expiry_ns,
            exit_plan=SignalExitPlanV1(
                stop_distance_bps=int(plan["stop_distance_bps"]),
                take_profit_bps=int(plan["take_profit_bps"]),
                max_holding_ns=int(plan["max_holding_seconds"]) * 1_000_000_000,
            ),
            entry_envelope=SignalEntryEnvelopeV1(
                root_expires_at_ns=int(case["root_expires_at_ms"]) * 1_000_000,
                reference_price=prepared.reference_price,
                max_price_drift_bps=200,
                universe_version=self._universe.digest,
            ),
        )
        return prepare_trade_signal_v2(signal)

    def _completed(self, task: asyncio.Task[bool]) -> None:
        self._active.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.exception("analysis_case_failed")

    async def label_once(self, *, limit: int = 4) -> int:
        """Fill due opportunity paths without holding a database transaction over I/O."""
        due = await self._db_async(
            lambda repos: repos.trading.due_analysis_outcomes(now_ms=_clock_ms(), limit=limit),
        )
        for row in due:
            now = _clock_ms()
            selection = row["target_selection"] or {}
            instrument = selection.get("instrument") if isinstance(selection, dict) else None
            anchor = (
                int(row["source_observed_at_ms"])
                if row["axis"] == "source"
                else int(row["decided_at_ms"]) + 60_000
                if row["decided_at_ms"] is not None
                else None
            )
            path: dict[str, Any]
            if not isinstance(instrument, dict) or anchor is None:
                path = {"status": "missing", "reason": "outcome_identity_or_anchor_missing"}
            else:
                end_at = anchor + int(row["horizon_seconds"]) * 1_000
                request = MarketDataRequest(
                    dataset="perp_bars",
                    native_symbol=str(instrument["native_symbol"]),
                    venue="binance.usdm",
                    environment=str(instrument["environment"]),
                    product="perpetual",
                    source_identity="binance_public_v1",
                    unit_definition="native_quote_v1",
                    start_ms=anchor // _BAR_MS * _BAR_MS,
                    end_ms=(end_at // _BAR_MS + 2) * _BAR_MS,
                    interval_ms=_BAR_MS,
                    max_age_ms=None,
                    deadline_at_monotonic=time.monotonic() + 10.0,
                )
                try:
                    result = await self.reader.market_data.fetch(request)
                    path = price_path_label(
                        result.payload, anchor_ms=anchor, horizon_seconds=int(row["horizon_seconds"])
                    )
                    path.update(
                        {
                            "source_identity": result.source_identity,
                            "source_version": result.source_version,
                            "unit_definition": result.unit_definition,
                            "received_at_ms": result.received_at_ms,
                            "market_status": result.status,
                            "request_receipts": result.request_receipts,
                            "missing_reasons": result.missing_reasons,
                        }
                    )
                except (TimeoutError, ValueError, OSError) as exc:
                    path = {"status": "missing", "reason": type(exc).__name__}
            # A temporarily incomplete provider response remains pending. At
            # 48 h past the horizon the label is explicitly missing.
            expired = now >= int(row["available_at_ms"]) + 48 * 3_600_000
            if path["status"] != "ok" and not expired:
                await self._db_async(
                    lambda repos, row=row, now=now: repos.trading.retry_analysis_outcome(
                        case_id=row["case_id"],
                        axis=row["axis"],
                        horizon_seconds=row["horizon_seconds"],
                        label_version=row["label_version"],
                        next_attempt_at_ms=now + 300_000,
                    ),
                    transaction=True,
                )
                continue
            path.update(
                {
                    "case_id": row["case_id"],
                    "axis": row["axis"],
                    "horizon_seconds": row["horizon_seconds"],
                    "label_version": row["label_version"],
                    "labeled_at_ms": now,
                }
            )
            ref = self.files.write(path)
            await self._db_async(
                lambda repos, row=row, path=path, ref=ref, now=now: repos.trading.settle_analysis_outcome(
                    case_id=row["case_id"],
                    axis=row["axis"],
                    horizon_seconds=row["horizon_seconds"],
                    label_version=row["label_version"],
                    status=path["status"],
                    return_bps=path.get("return_bps"),
                    path_ref=ref,
                    now_ms=now,
                ),
                transaction=True,
            )
        return len(due)

    def _label_completed(self, task: asyncio.Task[int]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.exception("analysis_outcome_label_failed")

    async def run(self, stop_event: asyncio.Event) -> None:
        """Relay keeps draining while model calls occupy separate bounded tasks."""

        next_heartbeat_at_ms = 0
        try:
            while not stop_event.is_set():
                try:
                    now_ms = _clock_ms()
                    if now_ms >= next_heartbeat_at_ms:
                        await self._db_async(
                            lambda repos, now_ms=now_ms: repos.trading.heartbeat_analysis_runtime(
                                runtime_id=self._runtime_id,
                                now_ms=now_ms,
                                active_policy=self.settings.trading.analysis.active_policy,
                                model_name=getattr(self.analyst, "model", None),
                                model_configured=self.analyst is not None,
                                publish_signals=self.settings.trading.analysis.publish_signals,
                                config_digest=self._config_digest,
                            ),
                            transaction=True,
                        )
                        next_heartbeat_at_ms = now_ms + 5_000
                    await self.relay_once()
                    while len(self._active) < self._max_active_cases:
                        case = await self._claim()
                        if case is None:
                            break
                        task = asyncio.create_task(self.analyze_one(case))
                        self._active.add(task)
                        task.add_done_callback(self._completed)
                    if self._label_task is None or self._label_task.done():
                        self._label_task = asyncio.create_task(self.label_once())
                        self._label_task.add_done_callback(self._label_completed)
                except Exception:
                    _LOG.exception("analysis_runner_cycle_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        finally:
            if self._active:
                await asyncio.gather(*self._active, return_exceptions=True)
            if self._label_task is not None:
                await asyncio.gather(self._label_task, return_exceptions=True)
            self._db_executor.shutdown(wait=True)
