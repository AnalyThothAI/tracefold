"""Operational price freshness and reaction backlog reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..row_values import optional_int
from .pricing import REACTION_HISTORY_MAX_AGE_MS, REACTION_METRIC_VERSION, quote_freshness


class ReviewStorage:
    conn: Any

    def price_status(self, *, now_ms: int) -> dict[str, Any]:
        """What an operator needs before the UI shows it: source freshness and Reaction backlog."""

        snapshots = self.quote_snapshots()  # type: ignore[attr-defined]
        sources: list[dict[str, Any]] = []
        for key, row in sorted(snapshots.items()):
            received_at_ms = int(row["received_at_ms"])
            entries = [entry for entry in (row.get("quotes") or {}).values() if isinstance(entry, Mapping)]
            freshness = [
                quote_freshness(
                    measured_at_ms=now_ms,
                    received_at_ms=received_at_ms,
                    source_at_ms=optional_int(entry.get("source_at_ms")),
                )
                for entry in entries
            ]
            source_ages = [item.source_age_ms for item in freshness if item.source_age_ms is not None]
            source_times = [
                value for entry in entries if (value := optional_int(entry.get("source_at_ms"))) is not None
            ]
            sources.append(
                {
                    "source_key": key,
                    "target_count": int(row.get("target_count") or 0),
                    "quote_count": len(entries),
                    "received_age_ms": max((item.received_age_ms for item in freshness), default=None),
                    "source_age_ms": max(source_ages, default=None),
                    "effective_age_ms": max((item.effective_age_ms for item in freshness), default=None),
                    "freshness_basis": (
                        "source_and_received" if source_ages else "received_only" if freshness else None
                    ),
                    "state": (
                        "stale"
                        if any(item.state == "stale" for item in freshness)
                        else "fresh"
                        if freshness
                        else "unavailable"
                    ),
                    "source_at_ms": min(source_times, default=None),
                    "received_at_ms": received_at_ms,
                }
            )
        row = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE state = 'partial') AS partial_n,
                   count(*) FILTER (WHERE state = 'complete') AS complete_n,
                   count(*) FILTER (WHERE state = 'unavailable') AS unavailable_n
              FROM news_event_reactions
             WHERE metric_version = %s AND anchor_at_ms >= %s
            """,
            (REACTION_METRIC_VERSION, int(now_ms) - 7 * 24 * 3_600_000),
        ).fetchone()
        counts = dict(row or {})
        return {
            "metric_version": REACTION_METRIC_VERSION,
            # The backlog SLO (#88 §14) is oldest-due age, not loop frequency: a turn can run on time and
            # still fall behind. Reporting it is what makes "healthy under 5 minutes" observable at all.
            "oldest_due_age_ms": self.oldest_due_age_ms(  # type: ignore[attr-defined]
                now_ms=now_ms, history_max_age_ms=REACTION_HISTORY_MAX_AGE_MS
            ),
            "sources": sources,
            "fresh_sources": sum(1 for source in sources if source["state"] == "fresh"),
            "quotes": sum(source["quote_count"] for source in sources),
            "reaction_partial_7d": int(counts.get("partial_n") or 0),
            "reaction_complete_7d": int(counts.get("complete_n") or 0),
            "reaction_unavailable_7d": int(counts.get("unavailable_n") or 0),
        }
