"""Per-attempt read-only ReAct tools with Case fences and append-only observations."""

from __future__ import annotations

import asyncio
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
from tracefold.trading.engine.features import (
    PROFILE_VERSION,
    WINDOW_VERSION,
    catalyst_text_values,
    closed_bar_window,
    extract_features,
    price_plan_window,
    window_ref,
)
from tracefold.trading.engine.marketdata import Dataset, MarketDataPort, MarketDataResult, analysis_market_request
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
    return text if text is not None else _json(payload)


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
        source_history_at: Callable[..., Awaitable[tuple[dict[str, Any], ...]]],
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
        self.event_materials: dict[str, dict[str, Any]] = {}
        self.amendment_materials: dict[str, dict[str, Any]] = {}
        self._fatal: str | None = None
        self._ledger: Any = None
        seed = json.loads(getattr(prepared.brief, "text", "{}"))
        for item in getattr(prepared, "source_history", ()):
            self._register_event(
                item,
                prepared.evidence_ref,
                int(seed.get("evidence", {}).get("source", {}).get("knowledge_cutoff_ms") or time.time() * 1000),
            )
        for item in getattr(prepared, "source_amendments", ()):
            self.amendment_materials["amendment:" + _content_sha(item)] = item

    def _register_plan(self, plan: EntryPlan, now_ms: int) -> bool:
        """Keep the first valid menu identity for an equivalent consumed window."""
        if plan.plan_id in self.plans:
            return False
        if any(not is_citable_evidence(self.evidence_catalog.get(ref) or {}) for ref in plan.required_evidence_refs):
            return False

        def key(item: EntryPlan) -> tuple[Any, ...]:
            material = tuple(
                self.evidence_catalog.get(ref, {}).get("window_identity", ref) for ref in item.required_evidence_refs
            )
            fields = item.model_dump(mode="json", exclude={"plan_id", "required_evidence_refs"})
            return (_json(fields), material)

        for prior in self.plans.values():
            if prior.expires_at_ms <= now_ms:
                continue
            if any(
                not is_citable_evidence(self.evidence_catalog.get(ref) or {}) for ref in prior.required_evidence_refs
            ):
                continue
            if key(prior) == key(plan):
                return False
        self.plans[plan.plan_id] = plan
        return True

    def _register_event(self, item: dict[str, Any], artifact_ref: str, known_at_ms: int) -> str:
        payload = item.get("payload") or {}
        ref = f"event:{_content_sha((item.get('trigger_id'), item.get('source_revision'), payload))}"
        first_visible = int(item["first_visible_at_ms"])
        self.event_materials.setdefault(ref, item)
        self.context_artifacts.setdefault(ref, artifact_ref)
        self.evidence_catalog.setdefault(
            ref,
            {
                "status": "ok",
                "source_ref": self.context_artifacts[ref],
                "source_revision": item.get("source_revision"),
                "values": {"text": _source_text(payload)},
                "unit_definition": {"text": "source_text"},
                "event_at_ms": int(item.get("source_observed_at_ms") or first_visible),
                "received_at_ms": first_visible,
                "knowledge_cutoff_ms": known_at_ms,
            },
        )
        return ref

    def fatal_error(self) -> str | None:
        return self._fatal

    def correction_facts(self) -> dict[str, Any]:
        return {
            ref: {
                "values": {
                    key: value[:160] if isinstance(value, str) else value
                    for key, value in (item.get("values") or {}).items()
                },
                "unit_definition": item.get("unit_definition"),
                "event_at_ms": item.get("event_at_ms"),
                "received_at_ms": item.get("received_at_ms"),
                "window_start_ms": item.get("window_start_ms"),
                "window_end_ms": item.get("window_end_ms"),
                "truncated": item.get("truncated", False),
            }
            for ref, item in self.evidence_catalog.items()
            if is_citable_evidence(item)
        }

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
        if hasattr(self._ledger, "remaining_ms"):
            self._ledger.remaining_ms()
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
            result = (
                await asyncio.wait_for(
                    operation(),
                    timeout=self._ledger.remaining_ms() / 1_000,
                )
                if hasattr(self._ledger, "remaining_ms")
                else await operation()
            )
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
            "tool_version": "trading_tool_v2",
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
            rows = await self.source_history_at(
                now,
                topic=topic,
                lookback_minutes=lookback_minutes,
                include_probe=True,
            )
            selected = rows[:8]
            events = []
            for item in selected:
                payload = item.get("payload") or {}
                ref = f"event:{_content_sha((item.get('trigger_id'), item.get('source_revision'), payload))}"
                artifact_ref = self.context_artifacts.get(ref) or await self.file_io(self.files.write, item)
                first_visible = int(item["first_visible_at_ms"])
                self._register_event(item, artifact_ref, now)
                events.append(
                    {
                        "ref": ref,
                        "first_visible_at_ms": first_visible,
                        "source_revision": item.get("source_revision"),
                        "text": _source_text(payload)[:2_048],
                    }
                )
            return {
                "status": "ok",
                "topic": topic,
                "known_at_ms": now,
                "lookback_minutes": lookback_minutes,
                "match_kind": "literal_case_insensitive",
                "events": events,
                "truncated": len(rows) > 8,
            }

        return await self._invoke(
            "get_event_context", {"topic": topic, "lookback_minutes": lookback_minutes}, operation
        )

    async def get_market_snapshot(self, dataset: str, window_minutes: int | None = None) -> str:
        """Read one allowed Binance dataset and append supported plans without replacing the seed."""

        async def operation() -> dict[str, Any]:
            if dataset not in _MARKET_DATASETS:
                raise ValueError("market_dataset_invalid")
            effective_window = (
                (0 if dataset in ("open_interest", "funding_basis") else 60)
                if window_minutes is None
                else window_minutes
            )
            if dataset in ("open_interest", "funding_basis"):
                if effective_window != 0:
                    raise ValueError("market_window_invalid")
            elif effective_window not in (15, 60, 240):
                raise ValueError("market_window_invalid")
            instrument = self.case["target_selection"]["instrument"]
            now = int(time.time() * 1000)
            interval = _OI_PERIOD_MS if dataset == "open_interest_history" else _BAR_MS
            end = now // interval * interval if dataset not in ("open_interest", "funding_basis") else None
            request = analysis_market_request(
                dataset=cast(Dataset, dataset),
                native_symbol=str(instrument["native_symbol"]),
                instrument_environment=str(instrument["environment"]),
                end_ms=end,
                window_minutes=effective_window if end is not None else None,
                deadline_at_monotonic=min(
                    time.monotonic() + 5.0,
                    getattr(self._ledger, "deadline_at_monotonic", float("inf")),
                ),
            )
            native = request.native_symbol
            environment = request.environment
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
            ref = f"market:{dataset}:{artifact_ref}"
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
                    if first.is_finite() and final.is_finite() and first > 0 and final >= 0:
                        values["oi_change_bps"] = str((final / first - 1) * 10_000)
                except (KeyError, InvalidOperation, TypeError):
                    pass
            derived: dict[str, Any] = {}
            covered_rows: tuple[dict[str, Any], ...] = ()
            missing = MarketDataResult(
                status="missing",
                payload=(),
                schema_version=result.schema_version,
                source_version=result.source_version,
                unit_definition=result.unit_definition,
                source_identity=result.source_identity,
                event_start_ms=None,
                event_end_ms=None,
                received_at_ms=None,
                missing_reasons=("not_requested",),
                request_receipts=(),
            )
            if dataset in ("perp_bars", "spot_bars", "market_bars"):
                if end is None:
                    raise ValueError("market_window_invalid")
                covered_rows = closed_bar_window(
                    result,
                    count=effective_window + 1,
                    end_ms=end,
                    cutoff_ms=available,
                    source_identity=request.source_identity,
                    unit_definition=request.unit_definition,
                )
                feature_set = extract_features(
                    {
                        name: result if name == dataset else missing
                        for name in ("perp_bars", "spot_bars", "market_bars", "open_interest", "funding_basis")
                    },
                    self.source["payload"],
                    expected_ends={dataset: end},
                    cutoff_ms=available,
                )
                prefixes = {"perp_bars": ("perp_",), "spot_bars": ("spot_",), "market_bars": ("btc_",)}
                derived = {
                    key: value
                    for key, value in feature_set.items()
                    if key.startswith(prefixes[dataset]) and value is not None
                }
                values.update(derived)
            elif dataset in ("open_interest", "funding_basis"):
                feature_set = extract_features(
                    {
                        name: result if name == dataset else missing
                        for name in _MARKET_DATASETS
                        if name != "open_interest_history"
                    },
                    self.source["payload"],
                    cutoff_ms=available,
                )
                names = (
                    ("binance_open_interest_quantity",)
                    if dataset == "open_interest"
                    else ("funding_rate_bps", "premium_bps")
                )
                derived = {name: feature_set[name] for name in names if feature_set[name] is not None}
                values.update(derived)
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
                    "environment": environment,
                    "native_symbol": native,
                },
            )
            if covered_rows:
                self.evidence_catalog.setdefault(
                    f"{ref}:window",
                    {
                        "status": "ok",
                        "source_ref": self.market_artifacts[ref],
                        "projection_version": WINDOW_VERSION,
                        "dataset": dataset,
                        "source": result.source_identity,
                        "environment": environment,
                        "native_symbol": native,
                        "window_start_ms": covered_rows[0]["event_at_ms"],
                        "window_end_ms": covered_rows[-1]["event_at_ms"],
                        "row_count": len(covered_rows),
                        "values": values,
                        "unit_definition": result.unit_definition,
                        "event_at_ms": covered_rows[-1]["event_at_ms"],
                        "received_at_ms": result.received_at_ms,
                        "knowledge_cutoff_ms": available,
                    },
                )
            feature_window_counts = {
                "perp_return_15m_bps": 16,
                "perp_return_60m_bps": 61,
                "perp_return_240m_bps": 241,
                "perp_volatility_1m_bps": 241,
                "perp_taker_buy_share_15m_bps": 15,
                "perp_taker_buy_share_60m_bps": 60,
                "spot_return_60m_bps": 61,
                "spot_taker_buy_share_60m_bps": 60,
                "btc_return_60m_bps": 61,
            }
            feature_refs: dict[str, str] = {}
            for name, value in derived.items():
                window = (
                    closed_bar_window(
                        result,
                        count=feature_window_counts[name],
                        end_ms=end,
                        cutoff_ms=available,
                        source_identity=request.source_identity,
                        unit_definition=request.unit_definition,
                    )
                    if name in feature_window_counts
                    else ()
                )
                feature_ref = f"feature:{name}:{artifact_ref}"
                self.evidence_catalog[feature_ref] = {
                    "status": "ok",
                    "source_ref": artifact_ref,
                    "dataset": dataset,
                    "projection_version": PROFILE_VERSION,
                    "source": result.source_identity,
                    "environment": environment,
                    "native_symbol": native,
                    "window_start_ms": window[0]["event_at_ms"] if window else None,
                    "window_end_ms": window[-1]["event_at_ms"] if window else None,
                    "row_count": len(window) if window else None,
                    "values": {"value": value},
                    "unit_definition": "bps" if name.endswith("_bps") else "native_contract_quantity",
                    "event_at_ms": window[-1]["event_at_ms"] if window else result.event_end_ms,
                    "received_at_ms": result.received_at_ms,
                    "knowledge_cutoff_ms": available,
                }
                feature_refs[name] = feature_ref
            added_plans: list[dict[str, Any]] = []
            price_rows = (
                price_plan_window(
                    result,
                    end_ms=end,
                    cutoff_ms=available,
                    source_identity=request.source_identity,
                    unit_definition=request.unit_definition,
                )
                if dataset == "perp_bars" and end is not None
                else ()
            )
            if price_rows:
                price_ref = window_ref(
                    dataset=dataset,
                    source=result.source_identity,
                    unit=result.unit_definition,
                    environment=environment,
                    symbol=native,
                    rows=price_rows,
                )
                self.evidence_catalog.setdefault(
                    price_ref,
                    {
                        "status": "ok",
                        "source_ref": artifact_ref,
                        "projection_version": WINDOW_VERSION,
                        "dataset": dataset,
                        "window_identity": price_ref,
                        "source": result.source_identity,
                        "environment": environment,
                        "native_symbol": native,
                        "window_start_ms": price_rows[0]["event_at_ms"],
                        "window_end_ms": price_rows[-1]["event_at_ms"],
                        "row_count": len(price_rows),
                        "values": {"close": price_rows[-1]["close"]},
                        "unit_definition": result.unit_definition,
                        "event_at_ms": price_rows[-1]["event_at_ms"],
                        "received_at_ms": result.received_at_ms,
                        "knowledge_cutoff_ms": available,
                    },
                )
                candidate_plans = build_entry_plans(
                    asset_id=str(self.case["target_asset_id"]),
                    instrument_semantics_digest=str(instrument["mapping_semantics_digest"]),
                    source_revision=str(self.source["source_revision"]),
                    source_fact=self.source["payload"],
                    source_first_visible_at_ms=self.source_first_visible_at_ms,
                    root_expires_at_ms=int(self.case["root_expires_at_ms"]),
                    perp_rows=price_rows,
                    parent_condition=(self.case.get("manifest") or {}).get("watch_condition")
                    if self.case.get("run_kind") == "conditional"
                    else None,
                    price_ref=price_ref,
                )
                added_plans.extend(
                    plan.model_dump(mode="json") for plan in candidate_plans if self._register_plan(plan, available)
                )
            return {
                "status": "unsupported" if result.status == "not_applicable" else result.status,
                "reason": result.missing_reasons,
                "dataset": dataset,
                "window_minutes": window_minutes,
                "effective_window_minutes": effective_window,
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
                "feature_values": derived,
                "feature_refs": feature_refs,
                "coverage_complete": bool(covered_rows),
                "added_plans": added_plans,
            }

        return await self._invoke(
            "get_market_snapshot", {"dataset": dataset, "window_minutes": window_minutes}, operation
        )

    async def _resolve_material(self, ref: str, start: int, end: int, *, max_chars: int) -> dict[str, Any]:
        """Resolve only this Case's catalogued material, then slice Unicode characters [start,end)."""
        if (
            ref not in self.evidence_catalog
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end - start > max_chars
        ):
            raise ValueError("evidence_range_or_ref_invalid")
        entry = self.evidence_catalog[ref]
        if ref == "source":
            material: Any = _source_text(self.source["payload"])
        elif ref in self.event_materials:
            material = _source_text(self.event_materials[ref].get("payload") or {})
        elif ref in self.amendment_materials:
            material = self.amendment_materials[ref]
        elif ref.startswith("market:"):
            artifact_ref = entry.get("source_ref")
            if not isinstance(artifact_ref, str):
                raise ValueError("evidence_archive_ref_missing")
            artifact = await self.file_io(self.files.read, artifact_ref)
            dataset = entry.get("dataset") or ref.split(":")[1]
            material = (artifact.get("market") or {}).get(dataset) if "market" in artifact else artifact.get("result")
            if material is None:
                raise ValueError("evidence_material_missing")
            if entry.get("window_start_ms") is not None:
                rows = material.get("payload") or ()
                material = {
                    "projection_version": entry["projection_version"],
                    "raw_status": material.get("status"),
                    "rows": [
                        row
                        for row in rows
                        if entry["window_start_ms"] <= int(row["event_at_ms"]) <= entry["window_end_ms"]
                    ],
                }
        else:
            material = entry
        rendered = material if isinstance(material, str) else _json(material)
        if start >= len(rendered):
            raise ValueError("evidence_range_or_ref_invalid")
        actual_end = min(end, len(rendered))
        return {
            "status": "ok",
            "ref": ref,
            "text": rendered[start:actual_end],
            "start": start,
            "end": actual_end,
            "total_chars": len(rendered),
            "truncated": start > 0 or actual_end < len(rendered),
            "source_ref": entry.get("source_ref"),
            "source": entry.get("source"),
            "update_id": entry.get("update_id"),
            "source_revision": entry.get("source_revision"),
            "affected_claim_refs": entry.get("affected_claim_refs"),
            "retired_claim_refs": entry.get("retired_claim_refs"),
            "unit_definition": entry.get("unit_definition"),
            "environment": entry.get("environment"),
            "native_symbol": entry.get("native_symbol"),
            "projection_version": entry.get("projection_version"),
            "event_at_ms": entry.get("event_at_ms"),
            "received_at_ms": entry.get("received_at_ms"),
            "knowledge_cutoff_ms": entry.get("knowledge_cutoff_ms"),
            "window_start_ms": entry.get("window_start_ms"),
            "window_end_ms": entry.get("window_end_ms"),
        }

    async def read_evidence(self, ref: str, start: int = 0, end: int = 4_096) -> str:
        """Read an authorized character range of the actual archived or public material."""
        return await self._invoke(
            "read_evidence",
            {"ref": ref, "start": start, "end": end},
            lambda: self._resolve_material(ref, start, end, max_chars=8_192),
        )

    async def assess_claims(self, claim: str, fact_refs: list[str], ranges: dict[str, list[int]] | None = None) -> str:
        """Ask native DSPy Predict/Choice about one claim and existing citable facts."""

        async def operation() -> dict[str, Any]:
            if self.semantics is None:
                raise ValueError("jev_unconfigured")
            if not 1 <= len(claim) <= 500 or not 1 <= len(fact_refs) <= 8 or len(set(fact_refs)) != len(fact_refs):
                raise ValueError("claim_arguments_invalid")
            if any(not is_citable_evidence(self.evidence_catalog.get(ref) or {}) for ref in fact_refs):
                raise ValueError("claim_fact_ref_unavailable")
            if ranges is not None and (
                set(ranges) - set(fact_refs)
                or any(not isinstance(value, list) or len(value) != 2 for value in ranges.values())
            ):
                raise ValueError("claim_ranges_invalid")
            resolved = [
                await self._resolve_material(
                    ref,
                    *(ranges.get(ref, [0, 4_096]) if ranges else [0, 4_096]),
                    max_chars=4_096,
                )
                for ref in fact_refs
            ]
            if sum(len(item["text"]) for item in resolved) > 16_384:
                raise ValueError("claim_material_budget_exceeded")
            evidence = [
                {
                    "ref": item["ref"],
                    "material": item["text"],
                    "scope": _json({key: value for key, value in item.items() if key not in ("text", "status")}),
                }
                for item in resolved
            ]
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
                            cost_unknown_reason="not_dispatched"
                            if not receipt.dispatched
                            else "provider_cost_unavailable"
                            if receipt.cost_microusd is None
                            else None,
                            status="not_dispatched"
                            if not receipt.dispatched
                            else "result_unknown"
                            if receipt.response_payload is None
                            else "completed",
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

            lm = self.semantics.bind(
                before_call=before,
                after_call=after,
                deadline_at_monotonic=self._ledger.deadline_at_monotonic,
            )
            answer = await dspy.Predict(ClaimSupport).acall(claim=claim, evidence=evidence, lm=lm)
            verdict = answer.verdict
            receipt = lm.history[-1]
            judgment = {
                "judgment_version": "claim_support_v1",
                "claim": claim,
                "fact_refs": fact_refs,
                "ranges": {item["ref"]: [item["start"], item["end"]] for item in resolved},
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
            return {
                "status": "ok",
                "judgment_ref": ref,
                "verdict": verdict.value,
                "confidence": verdict.confidence,
                "probabilities": verdict.probabilities,
                "fact_refs": fact_refs,
                "ranges": {item["ref"]: [item["start"], item["end"]] for item in resolved},
            }

        return await self._invoke(
            "assess_claims", {"claim": claim, "fact_refs": fact_refs, "ranges": ranges}, operation
        )
