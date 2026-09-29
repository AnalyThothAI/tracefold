"""Recorded typed forecast responses never choose an order or invoke runtime tools."""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.app.trading_assessor import TradingAssessor, load_program
from tracefold.trading.engine.case_view import BaseRates, build_case_view
from tracefold.trading.engine.paper import LegGeometry


def _view():
    return build_case_view(
        case_id="c" * 64,
        asset_id="crypto:SOL",
        trigger_kind="oi",
        decided_at_ms=1,
        source_fact={"oi_change_bps": "300", "measurement_definition": "5m"},
        features={"perp_return_15m_bps": "10"},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal(1),
        base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
    )


class _RecordedProgram:
    lm = None

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    async def acall(self, **kwargs: Any) -> Any:
        assert "case_view_json" in kwargs and "selected_plan_id" not in kwargs
        self.calls += 1
        return dspy.Prediction(assessment=self.response)


def test_recorded_response_drops_unknown_alias_and_keeps_both_probabilities() -> None:
    program = _RecordedProgram(
        {
            "drivers": [
                {"ref": "e1", "leans": "long", "note": "rise"},
                {"ref": "e99", "leans": "short", "note": "not in view"},
            ],
            "long": {"p_tp": "0.6", "p_sl": "0.2", "p_timeout": "0.2"},
            "short": {"p_tp": "0.2", "p_sl": "0.6", "p_timeout": "0.2"},
        }
    )
    assessor = TradingAssessor(program=program, lm=object(), timeout_s=1, concurrent=1)  # type: ignore[arg-type]
    result = asyncio.run(assessor.assess(_view()))
    assert result.status == "complete" and result.forecast is not None
    assert result.forecast.long.p_tp == Decimal("0.6")
    assert tuple(driver.ref for driver in result.forecast.drivers) == ("e1",)
    assert result.notes == ("unknown_evidence_alias:e99",)
    assert program.calls == 1


def test_bad_probability_sum_is_named_schema_failure() -> None:
    program = _RecordedProgram(
        {
            "drivers": [],
            "long": {"p_tp": "0.9", "p_sl": "0.9", "p_timeout": "0.9"},
            "short": {"p_tp": "0.2", "p_sl": "0.6", "p_timeout": "0.2"},
        }
    )
    result = asyncio.run(TradingAssessor(program=program, lm=object(), timeout_s=1, concurrent=1).assess(_view()))  # type: ignore[arg-type]
    assert result.status == "failed" and result.error_code == "schema"
    assert program.calls == 1


def test_program_artifact_is_hash_pinned_and_contains_no_lm_endpoint() -> None:
    path = Path(__file__).parents[2] / "tracefold/trading/programs/forecast_v1.json"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    program = load_program(path, sha)
    assert program.lm is None
    with pytest.raises(ValueError, match="trading_program_sha_mismatch"):
        load_program(path, "0" * 64)
