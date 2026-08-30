"""Deterministic projections over what an arm actually did.

The evaluator decides whether a candidate may be released; this module answers the narrower questions
its verdict is built from — what the production route emitted for a case, which fact clusters a set of
observations connects, what one arm cost per Predictor, and where two arms differ exactly.

Every function here is pure over rows and documents: no database handle, no clock, no model. That is
what lets the release report and the review desk quote the same numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ..artifact_identity import canonical_sha
from ..models import TriageVerdict
from ..program.contracts import EditorialEnvelope, ScoredJudgment
from .contracts import ArmManifest, DatasetCaseRef, ProposalReceipt


def _sha(value: Any) -> str:
    return canonical_sha(value)


def _call_cost_microusd(call: Mapping[str, Any]) -> int | None:
    value = call.get("provider_cost_microusd")
    return int(value) if value is not None else None


def _program_metric(observation: Mapping[str, Any]) -> dict[str, int | None]:
    usage = dict(observation.get("usage") or {})
    calls = list(observation.get("calls") or (observation.get("trace") or {}).get("calls") or [])
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    cost_microusd = usage.get("provider_cost_microusd")
    trace_entry_count = usage.get("call_count")
    physical_call_count = usage.get("physical_call_count")
    if physical_call_count is None:
        physical_call_count = sum(
            isinstance(call, Mapping) and call.get("physical_provider_call") is True for call in calls
        )
    if cost_microusd is None and int(physical_call_count) == 0:
        cost_microusd = 0
    wall_latency_ms = usage.get("wall_latency_ms")
    return {
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
        "call_count": int(physical_call_count),
        "trace_entry_count": int(trace_entry_count) if trace_entry_count is not None else len(calls),
        "provider_cost_microusd": int(cost_microusd) if cost_microusd is not None else None,
        "latency_ms": int(wall_latency_ms) if wall_latency_ms is not None else None,
    }


def _program_call_identity_complete(raw_call: Mapping[str, Any]) -> bool:
    """Validate the request route and any observed response model identity."""

    call = dict(raw_call)
    if (
        call.get("physical_provider_call") is not True
        or call.get("predictor") not in {"event_semantics", "reader_card"}
        or call.get("route") not in {"primary", "fallback"}
        or type(call.get("attempt")) is not int
        or not 1 <= int(call["attempt"]) <= 2
        or not str(call.get("model_binding") or "").strip()
    ):
        return False
    for field_name in (
        "request_sha256",
        "input_sha256",
        "runtime_model_sha256",
        "runtime_binding_sha256",
    ):
        value = str(call.get(field_name) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            return False
    runtime_provider = str(call.get("runtime_provider") or "").strip()
    runtime_model = str(call.get("runtime_model") or "").strip()
    runtime_model_sha = str(call["runtime_model_sha256"])
    runtime_binding_sha = str(call["runtime_binding_sha256"])
    if (
        not runtime_provider
        or not runtime_model
        or runtime_binding_sha
        != _sha(
            {
                "provider": runtime_provider,
                "model": runtime_model,
                "model_sha256": runtime_model_sha,
            }
        )
    ):
        return False
    response_provider = str(call.get("provider") or "").strip()
    response_model = str(call.get("model") or "").strip()
    response_model_sha = str(call.get("model_sha256") or "")
    response_values = (response_provider, response_model, response_model_sha)
    if any(response_values) and (
        not all(response_values) or response_model_sha != _sha({"provider": response_provider, "model": response_model})
    ):
        return False
    return not isinstance(call.get("validated_output"), Mapping) or all(response_values)


def _program_cost_by_predictor(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, dict[str, dict[str, Any]]] = {"stable": {}, "candidate": {}}
    for item in observations:
        for arm in ("stable", "candidate"):
            for program in (item.get(arm) or {}).get("program") or []:
                for raw_call in program.get("calls") or (program.get("trace") or {}).get("calls") or []:
                    call = dict(raw_call)
                    predictor = str(call.get("predictor") or "unknown")
                    route = str(call.get("route") or "unknown")
                    name = f"{predictor}:{route}"
                    bucket = result[arm].setdefault(
                        name,
                        {
                            "call_n": 0,
                            "trace_entry_n": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "provider_cost_microusd": 0,
                            "provider_cost_observed_call_n": 0,
                            "latencies_ms": [],
                        },
                    )
                    bucket["trace_entry_n"] += 1
                    if call.get("physical_provider_call") is not True:
                        continue
                    bucket["call_n"] += 1
                    bucket["input_tokens"] += int(call.get("input_tokens") or 0)
                    bucket["output_tokens"] += int(call.get("output_tokens") or 0)
                    bucket["total_tokens"] += int(call.get("total_tokens") or 0) or (
                        int(call.get("input_tokens") or 0) + int(call.get("output_tokens") or 0)
                    )
                    cost = _call_cost_microusd(call)
                    if cost is not None:
                        bucket["provider_cost_microusd"] += cost
                        bucket["provider_cost_observed_call_n"] += 1
                    if call.get("latency_ms") is not None:
                        bucket["latencies_ms"].append(int(call["latency_ms"]))
    for arm_buckets in result.values():
        for bucket in arm_buckets.values():
            latencies = bucket.pop("latencies_ms")
            bucket["latency_p95_ms"] = _percentile95(latencies)
    return result


def _observed_production_output(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a production verdict + first-delivery receipt without inference."""

    trace = dict(row.get("trace") or {})
    verdict = dict(row.get("verdict") or {}) if row.get("verdict") else None
    editorial = dict(row.get("model_editorial") or {}) if row.get("model_editorial") else None
    error_code = str(row.get("verdict_error_code") or "") or None
    if verdict is None:
        error_code = error_code or "assigned_without_verdict"
    delivery_state = str(row.get("delivery_state") or "")
    if delivery_state == "sent":
        delivery = "observed_sent"
    elif str(row.get("delivery_error_code") or "") == "ambiguous_after_crash":
        delivery = "ambiguous"
    else:
        delivery = "observed_not_sent"
    raw_program_trace = trace.get("program_trace")
    if raw_program_trace is not None and not isinstance(raw_program_trace, Mapping):
        raise ValueError("news_program_selected_execution_mismatch")
    program_trace = dict(raw_program_trace or {})
    raw_executions_value = trace.get("program_executions")
    raw_executions = [] if raw_executions_value is None else raw_executions_value
    if not isinstance(raw_executions, list) or any(not isinstance(item, Mapping) for item in raw_executions):
        raise ValueError("news_program_execution_index_mismatch")
    executions = [dict(item) for item in raw_executions]
    execution_index_values = [item.get("execution_index") for item in executions]
    if any(type(value) is not int for value in execution_index_values):
        raise ValueError("news_program_execution_index_mismatch")
    execution_indices = [cast(int, value) for value in execution_index_values]
    if execution_indices != list(range(len(executions))):
        raise ValueError("news_program_execution_index_mismatch")
    if executions:
        selected_index = trace.get("program_execution_index")
        if selected_index is None:
            if not (bool(row.get("degraded")) and row.get("verdict_error_code") and raw_program_trace is None):
                raise ValueError("news_program_selected_execution_mismatch")
        else:
            if type(selected_index) is not int or not 0 <= int(selected_index) < len(executions):
                raise ValueError("news_program_selected_execution_mismatch")
            selected_execution = next(
                execution for execution in executions if execution["execution_index"] == selected_index
            )
            selected_trace = selected_execution.get("trace")
            if not isinstance(selected_trace, Mapping) or _sha(program_trace) != _sha(dict(selected_trace)):
                raise ValueError("news_program_selected_execution_mismatch")
            if verdict is None or str(program_trace.get("verdict_sha256") or "") != _sha(verdict):
                raise ValueError("news_program_selected_verdict_mismatch")
    calls: list[dict[str, Any]] = []
    global_call_indices: list[int] = []
    for execution in sorted(executions, key=lambda item: int(item.get("execution_index") or 0)):
        execution_context = execution.get("context")
        if not isinstance(execution_context, Mapping):
            raise ValueError("news_program_execution_context_mismatch")
        context_sha = _sha(dict(execution_context))
        execution_context_sha = str(execution.get("context_sha256") or "")
        if context_sha != execution_context_sha:
            raise ValueError("news_program_execution_context_mismatch")
        execution_trace = execution.get("trace")
        if not isinstance(execution_trace, Mapping):
            raise ValueError("news_program_execution_context_mismatch")
        trace_context_sha = str(execution_trace.get("context_sha256") or "")
        if (
            len(execution_context_sha) != 64
            or any(char not in "0123456789abcdef" for char in execution_context_sha)
            or execution_context_sha != trace_context_sha
        ):
            raise ValueError("news_program_execution_context_mismatch")
        raw_execution_calls_value = execution_trace.get("calls")
        raw_execution_calls = [] if raw_execution_calls_value is None else raw_execution_calls_value
        if not isinstance(raw_execution_calls, list) or any(
            not isinstance(item, Mapping) for item in raw_execution_calls
        ):
            raise ValueError("news_program_execution_call_index_mismatch")
        execution_calls = [dict(item) for item in raw_execution_calls]
        raw_recording_indices_value = execution.get("recording_call_indices")
        raw_recording_indices = [] if raw_recording_indices_value is None else raw_recording_indices_value
        if not isinstance(raw_recording_indices, list):
            raise ValueError("news_program_execution_call_index_mismatch")
        if any(type(value) is not int for value in raw_recording_indices):
            raise ValueError("news_program_execution_call_index_mismatch")
        recording_indices = [int(value) for value in raw_recording_indices]
        if len(recording_indices) != len(execution_calls):
            raise ValueError("news_program_execution_call_index_mismatch")
        global_call_indices.extend(recording_indices)
        for local_index, call in enumerate(execution_calls):
            call["execution_index"] = int(execution.get("execution_index") or 0)
            call["execution_phase"] = str(execution.get("phase") or "unknown")
            call["execution_status"] = str(execution.get("status") or "unknown")
            call["execution_context_sha256"] = execution_context_sha
            call["recording_call_index"] = recording_indices[local_index]
            calls.append(call)
    if global_call_indices != list(range(len(calls))):
        raise ValueError("news_program_execution_call_index_mismatch")
    if not executions:
        calls = [dict(item) for item in program_trace.get("calls") or []]
    scored_judgment: dict[str, Any] | None = None
    if verdict is not None:
        if editorial is None:
            raise ValueError("news_learning_observed_editorial_missing")
        scored = ScoredJudgment.issue(
            verdict=TriageVerdict.model_validate(verdict),
            editorial=EditorialEnvelope.model_validate(editorial),
        )
        if str(row.get("judgment_sha256") or "") != scored.scored_judgment_sha256:
            raise ValueError("news_learning_observed_scored_judgment_identity_mismatch")
        scored_judgment = scored.model_dump(mode="json")
    provider_cost_microusd = trace.get("provider_cost_microusd")
    usage = {
        "wall_latency_ms": trace.get("latency_ms"),
        "call_count": trace.get("model_attempts"),
        "physical_call_count": trace.get("physical_model_attempts"),
        "input_tokens": trace.get("input_tokens"),
        "output_tokens": trace.get("output_tokens"),
        "cached_tokens": trace.get("cached_tokens"),
        "total_tokens": trace.get("total_tokens"),
        "provider_cost_microusd": provider_cost_microusd,
    }
    program = {
        "program_version": row.get("program_version"),
        "program_sha256": row.get("program_sha256"),
        "trace": program_trace,
        "calls": calls,
        "executions": executions,
        "usage": usage,
        "error_code": error_code,
    }
    return {
        "scored_judgment": scored_judgment,
        "verdict": verdict,
        "editorial": editorial,
        "runtime_manifest_sha": row.get("runtime_manifest_sha"),
        "final_decision": row.get("final_decision"),
        "delivered": delivery_state == "sent",
        "execution": "live",
        "delivery": delivery,
        "degraded": bool(row.get("degraded")),
        "error_code": error_code,
        "program": [program],
    }


def _observation_root(observations: Sequence[Mapping[str, Any]]) -> str:
    persisted_case_fields = (
        "case_id",
        "subject_kind",
        "event_id",
        "evidence_version",
        "external_snapshot_id",
        "evidence_sha256",
        "review_id",
        "cluster_id",
        "stratum",
        "should_push",
        "delivery_truth",
        "opened_at_ms",
    )
    leaves = [
        _sha(
            {
                "case_ref": {field: item.get("case_ref", {}).get(field) for field in persisted_case_fields},
                "stable": item.get("stable") or {},
                "candidate": item.get("candidate") or {},
                "comparison": item.get("comparison") or {},
            }
        )
        for item in observations
    ]
    return _sha({"observation_root_version": "news_observation_root_v1", "leaves": leaves})


def _percentile95(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _connected_fact_clusters(
    drafts: Sequence[tuple[DatasetCaseRef, str, str]],
) -> list[DatasetCaseRef]:
    """Collapse duplicate-of components and identical source facts into one N.

    ``duplicate_of`` may point outside the frozen window.  In that case all
    cases naming the same external target still share a component.  Exact
    normalized text is a deterministic fallback, not a semantic guess.
    """

    parent = list(range(len(drafts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    event_index = {str(case.event_id): index for index, (case, _, _) in enumerate(drafts) if case.event_id is not None}
    identities: dict[str, int] = {}
    for index, (case, duplicate_of, source_identity) in enumerate(drafts):
        keys = [f"text:{case.cluster_id}"]
        if source_identity.strip():
            keys.append(f"source:{source_identity.strip()}")
        if duplicate_of:
            target = event_index.get(duplicate_of)
            if target is not None:
                union(index, target)
            else:
                keys.append(f"duplicate_of:{duplicate_of}")
        for key in keys:
            previous = identities.setdefault(key, index)
            union(index, previous)

    members: dict[int, list[str]] = {}
    for index, (case, _, _) in enumerate(drafts):
        members.setdefault(find(index), []).append(case.cluster_id)
    cluster_sha = {
        root: _sha({"fact_cluster_version": "news_fact_cluster_v1", "members": sorted(set(values))})
        for root, values in members.items()
    }
    return [
        case.model_copy(update={"cluster_id": cluster_sha[find(index)]}) for index, (case, _, _) in enumerate(drafts)
    ]


def _arm_exact_diff(
    stable: ArmManifest,
    candidate: ArmManifest,
    *,
    proposal: ProposalReceipt,
) -> dict[str, Any]:
    """Return the exact, reviewable single-variable delta sealed with a candidate.

    This is operator evidence, not an executable patch: the write-set itself is the registered
    `PromptCandidateV1` the receipt names, and the evaluator's static validator remains the authority that
    rejects mixed changes. Since #202 there is one shape here, because there is one kind of candidate.
    """

    stable_payload = stable.model_dump(mode="json")
    candidate_payload = candidate.model_dump(mode="json")
    changed_fields = sorted(key for key in stable_payload if stable_payload[key] != candidate_payload[key])
    return {
        "candidate_kind": "prompt",
        "changed_fields": changed_fields,
        "stable_bundle_sha": stable.bundle_sha,
        "candidate_bundle_sha": candidate.bundle_sha,
        "stable_program_version": stable.program_version,
        "candidate_program_version": candidate.program_version,
        "stable_program_sha256": stable.program_sha256,
        "candidate_program_sha256": candidate.program_sha256,
        "prompt_candidate_sha256": proposal.prompt_candidate_sha256,
    }
