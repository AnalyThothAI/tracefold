from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tracefold.integrations.macro_sources import (
    MacroSourceClient,
    MacroSourceError,
    MacroSourceUnavailable,
)
from tracefold.macro import DocumentFact, ReleaseFact, SeriesFact, require_dataset
from tracefold.market import MarketObservationFact, MarketPositionFact, MarketSettlementFact

NOW_MS = int(datetime(2026, 7, 27, 12, tzinfo=UTC).timestamp() * 1_000)


def test_fred_csv_uses_explicit_backfill_bounds_and_emits_typed_series_facts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text="DATE,DGS10\n2026-07-01,4.20\n2026-07-02,4.25\n",
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("fred.dgs10"),
            partition_key="2026-07-01..2026-07-02",
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-02"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert [type(fact) for fact in batch.facts] == [SeriesFact, SeriesFact]
    assert batch.cursor == {
        "reference_date": "2026-07-02",
        "start_date": "2026-07-01",
        "end_date": "2026-07-02",
    }
    assert requests[0].url.params["cosd"] == "2026-07-01"
    assert requests[0].url.params["coed"] == "2026-07-02"


def test_bls_public_release_adapter_preserves_actual_and_prior() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "Results": {
                    "series": [
                        {
                            "seriesID": "LNS14000000",
                            "data": [
                                {"year": "2026", "period": "M06", "value": "4.1"},
                                {"year": "2026", "period": "M05", "value": "4.0"},
                            ],
                        }
                    ]
                },
            },
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("bls.unemployment.release"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 2
    latest = batch.facts[-1]
    assert isinstance(latest, ReleaseFact)
    assert latest.reference_period == "2026-M06"
    assert latest.actual_value == 4.1
    assert latest.prior_value == 4.0
    assert latest.estimate_value is None


def test_federal_reserve_rss_adapter_emits_immutable_document_fact() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260727a.htm</link>
      <guid>monetary20260727a</guid>
      <pubDate>Mon, 27 Jul 2026 10:00:00 GMT</pubDate>
      <description><![CDATA[<p>The Committee decided to maintain the target range.</p>]]></description>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.monetary_policy.documents"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    assert document.document_type == "statement"
    assert document.content_text == "The Committee decided to maintain the target range."
    assert document.source_url.startswith("https://www.federalreserve.gov/")


def test_missing_cfe_daily_file_is_retryable_not_permanently_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MacroSourceError, match="file_not_published") as error:
            client.fetch(
                require_dataset("cboe.cfe.vx.settlement"),
                partition_key="2026-07-27",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()
    assert not isinstance(error.value, MacroSourceUnavailable)


def test_cfe_settlement_uses_current_official_csv_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                "Product,Symbol,Expiration Date,Price\nVX,VX30/N6,2026-07-29,19.231\nIBHY,IBHY/U6,2026-09-18,98.625\n"
            ),
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("cboe.cfe.vx.settlement"),
            partition_key="2026-07-24",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    fact = batch.facts[0]
    assert isinstance(fact, MarketSettlementFact)
    assert fact.contract_code == "VX30/N6"
    assert fact.settlement_price == 19.231
    assert requests[0].url.params["dt"] == "2026-07-24"


def test_binance_spot_ignores_the_still_open_daily_candle() -> None:
    closed_at_ms = NOW_MS - 1
    open_candle_close_ms = NOW_MS + 86_400_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [NOW_MS - 86_400_000, "1", "2", "0.5", "65000", "10", closed_at_ms],
                [NOW_MS, "1", "2", "0.5", "66000", "10", open_candle_close_ms],
            ],
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("binance.btcusdt.spot"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    fact = batch.facts[0]
    assert isinstance(fact, MarketObservationFact)
    assert fact.observed_at_ms == closed_at_ms
    assert fact.received_at_ms == NOW_MS
    assert batch.cursor["observed_at_ms"] == closed_at_ms


def test_fred_liquidity_series_preserve_their_published_units() -> None:
    assert require_dataset("fred.wrbwfrbl").unit == "millions_usd"
    assert require_dataset("fred.wtregen").unit == "millions_usd"
    assert require_dataset("fred.rrpontsyd").unit == "billions_usd"


def test_cftc_tff_adapter_emits_official_contract_position_facts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-21T00:00:00.000",
                    "cftc_contract_market_code": "043602",
                    "contract_market_name": "UST 10Y NOTE",
                    "open_interest_all": "5272703",
                    "lev_money_positions_long": "382342",
                    "lev_money_positions_short": "2447147",
                    "pct_of_oi_lev_money_long": "7.3",
                    "pct_of_oi_lev_money_short": "46.4",
                    "pct_of_oi_asset_mgr_long": "62.0",
                    "pct_of_oi_asset_mgr_short": "13.9",
                    "pct_of_oi_dealer_long_all": "2.7",
                    "pct_of_oi_dealer_short_all": "12.5",
                }
            ],
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("cftc.tff.rates_positions"),
            partition_key="latest",
            cursor={"reference_date": "2026-07-14"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    fact = batch.facts[0]
    assert isinstance(fact, MarketPositionFact)
    assert fact.contract_name == "UST 10Y"
    assert fact.leveraged_net_pct_oi == -39.1
    assert fact.asset_manager_net_pct_oi == 48.1
    assert fact.dealer_net_pct_oi == -9.8
    assert requests[0].url.params["$order"] == "report_date_as_yyyy_mm_dd DESC"
    assert "043602" in requests[0].url.params["$where"]


def test_nasdaq_public_history_emits_previous_close_facts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": {"rCode": 200},
                "data": {
                    "tradesTable": {
                        "rows": [
                            {"date": "07/24/2026", "close": "$738.93"},
                            {"date": "07/23/2026", "close": "$735.34"},
                        ]
                    }
                },
            },
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("nasdaq.spy.history"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 2
    assert all(isinstance(fact, MarketObservationFact) for fact in batch.facts)
    assert [fact.value_numeric for fact in batch.facts] == [735.34, 738.93]
    assert batch.cursor["reference_date"] == "2026-07-24"
    assert requests[0].url.params["assetclass"] == "etf"
    assert requests[0].url.params["limit"] == "5000"


def test_disabled_nasdaq_public_source_is_explicitly_unavailable() -> None:
    client = MacroSourceClient(
        nasdaq_public_enabled=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
    )
    try:
        with pytest.raises(MacroSourceUnavailable, match="nasdaq_public_disabled"):
            client.fetch(
                require_dataset("nasdaq.spy.history"),
                partition_key="latest",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()
