"""`validate_candidate()`: does this policy change actually make the product better? (offline, no model, no broker)

One deep module behind one command. The caller says "here is a frozen corpus and a candidate policy" and gets
back a release decision with the evidence that produced it; it never has to know about window reconstruction,
ledger replay, duplicate measurement or gate arithmetic.

Why it exists: the Triage prompt burned eight versions in 32 hours and `decide()` four policy versions, none of
them with a before/after on the same inputs. `news replay-decisions` re-runs `decide()` against the *stored*
window snapshot, which is a first-order counterfactual: it can tell you a card would flip, but not what the flip
does to every later card's window and to the reader's ledger. The gate needs the second-order answer, so this
replays the corpus sequentially and rebuilds the state each decision sees.

Two rules keep the measurement honest:

* **The verdict is frozen.** We cannot re-ask the model, so a replay only judges `decide()`. A prompt candidate
  needs a different instrument and this module does not pretend otherwise.
* **The duplicate metric is not the policy's own metric.** `decide()` releases a card on character-bigram
  Jaccard; the gate scores duplicates with 3-gram containment over the shorter card. Scoring a rule with the rule
  is not evidence, it is a tautology.
"""

from __future__ import annotations

import collections
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict, json_ready
from tracefold.news.triage_rules import (
    ESCALATE_WINDOW_MS,
    PUSH_WINDOW_MS,
    DecidePolicy,
    GateFacts,
    StorylineStatus,
    decide,
)

CORPUS_VERSION: Final = "news_recall_corpus_v1"
GATE_VERSION: Final = "news_release_gate_v1"
_HOUR_MS: Final = 3600_000
_SEEN_WINDOW_MS: Final = 4 * _HOUR_MS
# Expectations an operator can attach to a case. `must_push` is the recall truth: the reader had to get this.
EXPECTATIONS: Final = frozenset({"must_push", "may_push", "may_drop"})
_MUST_PUSH_LABELS: Final = frozenset({"must_push", "missed"})
_MAY_DROP_LABELS: Final = frozenset({"noise", "dup"})
_RETENTION_LABELS: Final = frozenset({"good"})
# Independent duplicate metric (see module docstring): 3-gram containment over the shorter headline.
_DUPLICATE_CONTAINMENT: Final = 0.35
_STRONG_CONTAINMENT: Final = 0.55
# How many near-duplicate pairs a candidate may add per fact it stops losing. The operator's standing judgment is
# that a miss costs far more than a repeat — the throttle this gate was built for was losing 7 facts for every
# repeat it prevented — so duplicates are a bounded price on a recall win, never a veto on one. A candidate that
# adds duplicates *without* winning recall fails outright, and a near-verbatim repeat is never tradable at all.
_DUPLICATE_TRADE_RATIO: Final = 3.0


def _trigrams(text: str) -> frozenset[str]:
    compact = "".join(str(text or "").split())
    if len(compact) < 3:
        return frozenset()
    return frozenset(compact[i : i + 3] for i in range(len(compact) - 2))


def _containment(left: str, right: str) -> float:
    a, b = _trigrams(left), _trigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ corpus
@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One historical Triage decision, with everything `decide()` needs to be re-run against it."""

    event_id: str
    at_ms: int
    storyline_key: str
    verdict: TriageVerdict
    facts: GateFacts
    told: tuple[dict[str, Any], ...]
    degraded: bool
    subject: str
    expect: str
    label: str
    stored_final: str


@dataclass(frozen=True, slots=True)
class Corpus:
    version: str
    created_at_ms: int
    from_ms: int
    to_ms: int
    cases: tuple[CorpusCase, ...]
    sha256: str
    prompt_versions: Mapping[str, int] = field(default_factory=dict)

    @property
    def span_hours(self) -> float:
        return max(0.0, (self.to_ms - self.from_ms) / _HOUR_MS)

    def boundary(self) -> tuple[CorpusCase, ...]:
        """Cases somebody marked "the reader had to get this" — the set a candidate must improve on."""

        return tuple(case for case in self.cases if case.expect == "must_push")

    def retention(self) -> tuple[CorpusCase, ...]:
        """Cases an operator confirmed the pipeline already got right — the set a candidate must not disturb."""

        return tuple(case for case in self.cases if case.label in _RETENTION_LABELS)


def freeze_corpus(
    repos: Any, *, now_ms: int, hours: int, watchlist_symbols: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Export every Triage decision of the window as a self-hashing, replayable corpus.

    Only Events that reached Triage are cases: an Event the Gate suppressed has no verdict, so `decide()` has
    nothing to re-run against it. `expect` starts from whatever the operator labelled and stays `may_push`
    otherwise — an unlabelled case constrains nothing, which is the honest default when the label plane is empty.
    """

    since_ms = int(now_ms) - int(hours) * _HOUR_MS
    rows = repos.conn.execute(
        """
        SELECT v.event_id, v.created_at_ms, v.final_decision, v.degraded, v.verdict, v.trace,
               e.storyline_key, e.leader_title, e.grounded_assets, e.priority, e.admission, e.provider_score_max,
               v.prompt_version,
               (SELECT l.label ->> 'label' FROM news_event_labels l
                 WHERE l.event_id = v.event_id ORDER BY l.created_at_ms DESC LIMIT 1) AS label
          FROM news_verdicts v
          JOIN news_events e ON e.event_id = v.event_id
         WHERE v.stage = 'triage' AND v.created_at_ms >= %s
         ORDER BY v.created_at_ms
        """,
        (since_ms,),
    ).fetchall()
    cases: list[dict[str, Any]] = []
    skipped = 0
    prompt_versions: collections.Counter[str] = collections.Counter()
    for row in rows:
        raw = dict(row["verdict"] or {})
        try:
            # Verdicts stored before prompt v7 have no novelty field; replay them as new_fact (never a restatement).
            TriageVerdict.model_validate({"novelty": "new_fact", **raw})
        except ValueError:
            skipped += 1  # a retired verdict schema: unreplayable, and saying so beats silently shrinking the corpus
            continue
        trace = dict(row["trace"] or {})
        prompt_versions[str(row["prompt_version"] or "")] += 1
        cases.append(
            {
                "event_id": str(row["event_id"]),
                "at_ms": int(row["created_at_ms"]),
                "storyline_key": str(trace.get("storyline_key") or row["storyline_key"] or ""),
                "verdict": {"novelty": "new_fact", **raw},
                "gate": {
                    "grounded_assets": list(row["grounded_assets"] or []),
                    "provider_score": row["provider_score_max"],
                    "priority": str(row["priority"] or "normal"),
                    "admission": str(row["admission"] or ""),
                },
                "told": [t for t in (trace.get("told") or []) if isinstance(t, Mapping)],
                "degraded": bool(row["degraded"]),
                "subject": " ".join(str(row["leader_title"] or "").split())[:200],
                "expect": _expectation(row["label"]),
                "label": str(row["label"] or ""),
                "stored_final": str(row["final_decision"]),
            }
        )
    payload: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "created_at_ms": int(now_ms),
        "from_ms": min((c["at_ms"] for c in cases), default=since_ms),
        "to_ms": max((c["at_ms"] for c in cases), default=since_ms),
        "watchlist_symbols": sorted(watchlist_symbols),
        "prompt_versions": dict(sorted(prompt_versions.items())),
        "skipped_unreplayable_verdicts": skipped,
        "cases": cases,
    }
    payload["sha256"] = _sha256(payload)
    return payload


def _expectation(label: Any) -> str:
    text = str(label or "")
    if text in _MUST_PUSH_LABELS:
        return "must_push"
    if text in _MAY_DROP_LABELS:
        return "may_drop"
    return "may_push"


def load_corpus(payload: Mapping[str, Any], *, expectations: Mapping[str, str] | None = None) -> Corpus:
    """Rebuild a Corpus from a frozen payload, verifying its hash, and overlay reviewed expectations.

    The overlay is a separate file on purpose: the corpus is a mechanical export of what happened, the
    expectations are human judgments about what should have happened, and only the second belongs in review.
    """

    stored = str(payload.get("sha256") or "")
    recomputed = _sha256({k: v for k, v in payload.items() if k != "sha256"})
    if stored and stored != recomputed:
        raise ValueError("news_corpus_sha_mismatch")
    # A reviewed overlay is a document a person edits, so it carries prose: keys starting with `_` are notes for
    # whoever edits it next, not expectations. Rejecting them made the shipped fixture — and the command in
    # docs/DEVELOPMENT.md — exit 2 with the comment text quoted back as an invalid expectation.
    overlay = {str(k): str(v) for k, v in (expectations or {}).items() if not str(k).startswith("_")}
    unknown = sorted({v for v in overlay.values()} - EXPECTATIONS)
    if unknown:
        raise ValueError(f"news_corpus_expectation_invalid:{unknown[0]}")
    watchlist = frozenset(str(s) for s in (payload.get("watchlist_symbols") or []))
    cases: list[CorpusCase] = []
    for raw in payload.get("cases") or []:
        gate = dict(raw.get("gate") or {})
        cases.append(
            CorpusCase(
                event_id=str(raw["event_id"]),
                at_ms=int(raw["at_ms"]),
                storyline_key=str(raw.get("storyline_key") or ""),
                verdict=TriageVerdict.model_validate(dict(raw["verdict"])),
                facts=GateFacts(
                    grounded_assets=tuple(gate.get("grounded_assets") or []),
                    watchlist_symbols=watchlist,
                    provider_score=gate.get("provider_score"),
                    priority=str(gate.get("priority") or "normal"),
                    admission=str(gate.get("admission") or ""),
                ),
                told=tuple(dict(t) for t in (raw.get("told") or [])),
                degraded=bool(raw.get("degraded")),
                subject=str(raw.get("subject") or ""),
                expect=overlay.get(str(raw["event_id"]), str(raw.get("expect") or "may_push")),
                label=str(raw.get("label") or ""),
                stored_final=str(raw.get("stored_final") or ""),
            )
        )
    cases.sort(key=lambda c: c.at_ms)
    return Corpus(
        version=str(payload.get("corpus_version") or CORPUS_VERSION),
        created_at_ms=int(payload.get("created_at_ms") or 0),
        from_ms=int(payload.get("from_ms") or 0),
        to_ms=int(payload.get("to_ms") or 0),
        cases=tuple(cases),
        sha256=recomputed,
        prompt_versions=dict(payload.get("prompt_versions") or {}),
    )


# ------------------------------------------------------------------------------------------------ replay
@dataclass(frozen=True, slots=True)
class ArmReport:
    """What one policy did to the whole corpus, replayed in order with the state rebuilt each step."""

    delivered: tuple[str, ...]
    withheld: tuple[str, ...]
    # Withheld by a *rule* (storyline throttle, flood ceiling, hourly cap) rather than by the model's own drop.
    # Only these can move when a policy changes, so only these carry the recall cost of a policy decision.
    withheld_by_rule: tuple[str, ...]
    per_hour_peak: int
    per_hour_mean: float
    rules: Mapping[str, int]
    throttled_by: Mapping[str, int]
    duplicate_pairs: int
    strong_duplicate_pairs: int
    missed_facts: int
    hourly_cap_hits: int

    @property
    def delivered_set(self) -> frozenset[str]:
        return frozenset(self.delivered)


def replay_corpus(corpus: Corpus, policy: DecidePolicy, *, hourly_cap: int) -> ArmReport:
    """Re-decide every case in order, rebuilding the storyline windows and the reader's ledger as we go."""

    pushes: dict[str, list[tuple[int, int, str]]] = collections.defaultdict(list)
    # (at_ms, headline_zh, event_id, direction) — the direction is what lets the replay exercise the reversal
    # exemption `decide()` applies to the similarity withhold (#100).
    ledger: list[tuple[int, str, str, str]] = []
    delivered: list[str] = []
    withheld: list[str] = []
    withheld_by_rule: list[str] = []
    rules: collections.Counter[str] = collections.Counter()
    throttled: collections.Counter[str] = collections.Counter()
    for case in corpus.cases:
        history = pushes[case.storyline_key]
        window_2h = [h for h in history if h[0] >= case.at_ms - PUSH_WINDOW_MS]
        window_4h = [h for h in history if h[0] >= case.at_ms - ESCALATE_WINDOW_MS]
        seen = [entry for entry in ledger if entry[0] >= case.at_ms - _SEEN_WINDOW_MS]
        status = StorylineStatus(
            key=case.storyline_key,
            pushed_2h=len(window_2h),
            pushed_4h=len(window_4h),
            max_magnitude_2h=max((h[1] for h in window_2h), default=0),
            max_magnitude_4h=max((h[1] for h in window_4h), default=0),
            directions_2h=tuple(sorted({h[2] for h in window_2h})),
            directions_4h=tuple(sorted({h[2] for h in window_4h})),
            last_push_ago_ms=(case.at_ms - max(h[0] for h in history)) if history else None,
            told_directions=tuple(str(t.get("dir") or "") for t in case.told),
            seen_headlines=tuple(headline for (_, headline, _, _) in reversed(seen)),
            seen_event_ids=tuple(event_id for (_, _, event_id, _) in reversed(seen)),
            seen_directions=tuple(direction for (_, _, _, direction) in reversed(seen)),
        )
        sent_last_hour = sum(1 for entry in ledger if entry[0] >= case.at_ms - _HOUR_MS)
        result = decide(
            case.verdict,
            case.facts,
            status,
            hourly_cap_reached=sent_last_hour >= hourly_cap,
            degraded=case.degraded,
            policy=policy,
        )
        pushed = result.final in {"push", "escalate"}
        # The Deliverer drops a push (never an escalate) once the hourly cap is reached, so a decision to push is
        # not the same as a card the reader received (consumers.DelivererConsumer).
        reached_reader = pushed and not (result.final == "push" and sent_last_hour >= hourly_cap)
        if pushed:
            pushes[case.storyline_key].append((case.at_ms, case.verdict.magnitude, case.verdict.direction))
        if reached_reader:
            delivered.append(case.event_id)
            rules[str(result.override_rule or "")] += 1
            if not case.degraded:
                # A degraded card is a placeholder headline; it is not what the reader recognises as a card, and
                # the told ledger excludes it for the same reason.
                ledger.append((case.at_ms, case.verdict.headline_zh, case.event_id, case.verdict.direction))
        else:
            withheld.append(case.event_id)
            if result.throttled_by or pushed:
                # `pushed and not reached_reader` is the Deliverer's hourly cap: a rule withheld it just the same.
                withheld_by_rule.append(case.event_id)
                throttled[result.throttled_by or "hourly_cap"] += 1
    return _score(corpus, delivered, withheld, withheld_by_rule, rules, throttled, ledger)


def _score(
    corpus: Corpus,
    delivered: Sequence[str],
    withheld: Sequence[str],
    withheld_by_rule: Sequence[str],
    rules: Mapping[str, int],
    throttled: Mapping[str, int],
    ledger: Sequence[tuple[int, str, str, str]],
) -> ArmReport:
    by_hour = collections.Counter(at // _HOUR_MS for (at, _, _, _) in ledger)
    counts = sorted(by_hour.values())
    duplicates = strong = 0
    for index, (at, headline, _, _) in enumerate(ledger):
        for other_at, other, _, _ in ledger[index + 1 :]:
            if other_at - at > _SEEN_WINDOW_MS:
                break
            score = _containment(headline, other)
            if score >= _DUPLICATE_CONTAINMENT:
                duplicates += 1
            if score >= _STRONG_CONTAINMENT:
                strong += 1
    by_id = {case.event_id: case for case in corpus.cases}
    missed = _missed_facts([by_id[e] for e in withheld_by_rule if e in by_id], ledger)
    return ArmReport(
        delivered=tuple(delivered),
        withheld=tuple(withheld),
        withheld_by_rule=tuple(withheld_by_rule),
        per_hour_peak=max(counts, default=0),
        per_hour_mean=round(len(ledger) / corpus.span_hours, 2) if corpus.span_hours else 0.0,
        rules=dict(sorted(rules.items())),
        throttled_by=dict(sorted(throttled.items(), key=lambda kv: (-kv[1], kv[0]))),
        duplicate_pairs=duplicates,
        strong_duplicate_pairs=strong,
        missed_facts=missed,
        hourly_cap_hits=int(throttled.get("hourly_cap", 0)),
    )


def _missed_facts(withheld: Iterable[CorpusCase], ledger: Sequence[tuple[int, str, str, str]]) -> int:
    """Facts the reader never received in any wording — the recall cost of the arm.

    A withheld card counts only when nothing resembling it was delivered in the surrounding window (so a card
    merely delayed does not count), and several wordings of the same withheld fact collapse into one.
    """

    clusters: list[dict[str, Any]] = []
    for case in sorted(withheld, key=lambda c: c.at_ms):
        headline = case.verdict.headline_zh
        nearest = max(
            (
                _containment(headline, delivered)
                for (at, delivered, _, _) in ledger
                if abs(at - case.at_ms) <= _SEEN_WINDOW_MS
            ),
            default=0.0,
        )
        if nearest >= _DUPLICATE_CONTAINMENT:
            continue
        existing = next(
            (
                c
                for c in clusters
                if case.at_ms - c["at_ms"] <= _SEEN_WINDOW_MS
                and _containment(headline, c["headline"]) >= _DUPLICATE_CONTAINMENT
            ),
            None,
        )
        if existing is not None:
            existing["at_ms"] = case.at_ms
            continue
        clusters.append({"headline": headline, "at_ms": case.at_ms})
    return len(clusters)


# ------------------------------------------------------------------------------------------------ gate
@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    accepted: bool
    checks: Mapping[str, bool]
    evidence: Mapping[str, Any]

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, passed in self.checks.items() if not passed)


def validate_candidate(
    corpus: Corpus,
    *,
    stable: DecidePolicy,
    candidate: DecidePolicy,
    hourly_cap: int,
    trusted_root_sha: str = "",
    trusted_root_expected: str = "",
) -> ReleaseDecision:
    """Replay both policies over the same frozen corpus and decide whether the candidate may ship.

    The gate is deliberately asymmetric (chapter 9's double set): the boundary set — cases somebody marked as
    "the reader had to get this" — must strictly improve, while nothing already correct may regress. Duplicates
    and the reader's hourly budget bound the price of that improvement, so "deliver everything" cannot pass.
    """

    stable_arm = replay_corpus(corpus, stable, hourly_cap=hourly_cap)
    candidate_arm = replay_corpus(corpus, candidate, hourly_cap=hourly_cap)
    boundary = corpus.boundary()
    stable_delivered, candidate_delivered = stable_arm.delivered_set, candidate_arm.delivered_set
    boundary_stable = sum(1 for case in boundary if case.event_id in stable_delivered)
    boundary_candidate = sum(1 for case in boundary if case.event_id in candidate_delivered)
    critical_misses = [
        case.event_id
        for case in boundary
        if case.event_id in stable_delivered and case.event_id not in candidate_delivered
    ]
    retention = corpus.retention()
    retention_regressions = [
        case.event_id
        for case in retention
        if case.event_id in stable_delivered and case.event_id not in candidate_delivered
    ]
    # The mirror of a critical miss: a card marked "the reader should not have got this" that the deployed policy
    # already withholds, and the candidate would send.
    noise_regressions = [
        case.event_id
        for case in corpus.cases
        if case.expect == "may_drop" and case.event_id not in stable_delivered and case.event_id in candidate_delivered
    ]
    missed_delta = candidate_arm.missed_facts - stable_arm.missed_facts
    duplicate_delta = candidate_arm.duplicate_pairs - stable_arm.duplicate_pairs
    checks = {
        # The boundary set only ever gets better: a case somebody marked "the reader had to get this", and that
        # the deployed policy delivers, cannot be lost by a candidate. Cases *neither* arm delivers stay open and
        # are reported rather than blocked — some are unreachable by any policy (the model itself dropped them),
        # and a gate that deadlocks on those would stop every unrelated change.
        "no_critical_miss": not critical_misses,
        "no_retention_regression": not retention_regressions,
        "no_marked_noise_regression": not noise_regressions,
        # The two failure directions of a throttle change, each guarded: a candidate cannot buy quiet with misses,
        # and it cannot buy recall with unbounded repetition.
        "missed_facts_not_worse": missed_delta <= 0,
        "strong_duplicates_not_worse": (candidate_arm.strong_duplicate_pairs <= stable_arm.strong_duplicate_pairs),
        "duplicates_within_recall_trade": (
            duplicate_delta <= 0 or (missed_delta < 0 and -missed_delta >= _DUPLICATE_TRADE_RATIO * duplicate_delta)
        ),
        # Not an absolute ceiling: `decide()` does not fully control the peak (the hourly cap applies to `push`
        # and not to `escalate`), and the deployed arm already sits one card under the budget. An absolute check
        # would reject every candidate — including strict improvements — the moment a busy hour touches the cap,
        # which is the deadlock `no_critical_miss` was deliberately shaped to avoid.
        "peak_within_reader_budget": candidate_arm.per_hour_peak <= max(stable_arm.per_hour_peak, hourly_cap),
        "trusted_root_unchanged": (trusted_root_sha == trusted_root_expected) if trusted_root_expected else True,
    }
    evidence: dict[str, Any] = {
        "gate_version": GATE_VERSION,
        "policy_version": TRIAGE_POLICY_VERSION,
        "corpus": {
            "version": corpus.version,
            "sha256": corpus.sha256,
            "cases": len(corpus.cases),
            "span_hours": round(corpus.span_hours, 2),
            "prompt_versions": dict(corpus.prompt_versions),
        },
        "retention": {"cases": len(retention), "regressions": retention_regressions[:20]},
        "marked_noise": {
            "cases": sum(1 for case in corpus.cases if case.expect == "may_drop"),
            "regressions": noise_regressions[:20],
        },
        "boundary": {
            "cases": len(boundary),
            "stable_delivered": boundary_stable,
            "candidate_delivered": boundary_candidate,
            "recovered": [
                case.event_id
                for case in boundary
                if case.event_id in candidate_delivered and case.event_id not in stable_delivered
            ][:20],
            # Still failing under both policies: the standing recall debt, in the operator's face on every run.
            "open": [
                case.subject or case.event_id
                for case in boundary
                if case.event_id not in candidate_delivered and case.event_id not in stable_delivered
            ][:20],
            "critical_misses": critical_misses[:20],
        },
        "stable": {"policy": stable.as_dict(), **_arm_evidence(stable_arm)},
        "candidate": {"policy": candidate.as_dict(), **_arm_evidence(candidate_arm)},
        "delta": {
            "delivered": len(candidate_arm.delivered) - len(stable_arm.delivered),
            "missed_facts": candidate_arm.missed_facts - stable_arm.missed_facts,
            "duplicate_pairs": candidate_arm.duplicate_pairs - stable_arm.duplicate_pairs,
            "strong_duplicate_pairs": candidate_arm.strong_duplicate_pairs - stable_arm.strong_duplicate_pairs,
            "withheld_that_stable_delivered": len(stable_delivered - candidate_delivered),
            "delivered_that_stable_withheld": len(candidate_delivered - stable_delivered),
        },
        "checks": checks,
        "hourly_cap": int(hourly_cap),
        "duplicate_trade_ratio": _DUPLICATE_TRADE_RATIO,
    }
    evidence["accepted"] = all(checks.values())
    evidence["sha256"] = _sha256(evidence)
    return ReleaseDecision(accepted=bool(evidence["accepted"]), checks=checks, evidence=json_ready(evidence))


def _arm_evidence(arm: ArmReport) -> dict[str, Any]:
    return {
        "delivered": len(arm.delivered),
        "withheld": len(arm.withheld),
        "withheld_by_rule": len(arm.withheld_by_rule),
        "per_hour_mean": arm.per_hour_mean,
        "per_hour_peak": arm.per_hour_peak,
        "missed_facts": arm.missed_facts,
        "duplicate_pairs": arm.duplicate_pairs,
        "strong_duplicate_pairs": arm.strong_duplicate_pairs,
        "hourly_cap_hits": arm.hourly_cap_hits,
        "rules": dict(arm.rules),
        "throttled_by": dict(list(arm.throttled_by.items())[:12]),
    }


def candidate_policy(base: DecidePolicy, overrides: Mapping[str, Any]) -> DecidePolicy:
    """A candidate is the live policy plus named overrides — the only shape a policy change can take here."""

    known = base.as_dict()
    unknown = sorted(set(overrides) - set(known))
    if unknown:
        raise ValueError(f"news_policy_unknown_field:{unknown[0]}")
    values: dict[str, Any] = {}
    for name, raw in overrides.items():
        current = known[name]
        if isinstance(current, bool):
            values[name] = str(raw).strip().lower() in {"1", "true", "yes", "on"} if isinstance(raw, str) else bool(raw)
        elif isinstance(current, int):
            values[name] = int(raw)
        elif isinstance(current, float):
            values[name] = float(raw)
        elif isinstance(current, list):
            items = raw.split(",") if isinstance(raw, str) else raw
            values[name] = tuple(str(v).strip() for v in items if str(v).strip())
        else:
            values[name] = raw
    return DecidePolicy(**{**{k: (tuple(v) if isinstance(v, list) else v) for k, v in known.items()}, **values})


__all__ = [
    "CORPUS_VERSION",
    "EXPECTATIONS",
    "GATE_VERSION",
    "ArmReport",
    "Corpus",
    "CorpusCase",
    "ReleaseDecision",
    "candidate_policy",
    "freeze_corpus",
    "load_corpus",
    "replay_corpus",
    "validate_candidate",
]
