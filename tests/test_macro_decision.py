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

    chart = next(
        item
        for item in module["charts"]
        if item["title"] == "利率期货杠杆资金净仓位（占OI）"
    )
    assert chart["series"] == [
        "cftc.tff.rates_positions:042601",
        "cftc.tff.rates_positions:043602",
    ]
    assert [point["y"] for point in chart["points"]] == [-12.5, -39.1]
    assert {
        item["fact_ref"]
        for item in module["raw_evidence"]
        if item["dataset_id"] == "cftc.tff.rates_positions"
    } == {"position:2y", "position:10y"}
