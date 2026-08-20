"""Offline evaluation over stored verdicts and operator labels (learning plane, no model, no broker).

`evaluate_recent` reports the outcome confusion per decision dimension; `replay_decisions` re-runs the pure
`decide()` rules over stored verdicts (their gate facts and status-bar snapshot) with a candidate policy so
threshold changes can be judged on the same boundary/retention set before `TRIAGE_POLICY_VERSION` moves.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tracefold.news.models import TriageVerdict
from tracefold.news.triage_rules import DecidePolicy, GateFacts, decide, storyline_status_from_row

_PUSHED = frozenset({"push", "escalate"})


def _rows(repos: Any, *, since_ms: int, policy_version: str | None) -> list[dict[str, Any]]:
    """Every Event of the window, not only the ones that reached Triage: a Gate-suppressed Event has no verdict and
    is reported as ``final_decision='suppressed'`` so a ``missed`` label on it counts as a miss."""

    rows = repos.conn.execute(
        """
        SELECT e.event_id,
               COALESCE(v.final_decision, 'suppressed') AS final_decision,
               COALESCE(v.override_rule, e.admission) AS override_rule,
               v.throttled_by, COALESCE(v.degraded, false) AS degraded, v.policy_version, v.trace, v.verdict,
               e.asset_class, e.grounded_assets, e.priority, e.admission, e.provider_score_max,
               e.watchlist_hits, e.storyline_key, e.opened_at_ms,
               v.verdict ->> 'direction' AS direction, (v.verdict ->> 'magnitude')::int AS magnitude,
               v.verdict ->> 'event_type' AS event_type, v.verdict ->> 'audience' AS audience,
               (SELECT l.label ->> 'label' FROM news_event_labels l WHERE l.event_id = e.event_id
                 ORDER BY l.created_at_ms DESC LIMIT 1) AS label
          FROM news_events e
          LEFT JOIN LATERAL (
            SELECT * FROM news_verdicts x
             WHERE x.event_id = e.event_id AND x.stage = 'triage'
               AND (%s::text IS NULL OR x.policy_version = %s)
             ORDER BY x.created_at_ms DESC LIMIT 1
          ) v ON true
         WHERE e.opened_at_ms >= %s
         ORDER BY e.opened_at_ms
        """,
        (policy_version, policy_version, since_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def _outcome(row: Mapping[str, Any]) -> str | None:
    """Operator label as truth: good/wrong_direction/late/missed/must_push = the event mattered ("moved"),
    noise/dup = it did not. ``missed`` and ``must_push`` are both the operator saying "this should have been
    pushed" about something the pipeline held — ``must_push`` additionally enters the release gate's boundary
    set, but it has to count here too or marking one would move no metric at all."""

    label = row.get("label")
    if label in {"good", "wrong_direction", "late", "missed", "must_push"}:
        return "moved"
    if label in {"noise", "dup"}:
        return "flat"
    return None


def _confusion(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, dict[str, int]]:
    table: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in rows:
        outcome = _outcome(row)
        if outcome is None:
            continue
        pushed = row["final_decision"] in _PUSHED
        table[str(row.get(key) or "-")][f"{'pushed' if pushed else 'held'}_{outcome}"] += 1
    return {k: dict(v) for k, v in sorted(table.items())}


def _rates(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    labeled = [r for r in rows if _outcome(r) is not None]
    pushed = [r for r in labeled if r["final_decision"] in _PUSHED]
    held = [r for r in labeled if r["final_decision"] not in _PUSHED]
    dropped = [r for r in labeled if r["final_decision"] == "drop"]
    suppressed = [r for r in labeled if r["final_decision"] == "suppressed"]
    throttled = [r for r in labeled if r["final_decision"] == "throttled"]
    movers = [r for r in labeled if _outcome(r) == "moved"]
    moved_pushed = [r for r in pushed if _outcome(r) == "moved"]
    moved_dropped = [r for r in dropped if _outcome(r) == "moved"]
    moved_suppressed = [r for r in suppressed if _outcome(r) == "moved"]
    moved_throttled = [r for r in throttled if _outcome(r) == "moved"]
    flat_pushed = [r for r in pushed if _outcome(r) == "flat"]
    return {
        "labeled": len(labeled),
        "precision_at_push": round(len(moved_pushed) / len(pushed), 4) if pushed else None,
        # guardrail metrics: of everything the operator says mattered, how much did the pipeline hold?
        "missed_rate": round(len([r for r in movers if r in held]) / len(movers), 4) if movers else None,
        "false_push_rate": round(len(flat_pushed) / len(pushed), 4) if pushed else None,
        "missed_movers_rate": round(len(moved_dropped) / len(dropped), 4) if dropped else None,
        "suppressed_movers_rate": round(len(moved_suppressed) / len(suppressed), 4) if suppressed else None,
        "throttled_movers_rate": round(len(moved_throttled) / len(throttled), 4) if throttled else None,
    }


def evaluate_recent(repos: Any, *, now_ms: int, hours: int, policy_version: str | None) -> dict[str, Any]:
    since_ms = int(now_ms) - int(hours) * 3600_000
    rows = _rows(repos, since_ms=since_ms, policy_version=policy_version)
    decisions = collections.Counter(str(r["final_decision"]) for r in rows)
    storyline_first_push_min: list[int] = []
    pushes_per_storyline: collections.Counter[str] = collections.Counter()
    first_seen: dict[str, int] = {}
    for r in rows:
        key = str(r.get("storyline_key") or "")
        first_seen.setdefault(key, int(r["opened_at_ms"]))
        if r["final_decision"] in _PUSHED:
            pushes_per_storyline[key] += 1
            storyline_first_push_min.append(max(0, int(r["opened_at_ms"]) - first_seen[key]) // 60_000)
    return {
        "window_hours": int(hours),
        "policy_version": policy_version,
        "events": len(rows),
        "verdicts": sum(1 for r in rows if r.get("verdict")),
        "decisions": dict(decisions),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "human_labels": sum(1 for r in rows if r.get("label")),
        **_rates(rows),
        "by_admission": _confusion(rows, "admission"),
        "by_override_rule": _confusion(rows, "override_rule"),
        "by_throttled_by": _confusion(rows, "throttled_by"),
        "by_asset_class": _confusion(rows, "asset_class"),
        "by_audience": _confusion(rows, "audience"),
        "by_event_type": _confusion(rows, "event_type"),
        "storylines": {
            "count": len(first_seen),
            "pushes_per_storyline_max": max(pushes_per_storyline.values(), default=0),
            "first_push_delay_min_p50": _median(storyline_first_push_min),
        },
    }


def replay_decisions(
    repos: Any,
    *,
    now_ms: int,
    hours: int,
    watchlist_symbols: frozenset[str],
    policy: DecidePolicy,
) -> dict[str, Any]:
    """Re-run decide() over stored triage verdicts with a candidate policy; compare against stored decisions."""

    since_ms = int(now_ms) - int(hours) * 3600_000
    rows = _rows(repos, since_ms=since_ms, policy_version=None)
    replayed: list[dict[str, Any]] = []
    skipped = 0
    restatement_drops = 0
    distinct_bypass = 0
    changed: collections.Counter[str] = collections.Counter()
    for r in rows:
        if not r.get("verdict"):
            continue  # never reached Triage: nothing to re-decide
        try:
            # Verdicts stored before prompt v7 have no novelty field: replay them as new_fact (never a restatement).
            verdict = TriageVerdict.model_validate({"novelty": "new_fact", **dict(r.get("verdict") or {})})
        except ValueError:
            skipped += 1
            continue
        facts = GateFacts(
            grounded_assets=tuple(r.get("grounded_assets") or []),
            watchlist_symbols=watchlist_symbols,
            provider_score=r.get("provider_score_max"),
            priority=str(r.get("priority") or "normal"),
            admission=str(r.get("admission") or ""),
        )
        trace = dict(r.get("trace") or {})
        status = storyline_status_from_row(
            trace.get("status_final") or trace.get("status"),
            str(trace.get("storyline_key") or r.get("storyline_key") or ""),
            told=[t for t in (trace.get("told") or []) if isinstance(t, Mapping)],
        )
        outcome = decide(verdict, facts, status, policy=policy)
        replayed.append({**r, "final_decision": outcome.final, "override_rule": outcome.override_rule})
        if outcome.final != r["final_decision"]:
            changed[f"{r['final_decision']}->{outcome.final}"] += 1
        if outcome.override_rule == "restatement":
            restatement_drops += 1
        elif outcome.override_rule == "distinct_bypass":
            distinct_bypass += 1
    return {
        "window_hours": int(hours),
        "policy": policy.as_dict(),
        "events": len(rows),
        "verdicts": sum(1 for r in rows if r.get("verdict")),
        "replayed": len(replayed),
        "skipped_invalid_verdicts": skipped,
        "changed": dict(changed),
        "restatement_drops": restatement_drops,
        "distinct_bypass": distinct_bypass,
        "stored": _rates(rows),
        "candidate": _rates(replayed),
        "candidate_by_override_rule": _confusion(replayed, "override_rule"),
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


__all__ = ["evaluate_recent", "replay_decisions"]
