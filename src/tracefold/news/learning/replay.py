"""Sealed, target-run replay composition for CandidateEvaluator evidence.

The production Program remains the sole graph and assembler implementation.
This module only turns one immutable ``news_model_recordings`` corpus into an
explicit capability that can feed the recorded Predictor outcomes back through
that Program.  It never falls through to a live provider.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from ..artifact_identity import canonical_sha
from ..program.artifact import ProgramArtifact
from ..program.contracts import SemanticJudgment, TriageContext
from ..program.dspy_adapter import (
    PredictorAdapterError,
    PredictorRequest,
    PredictorResponse,
    ProviderCallObservation,
    RuntimeModelIdentity,
)
from ..program.graph import DspyNewsSemanticProgram
from ..program.runtime import (
    PROGRAM_FACTORY_ID,
    PROGRAM_SCHEMA_VERSION,
    PROGRAM_VERSION,
)

ArmName = Literal["stable", "candidate"]
_CAPABILITY_ISSUER = object()
_CORPUS_VERSION = "news_recording_corpus_v2"


class RecordingReplayError(ValueError):
    """The requested recording corpus cannot prove an exact Program replay."""


class RecordingReplayMiss(RecordingReplayError):
    """The requested run has no replay corpus or lacks a required recorded call."""


@dataclass(frozen=True, slots=True)
class ReplayArmSpec:
    arm: ArmName
    bundle_sha: str
    artifact: ProgramArtifact


@dataclass(frozen=True, slots=True)
class _ReplayOutcome:
    kind: Literal["success", "error", "novelty_defaulted"]
    response: PredictorResponse | None = None
    error_code: str | None = None
    retryable: bool = False
    output_failure: bool = False
    finish_reason: str | None = None
    provider_observation: ProviderCallObservation | None = None


@dataclass(frozen=True, slots=True)
class _Recording:
    recording_sha: str
    run_sha: str
    case_id: str
    arm: ArmName
    trial: int
    predictor_name: str
    call_index: int
    attempt: int
    route: str
    request_sha256: str
    response_sha256: str | None
    request: dict[str, Any]
    error_code: str | None
    outcome: _ReplayOutcome

    @property
    def corpus_leaf(self) -> dict[str, Any]:
        return {
            "recording_sha": self.recording_sha,
            "run_sha": self.run_sha,
            "case_id": self.case_id,
            "arm": self.arm,
            "trial": self.trial,
            "predictor_name": self.predictor_name,
            "call_index": self.call_index,
            "attempt": self.attempt,
            "route": self.route,
            "request_sha256": self.request_sha256,
            "response_sha256": self.response_sha256,
            "error_code": self.error_code,
            "outcome_kind": self.outcome.kind,
        }


class _ScopedRecordingAdapter:
    """One arm's ordered recordings, scoped by evaluator case and trial."""

    def __init__(self, recordings: Sequence[_Recording]) -> None:
        grouped: dict[tuple[str, int], list[_Recording]] = defaultdict(list)
        for recording in recordings:
            grouped[(recording.case_id, recording.trial)].append(recording)
        self._recordings = {
            key: tuple(sorted(values, key=lambda item: (item.call_index, item.attempt, item.recording_sha)))
            for key, values in grouped.items()
        }
        self._active: tuple[_Recording, ...] | None = None
        self._position = 0
        self._consumed: list[str] = []
        self._failure: RecordingReplayError | None = None

    @property
    def consumed(self) -> tuple[str, ...]:
        return tuple(self._consumed)

    def begin(self, *, case_id: str, trial: int) -> None:
        if self._active is not None:
            raise RecordingReplayError("news_learning_recording_replay_scope_nested")
        self._active = self._recordings.get((case_id, trial), ())
        self._position = 0
        self._failure = None

    def end(self) -> None:
        active = self._require_active()
        try:
            if self._failure is not None:
                raise self._failure
            if self._position != len(active):
                raise RecordingReplayError("news_learning_recording_replay_scope_incomplete")
        finally:
            self._active = None
            self._position = 0
            self._failure = None

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        recording = self._next()
        request = recording.request
        if str(request.get("model_binding") or "") != model_binding:
            self._fail("news_learning_recording_replay_call_order_mismatch")
        return RuntimeModelIdentity(
            provider=str(request.get("runtime_provider") or ""),
            model=str(request.get("runtime_model") or ""),
            model_sha256=str(request.get("runtime_model_sha256") or ""),
            binding_sha256=str(request.get("runtime_binding_sha256") or ""),
        )

    async def invoke(self, request: PredictorRequest, predictor: Any) -> PredictorResponse:
        del predictor
        recording = self._next()
        expected = recording.request
        request_payload = request.model_dump(mode="json")
        recorded_fields = (
            "program_version",
            "program_sha256",
            "context_sha256",
            "predictor",
            "attempt",
            "route",
            "signature_sha256",
            "instruction_sha256",
            "demos_sha256",
            "adapter_sha256",
            "model_binding",
            "runtime_provider",
            "runtime_model",
            "runtime_model_sha256",
            "runtime_binding_sha256",
            "upstream_sha256",
        )
        mismatched = [key for key in recorded_fields if expected.get(key) != request_payload.get(key)]
        if expected.get("input_sha256") != canonical_sha(request.inputs):
            mismatched.append("input_sha256")
        if request.request_sha256 != recording.request_sha256:
            mismatched.append("request_sha256")
        if mismatched:
            self._fail(f"news_learning_recording_replay_request_mismatch:{','.join(sorted(set(mismatched)))}")
        self._position += 1
        self._consumed.append(recording.recording_sha)
        outcome = recording.outcome
        if outcome.kind in {"success", "novelty_defaulted"}:
            if outcome.response is None:  # pragma: no cover - factory invariant
                raise RecordingReplayError("news_learning_recording_replay_outcome_invalid")
            return outcome.response
        raise PredictorAdapterError(
            str(outcome.error_code),
            retryable=outcome.retryable,
            output_failure=outcome.output_failure,
            finish_reason=outcome.finish_reason,
            provider_observation=outcome.provider_observation,
        )

    def _next(self) -> _Recording:
        active = self._require_active()
        if self._position >= len(active):
            self._miss("news_learning_recording_replay_call_missing")
        return active[self._position]

    def _miss(self, code: str) -> NoReturn:
        failure = RecordingReplayMiss(code)
        if self._failure is None:
            self._failure = failure
        raise failure

    def _fail(self, code: str) -> NoReturn:
        failure = RecordingReplayError(code)
        if self._failure is None:
            self._failure = failure
        raise failure

    def _require_active(self) -> tuple[_Recording, ...]:
        if self._active is None:
            raise RecordingReplayError("news_learning_recording_replay_scope_missing")
        return self._active


@dataclass(slots=True)
class _ReplayArm:
    bundle_sha: str
    adapter: _ScopedRecordingAdapter
    program: DspyNewsSemanticProgram


class RecordingReplayCapability:
    """Opaque proof-bearing authority to replay exactly one persisted run."""

    def __init__(
        self,
        *,
        issuer: object,
        run_sha: str,
        corpus_root: str,
        recording_shas: tuple[str, ...],
        arms: Mapping[ArmName, _ReplayArm],
        missing_code: str | None = None,
    ) -> None:
        if issuer is not _CAPABILITY_ISSUER:
            raise RecordingReplayError("news_learning_recording_replay_capability_invalid")
        self._issuer = issuer
        self.run_sha = run_sha
        self.corpus_root = corpus_root
        self.recording_shas = recording_shas
        self._arms = dict(arms)
        self._missing_code = missing_code

    def assert_for_run(self, run_sha: str) -> None:
        if self._issuer is not _CAPABILITY_ISSUER or self.run_sha != run_sha:
            raise RecordingReplayError("news_learning_recording_replay_capability_invalid")
        self._raise_if_missing()

    async def judge(
        self,
        *,
        arm: ArmName,
        bundle_sha: str,
        case_id: str,
        trial: int,
        context: TriageContext,
    ) -> SemanticJudgment:
        self._raise_if_missing()
        selected = self._arms.get(arm)
        if selected is None or selected.bundle_sha != bundle_sha:
            raise RecordingReplayError("news_learning_recording_replay_arm_mismatch")
        selected.adapter.begin(case_id=case_id, trial=trial)
        try:
            return await selected.program.judge(context)
        finally:
            selected.adapter.end()

    def sealed_receipt(self) -> dict[str, Any]:
        self._raise_if_missing()
        consumed: list[str] = []
        for selected in self._arms.values():
            consumed.extend(selected.adapter.consumed)
        if sorted(consumed) != sorted(self.recording_shas):
            raise RecordingReplayError("news_learning_recording_replay_corpus_incomplete")
        return {
            "run_sha": self.run_sha,
            "recording_n": len(self.recording_shas),
            "recording_corpus_root": self.corpus_root,
        }

    def _raise_if_missing(self) -> None:
        if self._missing_code is not None:
            raise RecordingReplayMiss(self._missing_code)


def load_recording_replay_capability(
    conn: Any,
    *,
    run_sha: str,
    arms: Sequence[ReplayArmSpec],
) -> RecordingReplayCapability:
    """Load and seal every physical call for exactly ``run_sha``."""

    specs = {spec.arm: spec for spec in arms}
    if set(specs) != {"stable", "candidate"} or len(specs) != len(arms):
        raise RecordingReplayError("news_learning_recording_replay_arms_invalid")
    if any(
        spec.artifact.schema_version != PROGRAM_SCHEMA_VERSION
        or spec.artifact.factory_id != PROGRAM_FACTORY_ID
        or spec.artifact.program_version != PROGRAM_VERSION
        for spec in specs.values()
    ):
        raise RecordingReplayError("news_learning_recording_replay_program_v1_unsupported")
    rows = conn.execute(
        """
        SELECT recording_sha, run_sha, case_id, arm, trial, predictor_name, call_index, attempt, route,
               request_sha256, response_sha256, request, response, provider, model, model_sha,
               latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens,
               provider_cost_microusd, finish_reason, error_code
          FROM news_model_recordings
         WHERE run_sha = %s
         ORDER BY case_id, arm, trial, call_index, attempt, recording_sha
        """,
        (run_sha,),
    ).fetchall()
    missing_code = "news_learning_recording_replay_corpus_missing" if not rows else None
    parsed: list[_Recording] = []
    for row in rows:
        arm = str(row["arm"])
        if arm not in specs:
            raise RecordingReplayError("news_learning_recording_replay_arm_invalid")
        recording_identity = {
            "run_sha": str(row["run_sha"]),
            "case_id": str(row["case_id"]),
            "arm": arm,
            "trial": int(row["trial"]),
            "predictor_name": str(row["predictor_name"]),
            "call_index": int(row["call_index"]),
            "attempt": int(row["attempt"]),
            "request_sha256": str(row["request_sha256"]),
        }
        if str(row["recording_sha"]) != canonical_sha(recording_identity):
            raise RecordingReplayError("news_learning_recording_replay_recording_identity_mismatch")
        spec = specs[cast(ArmName, arm)]
        request = dict(row["request"] or {})
        if (
            str(row["run_sha"]) != run_sha
            or str(request.get("program_sha256") or "") != spec.artifact.program_sha256
            or str(request.get("program_version") or "") != spec.artifact.program_version
            or str(request.get("request_sha256") or "") != str(row["request_sha256"])
            or str(request.get("predictor") or "") != str(row["predictor_name"])
            or request.get("call_index") != int(row["call_index"])
            or request.get("attempt") != int(row["attempt"])
            or str(request.get("route") or "") != str(row["route"])
        ):
            raise RecordingReplayError("news_learning_recording_replay_program_identity_mismatch")
        parsed.append(_parse_recording(row, arm=cast(ArmName, arm), request=request))
    if parsed and {recording.arm for recording in parsed} != {"stable", "candidate"}:
        missing_code = "news_learning_recording_replay_arms_missing"
    recording_shas = [recording.recording_sha for recording in parsed]
    if len(set(recording_shas)) != len(recording_shas):
        raise RecordingReplayError("news_learning_recording_replay_recording_identity_duplicate")
    leaves = [recording.corpus_leaf for recording in parsed]
    corpus_root = canonical_sha(
        {
            "recording_corpus_version": _CORPUS_VERSION,
            "run_sha": run_sha,
            "leaves": leaves,
        }
    )
    replay_arms: dict[ArmName, _ReplayArm] = {}
    for arm, spec in specs.items():
        adapter = _ScopedRecordingAdapter([recording for recording in parsed if recording.arm == arm])
        replay_arms[arm] = _ReplayArm(
            bundle_sha=spec.bundle_sha,
            adapter=adapter,
            program=DspyNewsSemanticProgram(
                spec.artifact,
                primary_adapter=adapter,
                fallback_adapter=adapter,
            ),
        )
    return RecordingReplayCapability(
        issuer=_CAPABILITY_ISSUER,
        run_sha=run_sha,
        corpus_root=corpus_root,
        recording_shas=tuple(recording.recording_sha for recording in parsed),
        arms=replay_arms,
        missing_code=missing_code,
    )


def _parse_recording(row: Mapping[str, Any], *, arm: ArmName, request: dict[str, Any]) -> _Recording:
    response = dict(row["response"] or {}) if row["response"] is not None else None
    response_sha256 = str(row["response_sha256"]) if row["response_sha256"] is not None else None
    if (response is None and response_sha256 is not None) or (
        response is not None and response_sha256 != canonical_sha(response)
    ):
        raise RecordingReplayError("news_learning_recording_replay_response_identity_mismatch")
    error_code = str(row["error_code"] or "") or None
    outcome = _parse_outcome(row, request=request, response=response, error_code=error_code)
    return _Recording(
        recording_sha=str(row["recording_sha"]),
        run_sha=str(row["run_sha"]),
        case_id=str(row["case_id"]),
        arm=arm,
        trial=int(row["trial"]),
        predictor_name=str(row["predictor_name"]),
        call_index=int(row["call_index"]),
        attempt=int(row["attempt"]),
        route=str(row["route"]),
        request_sha256=str(row["request_sha256"]),
        response_sha256=response_sha256,
        request=request,
        error_code=error_code,
        outcome=outcome,
    )


def _parse_outcome(
    row: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    error_code: str | None,
) -> _ReplayOutcome:
    if response is not None:
        parsed = PredictorResponse.model_validate(response)
        if error_code == "news_program_novelty_defaulted":
            output = dict(parsed.output)
            semantics = output.get("semantics")
            if not isinstance(semantics, Mapping):
                raise RecordingReplayError("news_learning_recording_replay_novelty_outcome_invalid")
            raw_semantics = dict(semantics)
            raw_semantics.pop("novelty", None)
            raw_semantics.pop("restates", None)
            parsed = parsed.model_copy(update={"output": {"semantics": raw_semantics}})
            return _ReplayOutcome(kind="novelty_defaulted", response=parsed)
        if error_code not in {
            None,
            "news_program_non_restatement_index_invalid",
            "news_program_restatement_index_invalid",
        }:
            raise RecordingReplayError(f"news_learning_recording_replay_outcome_unreplayable:{error_code}")
        return _ReplayOutcome(kind="success", response=parsed)
    if not error_code:
        raise RecordingReplayError("news_learning_recording_replay_outcome_missing")
    behavior = _error_behavior(error_code)
    observation = _provider_observation(row, request=request)
    return _ReplayOutcome(
        kind="error",
        error_code=error_code,
        retryable=behavior[0],
        output_failure=behavior[1],
        finish_reason=(str(row["finish_reason"]) if row["finish_reason"] is not None else None),
        provider_observation=observation,
    )


def _error_behavior(error_code: str) -> tuple[bool, bool]:
    if error_code == "news_program_route_deadline":
        raise RecordingReplayError("news_learning_recording_replay_outcome_unreplayable:route_deadline")
    if error_code == "news_program_runtime_binding_mismatch":
        raise RecordingReplayError("news_learning_recording_replay_outcome_unreplayable:runtime_binding")
    if error_code == "news_program_output_truncated" or error_code.startswith("news_program_dspy_output_"):
        return False, True
    if error_code in {"news_program_event_semantics_invalid", "news_program_reader_card_invalid"}:
        return False, True
    if error_code.startswith("news_program_transport_"):
        suffix = error_code.removeprefix("news_program_transport_")
        if any(marker in suffix for marker in ("timeout", "connection", "transport", "server", "rate", "temporar")):
            return True, False
        if any(marker in suffix for marker in ("auth", "invalidrequest", "contextwindow")):
            return False, False
    raise RecordingReplayError(f"news_learning_recording_replay_outcome_unreplayable:{error_code}")


def _provider_observation(
    row: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> ProviderCallObservation | None:
    provider = str(row["provider"] or "")
    model = str(row["model"] or "")
    if provider == "unobserved" or model == "unobserved" or not provider or not model:
        return None
    try:
        return ProviderCallObservation(
            provider=provider,
            model=model,
            model_sha256=str(row["model_sha"] or ""),
            latency_ms=int(row["latency_ms"] or 0),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cached_tokens=int(row["cached_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            provider_cost_microusd=(
                int(row["provider_cost_microusd"]) if row["provider_cost_microusd"] is not None else None
            ),
            finish_reason=(str(row["finish_reason"]) if row["finish_reason"] is not None else None),
            runtime_binding_sha256=str(request.get("runtime_binding_sha256") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise RecordingReplayError("news_learning_recording_replay_provider_observation_invalid") from exc


__all__ = [
    "RecordingReplayCapability",
    "RecordingReplayError",
    "RecordingReplayMiss",
    "ReplayArmSpec",
    "load_recording_replay_capability",
]
