from __future__ import annotations

from datetime import UTC, date, datetime

from tracefold.macro.projection import build_module_payload

NOW_MS = int(datetime(2026, 7, 27, 12, tzinfo=UTC).timestamp() * 1_000)


def _position_row(
    *,
    fact_id: str,
    contract_code: str,
    contract_name: str,
    report_date: date,
    value: float,
) -> dict:
    return {
        "position_fact_id": fact_id,
        "dataset_id": "cftc.tff.rates_positions",
        "contract_code": contract_code,
        "contract_name": contract_name,
        "report_date": report_date,
        "open_interest": 1_000_000.0,
        "leveraged_long": 100_000.0,
        "leveraged_short": 200_000.0,
        "leveraged_net_pct_oi": value,
        "asset_manager_net_pct_oi": 20.0,
        "dealer_net_pct_oi": -5.0,
        "published_at_ms": NOW_MS - 86_400_000,
        "received_at_ms": NOW_MS - 86_400_000,
        "source_url": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
        "unit": "percent_open_interest",
    }


def _series_row(*, dataset_id: str, reference_date: date) -> dict:
    return {
        "fact_id": f"fact:{dataset_id}",
        "dataset_id": dataset_id,
        "series_id": dataset_id.upper(),
        "reference_date": reference_date,
        "vintage_date": date(2026, 7, 27),
        "value_numeric": 4.25,
        "value_text": None,
        "unit": "percent",
        "published_at_ms": None,
        "received_at_ms": NOW_MS,
        "source_url": "https://fred.stlouisfed.org/",
    }


def _dataset_state(module: dict, dataset_id: str) -> dict:
    return next(item for item in module["dataset_states"] if item["dataset_id"] == dataset_id)


def test_daily_official_fact_does_not_expire_over_a_weekend() -> None:
    module = build_module_payload(
        module_id="rates_fed",
        now_ms=NOW_MS,
        series_rows=[_series_row(dataset_id="fred.dgs2", reference_date=date(2026, 7, 23))],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[{"dataset_id": "fred.dgs2", "status": "current"}],
        features=[],
    )

    assert _dataset_state(module, "fred.dgs2")["state"] == "current"


def test_weekly_official_fact_uses_the_weekly_freshness_budget() -> None:
    module = build_module_payload(
        module_id="liquidity_funding",
        now_ms=NOW_MS,
        series_rows=[_series_row(dataset_id="fred.walcl", reference_date=date(2026, 7, 22))],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[{"dataset_id": "fred.walcl", "status": "current"}],
        features=[],
    )

    assert _dataset_state(module, "fred.walcl")["state"] == "current"


def test_previous_close_market_fact_does_not_expire_over_a_weekend() -> None:
    friday_close_ms = int(datetime(2026, 7, 24, 20, tzinfo=UTC).timestamp() * 1_000)
    module = build_module_payload(
        module_id="cross_asset",
        now_ms=NOW_MS,
        series_rows=[],
        market_rows=[
            {
                "observation_id": "observation:spy",
                "dataset_id": "nasdaq.spy.history",
                "instrument_id": "spy",
                "field_name": "close",
                "value_numeric": 640.0,
                "unit": "price",
                "observed_at_ms": friday_close_ms,
                "published_at_ms": friday_close_ms,
                "received_at_ms": NOW_MS,
                "source_url": "https://api.nasdaq.com/",
            }
        ],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[{"dataset_id": "nasdaq.spy.history", "status": "current"}],
        features=[],
    )

    assert _dataset_state(module, "nasdaq.spy.history")["state"] == "current"


def test_monthly_and_quarterly_fred_facts_use_reference_period_cadence() -> None:
    module = build_module_payload(
        module_id="economy_inflation",
        now_ms=NOW_MS,
        series_rows=[
            _series_row(dataset_id="fred.pcepi", reference_date=date(2026, 5, 1)),
            _series_row(dataset_id="fred.gdpc1", reference_date=date(2026, 1, 1)),
        ],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[
            {"dataset_id": "fred.pcepi", "status": "current"},
            {"dataset_id": "fred.gdpc1", "status": "current"},
        ],
        features=[],
    )

    assert _dataset_state(module, "fred.pcepi")["state"] == "current"
    assert _dataset_state(module, "fred.gdpc1")["state"] == "current"


def test_rates_module_keeps_each_cftc_futures_contract_as_a_visible_series() -> None:
    rows = [
        _position_row(
            fact_id="position:2y",
            contract_code="042601",
            contract_name="UST 2Y",
            report_date=date(2026, 7, 21),
            value=-12.5,
        ),
        _position_row(
            fact_id="position:10y",
            contract_code="043602",
            contract_name="UST 10Y",
            report_date=date(2026, 7, 21),
            value=-39.1,
        ),
    ]
    module = build_module_payload(
        module_id="rates_fed",
        now_ms=NOW_MS,
        series_rows=[],
        market_rows=[],
        position_rows=rows,
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[
            {
                "dataset_id": "cftc.tff.rates_positions",
                "status": "current",
            }
        ],
        features=[],
    )

    chart = next(item for item in module["charts"] if item["title"] == "利率期货杠杆资金净仓位（占OI）")
    assert chart["series"] == [
        "cftc.tff.rates_positions:042601",
        "cftc.tff.rates_positions:043602",
    ]
    assert [point["y"] for point in chart["points"]] == [-12.5, -39.1]
    assert {
        item["fact_ref"] for item in module["raw_evidence"] if item["dataset_id"] == "cftc.tff.rates_positions"
    } == {"position:2y", "position:10y"}
