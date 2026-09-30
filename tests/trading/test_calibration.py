"""Training labels must be available before a strictly later held-out input window."""

from dataclasses import replace
from decimal import Decimal

import pytest

from tracefold.trading.engine.calibration import CalibrationCase, fit_and_validate
from tracefold.trading.engine.case_view import BaseRates, build_case_view
from tracefold.trading.engine.forecast import Forecast, LegProbabilities
from tracefold.trading.engine.paper import LegGeometry, PaperLeg


def _row(identity, cutoff, exit_at):
    view = build_case_view(
        case_id=identity,
        asset_id="crypto:SOL",
        trigger_kind="oi",
        decided_at_ms=cutoff,
        source_fact={},
        features={},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal(0),
        base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
    )
    forecast = Forecast(
        LegProbabilities(Decimal(".8"), Decimal(".1"), Decimal(".1")),
        LegProbabilities(Decimal(".1"), Decimal(".8"), Decimal(".1")),
    )
    long = PaperLeg(
        "long",
        "complete",
        "tp",
        None,
        cutoff + 1,
        exit_at,
        Decimal(100),
        Decimal(102),
        Decimal(200),
        Decimal(10),
        Decimal("1.9"),
    )
    short = replace(long, side="short", outcome="sl", gross_bps=Decimal(-100), net_r=Decimal("-1.1"))
    return CalibrationCase(view, forecast, (long, short))


def test_calibration_excludes_late_training_labels_and_never_promotes():
    training = tuple(_row(str(i), 100 + i, 300) for i in range(20))
    future = tuple(_row(f"future-{i}", 500 + i, 700) for i in range(20))
    leaking = _row("training-with-future-label", 200, 800)
    artifact, report = fit_and_validate(
        (*training, *future, leaking),
        evaluator_id="a" * 64,
        source_run_id="b" * 64,
        train_since_ms=1,
        train_until_ms=400,
        validate_until_ms=900,
    )
    assert artifact["trained_until_ms"] == 400
    assert report["training_cases"] == report["heldout_cases"] == 20
    assert report["excluded_cases"] == 1
    assert leaking.view.case_id not in artifact["training_manifest"]["training_cases"]
    assert not report["automatic_promotion"]
    assert report["paired_forecast"]["effective_days"] == 1
    assert report["paired_forecast"]["ci_low"] is None
    # Altering only future labels cannot alter the fitted artifact parameters.
    reversed_future = tuple(replace(row, legs=(replace(row.legs[0], outcome="sl"), row.legs[1])) for row in future)
    other, _ = fit_and_validate(
        (*training, *reversed_future, leaking),
        evaluator_id="a" * 64,
        source_run_id="b" * 64,
        train_since_ms=1,
        train_until_ms=400,
        validate_until_ms=900,
    )
    assert artifact["groups"] == other["groups"]
    with pytest.raises(ValueError, match="windows_invalid"):
        fit_and_validate(
            training,
            evaluator_id="a" * 64,
            source_run_id="b" * 64,
            train_since_ms=1,
            train_until_ms=400,
            validate_until_ms=400,
        )
