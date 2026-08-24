"""Pure audit projections for News semantic Program executions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..program.contracts import ProgramTrace, ProgramUsage, TriageContext
from ..reader_history import ReaderHistorySnapshot


def _told_trace(told: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The ledger exactly as the model saw it (plus event ids), so ``news why`` can name the restated card and
    CandidateEvaluator recordings can reproduce ``StorylineStatus.told_directions``."""

    return [
        {
            "i": int(t.get("i", i)),
            "event_id": str(t.get("event_id") or ""),
            "at_ms": int(t.get("at_ms") or 0),
            "m": int(t.get("m") or 0),
            "dir": str(t.get("dir") or ""),
            "headline_zh": str(t.get("headline_zh") or ""),
            "tier": str(t.get("tier") or ""),
            "similarity": float(t.get("similarity") or 0.0),
            "history_scope": str(t.get("history_scope") or "recent"),
            "retrieval_reason": str(t.get("retrieval_reason") or "recent"),
        }
        for i, t in enumerate(told)
    ]


def _told_from_context(context: TriageContext) -> list[dict[str, Any]]:
    """Return the exact ledger order/index visible to the Program in the shape used by policy and audit.

    ``grounded_assets`` carries the symbols the selector already resolved for each shown entry.  ``decide()``
    needs them: ``_names_another_instrument`` compares symbol sets, and a told row with no assets is read as
    "not evidence of a different instrument", which silently disabled the listing exemption in production.
    """

    return [
        {
            "i": entry.i,
            "event_id": entry.event_id,
            "at_ms": entry.at_ms,
            "m": entry.magnitude,
            "dir": entry.direction,
            "headline_zh": entry.headline_zh,
            "grounded_assets": list(entry.symbols),
            "tier": entry.tier,
            "similarity": entry.similarity,
            "history_scope": entry.history_scope,
            "retrieval_reason": entry.retrieval_reason,
        }
        for entry in context.told.entries
    ]


def _reader_history_trace(history: ReaderHistorySnapshot, told: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Audit the bounded sources and the retrieval reasons selected for the Program."""

    return {
        "recent_count": len(history.recent_seen_rows),
        "targeted_count": len(history.targeted_told_rows),
        "source_count": len(history.told_source_rows),
        "selected_count": len(told),
        "selected_reasons": [str(row.get("retrieval_reason") or "recent") for row in told],
    }


def _usage_from_partial_trace(program_trace: ProgramTrace | None, *, attempts: int) -> dict[str, Any]:
    """Recover the observable usage of a failed Program execution.

    ``SemanticJudgeError`` deliberately carries a partial trace rather than a
    second usage object.  Calls already made before the failure are nevertheless
    billable facts and must survive a stale-ledger re-ask.  Synthetic trace
    entries remain in ``call_count`` for audit, while only entries explicitly
    marked as physical provider calls contribute usage or cost.  With no such
    entry the observed physical cost is exactly zero.
    """

    calls = tuple(program_trace.calls) if program_trace is not None else ()
    physical_calls = tuple(call for call in calls if call.physical_provider_call)
    costs = [call.provider_cost_microusd for call in physical_calls]
    return {
        "wall_latency_ms": sum(call.latency_ms for call in physical_calls),
        "call_count": len(calls) if calls else max(0, int(attempts)),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(call.input_tokens for call in physical_calls),
        "output_tokens": sum(call.output_tokens for call in physical_calls),
        "cached_tokens": sum(call.cached_tokens for call in physical_calls),
        "total_tokens": sum(call.total_tokens for call in physical_calls),
        "provider_cost_microusd": (
            sum(int(cost) for cost in costs if cost is not None) if all(cost is not None for cost in costs) else None
        ),
    }


def _program_execution(
    *,
    execution_index: int,
    phase: str,
    status: str,
    context: TriageContext,
    program_trace: ProgramTrace | None,
    usage: ProgramUsage | Mapping[str, Any],
    answering_model: str | None = None,
    fallback_from: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one immutable-in-meaning online Program execution audit entry."""

    usage_payload = usage.model_dump(mode="json") if isinstance(usage, ProgramUsage) else dict(usage)
    trace_payload = program_trace.model_dump(mode="json") if program_trace is not None else None
    result: dict[str, Any] = {
        "execution_index": execution_index,
        "phase": phase,
        "status": status,
        "context_sha256": program_trace.context_sha256 if program_trace is not None else None,
        "context": context.model_dump(mode="json"),
        "trace": trace_payload,
        "usage": usage_payload,
        # ``_sync_program_audit`` assigns global call indices after appending
        # the execution.  That disambiguates two calls named
        # (event_semantics, attempt=1) in initial/re-ask runs.
        "recording_call_indices": list(range(len(program_trace.calls))) if program_trace is not None else [],
    }
    if answering_model is not None:
        result["answering_model"] = answering_model
    if fallback_from is not None:
        result["fallback_from"] = fallback_from
    if error is not None:
        result["error"] = dict(error)
    return result


def _sync_program_audit(
    trace: dict[str, Any],
    *,
    executions: Sequence[dict[str, Any]],
    selected_execution_index: int | None,
) -> None:
    """Project all executions plus the verdict-owning trace into verdict audit.

    The selected ``program_trace`` is always the trace whose ``verdict_sha256``
    belongs to the persisted verdict.  Initial and failed re-ask calls live in
    ``program_executions`` and contribute to the aggregate telemetry without
    being spliced into a trace with a different context/verdict identity.
    """

    trace["program_executions"] = list(executions)
    if not executions:
        for field_name in (
            "latency_ms",
            "model_attempts",
            "physical_model_attempts",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "provider_cost_microusd",
            "program_execution_index",
            "program_trace",
            "input_sha256",
            "novelty_defaulted",
            "model_fallback_from",
        ):
            trace.pop(field_name, None)
        return
    next_call_index = 0
    for execution in executions:
        execution_trace = execution.get("trace")
        calls = list(execution_trace.get("calls") or []) if isinstance(execution_trace, Mapping) else []
        execution["recording_call_indices"] = list(range(next_call_index, next_call_index + len(calls)))
        next_call_index += len(calls)
    call_count = sum(int(dict(execution.get("usage") or {}).get("call_count") or 0) for execution in executions)
    trace["latency_ms"] = sum(
        int(dict(execution.get("usage") or {}).get("wall_latency_ms") or 0) for execution in executions
    )
    trace["model_attempts"] = call_count
    trace["physical_model_attempts"] = sum(
        int(dict(execution.get("usage") or {}).get("physical_call_count") or 0) for execution in executions
    )
    for field_name in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
        trace[field_name] = sum(
            int(dict(execution.get("usage") or {}).get(field_name) or 0) for execution in executions
        )
    physical_call_bearing = [
        dict(execution.get("usage") or {})
        for execution in executions
        if int(dict(execution.get("usage") or {}).get("physical_call_count") or 0) > 0
    ]
    trace["provider_cost_microusd"] = (
        sum(int(usage["provider_cost_microusd"]) for usage in physical_call_bearing)
        if all(usage.get("provider_cost_microusd") is not None for usage in physical_call_bearing)
        else None
    )

    trace.pop("program_execution_index", None)
    trace.pop("program_trace", None)
    trace.pop("input_sha256", None)
    trace.pop("novelty_defaulted", None)
    trace.pop("model_fallback_from", None)
    if selected_execution_index is None:
        return
    selected = executions[selected_execution_index]
    selected_trace = selected.get("trace")
    if not isinstance(selected_trace, Mapping):
        raise ValueError("news_selected_program_trace_missing")
    trace["program_execution_index"] = selected_execution_index
    trace["program_trace"] = dict(selected_trace)
    trace["input_sha256"] = str(selected_trace["context_sha256"])
    if bool(selected_trace.get("novelty_defaulted")):
        trace["novelty_defaulted"] = True
    if selected.get("fallback_from"):
        trace["model_fallback_from"] = str(selected["fallback_from"])
