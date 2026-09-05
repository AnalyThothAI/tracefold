"""The typed vocabulary of one Triage route: its arm, its inputs, its attempts, and its outcome.

Separated from the consumer so that `triage.py` reads as a sequence of named phases over these shapes.
Nothing here touches the broker, the database, the clock or a model; the state that does change — the
circuit breaker's counters and the audit trail a re-ask amends — is named and carried explicitly rather
than living as locals inside one long function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, assert_never

from ..events.storyline import STORYLINE_REGISTRY_SHA256
from ..models import GATE_POLICY_VERSION, TriageVerdict
from ..program.contracts import ScoredJudgment, SemanticJudge, TriageContext
from ..reader_history import ReaderHistorySnapshot
from ..triage_rules import DecidePolicy, DecisionResult, DegradedJudgment, GateFacts, fallback_verdict
from .triage_audit import _reader_history_trace, _told_trace

_TriageJudgment = ScoredJudgment | DegradedJudgment
# What the editorial pipeline can produce. `oi` and `liquidation` were origins here for as long as
# market frames wore a verdict; they are stored facts now and produce no judgment at all (#553).
_JudgmentOrigin = Literal["model", "degraded"]


@dataclass
class _Circuit:
    failures: int = 0
    open_until_ms: int = 0
    threshold: int = 3
    open_seconds: float = 60.0

    def is_open(self, at_ms: int) -> bool:
        return at_ms < self.open_until_ms

    def record_failure(self, at_ms: int) -> bool:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until_ms = at_ms + int(self.open_seconds * 1000)
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0


@dataclass(frozen=True, slots=True)
class _TriageSettle:
    """Everything the in-transaction decide-and-persist step needs (built after the model call)."""

    event_id: str
    evidence_version: int
    evidence_sha256: str
    focus_fact_id: str
    judgment: _TriageJudgment
    facts: GateFacts
    final_key: str
    told: Sequence[Mapping[str, Any]]
    history: ReaderHistorySnapshot
    selected_context_sha: str
    novelty_context_sha: str
    prelim_key: str
    card: Mapping[str, Any]
    degraded: bool
    error_code: str | None
    model_name: str | None
    program_version: str
    program_sha256: str
    policy_version: str
    policy: DecidePolicy
    runtime_manifest_sha: str
    trace: dict[str, Any]
    stamp: int
    allow_stale: bool
    # #400: which durable transition the stable arm's Triage circuit owes PostgreSQL, applied inside the
    # same persist transaction as the verdict. There is no process-memory copy of incident state: an
    # open circuit re-asserts the incident on every settle and a stable Program answer closes it, so a
    # failed transaction converges on the next attempt instead of leaving PostgreSQL stale forever.
    circuit_incident: Literal["open", "close"] | None = None

    @property
    def verdict(self) -> TriageVerdict:
        judgment = self.judgment
        if isinstance(judgment, (ScoredJudgment, DegradedJudgment)):
            return judgment.verdict
        return assert_never(judgment)

    @property
    def origin(self) -> _JudgmentOrigin:
        judgment = self.judgment
        if isinstance(judgment, ScoredJudgment):
            return "model"
        if isinstance(judgment, DegradedJudgment):
            return "degraded"
        return assert_never(judgment)

    @property
    def judgment_sha256(self) -> str:
        judgment = self.judgment
        if isinstance(judgment, ScoredJudgment):
            return judgment.scored_judgment_sha256
        if isinstance(judgment, DegradedJudgment):
            return judgment.judgment_sha256
        return assert_never(judgment)

    @property
    def deterministic_decision(self) -> DecisionResult:
        judgment = self.judgment
        if isinstance(judgment, ScoredJudgment):
            raise TypeError("news_model_judgment_requires_transactional_status")
        if isinstance(judgment, DegradedJudgment):
            return judgment.decision
        return assert_never(judgment)


@dataclass(frozen=True, slots=True)
class _TriageOutcome:
    stale: bool
    final: str
    decision: DecisionResult | None
    stale_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ArmSelection:
    """Which Program, policy and breaker judge this Event, and every reason none of them can."""

    assignment: Mapping[str, Any]
    arm: str
    bundle_sha: str
    activation_id: str | None
    judge: SemanticJudge | None
    policy: DecidePolicy
    program_version: str
    program_sha256: str
    circuit: _Circuit
    validation_error: str
    candidate_artifact_missing: bool
    identity_mismatch: bool

    @property
    def unavailable_code(self) -> str:
        """Why there is no Program to ask. Ordered most specific first; each one is a distinct verdict."""

        if self.validation_error:
            return "news_canary_assignment_identity_invalid"
        if self.candidate_artifact_missing:
            return "news_canary_artifact_missing"
        if self.identity_mismatch:
            return "news_semantic_program_identity_mismatch"
        return "news_semantic_program_unconfigured"

    @property
    def circuit_open_code(self) -> str:
        return "news_canary_circuit_open" if self.arm == "candidate" else "news_triage_circuit_open"

    def trace_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "activation_id": self.assignment.get("activation_id"),
            "arm": self.arm,
            "bundle_sha": self.bundle_sha,
            "selector_version": self.assignment.get("selector_version"),
            "eligibility_reason": self.assignment.get("eligibility_reason"),
        }
        if self.validation_error:
            view["validation_error"] = self.validation_error
        return view


@dataclass(frozen=True, slots=True)
class _RouteInputs:
    """One consistent view of the Event: what the Program sees, and the hashes the persist step re-checks.

    Replaced wholesale by the stale re-ask rather than mutated, so the second ask cannot end up holding
    a new ledger beside an old context SHA.
    """

    event_id: str
    card: Mapping[str, Any]
    history: ReaderHistorySnapshot
    facts: GateFacts
    context: TriageContext
    told: Sequence[Mapping[str, Any]]
    selected_context_sha: str
    novelty_context_sha: str
    prelim_key: str
    wire_title: str
    stamp: int


@dataclass(slots=True)
class _ProgramAttempts:
    """What survives across the one permitted re-ask: the audit trail and the first valid judgment."""

    executions: list[dict[str, Any]] = field(default_factory=list)
    selected_index: int | None = None
    model_name: str | None = None
    first_judgment: ScoredJudgment | None = None
    reasked: bool = False
    reask_reason: str | None = None
    # Whether the stable Program answered on this route. It is the recovery signal the durable Triage
    # circuit incident is closed by, and it is recomputed on every attempt rather than remembered.
    stable_program_answered: bool = False


@dataclass(frozen=True, slots=True)
class _Judged:
    """The judgment this pass produced, and whether it came from the Program or from the fallback."""

    judgment: ScoredJudgment | DegradedJudgment
    degraded: bool
    error_code: str | None


def _gate_facts(card: Mapping[str, Any], watchlist_symbols: frozenset[str]) -> GateFacts:
    return GateFacts(
        grounded_assets=tuple(card.get("grounded_assets") or []),
        watchlist_symbols=watchlist_symbols,
        admission=str(card.get("admission") or ""),
        source_age_s=card.get("source_age_s"),
        member_count=max(1, int(card.get("member_count") or 1)),
    )


def _degraded_judgment(route: _RouteInputs, code: str) -> _Judged:
    """The typed deterministic result for an Event no Program answered."""

    judgment = fallback_verdict(route.facts, error_code=code, title=route.wire_title)
    return _Judged(judgment=judgment, degraded=True, error_code=code)


def _resolve_after_reask_failure(
    route: _RouteInputs,
    *,
    attempts: _ProgramAttempts,
    code: str,
    trace: dict[str, Any],
) -> _Judged:
    """The re-ask failed. Whether the first judgment survives depends on *why* it went stale.

    A told-only re-ask failure may keep it: its evidence is unchanged and only its novelty view predates
    the newest delivered card. An evidence change may not — that judgment read a different Event.
    """

    trace["reask_failed"] = code
    if attempts.reask_reason == "evidence":
        attempts.model_name = None
        attempts.selected_index = None
        return _degraded_judgment(route, code)
    first = attempts.first_judgment
    if first is None:
        # Callers only reach here after a re-ask, which is only entered with a first judgment in hand.
        raise ValueError("news_stale_first_judgment_missing")
    if attempts.selected_index is not None:
        attempts.executions[attempts.selected_index]["status"] = "accepted_after_reask_failure"
    return _Judged(judgment=first, degraded=False, error_code=None)


def _initial_trace(
    route: _RouteInputs,
    *,
    arm: _ArmSelection,
    queue_lag_ms: int,
    attempt: int,
    runtime_manifest_sha: str,
) -> dict[str, Any]:
    """The replayable record of what this route ran under, before it runs."""

    card = route.card
    return {
        "queue_lag_ms": queue_lag_ms,
        "attempt": attempt,
        "program_version": arm.program_version,
        "program_sha256": arm.program_sha256,
        "runtime_manifest_sha": runtime_manifest_sha,
        # The policy numbers and the Gate version that produced this decision, not just the rule name: a
        # verdict has to be replayable against the thresholds it actually ran under (#81).
        "policy": arm.policy.as_dict(),
        "gate_policy_version": GATE_POLICY_VERSION,
        # The identity of the alias registry that produced the storyline keys. It is an audit field,
        # deliberately outside `policy_sha256` and opening no learning epoch: maintaining the registry
        # is data maintenance, not a policy change (#509 D5).
        "storyline_registry_sha256": STORYLINE_REGISTRY_SHA256,
        "evidence_version": int(card.get("evidence_version") or 0),
        "evidence_sha256": str(card.get("evidence_sha256") or ""),
        "focus_fact_id": str(card.get("focus_fact_id") or ""),
        "storyline_key_preliminary": route.prelim_key,
        "status": {
            "storyline_key": route.prelim_key,
            "preliminary": True,
            "queue_lag_ms": queue_lag_ms,
        },
        "told": _told_trace(route.told),
        "told_count": len(route.told),
        "reader_history": _reader_history_trace(route.history, route.told),
        "agent_assignment": arm.trace_view(),
    }
