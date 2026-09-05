"""Semantic judgment, deterministic policy, and verdict persistence stage."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal

from ..artifact_identity import canonical_json, canonical_sha
from ..bus import (
    Q_TRIAGE,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    now_ms,
)
from ..events.storyline import final_storyline_key
from ..models import TRIAGE_POLICY_VERSION, json_ready
from ..program.contracts import (
    ScoredJudgment,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TriageContext,
)
from ..reader_history import ReaderHistorySnapshot
from ..release.canary import CanaryRuntimeArm
from ..source_contracts import EVENT_KINDS
from ..telemetry import NewsWorkSemantics
from ..triage_rules import (
    DEFAULT_POLICY,
    DecidePolicy,
    DecisionResult,
    GateFacts,
    decide,
    grounded_restatement,
    storyline_status,
)
from .runtime import NewsDatabasePort
from .triage_audit import (
    _program_execution,
    _reader_history_trace,
    _sync_program_audit,
    _told_from_context,
    _told_trace,
    _usage_from_partial_trace,
)
from .triage_history import _novelty_context_sha, _read_history, _recent_seen
from .triage_route import (
    _ArmSelection,
    _Circuit,
    _degraded_judgment,
    _gate_facts,
    _initial_trace,
    _Judged,
    _ProgramAttempts,
    _resolve_after_reask_failure,
    _RouteInputs,
    _TriageOutcome,
    _TriageSettle,
)

log = logging.getLogger("tracefold.news")

_INSTRUMENT_CACHE_TTL_MS = 10 * 60_000


@dataclass(frozen=True, slots=True)
class _PreparedTriageSettlement:
    decision: DecisionResult
    trace: dict[str, Any]
    verdict: dict[str, Any]
    verdict_json: str
    model_editorial: dict[str, Any] | None
    model_editorial_json: str | None
    judgment_sha256: str
    context_line: str
    trace_json: str


async def publish_verdict(
    bus: Any,
    db: NewsDatabasePort,
    *,
    event_id: str,
    trace_id: str,
    amqp_priority: int,
    policy_version: str,
    occurred_at_ms: int | None = None,
) -> Literal["marker_pending", "published"]:
    """Publish one settled push Verdict to Delivery, then mark its confirmed handoff."""

    stamp = now_ms()
    await bus.publish(
        BusMessage(
            kind="verdict",
            message_id=f"push:{event_id}",
            routing_key=RK_VERDICT_PUSH,
            payload={"event_id": event_id, "kind": "first"},
            trace_id=trace_id,
            occurred_at_ms=stamp if occurred_at_ms is None else int(occurred_at_ms),
            priority=amqp_priority,
        )
    )
    try:
        await db.tx(
            "news_triage_mark_published",
            lambda repos: repos.news.mark_verdict_published(
                event_id=event_id, stage="triage", policy_version=policy_version, now_ms=stamp
            ),
            timeout_seconds=1.0,
        )
        return "published"
    except (TransientError, DeferError) as exc:
        log.warning(
            "news Verdict handoff confirmed but marker remains pending event_id=%s policy_version=%s error=%s",
            event_id,
            policy_version,
            type(exc).__name__,
        )
        return "marker_pending"


def _circuit_incident_for(
    arm: _ArmSelection, attempts: _ProgramAttempts, *, stamp: int
) -> Literal["open", "close"] | None:
    """Which durable transition this settle owes the `triage_circuit_open` incident.

    Derived from state that survives a retry, never from a remembered edge: a stable Program answer
    means the provider is back, and an open stable breaker means it is not. A candidate arm owns a
    canary, not this incident, and the deterministic OI and liquidation lanes never reach here.
    """

    if arm.arm == "candidate":
        return None
    if attempts.stable_program_answered:
        return "close"
    return "open" if arm.circuit.is_open(stamp) else None


def _apply_circuit_incident(repos: Any, transition: Literal["open", "close"], *, now_ms: int) -> None:
    """The one durable Triage-circuit write, inside the verdict's transaction.

    Both statements are idempotent, so repeating them after a retried transaction is a no-op rather than
    a second incident or a second close.
    """

    if transition == "open":
        repos.news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms)
    else:
        repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms)


def _evaluate_canary_rolling_slo(repos: Any, *, activation_id: str, now_ms: int) -> dict[str, Any]:
    return dict(repos.news.evaluate_canary_rolling_slo(activation_id=activation_id, now_ms=now_ms))


class TriageConsumer:
    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        judge: SemanticJudge | None,
        program_version: str,
        program_sha256: str,
        watchlist_symbols: frozenset[str],
        watchlist: Sequence[str],
        concurrency: int,
        circuit_failures: int,
        circuit_open_seconds: float,
        policy: DecidePolicy = DEFAULT_POLICY,
        stable_bundle_sha: str | None = None,
        canary_arms: Mapping[str, CanaryRuntimeArm] | None = None,
        runtime_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.db = db
        self.judge = judge
        self.program_version = str(program_version)
        self.program_sha256 = str(program_sha256)
        self.watchlist_symbols = watchlist_symbols
        self.watchlist = list(watchlist)
        self.concurrency = int(concurrency)
        self.circuit = _Circuit(threshold=circuit_failures, open_seconds=circuit_open_seconds)
        self._circuit_failures = int(circuit_failures)
        self._circuit_open_seconds = float(circuit_open_seconds)
        self._candidate_circuits: dict[str, _Circuit] = {}
        self.policy = policy
        self._canary_enabled = stable_bundle_sha is not None
        self.stable_bundle_sha = (
            stable_bundle_sha
            or hashlib.sha256(
                json.dumps(
                    {"program": self.program_sha256, "policy": policy.as_dict()},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        self.canary_arms = dict(canary_arms or {})
        self.runtime_manifest = dict(runtime_manifest or {})
        self.runtime_manifest_sha = str(self.runtime_manifest.get("manifest_sha") or "")
        if len(self.runtime_manifest_sha) != 64 or any(c not in "0123456789abcdef" for c in self.runtime_manifest_sha):
            raise ValueError("news_runtime_manifest_sha_required")
        # #75: symbol aliases collapse one issuer's several contracts into one storyline bucket. Loaded lazily
        # from the universe snapshot and refreshed on the same TTL as the Gate's copy; `None` uses the seeds.
        self._aliases: dict[str, str] | None = None
        self._aliases_at_ms = 0

    def _refresh_aliases(self, repos: Any, *, now: int) -> None:
        if self._aliases is not None and now - self._aliases_at_ms < _INSTRUMENT_CACHE_TTL_MS:
            return
        self._aliases_at_ms = now
        table = repos.instruments.alias_map()
        self._aliases = table or None

    async def register_runtime_manifest(self) -> None:
        await self.db.tx(
            "news_agent_runtime_manifest",
            lambda repos: repos.news.register_agent_runtime_manifest(**self.runtime_manifest),
        )

    async def run(self, *, stop_event: asyncio.Event) -> None:
        # A fresh process starts with a closed circuit: an incident left open by a previous process is
        # over. This is not best-effort. Beginning to consume without knowing whether PostgreSQL still
        # shows a circuit open would make the durable incident permanently wrong, so the failure fails
        # the Workers root and the supervised restart tries again.
        await self.db.tx(
            "news_triage_circuit_reconcile",
            lambda repos: repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms()),
        )
        await self.bus.consume(Q_TRIAGE, self.handle, prefetch=self.concurrency, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        """Broker orchestration for one Event: load, route, settle, publish.

        Every step below is a named phase with its own function. What stays here is the sequence and the
        one loop the route is allowed to take — the single stale re-ask — because that sequence, not any
        individual phase, is what a reader has to hold to reason about the consumer.
        """

        event_id = str(message.payload.get("event_id") or "")
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_triage_load", lambda repos: self._load_with_aliases(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, history, admission, event_kind = bundle
        # Evidence snapshots are immutable by design, so a pre-cut queued message may still carry the
        # old candidate admission after a source-contract migration has held the material Event.  Route
        # from current PostgreSQL truth before looking for a settled verdict.
        #
        # Market kinds are absent from this dispatch on purpose (#553): a market observation is stored
        # with its typed fact at admission and opens no Event, so no message about one can arrive here.
        # An Event of a retired market kind is immutable historical evidence, and Triage leaves it alone.
        if admission == "recovery" or event_kind not in EVENT_KINDS:
            return
        if await self._republish_settled_verdict(event_id, message, policy_version=TRIAGE_POLICY_VERSION):
            return
        if str(card.get("evidence_schema_version") or "") != "news_event_evidence_v3":
            raise PermanentError("news_event_evidence_v3_required")
        facts = _gate_facts(card, self.watchlist_symbols)
        arm = await self._select_arm(card, event_id=event_id, stamp=stamp)
        queue_lag_ms = max(0, stamp - int(message.occurred_at_ms or stamp))
        route = self._route_inputs(
            card, history, event_id=event_id, facts=facts, stamp=stamp, queue_lag_ms=queue_lag_ms
        )
        trace = _initial_trace(
            route,
            arm=arm,
            queue_lag_ms=queue_lag_ms,
            attempt=message.attempt,
            runtime_manifest_sha=self.runtime_manifest_sha,
        )
        attempts = _ProgramAttempts()
        while True:
            judged = await self._judge_once(route=route, arm=arm, attempts=attempts, trace=trace)
            _sync_program_audit(trace, executions=attempts.executions, selected_execution_index=attempts.selected_index)
            settle = self._settle_for(route, arm=arm, attempts=attempts, judged=judged, trace=trace)

            def _refresh_history(repos: Any, current: _TriageSettle = settle) -> ReaderHistorySnapshot:
                return _read_history(
                    repos.news,
                    event_id=current.event_id,
                    card=current.card,
                    now_ms=current.stamp,
                )

            refreshed_history = await self.db.read(
                "news_triage_history_refresh",
                _refresh_history,
            )
            settle = replace(settle, history=refreshed_history)
            history_changed = (
                isinstance(settle.judgment, ScoredJudgment)
                and _novelty_context_sha(settle.card, refreshed_history, now_ms=settle.stamp)
                != settle.novelty_context_sha
            )
            if history_changed:
                if not settle.allow_stale:
                    raise TransientError("news_reader_history_changed")
                outcome = _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="told")
            else:
                prepared = self._prepare_settlement(settle)
                outcome = await self.db.tx(
                    "news_triage_persist",
                    functools.partial(self._persist_prepared_settlement, s=settle, prepared=prepared),
                )
            if outcome.stale:
                if not isinstance(judged.judgment, ScoredJudgment):
                    raise RuntimeError("news_stale_non_model_judgment")
                # A card landed while the model was thinking: ask once more with the ledger it did not see instead
                # of pushing a restatement the reader just received. Budgeted at ~0.6% of calls when the reader got
                # 8 pushes/h; at 38/h the 2026-09-01 audit measured 8.0% told re-asks and 8.5% evidence re-asks
                # (#491), so this is a paid second execution on roughly one judgment in six. Everything the model
                # and decide() look at is re-read under a fresh stamp so the second input is consistent.
                refreshed = await self._refresh_after_stale(
                    route,
                    event_id=event_id,
                    attempts=attempts,
                    judgment=judged.judgment,
                    stale_reason=outcome.stale_reason,
                    queue_lag_ms=queue_lag_ms,
                    trace=trace,
                )
                if refreshed is None:
                    return
                route = refreshed
                continue
            if arm.arm == "candidate" and arm.activation_id:
                with contextlib.suppress(ValueError, TransientError, DeferError):
                    await self.db.tx(
                        "news_canary_rolling_slo",
                        functools.partial(
                            _evaluate_canary_rolling_slo,
                            activation_id=arm.activation_id,
                            now_ms=route.stamp,
                        ),
                    )
            break
        if outcome.final in {"push", "escalate"}:
            await publish_verdict(
                self.bus,
                self.db,
                event_id=event_id,
                trace_id=message.trace_id,
                amqp_priority=message.priority,
                policy_version=TRIAGE_POLICY_VERSION,
            )

    async def _republish_settled_verdict(self, event_id: str, message: BusMessage, *, policy_version: str) -> bool:
        """Whether this Event already has a verdict. A push that never left the process is re-published."""

        existing = await self.db.read(
            "news_triage_existing",
            lambda repos: repos.news.get_verdict(event_id=event_id, stage="triage", policy_version=policy_version),
        )
        if existing is None:
            return False
        if existing.get("published_at_ms") is None and existing["final_decision"] in {"push", "escalate"}:
            await publish_verdict(
                self.bus,
                self.db,
                event_id=event_id,
                trace_id=message.trace_id,
                amqp_priority=message.priority,
                policy_version=policy_version,
            )
        return True

    def _route_inputs(
        self,
        card: Mapping[str, Any],
        history: ReaderHistorySnapshot,
        *,
        event_id: str,
        facts: GateFacts,
        stamp: int,
        queue_lag_ms: int,
    ) -> _RouteInputs:
        """The Event as the Program will see it, plus the hashes the persist step compares against.

        The told context is the <= TOLD_MAX cards the selector ranked against *this* candidate out of bounded
        recent plus targeted history. The model judges novelty against it, and its SHA — not the source rows'
        event-id set — is what the persist step compares, so only a change to what the model saw can make
        the judgment stale.
        """

        told_rows = [row.as_told_row() for row in history.told_source_rows]
        context = TriageContext.from_card(
            card,
            watchlist=tuple(self.watchlist),
            told_rows=told_rows,
            now_ms=stamp,
            queue_lag_ms=queue_lag_ms,
        )
        return _RouteInputs(
            event_id=event_id,
            card=card,
            history=history,
            facts=facts,
            context=context,
            told=_told_from_context(context),
            selected_context_sha=context.selected_context_sha256(),
            novelty_context_sha=context.novelty_context_sha256(),
            prelim_key=str(card.get("storyline_key") or ""),
            wire_title=str(card.get("leader_title") or ""),
            stamp=stamp,
        )

    async def _select_arm(self, card: Mapping[str, Any], *, event_id: str, stamp: int) -> _ArmSelection:
        """Which Program and policy judge this Event: the stable arm, a canary candidate, or neither.

        Every way the assignment can fail to name something this image can execute is kept apart, because
        each one is a different `error_code` on the degraded verdict and the reader of `news why` has to
        be able to tell a missing candidate artifact from an unconfigured Program.
        """

        assignment = (
            await self.db.tx(
                "news_canary_assign",
                lambda repos: repos.news.assign_agent_arm(
                    event_id=event_id,
                    stable_bundle_sha=self.stable_bundle_sha,
                    admission=str(card.get("admission") or ""),
                    ingest_mode=str(card.get("ingest_mode") or "live"),
                    now_ms=stamp,
                ),
            )
            if self._canary_enabled
            else {
                "activation_id": None,
                "arm": "stable",
                "bundle_sha": self.stable_bundle_sha,
                "selector_version": "test_stable_only_v2",
                "eligibility_reason": "canary_not_composed",
            }
        )
        bundle_sha = str(assignment["bundle_sha"])
        arm = str(assignment["arm"])
        validation_error = str(assignment.get("validation_error") or "")
        runtime_arm = self.canary_arms.get(bundle_sha) if arm == "candidate" else None
        candidate_artifact_missing = arm == "candidate" and runtime_arm is None
        stable_assignment_mismatch = arm == "stable" and bundle_sha != self.stable_bundle_sha
        assignment_shape_mismatch = arm not in {"stable", "candidate"}
        activation_id = str(assignment["activation_id"]) if assignment.get("activation_id") else None
        if candidate_artifact_missing and activation_id:
            await self._trip_canary(activation_id, "candidate_artifact_missing", stamp)
        runtime_missing = (
            bool(validation_error)
            or candidate_artifact_missing
            or stable_assignment_mismatch
            or assignment_shape_mismatch
        )
        return _ArmSelection(
            assignment=assignment,
            arm=arm,
            bundle_sha=bundle_sha,
            activation_id=activation_id,
            judge=(runtime_arm.program if runtime_arm is not None else (None if runtime_missing else self.judge)),
            policy=runtime_arm.policy if runtime_arm is not None else self.policy,
            program_version=runtime_arm.program_version if runtime_arm is not None else self.program_version,
            program_sha256=runtime_arm.program_sha256 if runtime_arm is not None else self.program_sha256,
            circuit=(
                self._candidate_circuits.setdefault(
                    bundle_sha,
                    _Circuit(threshold=self._circuit_failures, open_seconds=self._circuit_open_seconds),
                )
                if arm == "candidate"
                else self.circuit
            ),
            validation_error=validation_error,
            candidate_artifact_missing=candidate_artifact_missing,
            identity_mismatch=stable_assignment_mismatch or assignment_shape_mismatch,
        )

    async def _judge_once(
        self,
        *,
        route: _RouteInputs,
        arm: _ArmSelection,
        attempts: _ProgramAttempts,
        trace: dict[str, Any],
    ) -> _Judged:
        """One pass at a judgment: the Program, or the deterministic fallback that names why not."""

        if arm.judge is None:
            return _degraded_judgment(route, arm.unavailable_code)
        if arm.circuit.is_open(route.stamp):
            if attempts.first_judgment is None:
                return _degraded_judgment(route, arm.circuit_open_code)
            return _resolve_after_reask_failure(route, attempts=attempts, code=arm.circuit_open_code, trace=trace)
        trace["watchlist"] = list(self.watchlist)
        phase = "stale_reask" if attempts.first_judgment is not None else "initial"
        try:
            call = await arm.judge.judge(route.context)
        except SemanticJudgeError as exc:
            await self._record_program_failure(exc, route=route, arm=arm, attempts=attempts, trace=trace, phase=phase)
            if attempts.first_judgment is not None:
                return _resolve_after_reask_failure(route, attempts=attempts, code=exc.code, trace=trace)
            return _degraded_judgment(route, exc.code)
        return await self._accept_program_call(call, route=route, arm=arm, attempts=attempts, trace=trace, phase=phase)

    async def _record_program_failure(
        self,
        exc: SemanticJudgeError,
        *,
        route: _RouteInputs,
        arm: _ArmSelection,
        attempts: _ProgramAttempts,
        trace: dict[str, Any],
        phase: str,
    ) -> None:
        """Audit one failed execution, then charge it to the right breaker.

        An unusable *output* is the candidate's own contract breach and trips its canary; a retryable
        transport failure counts against the circuit and, on the stable arm only, opens an incident.
        """

        attempts.executions.append(
            _program_execution(
                execution_index=len(attempts.executions),
                phase=phase,
                status="failed",
                context=route.context,
                program_trace=exc.partial_trace,
                usage=_usage_from_partial_trace(exc.partial_trace, attempts=exc.attempts),
                error={
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "output_failure": exc.output_failure,
                    "finish_reason": exc.finish_reason,
                    "failing_predictor": exc.failing_predictor,
                    "primary_code": exc.primary_code,
                },
            )
        )
        trace.update({"model_failure_retryable": exc.retryable, "program_error": exc.code})
        if exc.finish_reason:
            trace["finish_reason"] = exc.finish_reason
        if exc.failing_predictor:
            trace["failing_predictor"] = exc.failing_predictor
        if exc.primary_code:
            trace["primary_error"] = exc.primary_code
        if exc.output_failure:
            log.warning("news semantic program output unusable event=%s code=%s", route.event_id, exc.code)
            if arm.arm == "candidate" and arm.activation_id:
                await self._trip_canary(arm.activation_id, "candidate_schema_contract_breach", route.stamp)
        elif exc.retryable:
            # Tripping the breaker is process state; the incident it implies is PostgreSQL's. The trip is
            # not written here — `_settle_for` reads the breaker and the persist transaction opens the
            # incident — so a failed transaction cannot leave the trip recorded only in memory.
            arm.circuit.record_failure(route.stamp)

    async def _accept_program_call(
        self,
        call: SemanticJudgment,
        *,
        route: _RouteInputs,
        arm: _ArmSelection,
        attempts: _ProgramAttempts,
        trace: dict[str, Any],
        phase: str,
    ) -> _Judged:
        """A Program answered. Bind it only if it is the exact Program this arm was assigned."""

        index = len(attempts.executions)
        attempts.executions.append(
            _program_execution(
                execution_index=index,
                phase=phase,
                status="completed",
                context=route.context,
                program_trace=call.trace,
                usage=call.usage,
                answering_model=call.answering_model,
                fallback_from=call.fallback_from,
            )
        )
        if call.program_version != arm.program_version or call.program_sha256 != arm.program_sha256:
            code = "news_semantic_program_identity_mismatch"
            attempts.executions[index]["status"] = "identity_mismatch"
            attempts.executions[index]["error"] = {"code": code}
            if arm.arm == "candidate" and arm.activation_id:
                await self._trip_canary(arm.activation_id, code, route.stamp)
            if attempts.first_judgment is not None:
                return _resolve_after_reask_failure(route, attempts=attempts, code=code, trace=trace)
            return _degraded_judgment(route, code)
        arm.circuit.record_success()
        attempts.stable_program_answered = attempts.stable_program_answered or arm.arm == "stable"
        attempts.model_name = call.answering_model
        attempts.selected_index = index
        attempts.executions[index]["status"] = "accepted"
        if call.fallback_from:
            log.warning(
                "news semantic program fallback answered event=%s model=%s primary_error=%s",
                route.event_id,
                call.answering_model,
                call.fallback_from,
            )
        return _Judged(judgment=call.scored(), degraded=False, error_code=None)

    def _settle_for(
        self,
        route: _RouteInputs,
        *,
        arm: _ArmSelection,
        attempts: _ProgramAttempts,
        judged: _Judged,
        trace: dict[str, Any],
    ) -> _TriageSettle:
        """Freeze the judgment, its final storyline key and its identities for the one persist transaction."""

        verdict = judged.judgment.verdict
        card = route.card
        return _TriageSettle(
            event_id=route.event_id,
            evidence_version=int(card.get("evidence_version") or 0),
            evidence_sha256=str(card.get("evidence_sha256") or ""),
            focus_fact_id=str(card.get("focus_fact_id") or ""),
            judgment=judged.judgment,
            facts=route.facts,
            # The final storyline key comes from the verdict (primaries/scope); duplicate evidence is traced on it.
            final_key=final_storyline_key(
                title=route.wire_title,
                headline_zh=verdict.headline_zh,
                scope=verdict.scope,
                verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
                grounded_assets=route.facts.grounded_assets,
                dedupe_family=str(card.get("dedupe_family") or "general"),
                aliases=self._aliases,
                degraded=judged.degraded,
            ),
            told=route.told,
            history=route.history,
            selected_context_sha=route.selected_context_sha,
            novelty_context_sha=route.novelty_context_sha,
            prelim_key=route.prelim_key,
            card=card,
            degraded=judged.degraded,
            error_code=judged.error_code,
            model_name=attempts.model_name,
            program_version=arm.program_version,
            program_sha256=arm.program_sha256,
            policy_version=TRIAGE_POLICY_VERSION,
            policy=arm.policy,
            runtime_manifest_sha=self.runtime_manifest_sha,
            trace=trace,
            stamp=route.stamp,
            allow_stale=not attempts.reasked and not judged.degraded,
            circuit_incident=_circuit_incident_for(arm, attempts, stamp=route.stamp),
        )

    async def _refresh_after_stale(
        self,
        route: _RouteInputs,
        *,
        event_id: str,
        attempts: _ProgramAttempts,
        judgment: ScoredJudgment,
        stale_reason: str | None,
        queue_lag_ms: int,
        trace: dict[str, Any],
    ) -> _RouteInputs | None:
        """Re-read the Event and the ledger under a fresh stamp so the second ask sees one consistent input."""

        attempts.reasked = True
        attempts.reask_reason = stale_reason
        attempts.first_judgment = judgment
        if attempts.selected_index is None:
            raise ValueError("news_stale_program_execution_missing")
        attempts.executions[attempts.selected_index]["status"] = (
            "superseded_evidence_change" if stale_reason == "evidence" else "superseded_stale_ledger"
        )
        trace["reask_reason"] = stale_reason
        trace["reasked_after_evidence_change" if stale_reason == "evidence" else "reasked_after_told_change"] = True
        trace["first_input_sha256"] = trace.get("input_sha256")
        trace["first_judgment"] = judgment.model_dump(mode="json")
        stamp = now_ms()
        bundle = await self.db.read("news_triage_reload", functools.partial(self._load, event_id=event_id, stamp=stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, history, _admission, event_kind = bundle
        if event_kind not in EVENT_KINDS:
            return None
        refreshed = self._route_inputs(
            card,
            history,
            event_id=event_id,
            facts=_gate_facts(card, self.watchlist_symbols),
            stamp=stamp,
            queue_lag_ms=queue_lag_ms,
        )
        if refreshed.prelim_key != route.prelim_key:
            trace["first_storyline_key_preliminary"] = route.prelim_key
            trace["storyline_key_preliminary"] = refreshed.prelim_key
        trace["evidence_version"] = int(card.get("evidence_version") or 0)
        trace["evidence_sha256"] = str(card.get("evidence_sha256") or "")
        trace["focus_fact_id"] = str(card.get("focus_fact_id") or "")
        trace["status"] = {
            "storyline_key": refreshed.prelim_key,
            "preliminary": True,
            "queue_lag_ms": queue_lag_ms,
        }
        trace["told"] = _told_trace(refreshed.told)
        trace["told_count"] = len(refreshed.told)
        trace["reader_history"] = _reader_history_trace(refreshed.history, refreshed.told)
        return refreshed

    def _prepare_settlement(self, s: _TriageSettle) -> _PreparedTriageSettlement:
        """Materialize the decision, Pydantic payloads and hashes before a database transaction opens."""

        seen = _recent_seen(s.history)
        status = storyline_status(s.final_key, told=s.told, seen=seen)
        trace = dict(s.trace)
        if isinstance(s.judgment, ScoredJudgment):
            decision = decide(s.judgment, s.facts, status, policy=s.policy, now_ms=s.stamp)
        else:
            decision = s.deterministic_decision
        trace["status_final"] = {"storyline_key": s.final_key}
        trace["storyline_key"] = s.final_key
        trace["verdict_sha256"] = canonical_sha(s.verdict.model_dump(mode="json"))
        trace["judgment_contract_version"] = s.judgment.judgment_contract_version
        trace["judgment_origin"] = s.origin
        judgment_sha256 = s.judgment_sha256
        trace["judgment_sha256"] = judgment_sha256
        model_editorial = None
        if isinstance(s.judgment, ScoredJudgment):
            trace["editorial_sha256"] = s.judgment.editorial.editorial_sha256
            model_editorial = s.judgment.editorial.model_dump(mode="json")
        else:
            trace["judgment"] = s.judgment.judgment_atom
        trace["runtime_manifest_sha"] = s.runtime_manifest_sha
        trace["seen_count"] = len(status.seen_headlines)
        trace["selected_context_sha256"] = s.selected_context_sha
        trace["novelty_context_sha256"] = s.novelty_context_sha
        if decision.seen_similarity is not None:
            # What the duplicate check actually measured, so `news why` can name the card this one resembled instead of
            # reporting a bare rule (#81). ``seen_scope=all`` means the normal
            # push path was compared with the received-card ledger.
            trace["seen_similarity"] = round(float(decision.seen_similarity), 4)
            trace["seen_scope"] = decision.seen_scope
            if 0 <= decision.seen_against < len(seen):
                # `seen_headlines` was built from `seen` in order, so the index names that ledger row.
                row = seen[decision.seen_against]
                trace["seen_against"] = {
                    "event_id": str(row.get("event_id") or ""),
                    "headline_zh": str(row.get("headline_zh") or ""),
                    "at_ms": int(row.get("at_ms") or 0),
                }
        if isinstance(s.judgment, ScoredJudgment) and grounded_restatement(s.verdict, status):
            trace["restates_event_id"] = s.told[s.verdict.restates]["event_id"]
        final = decision.final
        reason = decision.throttled_by or decision.override_rule or ""
        if isinstance(s.judgment, ScoredJudgment):
            taxonomy = s.judgment.editorial.taxonomy
            classification = "/".join(
                (taxonomy.event_family, taxonomy.change_state, taxonomy.assertion_status, taxonomy.source_authority)
            )
        else:
            classification = str(s.card.get("event_kind") or s.origin)
        context_line = (
            f"[{s.origin}:{classification}/{s.verdict.audience}/{s.verdict.direction} m{s.verdict.magnitude}"
            f" → {final}·{reason}] {s.verdict.headline_zh}"
        )
        verdict = json_ready(s.verdict)
        return _PreparedTriageSettlement(
            decision=decision,
            trace=trace,
            verdict=verdict,
            verdict_json=canonical_json(verdict),
            model_editorial=model_editorial,
            model_editorial_json=None if model_editorial is None else canonical_json(model_editorial),
            judgment_sha256=judgment_sha256,
            context_line=context_line,
            trace_json=canonical_json(trace),
        )

    def _persist_prepared_settlement(
        self,
        repos: Any,
        *,
        s: _TriageSettle,
        prepared: _PreparedTriageSettlement,
    ) -> _TriageOutcome:
        """Re-check the locked snapshot and persist only already-materialized values."""

        if s.circuit_incident is not None:
            _apply_circuit_incident(repos, s.circuit_incident, now_ms=s.stamp)
        repos.news.lock_storyline(s.final_key)
        locked_revision = repos.news.reader_history_revision(now_ms=s.stamp)
        if locked_revision != s.history.ledger_revision:
            if s.allow_stale:
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="told")
            raise TransientError("news_reader_history_changed")
        latest_evidence = repos.news.latest_evidence_identity(s.event_id)
        if latest_evidence is None:
            raise PermanentError("news_event_evidence_missing")
        evidence_changed = latest_evidence != (s.evidence_version, s.evidence_sha256)
        if evidence_changed:
            if s.allow_stale:
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="evidence")
            raise TransientError("news_event_evidence_changed")
        repos.news.insert_verdict(
            event_id=s.event_id,
            stage="triage",
            policy_version=s.policy_version,
            judgment_contract_version=s.judgment.judgment_contract_version,
            judgment_origin=s.origin,
            rule_baseline_decision=prepared.decision.rule_baseline,
            final_decision=prepared.decision.final,
            override_rule=prepared.decision.override_rule,
            throttled_by=prepared.decision.throttled_by,
            verdict=prepared.verdict,
            verdict_json=prepared.verdict_json,
            model_editorial=prepared.model_editorial,
            model_editorial_json=prepared.model_editorial_json,
            judgment_sha256=prepared.judgment_sha256,
            runtime_manifest_sha=s.runtime_manifest_sha,
            model=s.model_name,
            program_version=s.program_version,
            program_sha256=s.program_sha256,
            degraded=s.degraded,
            error_code=s.error_code,
            trace=prepared.trace,
            trace_json=prepared.trace_json,
            evidence_version=s.evidence_version,
            evidence_sha256=s.evidence_sha256,
            focus_fact_id=s.focus_fact_id,
            now_ms=s.stamp,
        )
        repos.news.set_storyline_key(event_id=s.event_id, storyline_key=s.final_key, now_ms=s.stamp)
        repos.news.set_context_line(
            event_id=s.event_id,
            context_line=prepared.context_line,
            followup_of=None,
            now_ms=s.stamp,
        )
        return _TriageOutcome(stale=False, final=prepared.decision.final, decision=prepared.decision)

    async def _trip_canary(self, activation_id: str, reason: str, stamp: int) -> None:
        with contextlib.suppress(ValueError, TransientError, DeferError):
            await self.db.tx(
                "news_canary_trip",
                lambda repos: repos.news.transition_canary(
                    activation_id=activation_id,
                    target_state="tripped",
                    reason=reason,
                    now_ms=stamp,
                ),
            )

    def _load_with_aliases(
        self, repos: Any, event_id: str, stamp: int
    ) -> tuple[dict[str, Any], ReaderHistorySnapshot, str, str] | None:
        """The Triage bundle plus a refreshed alias table, both from the one session (#75)."""

        self._refresh_aliases(repos, now=stamp)
        return self._load(repos, event_id, stamp)

    @staticmethod
    def _load(repos: Any, event_id: str, stamp: int) -> tuple[dict[str, Any], ReaderHistorySnapshot, str, str] | None:
        card = repos.news.event_card(event_id)
        routing = repos.news.event_admission(event_id)
        if card is None or routing is None:
            return None
        return (
            card,
            _read_history(repos.news, event_id=event_id, card=card, now_ms=stamp),
            str(routing.get("admission") or ""),
            str(routing.get("event_kind") or ""),
        )
