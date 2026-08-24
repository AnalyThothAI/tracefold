"""Semantic judgment, deterministic policy, and verdict persistence stage."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

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
from ..learning.canary import CanaryRuntimeArm
from ..models import GATE_POLICY_VERSION, TRIAGE_POLICY_VERSION, TriageVerdict, json_ready
from ..oi_signals import DEFAULT_OI_POLICY, OiPolicy, evaluate_oi, parse_oi_signal, program_sha256
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from ..semantic_contract import (
    TOLD_SOURCE_MAX,
    TOLD_WINDOW_MS,
    EditorialEnvelope,
    ScoredJudgment,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TriageContext,
)
from ..triage_rules import (
    DEFAULT_POLICY,
    DecidePolicy,
    GateFacts,
    decide,
    grounded_restatement,
    storyline_status,
)
from .runtime import NewsDatabasePort
from .triage_audit import (
    _program_execution,
    _sync_program_audit,
    _told_from_context,
    _told_trace,
    _usage_from_partial_trace,
)
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

# The Program sees the TOLD_MAX rows the selector ranked for this candidate; decide() measures a duplicate
# candidate against the whole bounded sent ledger the selector read from. Replaying the stored corpus, widening
# the comparison set from the 12-entry status bar caught 14 more duplicate pairs and 11 more facts the reader
# never received (#81). This is a memory bound, not a reader quota: a ledger filled with distinct facts never
# blocks the next distinct fact.
_SEEN_LEDGER_MAX = TOLD_SOURCE_MAX
_INSTRUMENT_CACHE_TTL_MS = 10 * 60_000


def _open_circuit_incident(repos: Any, *, now_ms: int) -> Any:
    return repos.news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms)


def _close_circuit_incidents(repos: Any, *, now_ms: int) -> Any:
    return repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms)


def _evaluate_canary_rolling_slo(repos: Any, *, activation_id: str, now_ms: int) -> dict[str, Any]:
    return dict(repos.news.evaluate_canary_rolling_slo(activation_id=activation_id, now_ms=now_ms))


class TriageConsumer:
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
        oi_policy: OiPolicy = DEFAULT_OI_POLICY,
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
        self._circuit_incident_open = False
        self.policy = policy
        self.oi_policy = oi_policy
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
        # A fresh process starts with a closed circuit: an incident left open by a previous process is over.
        with contextlib.suppress(TransientError, DeferError):
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
        published = await self._republish_settled_verdict(event_id, message)
        if published:
            return
        bundle = await self.db.read("news_triage_load", lambda repos: self._load_with_aliases(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, ledger_rows = bundle
        if str(card.get("evidence_schema_version") or "") != "news_event_evidence_v2":
            raise PermanentError("news_event_evidence_v2_required")
        facts = _gate_facts(card, self.watchlist_symbols)
        if str(card.get("admission") or "") == "telemetry_deterministic":
            # #137. Fixed-format open-interest telemetry: judged here by arithmetic instead of by two
            # structured model calls that would re-read four numbers a regex already has. Everything
            # after the judgment — decide(), the storyline lock, the verdict row, delivery, the receipt,
            # the outcome, the feed — is the ordinary path, because nothing after the judgment differs.
            await self._judge_telemetry(
                event_id=event_id,
                card=card,
                facts=facts,
                ledger_rows=ledger_rows,
                stamp=stamp,
                message=message,
            )
            return
        arm = await self._select_arm(card, event_id=event_id, stamp=stamp)
        queue_lag_ms = max(0, stamp - int(message.occurred_at_ms or stamp))
        route = self._route_inputs(
            card, ledger_rows, event_id=event_id, facts=facts, stamp=stamp, queue_lag_ms=queue_lag_ms
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
            outcome = await self.db.tx("news_triage_persist", functools.partial(self._decide_and_persist, s=settle))
            if outcome.stale:
                # A card landed while the model was thinking: ask once more with the ledger it did not see (rare,
                # ~0.6% of calls at 8 pushes/h) instead of pushing a restatement the reader just received. Everything
                # the model and decide() look at is re-read under a fresh stamp so the second input is consistent.
                route = await self._refresh_after_stale(
                    route,
                    event_id=event_id,
                    attempts=attempts,
                    judgment=judged.judgment,
                    stale_reason=outcome.stale_reason,
                    queue_lag_ms=queue_lag_ms,
                    trace=trace,
                )
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
            await self._publish_decision(
                event_id, outcome.final, trace_id=message.trace_id, amqp_priority=message.priority
            )

    async def _republish_settled_verdict(self, event_id: str, message: BusMessage) -> bool:
        """Whether this Event already has a verdict. A push that never left the process is re-published."""

        existing = await self.db.read(
            "news_triage_existing",
            lambda repos: repos.news.get_verdict(
                event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION
            ),
        )
        if existing is None:
            return False
        if existing.get("published_at_ms") is None and existing["final_decision"] in {"push", "escalate"}:
            await self._publish_decision(
                event_id, existing["final_decision"], trace_id=message.trace_id, amqp_priority=message.priority
            )
        return True

    def _route_inputs(
        self,
        card: Mapping[str, Any],
        ledger_rows: Sequence[Mapping[str, Any]],
        *,
        event_id: str,
        facts: GateFacts,
        stamp: int,
        queue_lag_ms: int,
    ) -> _RouteInputs:
        """The Event as the Program will see it, plus the hashes the persist step compares against.

        The told context is the <= TOLD_MAX cards the selector ranked against *this* candidate out of the
        bounded sent ledger. The model judges novelty against it, and its SHA — not the raw ledger's
        event-id set — is what the persist step compares, so only a change to what the model saw can make
        the judgment stale.
        """

        context = TriageContext.from_card(
            card,
            watchlist=tuple(self.watchlist),
            told_rows=ledger_rows,
            now_ms=stamp,
            queue_lag_ms=queue_lag_ms,
        )
        return _RouteInputs(
            event_id=event_id,
            card=card,
            ledger_rows=ledger_rows,
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
        elif exc.retryable and arm.circuit.record_failure(route.stamp):
            if arm.arm != "candidate":
                self._circuit_incident_open = True
                with contextlib.suppress(TransientError, DeferError):
                    await self.db.tx(
                        "news_triage_circuit",
                        functools.partial(_open_circuit_incident, now_ms=route.stamp),
                    )

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
        if arm.arm == "stable" and self._circuit_incident_open:
            self._circuit_incident_open = False
            with contextlib.suppress(TransientError, DeferError):
                await self.db.tx(
                    "news_triage_circuit_close",
                    functools.partial(_close_circuit_incidents, now_ms=route.stamp),
                )
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
                family=str(card.get("family") or "general"),
                aliases=self._aliases,
                degraded=judged.degraded,
            ),
            told=route.told,
            seen=route.ledger_rows,
            selected_context_sha=route.selected_context_sha,
            novelty_context_sha=route.novelty_context_sha,
            prelim_key=route.prelim_key,
            card=card,
            degraded=judged.degraded,
            error_code=judged.error_code,
            model_name=attempts.model_name,
            program_version=arm.program_version,
            program_sha256=arm.program_sha256,
            policy=arm.policy,
            runtime_manifest_sha=self.runtime_manifest_sha,
            trace=trace,
            stamp=route.stamp,
            allow_stale=not attempts.reasked and not judged.degraded,
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
    ) -> _RouteInputs:
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
        card, ledger_rows = bundle
        refreshed = self._route_inputs(
            card,
            ledger_rows,
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
        return refreshed

    async def _judge_telemetry(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        ledger_rows: Sequence[Mapping[str, Any]],
        stamp: int,
        message: BusMessage,
    ) -> None:
        """Deterministic judgment for one telemetry frame, then the ordinary settle path.

        No model call, so no arm assignment, no circuit breaker and no Program identity: the verdict
        carries `OI_PROGRAM_VERSION` instead, which is what the trace, `news why` and the release
        cohorts read.

        Rank, ledger and verdict are one transaction under one storyline lock. The rank is a count of
        this symbol's other frames in the window, so reading it outside the lock lets two frames for
        one symbol both see a history without the other, both claim the same rank, and both qualify —
        three cards in a window that allows two. The lock is the same key `_decide_and_persist` takes,
        and `pg_advisory_xact_lock` is re-entrant within a transaction, so it takes it again for free.
        """

        title = str(card.get("leader_title") or "")
        signal = parse_oi_signal(title)
        observed = int(card.get("opened_at_ms") or card.get("leader_published_at_ms") or stamp)
        trace: dict[str, Any] = {
            "queue_lag_ms": max(0, stamp - int(message.occurred_at_ms or stamp)),
            "attempt": message.attempt,
            "program_version": OI_PROGRAM_VERSION,
            "runtime_manifest_sha": self.runtime_manifest_sha,
            "policy": self.policy.as_dict(),
            "gate_policy_version": GATE_POLICY_VERSION,
            "evidence_version": int(card.get("evidence_version") or 0),
            "evidence_sha256": str(card.get("evidence_sha256") or ""),
            "focus_fact_id": str(card.get("focus_fact_id") or ""),
            "storyline_key_preliminary": str(card.get("storyline_key") or ""),
            "told": [],
            "told_count": 0,
        }
        if signal is None:
            # `1019` is provider provenance, not a parser guarantee. A frame that is not the template
            # carries no numbers this rule can act on; it is dropped deterministically rather than
            # falling through to a model call the Gate admitted it specifically to avoid.
            log.info("news telemetry frame not parseable event=%s title=%r", event_id, title[:120])
            verdict = TriageVerdict(
                novelty="new_fact",
                event_type="noise",
                assets=[],
                direction="neutral",
                scope="single_name",
                magnitude=0,
                actionable=False,
                confidence=1.0,
                decision="drop",
                headline_zh=title[:60] or "持仓异动帧无法解析",
                why_zh="",
            )
            trace["oi_signal"] = {"parsed": False}
            settle = self._telemetry_settle(
                event_id=event_id,
                card=card,
                facts=facts,
                verdict=verdict,
                ledger_rows=ledger_rows,
                trace=trace,
                stamp=stamp,
            )
            outcome = await self.db.tx("news_triage_persist", functools.partial(self._decide_and_persist, s=settle))
        else:

            def _rank_and_settle(repos: Any) -> _TriageOutcome:
                # The key is a pure function of the symbol for this admission, so it is known before
                # the verdict exists and can be locked first.
                repos.news.lock_storyline(f"asset:{signal.symbol}")
                earlier = repos.news.recent_oi_signal_times(
                    symbol=signal.symbol,
                    metric_version=OI_METRIC_VERSION,
                    since_ms=observed - self.oi_policy.window_ms,
                    before_ms=observed,
                    exclude_event_id=event_id,
                )
                judgment = evaluate_oi(signal, earlier_at_ms=earlier, now_ms=observed, policy=self.oi_policy)
                repos.news.insert_oi_signal(
                    event_id=event_id,
                    metric_version=OI_METRIC_VERSION,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    oi_change_bps=signal.oi_change_bps,
                    oi_value_usd=signal.oi_value_usd,
                    whale_long_profit_bps=signal.whale_long_profit_bps,
                    whale_oi_ratio_bps=signal.whale_oi_ratio_bps,
                    observed_at_ms=observed,
                    rank_in_window=judgment.rank_in_window,
                    now_ms=stamp,
                )
                trace["oi_signal"] = {
                    "parsed": True,
                    "symbol": signal.symbol,
                    "direction": signal.direction,
                    "oi_change_bps": signal.oi_change_bps,
                    "oi_value_usd": signal.oi_value_usd,
                    "whale_long_profit_bps": signal.whale_long_profit_bps,
                    "whale_oi_ratio_bps": signal.whale_oi_ratio_bps,
                    "rank_in_window": judgment.rank_in_window,
                    "rule": judgment.rule,
                    "policy": self.oi_policy.as_dict(),
                }
                return self._decide_and_persist(
                    repos,
                    s=self._telemetry_settle(
                        event_id=event_id,
                        card=card,
                        facts=facts,
                        verdict=judgment.verdict,
                        ledger_rows=ledger_rows,
                        trace=trace,
                        stamp=stamp,
                    ),
                )

            outcome = await self.db.tx("news_signal_judge", _rank_and_settle)
        if outcome.final in {"push", "escalate"}:
            await self._publish_decision(
                event_id, outcome.final, trace_id=message.trace_id, amqp_priority=message.priority
            )

    def _telemetry_settle(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        verdict: TriageVerdict,
        ledger_rows: Sequence[Mapping[str, Any]],
        trace: dict[str, Any],
        stamp: int,
    ) -> _TriageSettle:
        return _TriageSettle(
            event_id=event_id,
            evidence_version=int(card.get("evidence_version") or 0),
            evidence_sha256=str(card.get("evidence_sha256") or ""),
            focus_fact_id=str(card.get("focus_fact_id") or ""),
            judgment=ScoredJudgment.issue(
                verdict=verdict,
                editorial=EditorialEnvelope.issue(editorial_origin="telemetry_deterministic", relevance=None),
            ),
            facts=facts,
            final_key=final_storyline_key(
                title=str(card.get("leader_title") or ""),
                headline_zh=verdict.headline_zh,
                scope=verdict.scope,
                verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
                grounded_assets=facts.grounded_assets,
                family=str(card.get("family") or "general"),
                aliases=self._aliases,
                degraded=False,
            ),
            told=[],
            seen=ledger_rows,
            # An arithmetic judgment reads no ledger, so there is no selected context to go stale. The wide
            # ledger is still refreshed inside the lock, because `decide()` measures duplicates against it.
            selected_context_sha="",
            novelty_context_sha="",
            prelim_key=str(card.get("storyline_key") or ""),
            card=card,
            degraded=False,
            error_code=None,
            model_name=None,
            program_version=OI_PROGRAM_VERSION,
            program_sha256=program_sha256(self.oi_policy),
            policy=self.policy,
            runtime_manifest_sha=self.runtime_manifest_sha,
            trace=trace,
            # No model was thinking, so no card can have landed while it was: nothing to re-ask.
            allow_stale=False,
            stamp=stamp,
        )

    def _decide_and_persist(self, repos: Any, s: _TriageSettle) -> _TriageOutcome:
        """Inside one transaction, under the storyline's advisory lock: re-read the newest reader evidence and
        told entry, decide, and insert the verdict. Reports ``stale`` (no write) when a card landed after the model
        saw the ledger and the caller may still re-ask."""

        repos.news.lock_storyline(s.final_key)
        latest_evidence = repos.news.latest_evidence_snapshot(s.event_id)
        if latest_evidence is None:
            raise PermanentError("news_event_evidence_missing")
        evidence_changed = (
            int(latest_evidence["evidence_version"]) != s.evidence_version
            or str(latest_evidence["evidence_sha256"]) != s.evidence_sha256
        )
        if evidence_changed:
            if s.allow_stale:
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="evidence")
            # A second concurrent evidence change is not safe to bind to the
            # already-produced verdict.  Reconsume after the durable retry lane
            # rather than publishing a judgment over evidence it did not read.
            raise TransientError("news_event_evidence_changed")
        # The wide ledger is always re-read: `decide()` must measure this card against every card the reader
        # received, including one that landed while the model was thinking.  Only the *selected* context — the
        # bounded set of rows the model actually saw — decides whether the judgment is stale, so an unrelated new card
        # costs a cheap query instead of a second paid two-Predictor execution.
        seen = repos.news.told_ledger(now_ms=s.stamp, window_ms=TOLD_WINDOW_MS, limit=_SEEN_LEDGER_MAX)
        if s.allow_stale:
            refreshed = TriageContext.from_card(s.card, watchlist=(), told_rows=seen, now_ms=s.stamp, queue_lag_ms=0)
            if refreshed.novelty_context_sha256() != s.novelty_context_sha:
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="told")
        status = storyline_status(s.final_key, told=s.told, seen=seen)
        trace = s.trace
        decision = decide(
            s.judgment,
            s.facts,
            status,
            degraded=s.degraded,
            policy=s.policy,
        )
        trace["status_final"] = {"storyline_key": s.final_key}
        trace["storyline_key"] = s.final_key
        trace["verdict_sha256"] = s.judgment.verdict_sha256
        trace["editorial_sha256"] = s.judgment.editorial.editorial_sha256
        trace["scored_judgment_sha256"] = s.judgment.scored_judgment_sha256
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
        if grounded_restatement(s.verdict, status):
            trace["restates_event_id"] = s.told[s.verdict.restates]["event_id"]
        final = decision.final
        reason = decision.throttled_by or decision.override_rule or ""
        context_line = (
            f"[{s.verdict.audience}/{s.verdict.event_type}/{s.verdict.direction} m{s.verdict.magnitude}"
            f" → {final}·{reason}] {s.verdict.headline_zh}"
        )
        repos.news.insert_verdict(
            event_id=s.event_id,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            model_decision=(s.verdict.decision if s.judgment.editorial.editorial_origin == "model" else None),
            rule_baseline_decision=decision.rule_baseline,
            final_decision=final,
            override_rule=decision.override_rule,
            throttled_by=decision.throttled_by,
            verdict=json_ready(s.verdict),
            editorial=s.judgment.editorial.model_dump(mode="json"),
            scored_judgment_sha256=s.judgment.scored_judgment_sha256,
            runtime_manifest_sha=s.runtime_manifest_sha,
            model=s.model_name,
            program_version=s.program_version,
            program_sha256=s.program_sha256,
            degraded=s.degraded,
            error_code=s.error_code,
            trace=trace,
            evidence_version=s.evidence_version,
            evidence_sha256=s.evidence_sha256,
            focus_fact_id=s.focus_fact_id,
            now_ms=s.stamp,
        )
        repos.news.set_storyline_key(event_id=s.event_id, storyline_key=s.final_key, now_ms=s.stamp)
        repos.news.set_context_line(event_id=s.event_id, context_line=context_line, followup_of=None, now_ms=s.stamp)
        return _TriageOutcome(stale=False, final=final, decision=decision)

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
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """The Triage bundle plus a refreshed alias table, both from the one session (#75)."""

        self._refresh_aliases(repos, now=stamp)
        return self._load(repos, event_id, stamp)

    @staticmethod
    def _load(repos: Any, event_id: str, stamp: int) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        card = repos.news.event_card(event_id)
        if card is None:
            return None
        ledger = repos.news.told_ledger(now_ms=stamp, window_ms=TOLD_WINDOW_MS, limit=_SEEN_LEDGER_MAX)
        return card, ledger

    async def _publish_decision(self, event_id: str, final: str, *, trace_id: str, amqp_priority: int) -> None:
        stamp = now_ms()
        await self.bus.publish(
            BusMessage(
                kind="verdict",
                message_id=f"push:{event_id}",
                routing_key=RK_VERDICT_PUSH,
                payload={"event_id": event_id, "kind": "first"},
                trace_id=trace_id,
                occurred_at_ms=stamp,
                priority=amqp_priority,
            )
        )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_triage_mark_published",
                lambda repos: repos.news.mark_verdict_published(
                    event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION, now_ms=stamp
                ),
                timeout_seconds=1.0,
            )
