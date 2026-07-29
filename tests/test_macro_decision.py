from __future__ import annotations

import json
from datetime import UTC, date, datetime

from tracefold.macro import (
    NATURAL_CHANGE_REGISTRY,
    module_payloads,
    natural_change_calculation,
    resolve_thesis_session,
)
from tracefold.macro.coverage import COVERAGE_MANIFEST
from tracefold.macro.fed_roles import effective_roster_rows, match_effective_role
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.registry import DATASET_REGISTRY

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
        "source_url": "https://finance.yahoo.com/",
    }


def _module(module_id: str, *, now_ms: int = NOW_MS, **overrides: list[dict]) -> dict:
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
        now_ms=now_ms,
        **groups,
    )


def _dataset_state(module: dict, dataset_id: str) -> dict:
    return next(item for item in module["evidence"]["dataset_states"] if item["dataset_id"] == dataset_id)


def test_thesis_session_rolls_at_0850_new_york_and_skips_closed_days() -> None:
    before_due = int(datetime(2026, 7, 27, 12, 49, tzinfo=UTC).timestamp() * 1_000)
    at_due = int(datetime(2026, 7, 27, 12, 50, tzinfo=UTC).timestamp() * 1_000)
    sunday = int(datetime(2026, 7, 26, 16, tzinfo=UTC).timestamp() * 1_000)
    observed_holiday = int(datetime(2026, 7, 3, 16, tzinfo=UTC).timestamp() * 1_000)

    assert resolve_thesis_session(now_ms=before_due) == date(2026, 7, 24)
    assert resolve_thesis_session(now_ms=at_due) == date(2026, 7, 27)
    assert resolve_thesis_session(now_ms=sunday) == date(2026, 7, 24)
    assert resolve_thesis_session(now_ms=observed_holiday) == date(2026, 7, 2)


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

    assert _dataset_state(module, "fred.dgs2")["current_health"] == "current"


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


def test_implemented_fed_capabilities_exclude_unavailable_licensed_products() -> None:
    module = _module("rates_fed")
    capabilities = {item["capability_id"]: item for item in module["status"]["coverage"]["capabilities"]}

    assert module["status"]["coverage"]["state"] == "complete"
    assert capabilities["fed.reserve_bank_speeches"]["state"] == "available"
    assert capabilities["fed.roster"]["state"] == "available"
    assert capabilities["fed.document_analysis"]["state"] == "available"
    assert "rates.cme_policy_futures" not in capabilities
    assert all(spec.adapter_id != "unavailable" for spec in DATASET_REGISTRY.values())
    assert all(capability.requirement in {"required", "supporting"} for capability in COVERAGE_MANIFEST.values())
    assert "licensed_unavailable" not in json.dumps(module, sort_keys=True)


def test_natural_change_registry_covers_every_registered_dataset() -> None:
    assert set(NATURAL_CHANGE_REGISTRY) == set(DATASET_REGISTRY)
    release = natural_change_calculation("bls.cpi.release")
    assert release.revision_policy == "explicit_revised_prior_only"
    assert release.surprise_policy == "explicit_consensus_only"
    assert release.output_schema == "macro_natural_change_v1"


def test_economy_release_sources_are_canonical_and_fred_is_history_only() -> None:
    expected = {
        "economy.gdp": ("bea.gdp.release", "fred.gdpc1"),
        "economy.pce": ("bea.pce.release", "fred.pcepi"),
        "economy.core_pce": ("bea.core_pce.release", "fred.pcepilfe"),
    }
    for concept_id, (release_id, history_id) in expected.items():
        assert DATASET_REGISTRY[release_id].concept_id == concept_id
        assert DATASET_REGISTRY[release_id].source_role == "release"
        assert DATASET_REGISTRY[history_id].concept_id == concept_id
        assert DATASET_REGISTRY[history_id].source_role == "history"


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


def test_liquidity_payload_exposes_server_calculated_sofr_iorb_spread_history() -> None:
    module = _module(
        "liquidity_funding",
        series_rows=[
            _series_row(
                dataset_id=dataset_id,
                reference_date=reference_date,
                value=value,
            )
            for dataset_id, values in (
                ("fred.sofr", (4.30, 4.35)),
                ("fred.iorb", (4.40, 4.40)),
            )
            for reference_date, value in zip(
                (date(2026, 7, 17), date(2026, 7, 24)),
                values,
                strict=True,
            )
        ],
    )

    assert module["funding"]["sofr_minus_iorb_bp_history"] == [
        {"date": "2026-07-17", "value": -10.0},
        {"date": "2026-07-24", "value": -5.0},
    ]


def test_volatility_payload_exposes_server_normalized_cross_asset_history() -> None:
    module = _module(
        "volatility",
        series_rows=[
            _series_row(
                dataset_id=dataset_id,
                reference_date=reference_date,
                value=value,
            )
            for dataset_id, values in (
                ("fred.vxncls", (20.0, 22.0)),
                ("fred.gvzcls", (15.0, 18.0)),
                ("fred.ovxcls", (30.0, 27.0)),
            )
            for reference_date, value in zip(
                (date(2026, 7, 17), date(2026, 7, 24)),
                values,
                strict=True,
            )
        ],
    )

    normalized = module["cross_asset_implied"]["normalized"]
    assert normalized[0] == {
        "symbol": "VXN",
        "date": "2026-07-17",
        "normalized_value": 100.0,
    }
    assert normalized[-1] == {
        "symbol": "OVX",
        "date": "2026-07-24",
        "normalized_value": 90.0,
    }


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
    ]
    assert "trace_nav" not in module["confirmations"]
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
    intraday = int(datetime(2026, 7, 27, 11, 55, tzinfo=UTC).timestamp() * 1_000)
    market_rows = []
    for index, instrument_id in enumerate(("spy", "qqq", "iwm", "tlt", "ief", "lqd", "hyg", "dxy", "gld", "uso")):
        market_rows.extend(
            (
                _market_row(f"nasdaq.{instrument_id}.daily", prior, 100 + index),
                _market_row(f"nasdaq.{instrument_id}.daily", friday, 105 + index),
                _market_row(f"yfinance.{instrument_id}.intraday", intraday, 106 + index),
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
            for row in market_rows
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
    assert module["assets"]["benchmarks"][0]["symbol"] == "SPY"
    assert _dataset_state(module, "yfinance.spy.intraday")["current_health"] == "current"
    assert module["assets"]["proxies"][0]["sources"]["intraday_proxy"]["market_time_ms"] == intraday
    assert module["assets"]["proxies"][0]["history_dataset_id"] == "nasdaq.spy.daily"
    assert module["assets"]["proxies"][0]["sources"]["history"]["change_1w_pct"] == 5.0
    assert module["assets"]["proxies"][0]["sources"]["identity_policy"] == "separate_source_facts_no_blend"


def test_cross_asset_uses_latest_intraday_bar_within_the_same_session() -> None:
    earlier = int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000)
    later = int(datetime(2026, 7, 27, 19, 55, tzinfo=UTC).timestamp() * 1_000)
    module = _module(
        "cross_asset",
        now_ms=int(datetime(2026, 7, 27, 20, tzinfo=UTC).timestamp() * 1_000),
        market_rows=[
            _market_row("nasdaq.spy.daily", earlier, 100),
            _market_row("yfinance.spy.intraday", later, 110),
            _market_row("yfinance.spy.intraday", earlier, 100),
        ],
        target_states=[
            {
                "dataset_id": "yfinance.spy.intraday",
                "partition_key": "latest",
                "status": "current",
            }
        ],
    )

    spy = module["assets"]["proxies"][0]
    state = _dataset_state(module, "yfinance.spy.intraday")
    assert spy["sources"]["intraday_proxy"]["latest_value"] == 110
    assert spy["sources"]["intraday_proxy"]["market_time_ms"] == later
    assert spy["selection_policy"] == "decision_primary_only_no_fallback"
    assert state["last_market_at_ms"] == later
    assert state["current_health"] == "current"


def test_closed_equity_market_keeps_the_last_expected_bar_current() -> None:
    friday_last_bar = int(datetime(2026, 7, 24, 23, 55, tzinfo=UTC).timestamp() * 1_000)
    sunday = int(datetime(2026, 7, 26, 16, tzinfo=UTC).timestamp() * 1_000)
    module = _module(
        "cross_asset",
        now_ms=sunday,
        market_rows=[_market_row("yfinance.spy.intraday", friday_last_bar, 110)],
        target_states=[
            {
                "dataset_id": "yfinance.spy.intraday",
                "partition_key": "latest",
                "status": "current",
            }
        ],
    )

    state = _dataset_state(module, "yfinance.spy.intraday")
    assert state["market_state"] == "closed"
    assert state["current_health"] == "current"
    assert state["current_reason"] == "last_expected_bar_present"
    assert module["status"]["current_health"]["state"] == "degraded"


def test_natural_change_metrics_follow_dataset_cadence_and_are_comparable() -> None:
    daily_rows = [
        _series_row(
            dataset_id="fred.dgs2",
            reference_date=date(2026, 7, day),
            value=value,
        )
        for day, value in ((1, 4.0), (20, 4.1), (27, 4.25))
    ]
    monthly_rows = [
        _series_row(
            dataset_id="fred.cpiaucsl",
            reference_date=date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1),
            value=300 + month,
        )
        for month in range(1, 15)
    ]

    rates = _module("rates_fed", series_rows=daily_rows)
    economy = _module("economy_inflation", series_rows=monthly_rows)
    rates_change = next(change for change in rates["summary"]["top_changes"] if change["dataset_id"] == "fred.dgs2")
    cpi_change = next(change for change in economy["summary"]["top_changes"] if change["dataset_id"] == "fred.cpiaucsl")

    assert rates_change["cadence"] == "daily"
    assert set(rates_change["metrics"]) == {"change_1d_bp", "change_1w_bp", "change_1m_bp"}
    assert rates_change["metric_unit"] == "basis_points"
    assert cpi_change["cadence"] == "monthly"
    assert set(cpi_change["metrics"]) == {"mom_pct", "three_month_annualized_pct", "yoy_pct"}
    assert cpi_change["metric_unit"] == "percent"
    assert rates_change["importance_rank"] >= 1
    assert rates_change["importance_factors"]["standardized_magnitude"] >= 0
    assert "importance_explanation" in rates_change


def test_natural_change_does_not_relabel_out_of_window_or_missing_period_data() -> None:
    daily = _module(
        "rates_fed",
        series_rows=[
            _series_row(
                dataset_id="fred.dgs2",
                reference_date=date(2026, 7, 20),
                value=4.0,
            ),
            _series_row(
                dataset_id="fred.dgs2",
                reference_date=date(2026, 7, 27),
                value=4.25,
            ),
        ],
    )
    daily_change = next(change for change in daily["summary"]["top_changes"] if change["dataset_id"] == "fred.dgs2")
    assert daily_change["metrics"]["change_1d_bp"] is None
    assert daily_change["metrics"]["change_1w_bp"] == 25.0

    monthly = _module(
        "economy_inflation",
        series_rows=[
            _series_row(
                dataset_id="fred.cpiaucsl",
                reference_date=date(2026, 5, 1),
                value=300,
            ),
            _series_row(
                dataset_id="fred.cpiaucsl",
                reference_date=date(2026, 7, 1),
                value=303,
            ),
        ],
    )
    monthly_change = next(
        change for change in monthly["summary"]["top_changes"] if change["dataset_id"] == "fred.cpiaucsl"
    )
    assert monthly_change["metrics"]["mom_pct"] is None


def test_reconciliation_receipt_selects_primary_without_blending_proxy_identity() -> None:
    observed = int(datetime(2026, 7, 27, 12, tzinfo=UTC).timestamp() * 1_000)
    module = _module(
        "cross_asset",
        market_rows=[
            _market_row("binance.btcusdt.spot", observed, 117_000),
            _market_row("yfinance.btc_yahoo.intraday", observed, 117_100),
        ],
    )
    receipt = next(item for item in module["evidence"]["reconciliation_receipts"] if item["concept_id"] == "market.btc")
    bitcoin = next(item for item in module["assets"]["benchmarks"] if item["label"] == "Bitcoin")

    assert receipt["selected_dataset_id"] == "binance.btcusdt.spot"
    assert receipt["selection_policy"] == "decision_primary_only_no_fallback"
    assert receipt["identity_policy"] == "separate_source_facts_no_blend"
    assert bitcoin["sources"]["decision_primary"]["dataset_id"] == "binance.btcusdt.spot"
    assert bitcoin["sources"]["intraday_proxy"]["dataset_id"] == "yfinance.btc_yahoo.intraday"
    assert "latest_value" not in bitcoin


def test_fomc_roster_uses_non_overlapping_effective_snapshots() -> None:
    rows = [
        {
            "official_id": "a",
            "official_name": "Alice Example",
            "effective_start": date(2026, 1, 1),
            "effective_end": None,
            "received_at_ms": 1,
            "role_fact_id": "old-a",
        },
        {
            "official_id": "b",
            "official_name": "Bob Example",
            "effective_start": date(2026, 1, 1),
            "effective_end": None,
            "received_at_ms": 1,
            "role_fact_id": "old-b",
        },
        {
            "official_id": "b",
            "official_name": "Bob Example",
            "effective_start": date(2026, 3, 1),
            "effective_end": None,
            "received_at_ms": 2,
            "role_fact_id": "new-b",
        },
    ]

    effective = effective_roster_rows(rows)
    assert {row["effective_end"] for row in effective if row["effective_start"] == date(2026, 1, 1)} == {
        date(2026, 2, 28)
    }
    assert match_effective_role("Alice Example", effective_date=date(2026, 3, 2), role_rows=rows) is None
    current_bob = match_effective_role(
        "Bob Example",
        effective_date=date(2026, 3, 2),
        role_rows=rows,
    )
    assert current_bob is not None
    assert current_bob["role_fact_id"] == "new-b"
