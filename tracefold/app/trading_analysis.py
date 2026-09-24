"""Analysis harness: reliable relay, frozen evidence and one bounded Agent call."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analyst import PhysicalModelCall, TradeAnalyst
from tracefold.platform.market_identity import (
    AssetId,
    AssetRegistry,
    InstrumentRef,
    UniversePolicy,
    VerifiedAlias,
)
from tracefold.trading.engine.brief import AnalystBrief, build_brief
from tracefold.trading.engine.contracts import Candidate, ExitPlan
from tracefold.trading.engine.evaluation import EVALUATION_VERSION, evaluate_shadow
from tracefold.trading.engine.features import PROFILE_VERSION, extract_features, freeze_features
from tracefold.trading.engine.marketdata import Dataset, MarketDataPort, MarketDataRequest, MarketDataResult
from tracefold.trading.engine.outcomes import price_path_label
from tracefold.trading.engine.policy import InvalidAssessment, compile_assessment, decision_identity
from tracefold.trading.engine.strategy import (
    ENTRY_WINDOW_MS,
    build_event_price_candidates,
    range_cross_side,
    triggered_candidate,
)
from tracefold.trading.engine.target import SourceAsset, TargetSelection, select_target
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV2,
    SignalExitPlanV1,
    TradeSignalV2,
    market_key,
)
from tracefold.trading.storage.execution_stream import PreparedTradeSignal, prepare_trade_signal_v2

_BAR_MS = 60_000
_PROFILE_BARS = 16
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


class FrozenEvidenceError(ValueError):
    def __init__(self, reason: str, evidence_ref: str) -> None:
        super().__init__(reason)
        self.evidence_ref = evidence_ref


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
        source_first_visible_at_ms: int,
        execution_environment: str | None = None,
        source_history: tuple[dict[str, Any], ...] = (),
        source_history_at: Callable[[int], Awaitable[tuple[dict[str, Any], ...]]] | None = None,
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
            unit_definition = (
                "native_contract_quantity_v1"
                if dataset == "open_interest"
                else "quote_price_and_rate_v1"
                if dataset == "funding_basis"
                else "quote_per_base_and_volume_v1"
            )
            return MarketDataRequest(
                dataset=dataset,
                native_symbol=symbol,
                venue="binance.usdm",
                environment=environment,
                product="spot" if spot else "perpetual",
                source_identity="binance_public_v1",
                unit_definition=unit_definition,
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
        snapshot = {
            "snapshot_version": "evidence_snapshot_v1",
            "profile_version": PROFILE_VERSION,
            "case_id": case["case_id"],
            "knowledge_cutoff_ms": knowledge_cutoff,
            "data_environment": environment,
            "execution_environment": execution_environment,
            "source_fact": source_fact,
            "source_first_visible_at_ms": source_first_visible_at_ms,
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
        }
        failure_evidence_ref = await asyncio.to_thread(self.files.write, snapshot)
        try:
            if source_history_at is not None:
                try:
                    source_history = await source_history_at(knowledge_cutoff)
                except Exception as exc:
                    raise FrozenEvidenceError(f"source_history_{type(exc).__name__}", failure_evidence_ref) from exc
                snapshot["same_asset_source_history"] = source_history
                failure_evidence_ref = await asyncio.to_thread(self.files.write, snapshot)
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
            features = extract_features(results, source_fact)
            trigger_context = dict(case.get("manifest") or {}) if case.get("run_kind") == "conditional" else None
            candidates: tuple[Candidate, ...]
            if trigger_context is not None:
                condition = trigger_context["watch_condition"]
                reference_price = Decimal(str(trigger_context["watch_observed_value"]))
                reference_at_ms = int(trigger_context["watch_observed_at_ms"])
                candidates = (
                    triggered_candidate(
                        asset_id=str(selection["asset_id"]),
                        instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
                        condition=condition,
                        trigger_side=trigger_context["watch_trigger_side"],
                        trigger_at_ms=reference_at_ms,
                        trigger_close=reference_price,
                        previous_close=Decimal(str(trigger_context["watch_previous_close"])),
                        latest_closed_rows=results["perp_bars"].payload,
                    ),
                )
            else:
                candidates = build_event_price_candidates(
                    asset_id=str(selection["asset_id"]),
                    instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
                    source_fact=source_fact,
                    source_first_visible_at_ms=source_first_visible_at_ms,
                    perp_rows=results["perp_bars"].payload,
                )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise FrozenEvidenceError(str(exc), failure_evidence_ref) from exc
        snapshot["features"] = features
        snapshot["entry_reference"] = {"price": str(reference_price), "closed_at_ms": reference_at_ms}
        if trigger_context is not None:
            snapshot["trigger_context"] = trigger_context
        evidence_ref = await asyncio.to_thread(self.files.write, snapshot)
        typed_evidence = freeze_features(
            snapshot_ref=evidence_ref,
            knowledge_cutoff_ms=knowledge_cutoff,
            data_environment=environment,
            execution_environment=execution_environment,
            source_first_visible_at_ms=source_first_visible_at_ms,
            source_fact=source_fact,
            results=results,
            features=features,
        )
        brief_evidence: dict[str, dict[str, Any]] = {
            "source": {
                "status": "ok",
                "source_ref": evidence_ref,
                "values": {
                    key: source_fact[key]
                    for key in (
                        "oi_change_bps",
                        "oi_value_usd",
                        "measurement_definition",
                        "measurement_window_ms",
                        "headline_zh",
                        "title",
                        "why_zh",
                    )
                    if source_fact.get(key) is not None
                },
                "unit_definition": {
                    "oi_change_bps": "bps",
                    "oi_value_usd": "USD",
                    "measurement_definition": "text",
                    "measurement_window_ms": "ms",
                    "headline_zh": "text",
                    "title": "text",
                    "why_zh": "text",
                },
                "event_at_ms": source_fact.get("source_recorded_at_ms"),
                "received_at_ms": int(case.get("created_at_ms", knowledge_cutoff)),
                "knowledge_cutoff_ms": knowledge_cutoff,
            }
        }
        value_fields: dict[str, tuple[str, ...]] = {
            "perp_bars": ("close", "high", "low", "quote_volume"),
            "spot_bars": ("close", "high", "low", "quote_volume"),
            "market_bars": ("close", "high", "low", "quote_volume"),
            "open_interest": ("open_interest_quantity",),
            "funding_basis": ("mark_price", "index_price", "last_funding_rate"),
        }
        brief_evidence.update(
            {
                f"market:{name}": {
                    "status": result.status,
                    "source": result.source_identity,
                    "values": {
                        key: result.payload[-1][key]
                        for key in value_fields[name]
                        if result.payload and result.payload[-1].get(key) is not None
                    },
                    "unit_definition": result.unit_definition,
                    "event_at_ms": result.event_end_ms,
                    "event_end_ms": result.event_end_ms,
                    "received_at_ms": result.received_at_ms,
                    "knowledge_cutoff_ms": knowledge_cutoff,
                    "missing_reasons": result.missing_reasons,
                }
                for name, result in results.items()
            }
        )
        brief_evidence.update(
            {
                f"feature:{value.feature_id}": {
                    "status": value.status,
                    "source_ref": value.source_ref,
                    "values": {"value": value.value} if value.status == "ok" else {},
                    "unit_definition": value.unit,
                    "event_at_ms": value.event_at_ms,
                    "received_at_ms": value.received_at_ms,
                    "knowledge_cutoff_ms": knowledge_cutoff,
                    "feature_version": value.feature_version,
                }
                for value in typed_evidence.values
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
            trigger_context=trigger_context,
            typed_evidence=typed_evidence.model_dump(mode="json"),
        )
        brief_ref = await asyncio.to_thread(self.files.write, {"brief_json": brief.text})
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
        if settings.trading.analysis.strategy_publication_enabled and settings.trading.execution.mode != "paper":
            raise ValueError("strategy_publication_requires_paper_mode")
        self._active: set[asyncio.Task[bool]] = set()
        self._label_task: asyncio.Task[int] | None = None
        self._watch_task: asyncio.Task[int] | None = None
        self._evaluation_task: asyncio.Task[int] | None = None
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
        environment = (
            "demo"
            if self.settings.trading.analysis.strategy_publication_enabled
            and self.settings.trading.execution.mode == "paper"
            else "live"
        )
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
        status = "unavailable"
        evidence_ref = None
        assessment_ref = None
        brief_ref = None
        receipt = None
        decision = None
        prepared_signal: PreparedTradeSignal | None = None
        publish_block_reason: str | None = None
        analysis_error_code: str | None = None
        validation_errors: tuple[dict[str, str], ...] = ()
        try:
            source = await self._db_async(lambda repos: repos.trading.analysis_trigger(case["trigger_id"]))
            if source is None:
                raise ValueError("analysis_trigger_missing")

            async def source_history_at(cutoff_ms: int) -> tuple[dict[str, Any], ...]:
                return tuple(
                    await self._db_async(
                        lambda repos: repos.trading.recent_asset_source_context(
                            asset_id=str(case["target_asset_id"]),
                            known_at_ms=cutoff_ms,
                            exclude_trigger_id=str(case["trigger_id"]),
                        ),
                    )
                )

            prepared = await self.reader.prepare(
                case=case,
                source_fact=source["payload"],
                source_first_visible_at_ms=int(source["first_visible_at_ms"]),
                execution_environment=self.settings.trading.execution.mode,
                source_history_at=source_history_at,
            )
            evidence_ref = prepared.evidence_ref
            brief_ref = prepared.brief_ref
            await self._db_async(
                lambda repos: repos.trading.record_analysis_snapshot(
                    case_id=case["case_id"],
                    claim_attempt=int(case["claim_attempt"]),
                    claim_token=case["claim_token"],
                    evidence_ref=evidence_ref,
                    brief_ref=brief_ref,
                ),
                transaction=True,
            )
            if self.analyst is None:
                status = "policy_unconfigured"
            else:

                async def before_call(
                    call_index: int,
                    request_payload: dict[str, Any] | None,
                    timeout_ms: int,
                    remaining_ms: int,
                    cost_bound: int | None,
                ) -> None:
                    request_ref = await asyncio.to_thread(self.files.write, request_payload)
                    allowed = await self._db_async(
                        lambda repos: repos.trading.record_model_call_start(
                            case_id=case["case_id"],
                            claim_attempt=int(case["claim_attempt"]),
                            claim_token=case["claim_token"],
                            call_index=call_index,
                            request_ref=request_ref,
                            now_ms=_clock_ms(),
                            timeout_ms=timeout_ms,
                            reserved_cost_microusd=cost_bound,
                        ),
                        transaction=True,
                    )
                    if not allowed:
                        raise TimeoutError("model_case_fence_expired")

                async def after_call(call_index: int, call: PhysicalModelCall) -> None:
                    response_ref = (
                        await asyncio.to_thread(self.files.write, call.response_payload)
                        if call.response_payload is not None
                        else None
                    )
                    await self._db_async(
                        lambda repos: repos.trading.record_model_call_finish(
                            case_id=case["case_id"],
                            claim_attempt=int(case["claim_attempt"]),
                            claim_token=case["claim_token"],
                            call_index=call_index,
                            response_ref=response_ref,
                            finished_at_ms=_clock_ms(),
                            input_tokens=call.input_tokens,
                            output_tokens=call.output_tokens,
                            cost_microusd=call.cost_microusd,
                        ),
                        transaction=True,
                    )

                if isinstance(self.analyst, TradeAnalyst):
                    receipt = await self.analyst.assess(
                        prepared.brief,
                        deadline_at_ms=min(
                            int(case["lease_until_ms"]),
                            int(case["work_deadline_at_ms"]),
                            int(case["root_expires_at_ms"]),
                        ),
                        before_call=before_call,
                        after_call=after_call,
                    )
                else:
                    receipt = await self.analyst.assess(prepared.brief)
                if receipt.assessment is None:
                    status = receipt.error_code or "model_unavailable"
                else:
                    compiled = compile_assessment(
                        assessment=receipt.assessment,
                        candidates=prepared.candidates,
                        evidence_catalog=prepared.brief.evidence_catalog,
                        watch_expires_at_ms=int(case["root_expires_at_ms"]),
                    )
                    decision = compiled.model_dump(mode="json")
                    status = "analyzed"
                    if compiled.action == "TRADE" and self.settings.trading.analysis.publish_signals:
                        if not self.settings.trading.analysis.strategy_publication_enabled:
                            publish_block_reason = "strategy_not_validated"
                        else:
                            try:
                                prepared_signal = self._prepare_signal(case, prepared, decision)
                            except ValueError as exc:
                                # The analysis remains valid, but an invalid or expired
                                # execution envelope must be visible as a publication refusal.
                                publish_block_reason = str(exc)
        except FrozenEvidenceError as exc:
            status = "evidence_unavailable"
            analysis_error_code = str(exc)
            evidence_ref = exc.evidence_ref
        except InvalidAssessment as exc:
            status = "invalid_assessment"
            analysis_error_code = str(exc)
            validation_errors = ({"field": "assessment", "type": str(exc)},)
        except (ValueError, TimeoutError) as exc:
            status = "evidence_unavailable"
            analysis_error_code = str(exc)
        except Exception as exc:
            # A claimed Case still needs a discoverable attempt if an unexpected
            # preparation or model adapter failure occurs. Avoid exception text:
            # providers may include source content or credentials in it.
            status = "analysis_error"
            analysis_error_code = type(exc).__name__
        physical_calls: tuple[PhysicalModelCall, ...] = ()
        call_rows: list[dict[str, Any]] = []
        if receipt is not None:
            request_ref = (
                await asyncio.to_thread(self.files.write, receipt.request_payload)
                if receipt.request_payload is not None
                else None
            )
            response_ref = (
                await asyncio.to_thread(self.files.write, receipt.response_payload)
                if receipt.response_payload is not None
                else None
            )
            physical_calls = receipt.physical_calls or (
                (
                    PhysicalModelCall(
                        receipt.request_payload,
                        receipt.response_payload,
                        receipt.input_tokens,
                        receipt.output_tokens,
                        receipt.cost_microusd,
                        "provider_cost_unavailable" if receipt.cost_microusd is None else None,
                    ),
                )
                if receipt.response_payload is not None
                else ()
            )
            call_rows = [
                {
                    "request_ref": (
                        await asyncio.to_thread(self.files.write, call.request_payload)
                        if call.request_payload is not None
                        else None
                    ),
                    "response_ref": (
                        await asyncio.to_thread(self.files.write, call.response_payload)
                        if call.response_payload is not None
                        else None
                    ),
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "cost_microusd": call.cost_microusd,
                    "cost_unknown_reason": call.cost_unknown_reason,
                }
                for call in physical_calls
            ]
            validation_errors = receipt.validation_errors + validation_errors
            assessment_ref = await asyncio.to_thread(
                self.files.write,
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
                    "validation_errors": validation_errors,
                    "physical_calls": call_rows,
                },
            )
        await self._db_async(
            lambda repos: repos.trading.record_analysis_attempt(
                case_id=case["case_id"],
                claim_attempt=int(case["claim_attempt"]),
                claim_token=case["claim_token"],
                brief_ref=brief_ref,
                evidence_ref=evidence_ref,
                assessment_ref=assessment_ref,
                model_name=None if receipt is None else receipt.model,
                prompt_sha=None if receipt is None else receipt.prompt_sha,
                started_at_ms=None if receipt is None else receipt.started_at_ms,
                ended_at_ms=_clock_ms(),
                provider_status=None if receipt is None else receipt.status,
                analysis_status=status,
                error_code=analysis_error_code or (None if receipt is None else receipt.error_code),
                validation_errors=validation_errors,
                input_tokens=None if receipt is None else receipt.input_tokens,
                output_tokens=None if receipt is None else receipt.output_tokens,
                cost_microusd=None if receipt is None else receipt.cost_microusd,
                calls=tuple(call_rows),
                known_cost_microusd=0 if receipt is None else receipt.known_cost_microusd,
                unknown_cost_calls=0 if receipt is None else receipt.unknown_cost_calls,
                cost_upper_estimate_microusd=None if receipt is None else receipt.cost_upper_estimate_microusd,
            ),
            transaction=True,
        )
        settled_at_ms = _clock_ms()
        settled = await self._db_async(
            lambda repos: repos.trading.finish_analysis_case(
                case_id=case["case_id"],
                claim_token=case["claim_token"],
                now_ms=settled_at_ms,
                analysis_status=status,
                evidence_ref=evidence_ref,
                decision=decision,
                assessment_ref=assessment_ref,
                prepared_signal=prepared_signal,
                publish_block_reason=publish_block_reason,
            ),
            transaction=True,
        )
        if not settled:
            await self._db_async(
                lambda repos: repos.trading.mark_analysis_attempt_unsettled(
                    case_id=case["case_id"],
                    claim_attempt=int(case["claim_attempt"]),
                    claim_token=case["claim_token"],
                ),
                transaction=True,
            )
        if settled and decision is not None and decision.get("action") == "TRADE":
            try:
                await self._start_shadow_evaluation(case, decision, decision_at_ms=settled_at_ms)
            except Exception as exc:
                error_type = type(exc).__name__
                await self._db_async(
                    lambda repos, error_type=error_type: repos.trading.record_shadow_evaluation(
                        case_id=case["case_id"],
                        decision_at_ms=settled_at_ms,
                        scheduled_at_ms=settled_at_ms,
                        due_at_ms=settled_at_ms,
                        decision_quote_ref=None,
                        planned_quote_ref=None,
                        initial_result={
                            "status": "unevaluable",
                            "source": "shadow_simulation",
                            "evaluation_version": EVALUATION_VERSION,
                            "reason": f"capture_{error_type}",
                        },
                    ),
                    transaction=True,
                )
        return True

    async def _read_executable_quote(self, case: dict[str, Any]) -> dict[str, Any]:
        instrument = case["target_selection"]["instrument"]
        request = MarketDataRequest(
            dataset="book_ticker",
            native_symbol=str(instrument["native_symbol"]),
            venue="binance.usdm",
            environment=str(instrument["environment"]),
            product="perpetual",
            source_identity="binance_public_v1",
            unit_definition="bid_ask_quote_per_base_v1",
            start_ms=None,
            end_ms=None,
            interval_ms=None,
            max_age_ms=5_000,
            deadline_at_monotonic=time.monotonic() + 5.0,
        )
        try:
            result = await self.reader.market_data.fetch(request)
            return {
                "status": result.status,
                "payload": result.payload,
                "environment": str(instrument["environment"]),
                "source_identity": result.source_identity,
                "unit_definition": result.unit_definition,
                "request_receipts": result.request_receipts,
                "missing_reasons": result.missing_reasons,
            }
        except (TimeoutError, ValueError, OSError) as exc:
            return {"status": "error", "payload": (), "missing_reasons": (type(exc).__name__,)}

    async def _start_shadow_evaluation(
        self,
        case: dict[str, Any],
        decision: dict[str, Any],
        *,
        decision_at_ms: int,
    ) -> None:
        first_quote = await self._read_executable_quote(case)
        target_at = decision_at_ms + self.settings.trading.analysis.shadow_order_latency_ms
        if _clock_ms() < target_at:
            await asyncio.sleep((target_at - _clock_ms()) / 1_000)
        scheduled_at_ms = _clock_ms()
        planned_quote = await self._read_executable_quote(case)
        first_ref = await asyncio.to_thread(self.files.write, first_quote)
        planned_ref = await asyncio.to_thread(self.files.write, planned_quote)
        valid_quotes = (
            first_quote["status"] == "ok"
            and bool(first_quote["payload"])
            and planned_quote["status"] == "ok"
            and bool(planned_quote["payload"])
        )
        initial_result = (
            None
            if valid_quotes
            else {
                "status": "unevaluable",
                "reason": "executable_quote_missing",
                "source": "shadow_simulation",
                "evaluation_version": EVALUATION_VERSION,
            }
        )
        plan = ExitPlan.model_validate(decision["exit_plan"])
        due_at_ms = scheduled_at_ms + plan.max_holding_seconds * 1_000 + 60_000
        await self._db_async(
            lambda repos: repos.trading.record_shadow_evaluation(
                case_id=case["case_id"],
                decision_at_ms=decision_at_ms,
                scheduled_at_ms=scheduled_at_ms,
                due_at_ms=due_at_ms,
                decision_quote_ref=first_ref,
                planned_quote_ref=planned_ref,
                initial_result=initial_result,
            ),
            transaction=True,
        )

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
        expiry_ns = min(
            int(case["root_expires_at_ms"]) * 1_000_000,
            (prepared.reference_at_ms + ENTRY_WINDOW_MS) * 1_000_000,
        )
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
            entry_envelope=SignalEntryEnvelopeV2(
                root_expires_at_ns=int(case["root_expires_at_ms"]) * 1_000_000,
                reference_price=prepared.reference_price,
                structure_level=next(
                    candidate.entry_level
                    for candidate in prepared.candidates
                    if candidate.candidate_id == decision["entry_candidate_id"]
                ),
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

    async def label_once(self, *, limit: int = 4, label_version: str | None = None) -> int:
        """Fill due opportunity paths without holding a database transaction over I/O."""
        due = await self._db_async(
            lambda repos: repos.trading.due_analysis_outcomes(
                now_ms=_clock_ms(), limit=limit, label_version=label_version
            ),
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
                        result.payload,
                        anchor_ms=anchor,
                        horizon_seconds=int(row["horizon_seconds"]),
                        market_status=result.status,
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
            ref = await asyncio.to_thread(self.files.write, path)
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

    async def watch_once(self, *, limit: int = 8) -> int:
        """Observe closed bars; only a committed match may create a child Case."""
        rows = await self._db_async(lambda repos: repos.trading.due_watch_observations(now_ms=_clock_ms(), limit=limit))
        for row in rows:
            now = _clock_ms()
            condition = row["condition"]
            last_at = row["last_observed_at_ms"]
            observed_at: int | None = last_at
            observed_value: str | None = None
            previous_close: str | None = None
            trigger_side: str | None = None
            observed_path: list[tuple[int, str]] = []
            observation_status = "not_met"
            market_snapshot: dict[str, Any] = {"status": "not_requested"}
            selection = row["target_selection"] or {}
            instrument = selection.get("instrument") if isinstance(selection, dict) else None
            if not isinstance(instrument, dict):
                observation_status = "data_missing"
            else:
                start_at = int(last_at or condition["frozen_at_ms"])
                end_at = min(now, int(row["expires_at_ms"])) // _BAR_MS * _BAR_MS
                if end_at > start_at:
                    request = MarketDataRequest(
                        dataset="perp_bars",
                        native_symbol=str(instrument["native_symbol"]),
                        venue="binance.usdm",
                        environment=str(instrument["environment"]),
                        product="perpetual",
                        source_identity="binance_public_v1",
                        unit_definition="quote_per_base_and_volume_v1",
                        start_ms=start_at // _BAR_MS * _BAR_MS,
                        end_ms=end_at,
                        interval_ms=_BAR_MS,
                        max_age_ms=None,
                        deadline_at_monotonic=time.monotonic() + 5.0,
                    )
                    try:
                        result = await self.reader.market_data.fetch(request)
                        market_snapshot = {
                            "status": result.status,
                            "payload": result.payload,
                            "source_identity": result.source_identity,
                            "source_version": result.source_version,
                            "unit_definition": result.unit_definition,
                            "request_receipts": result.request_receipts,
                            "missing_reasons": result.missing_reasons,
                        }
                        if result.status not in ("ok", "partial"):
                            observation_status = "data_missing"
                        else:
                            next_at = start_at + _BAR_MS
                            previous = Decimal(
                                str(
                                    row["last_observed_value"]
                                    if row["last_observed_value"] is not None
                                    else condition["previous_close"]
                                )
                            )
                            for bar in sorted(result.payload, key=lambda item: int(item["event_at_ms"])):
                                stamp = int(bar["event_at_ms"])
                                if stamp < next_at or stamp > now:
                                    continue
                                if stamp != next_at:
                                    observation_status = "data_missing"
                                    break
                                value = Decimal(str(bar["close"]))
                                observed_at, observed_value = stamp, str(value)
                                observed_path.append((stamp, str(value)))
                                previous_close = str(previous)
                                side = range_cross_side(
                                    previous_close=previous,
                                    close=value,
                                    upper=Decimal(str(condition["upper_level"])),
                                    lower=Decimal(str(condition["lower_level"])),
                                )
                                if side is not None:
                                    trigger_side = side
                                    observation_status = (
                                        "satisfied"
                                        if now < min(int(row["expires_at_ms"]), stamp + ENTRY_WINDOW_MS)
                                        else "missed"
                                    )
                                    break
                                previous, next_at = value, stamp + _BAR_MS
                            if (
                                observed_at == last_at
                                and observation_status == "not_met"
                                and result.status == "partial"
                            ):
                                observation_status = "data_missing"
                    except (TimeoutError, ValueError, OSError) as exc:
                        observation_status = "data_missing"
                        market_snapshot = {"status": "error", "error_type": type(exc).__name__}

            if observation_status == "not_met" and not observed_path:
                if now < int(row["expires_at_ms"]):
                    continue
                observation_status = "expired"
            observation_ref = await asyncio.to_thread(
                self.files.write,
                {
                    "parent_case_id": row["parent_case_id"],
                    "condition": condition,
                    "checked_at_ms": now,
                    "observation_status": observation_status,
                    "observed_at_ms": observed_at,
                    "observed_value": observed_value,
                    "previous_close": previous_close,
                    "trigger_side": trigger_side,
                    "observed_path": observed_path,
                    "market": market_snapshot,
                },
            )

            def advance(
                repos: Any,
                parent_case_id: str = str(row["parent_case_id"]),
                checked_at_ms: int = now,
                check_status: str = observation_status,
                checked_bar_at_ms: int | None = observed_at,
                checked_value: str | None = observed_value,
                checked_previous_close: str | None = previous_close,
                checked_side: str | None = trigger_side,
                checked_path: tuple[tuple[int, str], ...] = tuple(observed_path),
                checked_ref: str = observation_ref,
            ) -> bool:
                return bool(
                    repos.trading.advance_watch_observation(
                        parent_case_id=parent_case_id,
                        now_ms=checked_at_ms,
                        observation_status=check_status,
                        observed_at_ms=checked_bar_at_ms,
                        observed_value=checked_value,
                        previous_close=checked_previous_close,
                        trigger_side=checked_side,
                        observed_path=checked_path,
                        observation_ref=checked_ref,
                    )
                )

            await self._db_async(advance, transaction=True)
        return len(rows)

    def _watch_completed(self, task: asyncio.Task[int]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.exception("analysis_watch_observation_failed")

    async def evaluate_once(self, *, limit: int = 4) -> int:
        """Label due shadow paths with executable quotes and explicit costs."""
        rows = await self._db_async(lambda repos: repos.trading.due_shadow_evaluations(now_ms=_clock_ms(), limit=limit))
        for row in rows:
            now = _clock_ms()
            decision = row["decision"]
            plan = ExitPlan.model_validate(decision["exit_plan"])
            selection = row["target_selection"] or {}
            instrument = selection.get("instrument") if isinstance(selection, dict) else None
            mark_ref: str | None = None
            funding_ref: str | None = None
            if not isinstance(instrument, dict):
                result = {
                    "status": "unevaluable",
                    "reason": "instrument_identity_missing",
                    "evaluation_version": EVALUATION_VERSION,
                    "source": "shadow_simulation",
                }
            else:
                try:
                    decision_quote_snapshot = await asyncio.to_thread(self.files.read, str(row["decision_quote_ref"]))
                    planned_quote_snapshot = await asyncio.to_thread(self.files.read, str(row["planned_quote_ref"]))
                    decision_quote = decision_quote_snapshot["payload"][0]
                    planned_quote = planned_quote_snapshot["payload"][0]
                    quote_environment = str(decision_quote_snapshot["environment"])
                    if quote_environment != str(planned_quote_snapshot["environment"]):
                        quote_environment = "mixed"
                except (OSError, ValueError, KeyError, IndexError, TypeError):
                    decision_quote = planned_quote = None
                    quote_environment = "unknown"
                start_at = int(row["scheduled_at_ms"])
                end_at = start_at + plan.max_holding_seconds * 1_000
                mark_request = MarketDataRequest(
                    dataset="mark_bars",
                    native_symbol=str(instrument["native_symbol"]),
                    venue="binance.usdm",
                    environment=str(instrument["environment"]),
                    product="perpetual",
                    source_identity="binance_public_v1",
                    unit_definition="mark_quote_per_base_v1",
                    start_ms=start_at // _BAR_MS * _BAR_MS,
                    end_ms=(end_at // _BAR_MS + 2) * _BAR_MS,
                    interval_ms=_BAR_MS,
                    max_age_ms=None,
                    deadline_at_monotonic=time.monotonic() + 10.0,
                )
                funding_request = MarketDataRequest(
                    dataset="funding_history",
                    native_symbol=str(instrument["native_symbol"]),
                    venue="binance.usdm",
                    environment=str(instrument["environment"]),
                    product="perpetual",
                    source_identity="binance_public_v1",
                    unit_definition="funding_rate_fraction_v1",
                    start_ms=start_at,
                    end_ms=end_at,
                    interval_ms=None,
                    max_age_ms=None,
                    deadline_at_monotonic=time.monotonic() + 10.0,
                )
                answers = await asyncio.gather(
                    self.reader.market_data.fetch(mark_request),
                    self.reader.market_data.fetch(funding_request),
                    return_exceptions=True,
                )
                mark = answers[0] if isinstance(answers[0], MarketDataResult) else None
                funding = answers[1] if isinstance(answers[1], MarketDataResult) else None
                if mark is not None:
                    mark_ref = await asyncio.to_thread(
                        self.files.write,
                        {
                            "status": mark.status,
                            "payload": mark.payload,
                            "source_identity": mark.source_identity,
                            "unit_definition": mark.unit_definition,
                            "request_receipts": mark.request_receipts,
                            "missing_reasons": mark.missing_reasons,
                        },
                    )
                if funding is not None:
                    funding_ref = await asyncio.to_thread(
                        self.files.write,
                        {
                            "status": funding.status,
                            "payload": funding.payload,
                            "source_identity": funding.source_identity,
                            "unit_definition": funding.unit_definition,
                            "request_receipts": funding.request_receipts,
                            "missing_reasons": funding.missing_reasons,
                        },
                    )
                result = evaluate_shadow(
                    side=str(decision["side"]),
                    decision_at_ms=int(row["decision_at_ms"]),
                    scheduled_at_ms=start_at,
                    decision_quote=decision_quote,
                    planned_quote=planned_quote,
                    mark_rows=() if mark is None else mark.payload,
                    mark_status="error" if mark is None else mark.status,
                    funding_events=() if funding is None else funding.payload,
                    funding_coverage_complete=funding is not None and funding.status == "ok",
                    exit_plan=plan,
                    fee_bps_per_side=self.settings.trading.analysis.shadow_fee_bps_per_side,
                    exit_spread_bps=self.settings.trading.analysis.shadow_exit_spread_bps,
                    quote_environment=quote_environment,
                    target_environment=str(instrument["environment"]),
                )
            retryable = result.get("reason") in (
                "mark_path_incomplete",
                "mark_path_gap",
                "mark_endpoint_missing",
                "funding_coverage_missing",
            )
            if retryable and now < int(row["due_at_ms"]) + 48 * 3_600_000:
                await self._db_async(
                    lambda repos, row=row, now=now: repos.trading.retry_shadow_evaluation(
                        case_id=row["case_id"],
                        next_attempt_at_ms=now + 300_000,
                    ),
                    transaction=True,
                )
                continue
            await self._db_async(
                lambda repos, row=row, result=result, mark_ref=mark_ref, funding_ref=funding_ref, now=now: (
                    repos.trading.settle_shadow_evaluation(
                        case_id=row["case_id"],
                        result=result,
                        mark_path_ref=mark_ref,
                        funding_ref=funding_ref,
                        now_ms=now,
                    )
                ),
                transaction=True,
            )
        return len(rows)

    def _evaluation_completed(self, task: asyncio.Task[int]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.exception("analysis_shadow_evaluation_failed")

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
                    if self._watch_task is None or self._watch_task.done():
                        self._watch_task = asyncio.create_task(self.watch_once())
                        self._watch_task.add_done_callback(self._watch_completed)
                    if self._evaluation_task is None or self._evaluation_task.done():
                        self._evaluation_task = asyncio.create_task(self.evaluate_once())
                        self._evaluation_task.add_done_callback(self._evaluation_completed)
                except Exception:
                    _LOG.exception("analysis_runner_cycle_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        finally:
            if self._active:
                await asyncio.gather(*self._active, return_exceptions=True)
            if self._label_task is not None:
                await asyncio.gather(self._label_task, return_exceptions=True)
            if self._watch_task is not None:
                await asyncio.gather(self._watch_task, return_exceptions=True)
            if self._evaluation_task is not None:
                await asyncio.gather(self._evaluation_task, return_exceptions=True)
            self._db_executor.shutdown(wait=True)
