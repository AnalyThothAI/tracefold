"""Semantic judgment, deterministic policy, and verdict persistence stage."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from .. import liquidations, oi_signals
from ..artifact_identity import canonical_sha
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
from ..models import GATE_POLICY_VERSION, TRIAGE_POLICY_VERSION, json_ready
from ..program.contracts import (
    ScoredJudgment,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TriageContext,
)
from ..reader_history import ReaderHistorySnapshot
from ..release.canary import CanaryRuntimeArm
from ..source_contracts import LIQUIDATION_SOURCE_IDENTITY, OI_SOURCE_IDENTITY
from ..telemetry import NewsWorkSemantics
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


def _open_circuit_incident(repos: Any, *, now_ms: int) -> Any:
    return repos.news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms)


def _close_circuit_incidents(repos: Any, *, now_ms: int) -> Any:
    return repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms)


def _evaluate_canary_rolling_slo(repos: Any, *, activation_id: str, now_ms: int) -> dict[str, Any]:
    return dict(repos.news.evaluate_canary_rolling_slo(activation_id=activation_id, now_ms=now_ms))


def _record_deterministic_assets(repos: Any, s: _TriageSettle) -> None:
    """Give the telemetry lanes the Event assets their Gate could not ground (#267).

    A deterministic judge resolves its symbol from the provider's own fixed template — the leading
    token of `NVDA OI Rise 4.55%, OI Value 32.17M, …`, or the typed liquidation fact — and writes it
    into the verdict as a primary. The admission Gate, reading the same wire text minutes earlier,
    grounds nothing at all, so these Events carried `grounded_assets = []` and no `news_event_assets`
    row: 112 of 112 in a production day. Everything keyed on that table was consequently blind to the
    whole lane — the Reaction planner (so `p0`, 1 H and 4 H were empty on every OI frame ever judged,
    and the Price Review's sample described only the model lane), the feed's `?symbol=` filter behind
    the token page, and the instrument-grounding funnel.

    Written *after* `insert_verdict`, in the same transaction, and for every decision rather than only
    the pushed ones: a Reaction the reader never received is exactly what the potential-miss review
    reads (#88 §6), and `due_reactions` already scopes itself to live Events on its own.

    Restricted to the deterministic origin on purpose. For a model-lane Event this table is the Gate's
    grounding evidence, provenance-checked against the Item; verdict primaries there are a model's
    reading, and promoting them here would let a hallucinated ticker seed a price measurement and
    enter reader history as a canonical asset.
    """

    if s.origin not in {"oi", "liquidation"}:
        return
    assets = [(asset.symbol, asset.market_type) for asset in s.verdict.assets if asset.role == "primary"]
    if not assets:
        # A frame that did not match the template names no symbol, and the parse failure is the whole
        # verdict. There is nothing to measure a price against.
        return
    repos.news.record_event_assets(event_id=s.event_id, assets=assets)


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
        oi_policy: oi_signals.OiPolicy = oi_signals.DEFAULT_OI_POLICY,
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
        bundle = await self.db.read("news_triage_load", lambda repos: self._load_with_aliases(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, history, admission, event_kind = bundle
        # Evidence snapshots are immutable by design, so a pre-cut queued message may still carry the
        # old candidate admission after a source-contract migration has held the material Event.  Route
        # from current PostgreSQL truth before looking for a settled verdict or invoking any judge.
        if admission == "recovery" or event_kind == "unsupported_market":
            return
        policy_version = liquidations.TRIAGE_POLICY_VERSION if event_kind == "liquidation" else TRIAGE_POLICY_VERSION
        if await self._republish_settled_verdict(event_id, message, policy_version=policy_version):
            return
        if str(card.get("evidence_schema_version") or "") != "news_event_evidence_v3":
            raise PermanentError("news_event_evidence_v3_required")
        facts = _gate_facts(card, self.watchlist_symbols)
        if event_kind == "oi":
            # #137. Fixed-format open-interest telemetry: judged here by arithmetic instead of by two
            # structured model calls that would re-read four numbers a regex already has. The typed OI
            # judgment owns its DecisionResult; the storyline lock, verdict row, delivery, receipt, outcome
            # and feed then use the ordinary settle path.
            await self._judge_telemetry(
                event_id=event_id,
                card=card,
                facts=facts,
                history=history,
                stamp=stamp,
                message=message,
            )
            return
        if event_kind == "liquidation":
            await self._judge_liquidation(
                event_id=event_id,
                card=card,
                facts=facts,
                history=history,
                stamp=stamp,
                message=message,
            )
            return
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
            outcome = await self.db.tx("news_triage_persist", functools.partial(self._decide_and_persist, s=settle))
            if outcome.stale:
                if not isinstance(judged.judgment, ScoredJudgment):
                    raise RuntimeError("news_stale_non_model_judgment")
                # A card landed while the model was thinking: ask once more with the ledger it did not see (rare,
                # ~0.6% of calls at 8 pushes/h) instead of pushing a restatement the reader just received. Everything
                # the model and decide() look at is re-read under a fresh stamp so the second input is consistent.
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
        if event_kind == "unsupported_market":
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

    async def _judge_telemetry(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        history: ReaderHistorySnapshot,
        stamp: int,
        message: BusMessage,
    ) -> None:
        """Deterministic judgment for one telemetry frame, then the ordinary settle path.

        No model call, so no arm assignment and no circuit breaker: the verdict carries the deterministic
        OI Program identity, which is what the trace, `news why` and the release cohorts read.

        Rank, ledger and verdict are one transaction under one storyline lock. The rank is a count of
        this symbol's other eligible frames in the window, so reading it outside the lock lets two frames for
        one symbol both see a history without the other, both claim the same rank, and both qualify —
        three cards in a window that allows two. The lock is the same key `_decide_and_persist` takes,
        and `pg_advisory_xact_lock` is re-entrant within a transaction, so it takes it again for free.
        """

        title = str(card.get("leader_title") or "")
        signal = oi_signals.parse_oi_signal(title)
        observed = int(card.get("opened_at_ms") or card.get("leader_published_at_ms") or stamp)
        trace: dict[str, Any] = {
            "queue_lag_ms": max(0, stamp - int(message.occurred_at_ms or stamp)),
            "attempt": message.attempt,
            "program_version": oi_signals.PROGRAM_VERSION,
            "program_sha256": oi_signals.program_sha256(self.oi_policy),
            "runtime_manifest_sha": self.runtime_manifest_sha,
            "policy": self.oi_policy.as_dict(),
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
            provider_metadata = card.get("provider_metadata")
            provider_source = (
                str(provider_metadata.get("source") or "") if isinstance(provider_metadata, Mapping) else ""
            )
            judgment, failure = oi_signals.oi_parse_failure(title, provider_source=provider_source)
            log.warning(
                "news_oi_parse_failed event_id=%s strategy_id=%s provider=opennews "
                "title_sha256=%s parser_version=%s failure_stage=source_contract_drift",
                event_id,
                OI_SOURCE_IDENTITY.strategy_id,
                failure["title_sha256"],
                failure["parser_version"],
            )
            trace["oi_signal"] = failure
            settle = self._deterministic_settle(
                event_id=event_id,
                card=card,
                facts=facts,
                judgment=judgment,
                history=history,
                trace=trace,
                stamp=stamp,
                error_code="oi_parse_failed",
                program_version=oi_signals.PROGRAM_VERSION,
                program_sha256=oi_signals.program_sha256(self.oi_policy),
            )

            def _settle_failure(repos: Any) -> _TriageOutcome:
                return self._decide_and_persist(repos, s=settle)

            outcome = await self.db.tx("news_triage_persist", _settle_failure)
        else:
            # What the provider proves about *how* the frame was measured, kept beside what it measured
            # (#265). `None` is a real answer — the interval is unproven — and the frame is still a
            # perfectly good reader card; it simply may not be read as a claim about an interval.
            source = oi_signals.oi_source_contract(card.get("provider_metadata"))

            def _rank_and_settle(repos: Any) -> _TriageOutcome:
                # The key is a pure function of the symbol for this admission, so it is known before
                # the verdict exists and can be locked first.
                repos.news.lock_storyline(f"asset:{signal.symbol}")
                earlier_eligible_count = repos.news.count_recent_eligible_oi_signals(
                    symbol=signal.symbol,
                    metric_version=oi_signals.METRIC_VERSION,
                    since_ms=observed - self.oi_policy.window_ms,
                    before_ms=observed,
                    whale_oi_ratio_above_bps=self.oi_policy.whale_oi_ratio_above_bps,
                    oi_change_at_least_bps=self.oi_policy.oi_change_at_least_bps,
                    exclude_event_id=event_id,
                )
                judgment = oi_signals.evaluate_oi(
                    signal,
                    earlier_eligible_count=earlier_eligible_count,
                    policy=self.oi_policy,
                )
                provider_metadata = card.get("provider_metadata")
                source_venue = (
                    str(provider_metadata.get("source") or "") or None
                    if isinstance(provider_metadata, Mapping)
                    else None
                )
                repos.news.insert_oi_signal(
                    event_id=event_id,
                    metric_version=oi_signals.METRIC_VERSION,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    oi_change_bps=signal.oi_change_bps,
                    oi_value_usd=signal.oi_value_usd,
                    whale_long_profit_bps=signal.whale_long_profit_bps,
                    whale_oi_ratio_bps=signal.whale_oi_ratio_bps,
                    observed_at_ms=observed,
                    rank_in_window=judgment.rank_in_window,
                    now_ms=stamp,
                    source_strategy_id=None if source is None else source.strategy_id,
                    source_contract_version=None if source is None else source.contract_version,
                    measurement_window_ms=None if source is None else source.measurement_window_ms,
                    source_item_id=str(card["leader_item_id"]),
                    source_venue=source_venue,
                )
                trace["oi_signal"] = oi_signals.oi_judgment_trace(judgment, policy=self.oi_policy, source=source)
                return self._decide_and_persist(
                    repos,
                    s=self._deterministic_settle(
                        event_id=event_id,
                        card=card,
                        facts=facts,
                        judgment=judgment,
                        history=history,
                        trace=trace,
                        stamp=stamp,
                        program_version=oi_signals.PROGRAM_VERSION,
                        program_sha256=oi_signals.program_sha256(self.oi_policy),
                    ),
                )

            outcome = await self.db.tx("news_signal_judge", _rank_and_settle)
        if outcome.final in {"push", "escalate"}:
            await publish_verdict(
                self.bus,
                self.db,
                event_id=event_id,
                trace_id=message.trace_id,
                amqp_priority=message.priority,
                policy_version=TRIAGE_POLICY_VERSION,
            )

    async def _judge_liquidation(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        history: ReaderHistorySnapshot,
        stamp: int,
        message: BusMessage,
    ) -> None:
        """Read the admission-time typed fact and persist one deterministic, direction-neutral verdict."""

        provider_metadata = card.get("provider_metadata")
        provider_source = str(provider_metadata.get("source") or "") if isinstance(provider_metadata, Mapping) else ""
        base_trace: dict[str, Any] = {
            "queue_lag_ms": max(0, stamp - int(message.occurred_at_ms or stamp)),
            "attempt": message.attempt,
            "program_version": liquidations.PROGRAM_VERSION,
            "program_sha256": liquidations.program_sha256(),
            "runtime_manifest_sha": self.runtime_manifest_sha,
            "policy": {"policy_version": liquidations.TRIAGE_POLICY_VERSION},
            "gate_policy_version": liquidations.ADMISSION_POLICY_VERSION,
            "evidence_version": int(card.get("evidence_version") or 0),
            "evidence_sha256": str(card.get("evidence_sha256") or ""),
            "focus_fact_id": str(card.get("focus_fact_id") or ""),
            "storyline_key_preliminary": str(card.get("storyline_key") or ""),
            "told": [],
            "told_count": 0,
        }

        def _settle(repos: Any) -> _TriageOutcome:
            row = repos.news.market_liquidation(
                item_id=str(card.get("leader_item_id") or ""),
                fact_id=str(card.get("focus_fact_id") or ""),
                parser_version=liquidations.PARSER_VERSION,
            )
            error_code = None
            if row is None:
                judgment, fact_trace = liquidations.parse_failure(
                    str(card.get("leader_title") or ""), provider_source=provider_source
                )
                error_code = "liquidation_parse_failed"
                log.warning(
                    "news_liquidation_parse_failed event_id=%s strategy_id=%s provider=opennews "
                    "title_sha256=%s parser_version=%s failure_stage=source_contract_drift",
                    event_id,
                    LIQUIDATION_SOURCE_IDENTITY.strategy_id,
                    fact_trace["title_sha256"],
                    fact_trace["parser_version"],
                )
            else:
                fact = liquidations.LiquidationFact(
                    source_key=str(row["source_key"]),
                    item_id=str(row["item_id"]),
                    fact_id=str(row["fact_id"]),
                    symbol=str(row["symbol"]),
                    venue=str(row["venue"]),  # type: ignore[arg-type]
                    liquidated_position_side=str(row["liquidated_position_side"]),  # type: ignore[arg-type]
                    forced_order_side=str(row["forced_order_side"]),  # type: ignore[arg-type]
                    notional_usd=row["notional_usd"],
                    quantity=row["quantity"],
                    price=row["price"],
                    event_at_ms=int(row["event_at_ms"]),
                    received_at_ms=int(row["received_at_ms"]),
                    provider_record_identity=str(row["provider_record_identity"]),
                    symbol_contract_identity=str(row["symbol_contract_identity"]),
                    position_side_semantics=str(row["position_side_semantics"]),
                    quantity_semantics=str(row["quantity_semantics"]),
                    notional_semantics=str(row["notional_semantics"]),
                    price_semantics=str(row["price_semantics"]),
                    completeness_assumption=str(row["completeness_assumption"]),
                    throttle_assumption=str(row["throttle_assumption"]),
                    source_contract_version=str(row["source_contract_version"]),
                    source_contract_complete=bool(row["source_contract_complete"]),
                    parser_version=str(row["parser_version"]),
                )
                judgment = liquidations.judge(fact)
                fact_trace = liquidations.trace(fact)
            trace = {**base_trace, "liquidation": fact_trace}
            return self._decide_and_persist(
                repos,
                s=self._deterministic_settle(
                    event_id=event_id,
                    card=card,
                    facts=facts,
                    judgment=judgment,
                    history=history,
                    trace=trace,
                    stamp=stamp,
                    error_code=error_code,
                    program_version=liquidations.PROGRAM_VERSION,
                    program_sha256=liquidations.program_sha256(),
                    policy_version=liquidations.TRIAGE_POLICY_VERSION,
                ),
            )

        outcome = await self.db.tx("news_liquidation_judge", _settle)
        if outcome.final in {"push", "escalate"}:
            await publish_verdict(
                self.bus,
                self.db,
                event_id=event_id,
                trace_id=message.trace_id,
                amqp_priority=message.priority,
                policy_version=liquidations.TRIAGE_POLICY_VERSION,
            )

    def _deterministic_settle(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        judgment: oi_signals.OiJudgment | liquidations.LiquidationJudgment,
        history: ReaderHistorySnapshot,
        trace: dict[str, Any],
        stamp: int,
        error_code: str | None = None,
        program_version: str,
        program_sha256: str,
        policy_version: str = TRIAGE_POLICY_VERSION,
    ) -> _TriageSettle:
        verdict = judgment.verdict
        return _TriageSettle(
            event_id=event_id,
            evidence_version=int(card.get("evidence_version") or 0),
            evidence_sha256=str(card.get("evidence_sha256") or ""),
            focus_fact_id=str(card.get("focus_fact_id") or ""),
            judgment=judgment,
            facts=facts,
            final_key=final_storyline_key(
                title=str(card.get("leader_title") or ""),
                headline_zh=verdict.headline_zh,
                scope=verdict.scope,
                verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
                grounded_assets=facts.grounded_assets,
                dedupe_family=str(card.get("dedupe_family") or "general"),
                aliases=self._aliases,
                degraded=False,
            ),
            told=[],
            history=history,
            # An arithmetic judgment reads no semantic history, so there is no selected context to go stale.
            selected_context_sha="",
            novelty_context_sha="",
            prelim_key=str(card.get("storyline_key") or ""),
            card=card,
            degraded=False,
            error_code=error_code,
            model_name=None,
            program_version=program_version,
            program_sha256=program_sha256,
            policy_version=policy_version,
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
        history = _read_history(repos.news, event_id=s.event_id, card=s.card, now_ms=s.stamp)
        seen = _recent_seen(history)
        if s.allow_stale and _novelty_context_sha(s.card, history, now_ms=s.stamp) != s.novelty_context_sha:
            return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="told")
        status = storyline_status(s.final_key, told=s.told, seen=seen)
        trace = s.trace
        if isinstance(s.judgment, ScoredJudgment):
            decision = decide(s.judgment, s.facts, status, policy=s.policy)
        else:
            decision = s.deterministic_decision
        trace["status_final"] = {"storyline_key": s.final_key}
        trace["storyline_key"] = s.final_key
        trace["verdict_sha256"] = canonical_sha(s.verdict.model_dump(mode="json"))
        trace["judgment_contract_version"] = s.judgment.judgment_contract_version
        trace["judgment_origin"] = s.origin
        trace["judgment_sha256"] = s.judgment_sha256
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
        repos.news.insert_verdict(
            event_id=s.event_id,
            stage="triage",
            policy_version=s.policy_version,
            judgment_contract_version=s.judgment.judgment_contract_version,
            judgment_origin=s.origin,
            rule_baseline_decision=decision.rule_baseline,
            final_decision=final,
            override_rule=decision.override_rule,
            throttled_by=decision.throttled_by,
            verdict=json_ready(s.verdict),
            model_editorial=model_editorial,
            judgment_sha256=s.judgment_sha256,
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
        _record_deterministic_assets(repos, s)
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
