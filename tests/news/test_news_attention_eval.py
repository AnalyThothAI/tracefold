"""The comparison entry stays offline unless a model route is selected explicitly."""

from __future__ import annotations

import asyncio
from pathlib import Path

from scripts.eval_news_attention import evaluate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/news/issue_725_attention_cases.json"


def test_recorded_attention_comparison_uses_frozen_inputs_without_a_model() -> None:
    report = asyncio.run(evaluate(FIXTURE))

    assert report["mode"] == "recorded"
    assert report["totals"]["cases"] == 6
    assert report["totals"]["noise_notified"] == 3
    assert report["totals"]["physical_calls"] is None
    assert report["totals"]["technical_failures"] == 0
    assert {row["recording_origin"] for row in report["cases"]} == {"synthetic_notify_all_baseline"}
