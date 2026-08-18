"""Offline evaluation over stored verdicts + market marks (labels are optional overrides)."""

from __future__ import annotations

import collections
from typing import Any

MOVED_THRESHOLD_PCT = 2.0


def evaluate_recent(repos: Any, *, now_ms: int, hours: int, policy_version: str | None) -> dict[str, Any]:
    since_ms = int(now_ms) - int(hours) * 3600_000
    rows = repos.conn.execute(
        """
        SELECT v.event_id, v.final_decision, v.degraded, v.policy_version,
               v.verdict ->> 'direction' AS direction, (v.verdict ->> 'magnitude')::int AS magnitude,
               (SELECT max(abs(m.price_change_pct)) FROM news_event_market_marks m
                 WHERE m.event_id = v.event_id AND m.mark IN ('30m','4h')) AS max_abs_move,
               (SELECT m.price_change_pct FROM news_event_market_marks m
                 WHERE m.event_id = v.event_id AND m.mark = '4h'
                 ORDER BY abs(m.price_change_pct) DESC NULLS LAST LIMIT 1) AS move_4h,
               (SELECT l.label FROM news_event_labels l WHERE l.event_id = v.event_id
                 ORDER BY l.created_at_ms DESC LIMIT 1) AS label
          FROM news_verdicts v
         WHERE v.stage = 'triage' AND v.created_at_ms >= %s
           AND (%s::text IS NULL OR v.policy_version = %s)
        """,
        (since_ms, policy_version, policy_version),
    ).fetchall()
    decisions = collections.Counter(str(r["final_decision"]) for r in rows)
    labeled = [r for r in rows if r["max_abs_move"] is not None]
    pushed = [r for r in labeled if r["final_decision"] in {"push", "escalate"}]
    dropped = [r for r in labeled if r["final_decision"] in {"drop", "throttled"}]
    moved_pushed = [r for r in pushed if float(r["max_abs_move"]) >= MOVED_THRESHOLD_PCT]
    moved_dropped = [r for r in dropped if float(r["max_abs_move"]) >= MOVED_THRESHOLD_PCT]
    direction_hits = 0
    direction_total = 0
    for r in pushed:
        move = r["move_4h"]
        if move is None or r["direction"] not in {"bullish", "bearish"}:
            continue
        direction_total += 1
        if (float(move) > 0) == (r["direction"] == "bullish"):
            direction_hits += 1
    return {
        "window_hours": int(hours),
        "policy_version": policy_version,
        "verdicts": len(rows),
        "decisions": dict(decisions),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "labeled_by_market": len(labeled),
        "precision_at_push": round(len(moved_pushed) / len(pushed), 4) if pushed else None,
        "missed_movers_rate": round(len(moved_dropped) / len(dropped), 4) if dropped else None,
        "direction_accuracy": round(direction_hits / direction_total, 4) if direction_total else None,
        "human_labels": sum(1 for r in rows if r["label"]),
        "moved_threshold_pct": MOVED_THRESHOLD_PCT,
    }


__all__ = ["MOVED_THRESHOLD_PCT", "evaluate_recent"]
