from __future__ import annotations

from datetime import UTC, date, datetime

from tracefold.macro import module_payloads, resolve_judgment_session
from tracefold.macro.module_payloads import build_typed_module_payload

NOW_MS = int(datetime(2026, 7, 27, 12, tzinfo=UTC).timestamp() * 1_000)


def _series_row(
    *,
    dataset_id: str,
    reference_date: date,
    value: float = 4.25,
    series_id: str | None = None,
) -> dict:
    return {
        "fact_id": f"fact:{dataset_id}:{series_id or dataset_id}:{reference_date}",
        "dataset_id": dataset_id,
        "series_id": series_id or dataset_id.upper(),
        "reference_date": reference_date,
        "vintage_date": date(2026, 7, 27),
        "value_numeric": value,
        "value_text": None,
        "unit": "percent",
        "published_at_ms": None,
        "received_at_ms": NOW_MS,
        "source_url": "https://example.com/official",
    }


def _market_row(dataset_id: str, observed_at_ms: int, value: float) -> dict:
    return {
        "observation_id": f"observation:{dataset_id}:{observed_at_ms}",
        "dataset_id": dataset_id,
        "instrument_id": dataset_id,
        "field_name": "close",
        "value_numeric": value,
        "unit": "price",
        "observed_at_ms": observed_at_ms,
        "published_at_ms": observed_at_ms,
        "received_at_ms": NOW_MS,
        "source_url": "https://api.nasdaq.com/",
    }


def _module(module_id: str, **overrides: list[dict]) -> dict:
    groups = {
        "series_rows": [],
        "market_rows": [],
        "position_rows": [],
        "settlement_rows": [],
        "release_rows": [],
        "document_rows": [],
        "target_states": [],
    }
    groups.update(overrides)
    return build_typed_module_payload(
        module_id=module_id,  # type: ignore[arg-type]
        now_ms=NOW_MS,
        **groups,
    )


def _dataset_state(module: dict, dataset_id: str) -> dict:
    return next(item for item in module["evidence"]["dataset_states"] if item["dataset_id"] == dataset_id)


def test_judgment_session_rolls_at_0850_new_york_and_skips_closed_days() -> None:
    before_due = int(datetime(2026, 7, 27, 12, 49, tzinfo=UTC).timestamp() * 1_000)
    at_due = int(datetime(2026, 7, 27, 12, 50, tzinfo=UTC).timestamp() * 1_000)
    sunday = int(datetime(2026, 7, 26, 16, tzinfo=UTC).timestamp() * 1_000)
    observed_holiday = int(datetime(2026, 7, 3, 16, tzinfo=UTC).timestamp() * 1_000)

    assert resolve_judgment_session(now_ms=before_due) == date(2026, 7, 24)
    assert resolve_judgment_session(now_ms=at_due) == date(2026, 7, 27)
    assert resolve_judgment_session(now_ms=sunday) == date(2026, 7, 24)
    assert resolve_judgment_session(now_ms=observed_holiday) == date(2026, 7, 2)


def test_daily_official_fact_does_not_expire_over_a_weekend() -> None:
    module = _module(
        "rates_fed",
        series_rows=[_series_row(dataset_id="fred.dgs2", reference_date=date(2026, 7, 23))],
        target_states=[
            {
                "dataset_id": "fred.dgs2",
                "partition_key": "latest",
                "status": "current",
            }
        ],
    )

    assert _dataset_state(module, "fred.dgs2")["state"] == "current"


def test_coverage_manifest_keeps_expected_but_unregistered_capability_visible(
    monkeypatch,
) -> None:
    registry = dict(module_payloads.DATASET_REGISTRY)
    registry.pop("federal_reserve.reserve_bank.speeches")
    monkeypatch.setattr(module_payloads, "DATASET_REGISTRY", registry)
    module = _module("rates_fed")
    capabilities = {item["capability_id"]: item for item in module["status"]["coverage"]["capabilities"]}

    assert module["status"]["coverage"]["state"] == "partial"
    assert capabilities["fed.reserve_bank_speeches"]["state"] == "missing"


def test_implemented_fed_capabilities_are_distinct_from_licensed_unavailable() -> None:
    module = _module("rates_fed")
    capabilities = {item["capability_id"]: item for item in module["status"]["coverage"]["capabilities"]}

    assert module["status"]["coverage"]["state"] == "licensed_unavailable"
    assert capabilities["fed.reserve_bank_speeches"]["state"] == "available"
    assert capabilities["fed.roster"]["state"] == "available"
    assert capabilities["fed.document_analysis"]["state"] == "available"
    assert capabilities["rates.cme_policy_futures"]["state"] == "licensed_unavailable"


def test_rates_payload_contains_true_curve_snapshots_spreads_and_shape() -> None:
    rows = []
    for reference_date, values in (
        (date(2026, 7, 17), {"3M": 4.3, "2Y": 4.0, "5Y": 4.1, "10Y": 4.2, "30Y": 4.5}),
        (date(2026, 7, 24), {"3M": 4.25, "2Y": 4.1, "5Y": 4.25, "10Y": 4.45, "30Y": 4.7}),
    ):
        rows.extend(
            _series_row(
                dataset_id="treasury.daily_nominal_curve",
                reference_date=reference_date,
                value=value,
                series_id=tenor,
            )
            for tenor, value in values.items()
        )
    module = _module(
        "rates_fed",
        series_rows=rows,
        target_states=[
            {
                "dataset_id": "treasury.daily_nominal_curve",
                "partition_key": "latest",
                "status": "current",
            }
        ],
    )

    snapshots = module["curve"]["nominal_snapshots"]
    assert [point["tenor"] for point in snapshots[0]["points"]] == [
        "3M",
        "2Y",
        "5Y",
        "10Y",
        "30Y",
    ]
    assert module["curve"]["spreads"]["2s10s"][-1]["value_bp"] == 35.0
    assert module["curve"]["classification"]["state"] == "bear_steepening"


def test_credit_payload_exposes_rating_ladder_sample_size_and_no_composite_score() -> None:
    dataset_ids = (
        "fred.bamlc0a0cm",
        "fred.bamlc0a4cbbb",
        "fred.bamlh0a1hybb",
        "fred.bamlh0a2hyb",
        "fred.bamlh0a3hyc",
    )
    rows = [
        _series_row(
            dataset_id=dataset_id,
            reference_date=reference_date,
            value=float(index + day),
        )
        for index, dataset_id in enumerate(dataset_ids)
        for day, reference_date in enumerate((date(2026, 7, 17), date(2026, 7, 24)))
    ]
    module = _module("credit", series_rows=rows)

    assert [row["dataset_id"] for row in module["spread_ladder"]["rows"]] == list(dataset_ids)
    assert all(row["sample_count"] == 2 for row in module["spread_ladder"]["rows"])
    assert [row["dimension_id"] for row in module["cycle_dimensions"]] == [
        "spread_level_velocity",
        "funding_cost",
        "credit_supply",
        "credit_quality",
        "market_liquidity",
    ]
    assert module["cycle_dimensions"][-1]["state"] == "licensed_unavailable"
    assert "score" not in module
    assert "composite_score" not in module["spread_ladder"]


def test_credit_bank_supply_contains_standard_and_demand_for_three_categories() -> None:
    dataset_ids = (
        "fred.drtscilm",
        "fred.drsdcilm",
        "fred.sublpdrcsn",
        "fred.sublpdrcdn",
        "fred.drtsclcc",
        "fred.demcc",
    )
    module = _module(
        "credit",
        series_rows=[
            _series_row(
                dataset_id=dataset_id,
                reference_date=date(2026, 4, 1),
                value=float(index),
            )
            for index, dataset_id in enumerate(dataset_ids)
        ],
    )

    assert [row["dataset_id"] for row in module["bank_lending"]["indicators"]] == list(dataset_ids)
    supply = next(row for row in module["cycle_dimensions"] if row["dimension_id"] == "credit_supply")
    assert supply["state"] != "insufficient"
    assert supply["evidence_dataset_ids"] == list(dataset_ids)


def test_cross_asset_payload_builds_fixed_proxy_matrix_and_normalized_comparison() -> None:
    friday = int(datetime(2026, 7, 24, 20, tzinfo=UTC).timestamp() * 1_000)
    prior = int(datetime(2026, 7, 17, 20, tzinfo=UTC).timestamp() * 1_000)
    market_rows = []
    for index, dataset_id in enumerate(
        (
            "nasdaq.spy.history",
            "nasdaq.qqq.history",
            "nasdaq.iwm.history",
            "nasdaq.tlt.history",
            "nasdaq.ief.history",
            "nasdaq.lqd.history",
            "nasdaq.hyg.history",
            "nasdaq.dxy.history",
            "nasdaq.gld.history",
            "nasdaq.uso.history",
        )
    ):
        market_rows.extend(
            (
                _market_row(dataset_id, prior, 100 + index),
                _market_row(dataset_id, friday, 105 + index),
            )
        )
    module = _module(
        "cross_asset",
        market_rows=market_rows,
        target_states=[
            {
                "dataset_id": row["dataset_id"],
                "partition_key": "latest",
                "status": "current",
            }
            for row in market_rows[1::2]
        ],
    )

    assert [row["symbol"] for row in module["assets"]["proxies"]] == [
        "SPY",
        "QQQ",
        "IWM",
        "TLT",
        "IEF",
        "LQD",
        "HYG",
        "UUP",
        "GLD",
        "USO",
    ]
    assert {row["symbol"] for row in module["assets"]["normalized"]} == {
        "SPY",
        "QQQ",
        "IWM",
        "TLT",
        "IEF",
        "LQD",
        "HYG",
        "UUP",
        "GLD",
        "USO",
    }
    assert _dataset_state(module, "nasdaq.spy.history")["state"] == "current"
