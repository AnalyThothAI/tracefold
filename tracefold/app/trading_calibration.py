"""Read frozen ledger facts, fit offline, and write reviewable local candidate artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tracefold.app.repository_session import repositories
from tracefold.trading.engine.calibration import CalibrationCase, fit_and_validate
from tracefold.trading.engine.case_view import case_view_from_record
from tracefold.trading.engine.forecast import forecast_from_record
from tracefold.trading.engine.paper import paper_leg_from_record


def calibrate(
    settings: Any,
    *,
    source_run_id: str,
    train_since_ms: int,
    train_until_ms: int,
    validate_until_ms: int,
    output: Path,
) -> dict[str, Any]:
    rows = []
    with repositories(settings, application_name="tracefold_trading_calibration") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION READ ONLY")
        run = repos.trading.evaluation_run(source_run_id)
        if run is None:
            raise ValueError("calibration_source_run_missing")
        for case in repos.trading.frozen_cases(since_ms=train_since_ms, until_ms=validate_until_ms):
            assessment = repos.trading.assessment_for_run(case_id=case["case_id"], run_id=source_run_id)
            if assessment is None or assessment["status"] != "ok":
                continue
            view = case_view_from_record(case["view"])
            forecast = forecast_from_record(assessment["forecast"], assessment["drivers"])
            detail = repos.trading.analysis_case(case["case_id"])
            legs = {
                leg["side"]: paper_leg_from_record(leg)
                for leg in detail["paper_legs"]
                if leg["geometry_version"] == view.geometry.version
            }
            if forecast is not None and set(legs) == {"long", "short"}:
                rows.append(CalibrationCase(view, forecast, (legs["long"], legs["short"])))
    artifact, report = fit_and_validate(
        tuple(rows),
        evaluator_id=run["evaluator_id"],
        source_run_id=source_run_id,
        train_since_ms=train_since_ms,
        train_until_ms=train_until_ms,
        validate_until_ms=validate_until_ms,
    )
    raw = (json.dumps(artifact, sort_keys=True, indent=2, default=str) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    report_path = output.with_suffix(".validation.json")
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2, default=str) + "\n")
    return {
        "artifact": str(output.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "validation": str(report_path.resolve()),
        "report": report,
    }
