"""Per-attempt read-only ReAct tools with Case fences and append-only observations."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import dspy  # type: ignore[import-untyped]
from dspy.adapters.types.decision import Choice  # type: ignore[import-untyped]
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
)

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.system_one import SystemOneConnection, SystemOneReceipt
from tracefold.app.trading_analyst import PhysicalModelCall
from tracefold.trading.engine.features import catalyst_text_values
from tracefold.trading.engine.marketdata import Dataset, MarketDataPort, MarketDataRequest
from tracefold.trading.engine.plans import EntryPlan, build_entry_plans
from tracefold.trading.engine.policy import is_citable_evidence

_BAR_MS = 60_000
_OI_PERIOD_MS = 300_000
_MARKET_DATASETS = frozenset(
    {"perp_bars", "spot_bars", "market_bars", "open_interest_history", "open_interest", "funding_basis"}
)


class ClaimSupport(dspy.Signature):
    """Judge whether the selected evidence supports one precise claim."""

    claim: str = dspy.InputField(desc="A single, bounded factual claim")
    evidence: list[dict[str, str]] = dspy.InputField(desc="Selected source refs, text, units and event times")
    verdict: Choice[
        ("supports", "The selected evidence supports the claim."),  # noqa: F722, F821, UP037
        ("contradicts", "The selected evidence contradicts the claim."),  # noqa: F722, F821, UP037
        ("mixed", "The selected evidence both supports and contradicts the claim."),  # noqa: F722, F821, UP037
        ("insufficient", "The selected evidence is insufficient."),  # noqa: F722, F821, UP037
    ] = dspy.OutputField(desc="Classify support by the cited material only; this is not trade approval.")


class _ToolFatal(RuntimeError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _content_sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _source_text(payload: dict[str, Any]) -> str:
    """A News catalyst's deterministic public text; any other source fact as its bounded JSON."""

    text = catalyst_text_values(payload).get("text")
    return (text if text is not None else _json(payload))[:2_048]


class CaseToolContext:
    def __init__(
        self,
        *,
        case: dict[str, Any],
        source: dict[str, Any],
        source_first_visible_at_ms: int,
        prepared: Any,
        market_data: MarketDataPort,
        files: AnalysisFiles,
        file_io: Callable[..., Awaitable[Any]],
        authorize: Callable[[], Awaitable[bool]],
        source_history_at: Callable[[int], Awaitable[tuple[dict[str, Any], ...]]],
        semantics: SystemOneConnection | None,
    ) -> None:
        self.case = case
        self.source = source
        self.source_first_visible_at_ms = source_first_visible_at_ms
        self.prepared = prepared
        self.market_data = market_data
        self.files = files
        self.file_io = file_io
        self.authorize = authorize
        self.source_history_at = source_history_at
        self.semantics = semantics
        self.evidence_catalog = dict(prepared.brief.evidence_catalog)
        self.plans: dict[str, EntryPlan] = {plan.plan_id: plan for plan in prepared.plans}
        self.judgment_refs: set[str] = set()
        self.tool_refs: list[str] = []
        self.market_artifacts: dict[str, str] = {}
        self.context_artifacts: dict[str, str] = {}
        self._fatal: str | None = None
        self._ledger: Any = None

    def fatal_error(self) -> str | None:
        return self._fatal

    def tools(self, ledger: Any) -> list[dspy.Tool]:
        self._ledger = ledger
        result = [
            dspy.Tool(self.get_event_context),
            dspy.Tool(self.get_market_snapshot),
            dspy.Tool(self.read_evidence),
        ]
        if self.semantics is not None:
            result.append(dspy.Tool(self.assess_claims))
        return result

    async def _check(self) -> None:
        if self._fatal is not None:
            raise _ToolFatal(self._fatal)
        if not await self.authorize():
            self._fatal = "analysis_tool_scope_expired"
            raise _ToolFatal(self._fatal)

    async def _invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> str:
        await self._check()
        try:
            result = await operation()
        except ValueError as exc:
            result = {"status": "error", "reason": str(exc)[:96]}
        except (TypeSafeAPIError, TypeSafeAPIConnectionError, TypeSafeAPIResponseValidationError) as exc:
            result = {"status": "error", "reason": type(exc).__name__}
        except (TimeoutError, OSError) as exc:
            result = {"status": "error", "reason": type(exc).__name__}
        except _ToolFatal as exc:
            self._fatal = str(exc)
            raise
        except Exception as exc:
            self._fatal = f"tool_{type(exc).__name__}"
            raise _ToolFatal(self._fatal) from exc
        await self._check()
        record = {
            "tool_version": "trading_tool_v1",
            "case_id": self.case["case_id"],
            "claim_attempt": self.case["claim_attempt"],
            "tool": name,
            "arguments": arguments,
            "result": result,
            "available_at_ms": int(time.time() * 1000),
        }
        try:
            ref = await self.file_io(self.files.write, record)
        except Exception as exc:
            self._fatal = "tool_archive_failed"
            raise _ToolFatal(self._fatal) from exc
        self.tool_refs.append(ref)
        return _json({**result, "tool_ref": ref})

    async def get_event_context(self, topic: str, lookback_minutes: int = 60) -> str:
        """Read bounded same-asset public event history known during this Case."""

        async def operation() -> dict[str, Any]:
            if not 1 <= len(topic) <= 80 or lookback_minutes not in (15, 60, 240, 1_440):
                raise ValueError("event_context_arguments_invalid")
            now = int(time.time() * 1000)
            rows = await self.source_history_at(now)
            selected = [
                item
                for item in rows
                if int(item.get("first_visible_at_ms") or 0) >= now - lookback_minutes * 60_000
                and topic.casefold() in _json(item.get("payload") or {}).casefold()
            ][:8]
            events = []
            for item in selected:
                payload = item.get("payload") or {}
                ref = f"event:{_content_sha((item.get('trigger_id'), item.get('source_revision'), payload))}"
                artifact_ref = await self.file_io(self.files.write, item)
                self.context_artifacts[ref] = artifact_ref
                first_visible = int(item["first_visible_at_ms"])
                self.evidence_catalog.setdefault(
                    ref,
                    {
                        "status": "ok",
                        "source_ref": artifact_ref,
                        "values": {"text": _source_text(payload)},
                        "unit_definition": {"text": "source_text"},
                        "event_at_ms": int(item.get("source_observed_at_ms") or first_visible),
                        "received_at_ms": first_visible,
                        "knowledge_cutoff_ms": now,
                    },
                )
                events.append(
                    {
                        "ref": ref,
                        "first_visible_at_ms": first_visible,
                        "source_revision": item.get("source_revision"),
                        "text": _source_text(payload),
                    }
                )
            return {
                "status": "ok" if selected else "missing",
                "topic": topic,
                "known_at_ms": now,
                "events": events,
                "truncated": len(rows) > len(selected),
            }

        return await self._invoke(
            "get_event_context", {"topic": topic, "lookback_minutes": lookback_minutes}, operation
        )

    async def get_market_snapshot(self, dataset: str, window_minutes: int = 60) -> str:
        """Read one allowed Binance dataset and append supported plans without replacing the seed."""

        async def operation() -> dict[str, Any]:
            if dataset not in _MARKET_DATASETS:
                raise ValueError("market_dataset_invalid")
            if dataset in ("open_interest", "funding_basis"):
                if window_minutes != 0:
                    raise ValueError("market_window_invalid")
            elif window_minutes not in (15, 60, 240):
                raise ValueError("market_window_invalid")
            instrument = self.case["target_selection"]["instrument"]
            native = "BTCUSDT" if dataset == "market_bars" else str(instrument["native_symbol"])
            environment = str(instrument["environment"])
            now = int(time.time() * 1000)
            interval = _OI_PERIOD_MS if dataset == "open_interest_history" else _BAR_MS
            end = now // interval * interval if dataset not in ("open_interest", "funding_basis") else None
            start = (
                end - window_minutes * _BAR_MS
                if dataset == "open_interest_history" and end is not None
                else end - (window_minutes + 1) * interval
                if end is not None
                else None
            )
            request = MarketDataRequest(
                dataset=cast(Dataset, dataset),
                native_symbol=native,
                venue="binance.usdm",
                environment=environment,
                product="spot" if dataset == "spot_bars" else "perpetual",
                source_identity="binance_public_v1",
                unit_definition=(
                    "base_quantity_and_quote_value_v1"
                    if dataset == "open_interest_history"
                    else "native_contract_quantity_v1"
                    if dataset == "open_interest"
                    else "quote_price_and_rate_v1"
                    if dataset == "funding_basis"
                    else "quote_per_base_and_volume_v1"
                ),
                start_ms=start,
                end_ms=end,
                interval_ms=interval if end is not None else None,
                max_age_ms=None if end is not None else 90_000,
                deadline_at_monotonic=time.monotonic() + 5.0,
            )
            result = await self.market_data.fetch(request)
            available = int(time.time() * 1000)
            if result.source_identity != request.source_identity or result.unit_definition != request.unit_definition:
                raise _ToolFatal("market_response_identity_mismatch")
            if result.received_at_ms is not None and result.received_at_ms > available:
                raise _ToolFatal("market_response_time_invalid")
            raw = {
                "request": request.__dict__
                if hasattr(request, "__dict__")
                else {key: getattr(request, key) for key in request.__dataclass_fields__},
                "result": {key: getattr(result, key) for key in result.__dataclass_fields__},
                "available_at_ms": available,
            }
            artifact_ref = await self.file_io(self.files.write, raw)
            stable_rows = [
                {
                    key: value
                    for key, value in row.items()
                    if key not in ("received_at_ms", "server_clock_sampled_at_ms")
                }
                for row in result.payload
            ]
            ref = f"market:{dataset}:{_content_sha((dataset, environment, native, stable_rows))}"
            self.market_artifacts[ref] = artifact_ref
            last = result.payload[-1] if result.payload else {}
            values = {
                key: last[key]
                for key in (
                    "close",
                    "open_interest_quantity",
                    "sum_open_interest_quantity",
                    "sum_open_interest_value",
                    "mark_price",
                    "index_price",
                    "last_funding_rate",
                )
                if key in last
            }
            if dataset == "open_interest_history" and result.status == "ok" and len(result.payload) >= 2:
                try:
                    first = Decimal(str(result.payload[0]["sum_open_interest_quantity"]))
                    final = Decimal(str(result.payload[-1]["sum_open_interest_quantity"]))
                    if first > 0 and final.is_finite():
                        values["oi_change_bps"] = str((final / first - 1) * 10_000)
                except (KeyError, InvalidOperation, TypeError):
                    pass
            self.evidence_catalog.setdefault(
                ref,
                {
                    "status": result.status,
                    "source_ref": artifact_ref,
                    "source": result.source_identity,
                    "values": values,
                    "unit_definition": result.unit_definition,
                    "event_at_ms": result.event_end_ms,
                    "received_at_ms": result.received_at_ms,
                    "knowledge_cutoff_ms": available,
                    "missing_reasons": result.missing_reasons,
                },
            )
            added_plans: list[dict[str, Any]] = []
            if dataset == "perp_bars" and result.status == "ok":
                for plan in build_entry_plans(
                    asset_id=str(self.case["target_asset_id"]),
                    instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
                    source_revision=str(self.source["source_revision"]),
                    source_fact=self.source["payload"],
                    source_first_visible_at_ms=self.source_first_visible_at_ms,
                    root_expires_at_ms=int(self.case["root_expires_at_ms"]),
                    perp_rows=result.payload,
                    parent_condition=(self.case.get("manifest") or {}).get("watch_condition")
                    if self.case.get("run_kind") == "conditional"
                    else None,
                    price_ref=ref,
                ):
                    if plan.plan_id not in self.plans:
                        self.plans[plan.plan_id] = plan
                        added_plans.append(plan.model_dump(mode="json"))
            return {
                "status": "unsupported" if result.status == "not_applicable" else result.status,
                "reason": result.missing_reasons,
                "dataset": dataset,
                "window_minutes": window_minutes,
                "environment": environment,
                "native_symbol": native,
                "ref": ref,
                "artifact_ref": artifact_ref,
                "event_start_ms": result.event_start_ms,
                "event_end_ms": result.event_end_ms,
                "received_at_ms": result.received_at_ms,
                "available_at_ms": available,
                "row_count": len(result.payload),
                "values": values,
                "added_plans": added_plans,
            }

        return await self._invoke(
            "get_market_snapshot", {"dataset": dataset, "window_minutes": window_minutes}, operation
        )

    async def read_evidence(self, ref: str, start: int = 0, end: int = 4_096) -> str:
        """Read a bounded portion of an already granted Case evidence reference."""

        async def operation() -> dict[str, Any]:
            if ref not in self.evidence_catalog or start < 0 or end <= start or end - start > 8_192:
                raise ValueError("evidence_range_or_ref_invalid")
            if ref == "source":
                material = self.source["payload"]
            elif ref in self.context_artifacts:
                material = await self.file_io(self.files.read, self.context_artifacts[ref])
            elif ref in self.market_artifacts:
                material = await self.file_io(self.files.read, self.market_artifacts[ref])
            elif ref.startswith("market:"):
                dataset = ref.removeprefix("market:")
                snapshot = await self.file_io(self.files.read, self.prepared.evidence_ref)
                material = snapshot.get("market", {}).get(dataset)
            else:
                material = self.evidence_catalog[ref]
            rendered = _json(material)
            return {
                "status": "ok",
                "ref": ref,
                "text": rendered[start:end],
                "truncated": end < len(rendered),
                "total_chars": len(rendered),
            }

        return await self._invoke("read_evidence", {"ref": ref, "start": start, "end": end}, operation)

    async def assess_claims(self, claim: str, fact_refs: list[str]) -> str:
        """Ask native DSPy Predict/Choice about one claim and existing citable facts."""

        async def operation() -> dict[str, Any]:
            if self.semantics is None:
                raise ValueError("jev_unconfigured")
            if not 1 <= len(claim) <= 500 or not 1 <= len(fact_refs) <= 8 or len(set(fact_refs)) != len(fact_refs):
                raise ValueError("claim_arguments_invalid")
            if any(not is_citable_evidence(self.evidence_catalog.get(ref) or {}) for ref in fact_refs):
                raise ValueError("claim_fact_ref_unavailable")
            evidence = [{"ref": ref, "material": _json(self.evidence_catalog[ref])[:4_096]} for ref in fact_refs]
            holder: dict[str, int] = {}

            async def before(request: dict[str, Any]) -> None:
                index, _timeout = await self._ledger.start(request)
                holder["index"] = index

            async def after(receipt: SystemOneReceipt) -> None:
                index = holder["index"]
                try:
                    await self._ledger.finish(
                        index,
                        PhysicalModelCall(
                            request_payload=receipt.request_payload,
                            response_payload=receipt.response_payload,
                            input_tokens=receipt.input_tokens,
                            output_tokens=receipt.output_tokens,
                            cost_microusd=receipt.cost_microusd,
                            cost_unknown_reason="provider_cost_unavailable" if receipt.cost_microusd is None else None,
                            status="result_unknown" if receipt.response_payload is None else "completed",
                            finished_at_ms=int(time.time() * 1000),
                            error_type=receipt.error_type,
                            phase="jev",
                            endpoint=receipt.endpoint,
                            requested_model=receipt.requested_model,
                            served_model=receipt.served_model,
                        ),
                    )
                except Exception as exc:
                    self._fatal = "jev_call_record_failed"
                    raise _ToolFatal(self._fatal) from exc

            lm = self.semantics.bind(before_call=before, after_call=after)
            answer = await dspy.Predict(ClaimSupport).acall(claim=claim, evidence=evidence, lm=lm)
            verdict = answer.verdict
            receipt = lm.history[-1]
            judgment = {
                "judgment_version": "claim_support_v1",
                "claim": claim,
                "fact_refs": fact_refs,
                "value": verdict.value,
                "confidence": verdict.confidence,
                "probabilities": verdict.probabilities,
                "endpoint": receipt.endpoint,
                "requested_model": receipt.requested_model,
                "served_model": receipt.served_model,
                "provider_request_id": receipt.request_id,
                "provider": receipt.provider,
                "available_at_ms": int(time.time() * 1000),
            }
            try:
                ref = await self.file_io(self.files.write, judgment)
            except Exception as exc:
                self._fatal = "jev_judgment_archive_failed"
                raise _ToolFatal(self._fatal) from exc
            self.judgment_refs.add(ref)
            return {"status": "ok", "judgment_ref": ref, "verdict": verdict.value}

        return await self._invoke("assess_claims", {"claim": claim, "fact_refs": fact_refs}, operation)
