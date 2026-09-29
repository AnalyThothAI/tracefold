"""One PostgreSQL-backed scoreboard query and projection shared by CLI and HTTP."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from tracefold.trading.engine.forecast import Driver, Forecast, LegProbabilities, PolicyDecision
from tracefold.trading.engine.paper import PaperLeg
from tracefold.trading.engine.scoreboard import ScoredCase, forecast_score, policy_scores

SCOREBOARD_CASES_SQL = (
    "SELECT case_id,asset_id,trigger_kind,created_at_ms,view FROM trading_cases "
    "WHERE created_at_ms >= %s AND created_at_ms < %s"
)
SCOREBOARD_TRIGGER_COUNT_SQL = "SELECT count(*) AS n FROM trading_triggers WHERE created_at_ms>=%s AND created_at_ms<%s"
SCOREBOARD_ASSESSMENTS_SQL = (
    "SELECT case_id,program_sha,route,status,forecast,drivers FROM trading_assessments "
    "WHERE case_id=ANY(%s) AND (%s::text IS NULL OR program_sha=%s)"
)
SCOREBOARD_ACTIONS_SQL = (
    "SELECT case_id,program_sha,policy_id,policy_version,action,reason,expected_r,publish_status,signal_id "
    "FROM trading_policy_actions WHERE case_id=ANY(%s) AND (%s::text IS NULL OR program_sha=%s)"
)
SCOREBOARD_LEGS_SQL = (
    "SELECT case_id,side,status,outcome,reason,anchor_at_ms,exit_at_ms,anchor_price,exit_price,gross_bps,"
    "cost_bps,net_r FROM trading_paper_legs WHERE case_id=ANY(%s) AND geometry_version='leg_geometry_v1'"
)
SCOREBOARD_EXECUTIONS_SQL = (
    "SELECT a.program_sha,a.case_id,a.action,p.net_pnl,p.reserved_notional,p.stop_bps "
    "FROM trading_policy_actions a JOIN trading_plans p ON p.signal_id=a.signal_id "
    "WHERE a.case_id=ANY(%s) AND a.signal_id IS NOT NULL AND p.pnl_status='complete'"
)
SCOREBOARD_DISPOSITIONS_SQL = (
    "SELECT count(*) FILTER (WHERE d.disposition='accepted') AS accepted, "
    "count(*) FILTER (WHERE p.opened_at_ns IS NOT NULL) AS filled "
    "FROM trading_dispositions d LEFT JOIN trading_plans p ON p.signal_id=d.input_id "
    "WHERE d.input_kind='signal' AND d.input_id=ANY(%s)"
)


def _probabilities(value: dict[str, Any]) -> LegProbabilities:
    return LegProbabilities(*(Decimal(str(value[key])) for key in ("p_tp", "p_sl", "p_timeout")))


def _forecast(value: dict[str, Any], drivers: list[dict[str, Any]]) -> Forecast:
    return Forecast(
        _probabilities(value["long"]),
        _probabilities(value["short"]),
        tuple(Driver(item["ref"], item["leans"], item["note"]) for item in drivers),
    )


def _leg(row: dict[str, Any]) -> PaperLeg:
    return PaperLeg(
        row["side"],
        row["status"],
        row["outcome"],
        row["reason"],
        row["anchor_at_ms"],
        row["exit_at_ms"],
        row["anchor_price"],
        row["exit_price"],
        row["gross_bps"],
        row["cost_bps"],
        row["net_r"],
    )


def _missing_leg(side: Literal["long", "short"]) -> PaperLeg:
    return PaperLeg(side, "missing", None, "label_pending", None, None, None, None, None, None, None)


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple | list):
        return [_wire(item) for item in value]
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    return value


class ScoreboardStorage:
    conn: Any

    def scoreboard(self, *, since_ms: int, until_ms: int, program_sha: str | None = None) -> dict[str, Any]:
        if since_ms < 0 or until_ms <= since_ms:
            raise ValueError("scoreboard_window_invalid")
        cases = {
            row["case_id"]: dict(row)
            for row in self.conn.execute(
                SCOREBOARD_CASES_SQL,
                (since_ms, until_ms),
            ).fetchall()
        }
        trigger_count = self.conn.execute(
            SCOREBOARD_TRIGGER_COUNT_SQL,
            (since_ms, until_ms),
        ).fetchone()["n"]
        if not cases:
            return {
                "window": {"since_ms": since_ms, "until_ms": until_ms},
                "funnel": {
                    "triggers": int(trigger_count),
                    "selected": 0,
                    "assessed": 0,
                    "published": 0,
                    "execution_accepted": 0,
                    "filled": 0,
                },
                "programs": [],
            }
        ids = list(cases)
        assessments = [
            dict(row)
            for row in self.conn.execute(
                SCOREBOARD_ASSESSMENTS_SQL,
                (ids, program_sha, program_sha),
            ).fetchall()
        ]
        actions = [
            dict(row)
            for row in self.conn.execute(
                SCOREBOARD_ACTIONS_SQL,
                (ids, program_sha, program_sha),
            ).fetchall()
        ]
        legs = [
            dict(row)
            for row in self.conn.execute(
                SCOREBOARD_LEGS_SQL,
                (ids,),
            ).fetchall()
        ]
        execution_rows = [
            dict(row)
            for row in self.conn.execute(
                SCOREBOARD_EXECUTIONS_SQL,
                (ids,),
            ).fetchall()
        ]
        assessments_by = {(row["case_id"], row["program_sha"]): row for row in assessments}
        actions_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in actions:
            actions_by.setdefault((row["case_id"], row["program_sha"]), []).append(row)
        legs_by: dict[str, dict[str, dict[str, Any]]] = {}
        for row in legs:
            legs_by.setdefault(row["case_id"], {})[row["side"]] = row
        programs = []
        identities = sorted({(row["program_sha"], row["route"]) for row in assessments})
        for identity, route in identities:
            scored: list[ScoredCase] = []
            failure_counts: dict[str, int] = {}
            for case_id, case in cases.items():
                candidate = assessments_by.get((case_id, identity))
                assessment = candidate if candidate is not None and candidate["route"] == route else None
                if assessment is not None and assessment["status"] != "ok":
                    failure_counts[assessment["status"]] = failure_counts.get(assessment["status"], 0) + 1
                action_rows = actions_by.get((case_id, identity), []) if assessment is not None else []
                paper = legs_by.get(case_id, {})
                view = case["view"] or {}
                rates = view.get("base_rates") or ()
                rates_by = {item["side"]: item for item in rates}

                def rate(side: str, rates_by: dict[str, Any] = rates_by) -> LegProbabilities | None:
                    record = rates_by.get(side)
                    return (
                        None
                        if record is None or record["probabilities"] is None
                        else _probabilities(record["probabilities"])
                    )

                scored.append(
                    ScoredCase(
                        case_id=case_id,
                        asset_id=case["asset_id"],
                        day=datetime.fromtimestamp(int(case["created_at_ms"]) / 1_000, tz=UTC).date().isoformat(),
                        trigger_kind=case["trigger_kind"],
                        decisions=tuple(
                            PolicyDecision(
                                item["policy_id"],
                                item["policy_version"],
                                item["action"],
                                item["reason"],
                                item["expected_r"],
                            )
                            for item in action_rows
                        ),
                        legs=(
                            _leg(paper["long"]) if "long" in paper else _missing_leg("long"),
                            _leg(paper["short"]) if "short" in paper else _missing_leg("short"),
                        ),
                        forecast=None
                        if assessment is None or assessment["forecast"] is None
                        else _forecast(assessment["forecast"], assessment["drivers"]),
                        baseline_long=rate("long"),
                        baseline_short=rate("short"),
                    )
                )
            deviations = []
            for execution in execution_rows:
                candidate = assessments_by.get((execution["case_id"], identity))
                if execution["program_sha"] != identity or candidate is None or candidate["route"] != route:
                    continue
                paper_row = legs_by.get(execution["case_id"], {}).get(execution["action"])
                risk = execution["reserved_notional"] * execution["stop_bps"] / 10_000
                if paper_row is not None and paper_row["net_r"] is not None and risk > 0:
                    deviations.append(execution["net_pnl"] / risk - paper_row["net_r"])
            programs.append(
                {
                    "program_sha": identity,
                    "route": route,
                    "assessments": sum(row["program_sha"] == identity and row["route"] == route for row in assessments),
                    "failures": failure_counts,
                    "policies": _wire([asdict(item) for item in policy_scores(tuple(scored))]),
                    "forecast": _wire(asdict(forecast_score(tuple(scored)))),
                    "execution_deviation": {
                        "scored": len(deviations),
                        "average_r_delta": str(sum(deviations, Decimal(0)) / len(deviations))
                        if len(deviations) >= 10
                        else None,
                        "status": "ok" if len(deviations) >= 10 else "insufficient_data",
                    },
                }
            )
        published = [row["signal_id"] for row in actions if row["publish_status"] == "published"]
        executions = self.conn.execute(
            SCOREBOARD_DISPOSITIONS_SQL,
            (published,),
        ).fetchone()
        return {
            "window": {"since_ms": since_ms, "until_ms": until_ms},
            "funnel": {
                "triggers": int(trigger_count),
                "selected": len(cases),
                "assessed": len({row["case_id"] for row in assessments if row["status"] == "ok"}),
                "published": len(published),
                "execution_accepted": int(executions["accepted"]),
                "filled": int(executions["filled"]),
            },
            "programs": programs,
        }
