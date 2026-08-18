"""Offline evaluation over stored verdicts, market marks, and labels (learning plane, no model, no broker).

`evaluate_recent` reports the outcome confusion per decision dimension; `replay_decisions` re-runs the pure
`decide()` rules over stored verdicts (their gate facts and status-bar snapshot) with a candidate policy so
threshold changes can be judged on the same boundary/retention set before `TRIAGE_POLICY_VERSION` moves.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable, Mapping
from typing import Any

from tracefold.news.models import TriageVerdict
from tracefold.news.triage_rules import DecidePolicy, GateFacts, decide, storyline_status_from_row

MOVED_THRESHOLD_PCT = 2.0
_PUSHED = frozenset({"push", "escalate"})


def _rows(repos: Any, *, since_ms: int, policy_version: str | None) -> list[dict[str, Any]]:
    rows = repos.conn.execute(
        """
        SELECT v.event_id, v.final_decision, v.override_rule, v.throttled_by, v.degraded, v.policy_version, v.trace,
               v.verdict, e.asset_class, e.grounded_assets, e.priority, e.admission, e.provider_score_max,
               e.watchlist_hits, e.storyline_key, e.opened_at_ms,
               v.verdict ->> 'direction' AS direction, (v.verdict ->> 'magnitude')::int AS magnitude,
               v.verdict ->> 'event_type' AS event_type,
               (SELECT max(abs(m.price_change_pct)) FROM news_event_market_marks m
                 WHERE m.event_id = v.event_id AND m.mark IN ('30m','4h')) AS max_abs_move,
               (SELECT m.price_change_pct FROM news_event_market_marks m
                 WHERE m.event_id = v.event_id AND m.mark = '4h'
                 ORDER BY abs(m.price_change_pct) DESC NULLS LAST LIMIT 1) AS move_4h,
               (SELECT l.label ->> 'label' FROM news_event_labels l WHERE l.event_id = v.event_id
                 ORDER BY l.created_at_ms DESC LIMIT 1) AS label
          FROM news_verdicts v JOIN news_events e ON e.event_id = v.event_id
         WHERE v.stage = 'triage' AND v.created_at_ms >= %s
           AND (%s::text IS NULL OR v.policy_version = %s)
         ORDER BY v.created_at_ms
        """,
        (since_ms, policy_version, policy_version),
    ).fetchall()
    return [dict(r) for r in rows]


def _outcome(row: Mapping[str, Any]) -> str | None:
    """market truth first (moved / flat), then a human label; None when unlabeled."""

    move = row.get("max_abs_move")
    if move is not None:
        return "moved" if float(move) >= MOVED_THRESHOLD_PCT else "flat"
    label = row.get("label")
    if label in {"good", "wrong_direction", "late"}:
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


def _rates(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    labeled = [r for r in rows if _outcome(r) is not None]
    pushed = [r for r in labeled if r["final_decision"] in _PUSHED]
    dropped = [r for r in labeled if r["final_decision"] == "drop"]
    throttled = [r for r in labeled if r["final_decision"] == "throttled"]
    moved_pushed = [r for r in pushed if _outcome(r) == "moved"]
    moved_dropped = [r for r in dropped if _outcome(r) == "moved"]
    moved_throttled = [r for r in throttled if _outcome(r) == "moved"]
    direction_hits = direction_total = 0
    for r in pushed:
        move = r.get("move_4h")
        if move is None or r.get("direction") not in {"bullish", "bearish"}:
            continue
        direction_total += 1
        if (float(move) > 0) == (r["direction"] == "bullish"):
            direction_hits += 1
    return {
        "labeled": len(labeled),
        "precision_at_push": round(len(moved_pushed) / len(pushed), 4) if pushed else None,
        "missed_movers_rate": round(len(moved_dropped) / len(dropped), 4) if dropped else None,
        "throttled_movers_rate": round(len(moved_throttled) / len(throttled), 4) if throttled else None,
        "direction_accuracy": round(direction_hits / direction_total, 4) if direction_total else None,
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
        "verdicts": len(rows),
        "decisions": dict(decisions),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "human_labels": sum(1 for r in rows if r.get("label")),
        "moved_threshold_pct": MOVED_THRESHOLD_PCT,
        **_rates(rows),
        "by_override_rule": _confusion(rows, "override_rule"),
        "by_throttled_by": _confusion(rows, "throttled_by"),
        "by_asset_class": _confusion(rows, "asset_class"),
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
    changed: collections.Counter[str] = collections.Counter()
    for r in rows:
        try:
            verdict = TriageVerdict.model_validate(dict(r.get("verdict") or {}))
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
        status = storyline_status_from_row((r.get("trace") or {}).get("status"), str(r.get("storyline_key") or ""))
        outcome = decide(verdict, facts, status, policy=policy)
        replayed.append({**r, "final_decision": outcome.final, "override_rule": outcome.override_rule})
        if outcome.final != r["final_decision"]:
            changed[f"{r['final_decision']}->{outcome.final}"] += 1
    return {
        "window_hours": int(hours),
        "policy": {
            "escalate_magnitude": policy.escalate_magnitude,
            "min_push_magnitude": policy.min_push_magnitude,
            "min_watchlist_magnitude": policy.min_watchlist_magnitude,
        },
        "verdicts": len(rows),
        "replayed": len(replayed),
        "skipped_invalid_verdicts": skipped,
        "changed": dict(changed),
        "stored": _rates(rows),
        "candidate": _rates(replayed),
        "candidate_by_override_rule": _confusion(replayed, "override_rule"),
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


__all__ = ["MOVED_THRESHOLD_PCT", "evaluate_recent", "replay_decisions"]
