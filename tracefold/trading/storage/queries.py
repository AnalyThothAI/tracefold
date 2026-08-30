"""Read projections for the three product surfaces, one durable aggregate each.

The split is the product's (#331):

    Source / Admission   `gate.py` — every OI fact and the admission answer it received
    Case / Decision      `console_cases` and `case_counts` — frozen manifests and frozen policy checks
    Intent / Outcome     `console_intents` and `intent_counts` — execution lifecycle and exposure
    runtime readiness    `runtime_summary` — control, engine, capability, and bounded durable totals

Nothing here re-interprets a Case against today's configuration, and nothing mixes the aggregates:
`console_intents` used to return `cases_without_intents` beside its Intents, which put two different
durable objects behind one contract and made the console's "no data" state ambiguous.

Every count is a bounded aggregation over durable rows. The polling-driven funnel it replaced counted
one entry per *re-read* of the same frame, so its magnitudes were a function of the poll interval and
its document reset at UTC midnight — which left a question about yesterday with no evidence at all.
"""

# S608 exemptions below compose fixed SELECT/filter fragments selected by typed options; all values stay bound.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from ..intent import ACTIVE_INTENT_STATES
from .query_sql import (
    AUTHORITY_PROJECTION_SQL,
    console_capital_evidence_sql,
    console_cases_sql,
    console_intents_sql,
)

_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("source_observed_to_verdict_persisted", "c.trigger_persisted_at_ms - c.source_observed_at_ms"),
    ("verdict_persisted_to_case_created", "c.created_at_ms - c.trigger_persisted_at_ms"),
    ("case_created_to_case_decided", "c.decided_at_ms - c.created_at_ms"),
    ("case_created_to_intent_emitted", "i.created_at_ms - c.created_at_ms"),
    ("intent_emitted_to_adopted", "i.adopted_at_ms - i.created_at_ms"),
    ("intent_emitted_to_entry_fenced", "i.entry_fenced_at_ms - i.created_at_ms"),
    (
        "entry_fence_requested_to_entry_fenced",
        "i.entry_fenced_at_ms - i.entry_fence_requested_at_ms",
    ),
    ("entry_fenced_to_entry_submitted", "i.entry_submitted_at_ms - i.entry_fenced_at_ms"),
    ("entry_submitted_to_entry_accepted", "i.entry_accepted_at_ms - i.entry_submitted_at_ms"),
    ("entry_submitted_to_position_opened", "i.opened_at_ms - i.entry_submitted_at_ms"),
    ("entry_fenced_to_position_opened", "i.opened_at_ms - i.entry_fenced_at_ms"),
    ("position_opened_to_closed_flat", "i.flat_verified_at_ms - i.opened_at_ms"),
)


class QueryStorage:
    conn: Any

    # ------------------------------------------------------------------ runtime / execution readiness
    def runtime_summary(self, *, since_ms: int, now_ms: int) -> dict[str, Any]:
        """The bounded durable totals the readiness surface may show, and nothing about a policy.

        Deliberately small. The status route used to publish every operator floor and every strategy
        config beside these, which invited a console to compare a Case frozen last week against a
        threshold edited yesterday — and print a conflict on a row that passed.
        """

        day_start_ms = int(now_ms) // 86_400_000 * 86_400_000
        active = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE execution_state = ANY(%s)",
            (list(ACTIVE_INTENT_STATES),),
        ).fetchone()
        entries_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE entry_fenced_at_ms >= %s AND entry_fenced_at_ms < %s",
            (day_start_ms, day_start_ms + 86_400_000),
        ).fetchone()
        closed_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents "
            "WHERE terminal_outcome = 'CLOSED_FLAT' AND closed_at_ms >= %s AND closed_at_ms < %s",
            (day_start_ms, day_start_ms + 86_400_000),
        ).fetchone()
        return {
            **self.latest_lifecycle_milestones(),
            "day_key": datetime.fromtimestamp(day_start_ms / 1000, tz=UTC).strftime("%Y-%m-%d"),
            "active_intents": _count(active),
            "entries_today": _count(entries_today),
            "closed_intents_today": _count(closed_today),
            "cases_24h": sum(self.case_counts(since_ms=since_ms).values()),
            "intents_24h": sum(self.intent_counts(since_ms=since_ms)["by_state"].values()),
        }

    def latest_lifecycle_milestones(self) -> dict[str, int | None]:
        cases = self.conn.execute("SELECT max(created_at_ms) AS created FROM trading_cases").fetchone()
        intents = self.conn.execute(
            "SELECT max(created_at_ms) AS emitted, max(entry_fenced_at_ms) AS fenced, "
            "max(opened_at_ms) AS opened, max(closed_at_ms) AS closed FROM trading_intents"
        ).fetchone()
        return {
            "latest_case_created_at_ms": _at(cases, "created"),
            "latest_intent_emitted_at_ms": _at(intents, "emitted"),
            "latest_entry_fenced_at_ms": _at(intents, "fenced"),
            "latest_position_opened_at_ms": _at(intents, "opened"),
            "latest_position_closed_at_ms": _at(intents, "closed"),
        }

    def stage_latency_ms(self, *, since_ms: int) -> dict[str, dict[str, int]]:
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
              LEFT JOIN trading_intents i ON i.case_id = c.case_id
             WHERE c.created_at_ms >= %s
            """,  # noqa: S608
            (int(since_ms),),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return one row
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

    # ------------------------------------------------------------------ Case / Decision
    def case_counts(self, *, since_ms: int) -> dict[str, int]:
        """Raw `state` distribution over the window. Historical states are reported as they are stored."""

        rows = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def case_reason_counts(self, *, since_ms: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT coalesce(policy_reason, 'undecided') AS reason, count(*) AS n "
            "FROM trading_cases WHERE created_at_ms >= %s GROUP BY 1",
            (int(since_ms),),
        ).fetchall()
        return {str(row["reason"]): int(row["n"]) for row in rows}

    def case_capital_reason_counts(self, *, since_ms: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT capital_reason AS reason, count(*) AS n FROM trading_cases "
            "WHERE created_at_ms >= %s AND capital_reason IS NOT NULL GROUP BY capital_reason",
            (int(since_ms),),
        ).fetchall()
        return {str(row["reason"]): int(row["n"]) for row in rows}

    def console_cases(
        self,
        *,
        since_ms: int,
        underlying_key: str | None = None,
        states: tuple[str, ...] = (),
        before: tuple[int, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Frozen Cases with their frozen policy evidence, newest first.

        The Intent link is a single id, never a joined lifecycle: an execution state belongs to the
        Intent aggregate, and duplicating it here is how two surfaces came to render the same row with
        two different vocabularies.
        """

        params: list[Any] = [int(since_ms)]
        if underlying_key:
            params.append(underlying_key)
        if states:
            params.append(list(states))
        if before is not None:
            params.extend((int(before[0]), str(before[1])))
        params.append(int(limit))
        rows = self.conn.execute(
            console_cases_sql(
                underlying=underlying_key is not None,
                states=bool(states),
                before=before is not None,
            ),
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ Intent / Outcome
    def intent_counts(self, *, since_ms: int) -> dict[str, dict[str, int]]:
        states = self.conn.execute(
            "SELECT execution_state, count(*) AS n FROM trading_intents "
            "WHERE created_at_ms >= %s GROUP BY execution_state",
            (int(since_ms),),
        ).fetchall()
        outcomes = self.conn.execute(
            "SELECT terminal_outcome, count(*) AS n FROM trading_intents "
            "WHERE terminal_outcome IS NOT NULL AND updated_at_ms >= %s GROUP BY terminal_outcome",
            (int(since_ms),),
        ).fetchall()
        reasons = self.conn.execute(
            "SELECT reason_code, count(*) AS n FROM trading_intents "
            "WHERE reason_code IS NOT NULL AND updated_at_ms >= %s GROUP BY reason_code",
            (int(since_ms),),
        ).fetchall()
        return {
            "by_state": {str(row["execution_state"]): int(row["n"]) for row in states},
            "by_outcome": {str(row["terminal_outcome"]): int(row["n"]) for row in outcomes},
            "by_reason": {str(row["reason_code"]): int(row["n"]) for row in reasons},
        }

    def console_intents(
        self,
        *,
        since_ms: int,
        closed_from_ms: int | None = None,
        closed_until_ms: int | None = None,
        underlying_key: str | None = None,
        states: tuple[str, ...] = (),
        before: tuple[int, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if closed_from_ms is not None and closed_until_ms is not None:
            params: list[Any] = [list(ACTIVE_INTENT_STATES), int(closed_from_ms), int(closed_until_ms)]
        else:
            params = [list(ACTIVE_INTENT_STATES), int(since_ms)]
        if underlying_key:
            params.append(underlying_key)
        if states:
            params.append(list(states))
        if before is not None:
            params.extend((int(before[0]), str(before[1])))
        params.append(int(limit))
        rows = self.conn.execute(
            console_intents_sql(
                closed_window=closed_from_ms is not None and closed_until_ms is not None,
                underlying=underlying_key is not None,
                states=bool(states),
                before=before is not None,
            ),
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ Capital evidence
    def authority_projection(self) -> list[dict[str, Any]]:
        """Current redacted authority chain per closed binding; payloads contain no credentials."""

        rows = self.conn.execute(AUTHORITY_PROJECTION_SQL).fetchall()
        return [dict(row) for row in rows]

    def console_capital_evidence(
        self,
        *,
        binding: str | None = None,
        statuses: tuple[str, ...] = (),
        before: tuple[int, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Bounded reservation/authorization/outcome proof, newest state update first."""

        params: list[Any] = []
        if binding:
            params.append(binding)
        if statuses:
            params.append(list(statuses))
        if before is not None:
            params.extend((int(before[0]), str(before[1])))
        params.append(int(limit))
        rows = self.conn.execute(
            console_capital_evidence_sql(
                binding=binding is not None,
                statuses=bool(statuses),
                before=before is not None,
            ),
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def _count(row: Any) -> int:
    return 0 if row is None else int(row["n"])


def _at(row: Any, key: str) -> int | None:
    value = None if row is None else row[key]
    return None if value is None else int(value)


__all__ = ["QueryStorage"]
