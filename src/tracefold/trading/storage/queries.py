"""Trading order observations and aggregate status reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .sql_values import _dumps

# The pipeline stages #211 asks to be able to report, each as the difference between two durable
# timestamps. Named here once so the SELECT list and the returned document cannot drift apart, and
# deliberately keyed by stage rather than by symbol, event or order: this is one bounded document.
#
# Both middle stages are measured from `case_created` rather than chained, because the runner writes
# the order *before* it settles the case — `_place` commits its own transaction and `_settle` is what
# terminalises the case afterwards. `case_decided -> order_prepared` would therefore always be
# negative or zero however the clock is sampled. `order_prepared` is nested inside `case_decided`, and
# the difference between the two is the settle write; the model call is inside both.
_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("source_observed_to_verdict_persisted", "c.trigger_persisted_at_ms - c.source_observed_at_ms"),
    ("verdict_persisted_to_case_created", "c.created_at_ms - c.trigger_persisted_at_ms"),
    ("case_created_to_order_prepared", "o.created_at_ms - c.created_at_ms"),
    ("case_created_to_case_decided", "c.decided_at_ms - c.created_at_ms"),
    ("order_prepared_to_position_opened", "o.position_opened_at_ms - o.created_at_ms"),
)


class QueryStorage:
    conn: Any

    # ------------------------------------------------------------------ observations
    def record_observation(
        self,
        *,
        order_id: str,
        observation_kind: str,
        content_sha256: str,
        content: Mapping[str, Any],
        now_ms: int,
    ) -> None:
        """An unchanged remote answer bumps a counter; it does not append another row forever."""

        self.conn.execute(
            """
            INSERT INTO trading_order_observations (
              order_id, observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, 1)
            ON CONFLICT (order_id, observation_kind, content_sha256) DO UPDATE
               SET last_seen_at_ms = EXCLUDED.last_seen_at_ms,
                   seen_count = trading_order_observations.seen_count + 1
            """,
            (order_id, observation_kind, content_sha256, _dumps(dict(content)), int(now_ms), int(now_ms)),
        )

    def observations(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count "
            "FROM trading_order_observations WHERE order_id = %s ORDER BY last_seen_at_ms DESC LIMIT %s",
            (order_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ status
    def status_counts(self, *, since_ms: int) -> dict[str, Any]:
        cases = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        orders = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_orders WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        kinds = self.conn.execute(
            "SELECT case_kind, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY case_kind",
            (int(since_ms),),
        ).fetchall()
        # Only *measured* exits enter the realised-PnL denominator. An operator-resolved order closed
        # a position — so it cools the symbol down — but nobody computed a return for it, and counting
        # it turned one +150 bps winner beside three resolutions into a reported mean of 37.5.
        realized = self.conn.execute(
            "SELECT count(*) AS n, coalesce(sum(realized_bps), 0) AS total_bps "
            "FROM trading_orders WHERE realized_bps IS NOT NULL "
            "AND position_closed_at_ms IS NOT NULL AND position_closed_at_ms >= %s",
            (int(since_ms),),
        ).fetchone()
        return {
            "cases_by_state": {str(row["state"]): int(row["n"]) for row in cases},
            "cases_by_kind": {str(row["case_kind"]): int(row["n"]) for row in kinds},
            "orders_by_state": {str(row["state"]): int(row["n"]) for row in orders},
            "closed_orders": 0 if realized is None else int(realized["n"]),
            "closed_realized_bps": 0 if realized is None else int(realized["total_bps"]),
        }

    def stage_latency_ms(self, *, since_ms: int) -> dict[str, dict[str, int]]:
        """Median and p95 for each pipeline stage over the window, read from the ledger itself.

        A latency report computed from the same rows the audit reads is exactly as replayable as the
        audit — there is no second store to disagree with, and no per-symbol, per-event or per-order
        key anywhere in it. Cases written before the upstream stamps existed simply do not count
        towards the two upstream stages: an ordered-set aggregate skips NULL inputs, so `n` says how
        much evidence each number rests on rather than the number quietly averaging over absence.
        """

        selects = ",\n                   ".join(
            f"count({expression}) AS {name}_n,\n"
            f"                   percentile_disc(0.5) WITHIN GROUP (ORDER BY {expression}) AS {name}_p50,\n"
            f"                   percentile_disc(0.95) WITHIN GROUP (ORDER BY {expression}) AS {name}_p95"
            for name, expression in _STAGES
        )
        row = self.conn.execute(
            f"""
            SELECT {selects}
              FROM trading_cases c
              LEFT JOIN trading_orders o ON o.case_id = c.case_id
             WHERE c.created_at_ms >= %s
            """,
            (int(since_ms),),
        ).fetchone()
        if row is None:  # pragma: no cover - an aggregate query always returns one row
            return {name: {"n": 0} for name, _ in _STAGES}
        report: dict[str, dict[str, int]] = {}
        for name, _ in _STAGES:
            stage: dict[str, int] = {"n": int(row[f"{name}_n"] or 0)}
            for quantile in ("p50", "p95"):
                value = row[f"{name}_{quantile}"]
                if value is not None:
                    stage[quantile] = int(value)
            report[name] = stage
        return report


__all__ = ["QueryStorage"]
