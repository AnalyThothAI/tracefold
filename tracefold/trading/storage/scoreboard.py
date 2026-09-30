"""One PostgreSQL-backed scoreboard query and projection shared by CLI and HTTP."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from tracefold.trading.engine.forecast import (
    Driver,
    Forecast,
    LegProbabilities,
    PolicyDecision,
    calibrated_probabilities,
)
from tracefold.trading.engine.paper import PaperLeg, paper_leg_from_record
from tracefold.trading.engine.scoreboard import ScoredCase, cohort_scores, forecast_score, paired_score, policy_scores

SCOREBOARD_CASES_SQL = (
    "SELECT c.case_id,c.asset_id,c.trigger_kind,c.created_at_ms,c.view,c.geometry_version,c.episode_id,c.episode_role,"
    "t.payload FROM trading_cases c JOIN trading_triggers t USING(trigger_id) "
    "WHERE c.created_at_ms >= %s AND c.created_at_ms < %s"
)
SCOREBOARD_TRIGGER_COUNT_SQL = "SELECT count(*) AS n FROM trading_triggers WHERE created_at_ms>=%s AND created_at_ms<%s"
SCOREBOARD_ASSESSMENTS_SQL = (
    "SELECT a.assessment_id,a.case_id,a.run_id,a.evaluator_id,a.program_sha,a.route,a.status,a.forecast,"
    "a.drivers,a.notes,a.input_tokens,a.output_tokens,a.started_at_ms,a.ended_at_ms,a.error_metadata,"
    "a.reused_assessment_id,r.kind,r.evaluator_spec,r.manifest,r.created_at_ms AS run_created_at_ms "
    "FROM trading_assessments a JOIN trading_evaluation_runs r USING(run_id) "
    "WHERE case_id=ANY(%s) AND (%s::text IS NULL OR program_sha=%s)"
)
SCOREBOARD_ACTIONS_SQL = (
    "SELECT assessment_id,policy_config,case_id,program_sha,policy_id,policy_version,action,reason,"
    "expected_r,publish_status,signal_id "
    "FROM trading_policy_actions WHERE case_id=ANY(%s) AND (%s::text IS NULL OR program_sha=%s)"
)
SCOREBOARD_LEGS_SQL = (
    "SELECT case_id,side,status,outcome,reason,anchor_at_ms,exit_at_ms,anchor_price,exit_price,gross_bps,"
    "cost_bps,net_r FROM trading_paper_legs l JOIN trading_cases c USING(case_id) "
    "WHERE case_id=ANY(%s) AND l.geometry_version=c.geometry_version"
)
SCOREBOARD_EXECUTIONS_SQL = (
    "SELECT a.assessment_id,a.program_sha,a.case_id,a.action,p.net_pnl,e.entry_notional,p.stop_bps "
    "FROM trading_policy_actions a JOIN trading_plans p ON p.signal_id=a.signal_id "
    "JOIN LATERAL (SELECT sum(f.quantity*f.price) AS entry_notional FROM trading_orders o "
    "JOIN trading_fill_attributions fa ON fa.client_order_id=o.client_order_id "
    "JOIN trading_fills f ON (f.environment,f.native_symbol,f.trade_id)="
    "(fa.environment,fa.native_symbol,fa.trade_id) WHERE o.plan_id=p.plan_id AND o.leg='entry') e ON true "
    "WHERE a.case_id=ANY(%s) AND a.signal_id IS NOT NULL AND p.pnl_status='complete' AND p.opened_at_ns IS NOT NULL"
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
                "comparisons": [],
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
        assessments_by = {(row["case_id"], row["run_id"]): row for row in assessments}
        actions_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in actions:
            actions_by.setdefault((row["case_id"], row["assessment_id"]), []).append(row)
        legs_by: dict[str, dict[str, dict[str, Any]]] = {}
        for row in legs:
            legs_by.setdefault(row["case_id"], {})[row["side"]] = row
        programs = []
        scored_runs: dict[str, tuple[ScoredCase, ...]] = {}
        comparisons: list[dict[str, Any]] = []
        identities = sorted({row["run_id"] for row in assessments})
        for identity in identities:
            run = next(row for row in assessments if row["run_id"] == identity)
            route = run["route"]
            scored: list[ScoredCase] = []
            failure_counts: dict[str, int] = {}
            manifest_ids = set(run["manifest"].get("case_ids", cases))
            for case_id, case in cases.items():
                if case_id not in manifest_ids:
                    continue
                candidate = assessments_by.get((case_id, identity))
                assessment = candidate
                if assessment is not None and assessment["status"] != "ok":
                    failure_counts[assessment["status"]] = failure_counts.get(assessment["status"], 0) + 1
                action_rows = (
                    actions_by.get((case_id, assessment["assessment_id"]), []) if assessment is not None else []
                )
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
                            paper_leg_from_record(paper["long"]) if "long" in paper else _missing_leg("long"),
                            paper_leg_from_record(paper["short"]) if "short" in paper else _missing_leg("short"),
                        ),
                        forecast=None
                        if assessment is None or assessment["forecast"] is None
                        else _forecast(assessment["forecast"], assessment["drivers"]),
                        baseline_long=rate("long"),
                        baseline_short=rate("short"),
                        episode_id=case["episode_id"],
                        episode_role=case["episode_role"] or "unknown",
                        source_age_ms=None
                        if case["payload"].get(
                            "provider_event_at_ms" if case["trigger_kind"] == "oi" else "first_available_at_ms"
                        )
                        is None
                        or view.get("decided_at_ms") is None
                        else int(view["decided_at_ms"])
                        - int(
                            case["payload"][
                                "provider_event_at_ms" if case["trigger_kind"] == "oi" else "first_available_at_ms"
                            ]
                        ),
                        ingest_mode=case["payload"].get("ingest_mode", "unknown"),
                        geometry_version=case["geometry_version"] or "unknown",
                        assessment_status="unknown" if assessment is None else assessment["status"],
                    )
                )
            scored_runs[identity] = tuple(scored)
            policies = policy_scores(tuple(scored))
            forecast_policies = [
                (item.policy_id, item.policy_version) for item in policies if item.policy_id == "forecast"
            ]
            for forecast_policy in forecast_policies:
                for baseline in policies:
                    if baseline.policy_id == "forecast":
                        continue
                    comparison = paired_score(
                        tuple(scored),
                        tuple(scored),
                        left_policy=forecast_policy,
                        right_policy=(baseline.policy_id, baseline.policy_version),
                    )
                    comparisons.append({"left_run": identity, "right_run": identity, **asdict(comparison)})
            calibrated_scores = {}
            for _, version in forecast_policies:
                calibrated_cases: list[ScoredCase] = []
                for scored_case in scored:
                    assessment = assessments_by.get((scored_case.case_id, identity))
                    configs = (
                        actions_by.get((scored_case.case_id, assessment["assessment_id"]), []) if assessment else []
                    )
                    configuration = next(
                        (
                            a["policy_config"]
                            for a in configs
                            if a["policy_id"] == "forecast" and a["policy_version"] == version
                        ),
                        None,
                    )
                    if (
                        scored_case.forecast is None
                        or configuration is None
                        or "probability_temperature" not in configuration
                    ):
                        continue
                    temperature = Decimal(configuration["probability_temperature"])
                    calibrated_cases.append(
                        replace(
                            scored_case,
                            forecast=Forecast(
                                calibrated_probabilities(scored_case.forecast.long, temperature),
                                calibrated_probabilities(scored_case.forecast.short, temperature),
                            ),
                        )
                    )
                calibrated_scores[version] = _wire(asdict(forecast_score(tuple(calibrated_cases))))
            deviations = []
            for execution in execution_rows:
                candidate = assessments_by.get((execution["case_id"], identity))
                if candidate is None or execution["assessment_id"] != candidate["assessment_id"]:
                    continue
                paper_row = legs_by.get(execution["case_id"], {}).get(execution["action"])
                risk = (execution["entry_notional"] or Decimal(0)) * execution["stop_bps"] / 10_000
                if paper_row is not None and paper_row["net_r"] is not None and risk > 0:
                    deviations.append(execution["net_pnl"] / risk - paper_row["net_r"])
            programs.append(
                {
                    "run_id": identity,
                    "evaluator_id": run["evaluator_id"],
                    "mode": run["kind"],
                    "evaluator_spec": run["evaluator_spec"],
                    "manifest": run["manifest"],
                    "program_sha": run["program_sha"],
                    "route": route,
                    "assessments": sum(row["run_id"] == identity for row in assessments),
                    "failures": failure_counts,
                    "policies": _wire([asdict(item) for item in policies]),
                    "cohorts": _wire(cohort_scores(tuple(scored))),
                    "forecast": _wire(asdict(forecast_score(tuple(scored)))),
                    "calibrated_forecasts": calibrated_scores,
                    "execution_deviation": {
                        "scored": len(deviations),
                        "average_r_delta": str(sum(deviations, Decimal(0)) / len(deviations))
                        if len(deviations) >= 10
                        else None,
                        "status": "ok" if len(deviations) >= 10 else "insufficient_data",
                    },
                }
            )
        for index, left_run in enumerate(identities):
            for right_run in identities[index + 1 :]:
                left_policies = {
                    (d.policy_id, d.version)
                    for c in scored_runs[left_run]
                    for d in c.decisions
                    if d.policy_id == "forecast"
                }
                right_policies = {
                    (d.policy_id, d.version)
                    for c in scored_runs[right_run]
                    for d in c.decisions
                    if d.policy_id == "forecast"
                }
                for left_policy in sorted(left_policies):
                    for right_policy in sorted(right_policies):
                        comparison = paired_score(
                            scored_runs[left_run],
                            scored_runs[right_run],
                            left_policy=left_policy,
                            right_policy=right_policy,
                        )
                        comparisons.append({"left_run": left_run, "right_run": right_run, **asdict(comparison)})
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
            "comparisons": _wire(comparisons),
        }
