from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.review.desk import _selection


def _relevance(
    *,
    impact_breadth: str = "single_instrument",
    tradability: str = "direct",
    surprise: str = "unknown",
    development_delta: str = "material_detail",
    reader_value: str = "realtime",
) -> dict[str, Any]:
    return {
        "impact_breadth": impact_breadth,
        "tradability": tradability,
        "surprise": surprise,
        "development_delta": development_delta,
        "channels": ["risk_premium"],
        "affected_markets": ["single_asset"],
        "reader_value": reader_value,
    }


def _row(
    *,
    scope: str,
    relevance: dict[str, Any] | None,
    editorial_origin: str = "model",
    queue_priority: str = "normal",
) -> dict[str, Any]:
    return {
        "verdict": {"scope": scope},
        "model_editorial": {
            "editorial_origin": editorial_origin,
            "relevance": relevance,
        },
        "queue_priority": queue_priority,
        "final_decision": "drop",
    }


@pytest.mark.parametrize("scope", ["sector", "single_name"])
def test_regional_direct_stratum_is_driven_by_typed_relevance_not_macro_scope(scope: str) -> None:
    selection = _selection(
        _row(
            scope=scope,
            relevance=_relevance(impact_breadth="regional", tradability="second_order"),
        )
    )

    assert selection == {
        "stratum": "regional_direct_exception",
        "stratum_zh": "区域事件直接交易例外",
        "reason": "trade_relevance_targeted_stratum",
        "reason_zh": "按交易相关性边界定向抽样",
        "sampling_probability": 1.0,
        "selection_version": "news_review_sampler_v3",
    }


@pytest.mark.parametrize("scope", ["sector", "single_name"])
def test_color_only_stratum_is_driven_by_typed_relevance_not_macro_scope(scope: str) -> None:
    selection = _selection(
        _row(
            scope=scope,
            relevance=_relevance(
                tradability="contextual",
                development_delta="color_only",
                reader_value="background",
            ),
        )
    )

    assert selection["stratum"] == "color_only_progression"
    assert selection["sampling_probability"] == 1.0
    assert selection["selection_version"] == "news_review_sampler_v3"


def test_queue_high_model_event_remains_in_typed_target_strata() -> None:
    selection = _selection(
        _row(
            scope="sector",
            relevance=_relevance(impact_breadth="regional"),
            queue_priority="high",
        )
    )

    assert selection["stratum"] == "regional_direct_exception"


@pytest.mark.parametrize(
    "editorial_origin,relevance",
    [
        ("telemetry_deterministic", _relevance(impact_breadth="regional")),
        ("model", None),
    ],
)
def test_non_model_or_missing_relevance_does_not_enter_typed_strata(
    editorial_origin: str,
    relevance: dict[str, Any] | None,
) -> None:
    selection = _selection(
        _row(
            scope="macro",
            relevance=relevance,
            editorial_origin=editorial_origin,
        )
    )

    assert selection["stratum"] == "model_drop"
    assert selection["reason"] == "semantic_or_policy_hold"


def test_only_remaining_model_macro_candidate_enters_macro_random_control() -> None:
    selection = _selection(
        _row(
            scope="macro",
            relevance=_relevance(
                impact_breadth="cross_asset",
                surprise="material_vs_expectation",
                development_delta="state_change",
                reader_value="realtime",
            ),
        )
    )

    assert selection["stratum"] == "macro_random_control"
    assert selection["sampling_probability"] == 0.25
