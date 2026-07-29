from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pandas as pd
import pytest

import tracefold.integrations.macro_sources.client as macro_source_client
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
        "backfill_complete": True,
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


def test_bea_gdp_release_page_preserves_estimate_revision_and_release_clock() -> None:
    listing = """
    <html><main>
      <a href="/news/2026/gdp-third-estimate-1st-quarter-2026">
        GDP (Third Estimate), 1st Quarter 2026
      </a>
      <a href="/news/2026/gross-domestic-product-by-county-2025">
        Gross Domestic Product by County, 2025
      </a>
    </main></html>
    """
    release = """
    <html>
      <title>GDP (Third Estimate), 1st Quarter 2026 | U.S. Bureau of Economic Analysis (BEA)</title>
      <main>
        <p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Thursday, June 25, 2026</p>
        <table>
          <tr><th></th><th>Advance Estimate</th><th>Second Estimate</th><th>Third Estimate</th></tr>
          <tr><td>Real GDP</td><td>2.0</td><td>1.6</td><td>2.1</td></tr>
        </table>
      </main>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=release if "gdp-third-estimate" in request.url.path else listing,
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("bea.gdp.release"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    fact = batch.facts[0]
    assert isinstance(fact, ReleaseFact)
    assert fact.reference_period == "2026-Q1"
    assert fact.actual_value == 2.1
    assert fact.prior_value == 1.6
    assert fact.revised_prior_value == 2.1
    assert fact.estimate_value is None
    assert fact.published_at_ms == int(datetime(2026, 6, 25, 12, 30, tzinfo=UTC).timestamp() * 1_000)
    assert fact.scheduled_at_ms == fact.published_at_ms
    assert fact.source_url.endswith("/news/2026/gdp-third-estimate-1st-quarter-2026")


@pytest.mark.parametrize(
    ("dataset_id", "expected_actual", "expected_prior"),
    (
        ("bea.pce.release", 0.4, 0.4),
        ("bea.core_pce.release", 0.3, 0.3),
    ),
)
def test_bea_pce_release_page_preserves_headline_table_values(
    dataset_id: str,
    expected_actual: float,
    expected_prior: float,
) -> None:
    listing = """
    <html><main>
      <a href="/news/2026/personal-income-and-outlays-may-2026">
        Personal Income and Outlays, May 2026
      </a>
    </main></html>
    """
    release = """
    <html>
      <title>Personal Income and Outlays, May 2026 | U.S. Bureau of Economic Analysis (BEA)</title>
      <main>
        <p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Thursday, June 25, 2026</p>
        <table>
          <tr><th></th><th>April</th><th>May</th></tr>
          <tr><td>PCE price index</td><td>0.4</td><td>0.4</td></tr>
          <tr><td>PCE price index excluding food and energy</td><td>0.3</td><td>0.3</td></tr>
        </table>
      </main>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=release if "personal-income-and-outlays" in request.url.path else listing,
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset(dataset_id),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    fact = batch.facts[0]
    assert isinstance(fact, ReleaseFact)
    assert fact.reference_period == "2026-M05"
    assert fact.actual_value == expected_actual
    assert fact.prior_value == expected_prior
    assert fact.revised_prior_value is None
    assert fact.published_at_ms == int(datetime(2026, 6, 25, 12, 30, tzinfo=UTC).timestamp() * 1_000)


def test_federal_reserve_speech_adapter_fetches_official_full_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/newsevents/2026-speeches.htm"):
            return httpx.Response(
                200,
                text=('<a href="/newsevents/speech/example20260727a.htm">Monetary Policy</a>'),
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><title>Monetary Policy - Federal Reserve Board</title>"
                "<main><h1>Monetary Policy</h1><p>July 27, 2026 "
                "Chair Example Q. Official At the Policy Forum. "
                "The policy outlook depends on incoming inflation and labor-market evidence. "
                * 8
                + "</p></main></html>"
            ),
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.board.speeches"),
            partition_key="latest",
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    assert document.document_type == "speech"
    assert "incoming inflation and labor-market evidence" in document.content_text
    assert document.metadata["speaker_name"] == "Example Q. Official"
    assert document.source_url.startswith("https://www.federalreserve.gov/")


def test_treasury_curve_adapter_emits_one_series_fact_per_tenor() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns="http://www.w3.org/2005/Atom">
      <entry><content type="application/xml"><m:properties>
        <d:NEW_DATE m:type="Edm.DateTime">2026-07-24T00:00:00</d:NEW_DATE>
        <d:BC_1MONTH m:type="Edm.Double">4.31</d:BC_1MONTH>
        <d:BC_2YEAR m:type="Edm.Double">3.88</d:BC_2YEAR>
        <d:BC_10YEAR m:type="Edm.Double">4.42</d:BC_10YEAR>
        <d:BC_30YEAR m:type="Edm.Double">4.96</d:BC_30YEAR>
      </m:properties></content></entry>
    </feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("treasury.daily_nominal_curve"),
            partition_key="2026-07-24..2026-07-24",
            cursor={"start_date": "2026-07-24", "end_date": "2026-07-24"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert [(fact.series_id, fact.value_numeric) for fact in batch.facts] == [
        ("10Y", 4.42),
        ("1M", 4.31),
        ("2Y", 3.88),
        ("30Y", 4.96),
    ]
    assert batch.cursor["reference_date"] == "2026-07-24"


def test_fomc_calendar_adapter_stores_distinct_full_text_document_types() -> None:
    calendar = """
    <html><main>
      <a href="/newsevents/pressreleases/monetary20260729a.htm">Statement</a>
      <a href="/newsevents/pressreleases/monetary20260729a1.htm">Implementation Note</a>
      <a href="/monetarypolicy/fomcminutes20260617.htm">Minutes</a>
      <a href="/monetarypolicy/fomcprojtabl20260617.htm">Projection Materials</a>
    </main></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fomccalendars.htm"):
            return httpx.Response(200, text=calendar, request=request)
        return httpx.Response(
            200,
            text=(
                f"<html><title>{request.url.path}</title><main>"
                "<p>Release Date: July 8, 2026</p>"
                f"<p>{'Official policy body. ' * 30}</p></main></html>"
            ),
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert {fact.document_type for fact in batch.facts} == {
        "statement",
        "implementation",
        "minutes",
        "sep",
    }
    assert all(len(fact.content_text) > 200 for fact in batch.facts)


def test_fomc_calendar_adapter_extracts_official_sep_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calendar = """
    <html><main>
      <a href="/monetarypolicy/files/fomcprojtabl20260617.pdf">
        Projection Materials
      </a>
    </main></html>
    """
    monkeypatch.setattr(
        macro_source_client,
        "_extract_pdf_text",
        lambda payload: "Official Summary of Economic Projections. " * 12,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fomccalendars.htm"):
            return httpx.Response(200, text=calendar, request=request)
        return httpx.Response(200, content=b"%PDF-1.7 test", request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    assert document.document_type == "sep"
    assert document.metadata["body_source"] == "official_pdf"
    assert len(document.content_text) > 200


def test_fomc_minutes_capture_effective_meeting_role_and_voting_records() -> None:
    calendar = """
    <html><main>
      <a href="/monetarypolicy/fomcminutes20260617.htm">Minutes</a>
    </main></html>
    """
    minutes = """
    <html><title>Minutes</title><main>
      <p>Release Date: July 8, 2026</p>
      <p>Official policy body. Official policy body. Official policy body.</p>
      <p><strong>Attendance</strong><br />
      Example Chair, Chair<br />
      2 Example Voter<br />
      3</p>
      <p>Example Alternate, Alternate Members of the Committee</p>
      <p>Example President, Presidents of the Federal Reserve Banks of Example, respectively</p>
    </main></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=calendar if request.url.path.endswith("fomccalendars.htm") else minutes,
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    roles = document.metadata["fomc_role_records"]
    assert [(role["official_name"], role["fomc_voter"]) for role in roles] == [
        ("Example Chair", True),
        ("Example Voter", True),
        ("Example Alternate", False),
        ("Example President", False),
    ]


def test_fomc_minutes_capture_legacy_present_role_and_voting_records() -> None:
    calendar = """
    <html><main>
      <a href="/monetarypolicy/fomcminutes20210728.htm">Minutes</a>
    </main></html>
    """
    minutes = """
    <html><title>Minutes</title><main>
      <p>Release Date: August 18, 2021</p>
      <p>Official policy body. Official policy body. Official policy body.</p>
      <p><strong>PRESENT:</strong></p>
      <p>
      Example Chair, Chair<br />
      2 Example Voter<br />
      3</p>
      <p>Example Alternate, Alternate Members of the Committee</p>
      <p>Example President, Presidents of the Federal Reserve Banks of Example, respectively</p>
    </main></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=calendar if request.url.path.endswith("fomccalendars.htm") else minutes,
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="2021-07-27..2021-07-28",
            cursor={"start_date": "2021-07-27", "end_date": "2021-07-28"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    roles = document.metadata["fomc_role_records"]
    assert [(role["official_name"], role["fomc_voter"]) for role in roles] == [
        ("Example Chair", True),
        ("Example Voter", True),
        ("Example Alternate", False),
        ("Example President", False),
    ]


def test_reserve_bank_sitemap_adapter_accepts_only_official_full_speech_pages() -> None:
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://www.bostonfed.org/news-and-events/speeches/2026/07/policy-outlook</loc>
        <lastmod>2026-07-24</lastmod>
      </url>
      <url>
        <loc>https://www.bostonfed.org/news-and-events/press-releases/2026/07/other</loc>
        <lastmod>2026-07-24</lastmod>
      </url>
    </urlset>"""
    speech = """
    <html><head>
      <title>Policy Outlook | Federal Reserve Bank of Boston</title>
      <meta name="author" content="Example President" />
      <meta property="article:published_time" content="2026-07-24T09:00:00-04:00" />
    </head><main><h1>Policy Outlook</h1><p>
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    The inflation and employment outlook will guide monetary policy and the policy rate.
    </p></main></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="Sitemap: https://www.bostonfed.org/sitemap.xml\n",
                request=request,
            )
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap, request=request)
        return httpx.Response(200, text=speech, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.reserve_bank.speeches"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    document = batch.facts[0]
    assert isinstance(document, DocumentFact)
    assert document.metadata["speaker_name"] == "Example President"
    assert document.metadata["body_source"] == "official_reserve_bank_page"
    assert document.source_url.startswith("https://www.bostonfed.org/")
    assert len(document.content_text) > 500


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
    assert fact.fact_schema_version == "market_settlement_v2"
    assert fact.contract_code == "VX30/N6"
    assert fact.contract_expiration_date == date(2026, 7, 29)
    assert fact.settlement_price == 19.231
    assert requests[0].url.params["dt"] == "2026-07-24"


def test_cfe_settlement_skips_a_published_but_empty_current_file() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params["dt"] == "2026-07-27":
            return httpx.Response(
                200,
                text="Product,Symbol,Expiration Date,Price\n",
                request=request,
            )
        return httpx.Response(
            200,
            text="Product,Symbol,Expiration Date,Price\nVX,VX30/N6,2026-07-29,19.231\n",
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("cboe.cfe.vx.settlement"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert batch.facts[0].trade_date == date(2026, 7, 24)
    assert [request.url.params["dt"] for request in requests] == [
        "2026-07-27",
        "2026-07-24",
    ]


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
    assert require_dataset("binance.btcusdt.spot").clock_kind == "daily_settlement"
    assert require_dataset("binance.btcusdt.spot").frequency == "daily"


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


def test_nasdaq_daily_history_requests_five_year_window_and_emits_daily_facts() -> None:
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
                            {"date": "07/24/2026", "close": "$638.47"},
                            {"date": "07/23/2026", "close": "$635.34"},
                        ]
                    }
                },
            },
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("nasdaq.spy.daily"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert [fact.value_numeric for fact in batch.facts] == [635.34, 638.47]
    assert all(isinstance(fact, MarketObservationFact) for fact in batch.facts)
    assert requests[0].url.params["fromdate"] == "2021-07-27"
    assert requests[0].url.params["todate"] == "2026-07-27"
    assert requests[0].url.params["limit"] == "5000"


def test_yfinance_intraday_emits_market_time_facts_and_switches_to_incremental_period() -> None:
    calls: list[dict[str, object]] = []

    def load(symbol: str, **kwargs: object) -> pd.DataFrame:
        calls.append({"symbol": symbol, **kwargs})
        return pd.DataFrame(
            {
                "Open": [735.0, 738.0],
                "High": [736.0, 739.0],
                "Low": [734.0, 737.0],
                "Close": [735.34, 738.93],
                "Volume": [1_000.0, 2_000.0],
            },
            index=pd.to_datetime(
                ["2026-07-27T19:50:00-04:00", "2026-07-27T19:55:00-04:00"],
                utc=True,
            ),
        )

    client = MacroSourceClient(yfinance_history_loader=load)
    try:
        initial = client.fetch(
            require_dataset("yfinance.spy.intraday"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
        incremental = client.fetch(
            require_dataset("yfinance.spy.intraday"),
            partition_key="latest",
            cursor=initial.cursor,
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(initial.facts) == 2
    assert all(isinstance(fact, MarketObservationFact) for fact in initial.facts)
    assert [fact.value_numeric for fact in initial.facts] == [735.34, 738.93]
    assert initial.cursor == {"observed_at_ms": initial.facts[-1].observed_at_ms}
    assert initial.diagnostics["latest_market_at_ms"] == initial.facts[-1].observed_at_ms
    assert initial.facts[-1].source_id == "yahoo_finance"
    assert initial.facts[-1].raw_data["interval"] == "5m"
    assert calls[0]["period"] == "1mo"
    assert calls[1]["period"] == "1d"
    assert incremental.response_hash == initial.response_hash


def test_yfinance_daily_continuous_proxy_uses_five_year_then_monthly_period() -> None:
    calls: list[dict[str, object]] = []

    def load(symbol: str, **kwargs: object) -> pd.DataFrame:
        calls.append({"symbol": symbol, **kwargs})
        return pd.DataFrame(
            {
                "Open": [6_300.0],
                "High": [6_350.0],
                "Low": [6_290.0],
                "Close": [6_340.5],
                "Volume": [1_000.0],
            },
            index=pd.to_datetime(["2026-07-24T00:00:00Z"], utc=True),
        )

    client = MacroSourceClient(yfinance_history_loader=load)
    try:
        initial = client.fetch(
            require_dataset("yfinance.es_future.daily"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
        client.fetch(
            require_dataset("yfinance.es_future.daily"),
            partition_key="latest",
            cursor=initial.cursor,
            now_ms=NOW_MS,
        )
        bounded = client.fetch(
            require_dataset("yfinance.es_future.daily"),
            partition_key="2021-07-27..2026-07-27",
            cursor={"start_date": "2021-07-27", "end_date": "2026-07-27"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert calls[0]["period"] == "5y"
    assert calls[0]["interval"] == "1d"
    assert calls[0]["prepost"] is False
    assert calls[1]["period"] == "1mo"
    assert calls[2]["period"] == "5y"
    assert initial.facts[0].dataset_id == "yfinance.es_future.daily"
    assert bounded.cursor["start_date"] == "2021-07-27"
    assert bounded.cursor["end_date"] == "2026-07-27"
    assert bounded.cursor["backfill_complete"] is True


def test_disabled_yfinance_source_is_explicitly_unavailable() -> None:
    client = MacroSourceClient(
        yfinance_enabled=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
    )
    try:
        with pytest.raises(MacroSourceUnavailable, match="yfinance_disabled"):
            client.fetch(
                require_dataset("yfinance.spy.intraday"),
                partition_key="latest",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()


def test_reserve_bank_speech_discovery_isolates_one_malformed_sitemap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text=("Sitemap: https://www.bostonfed.org/broken.xml\nSitemap: https://www.bostonfed.org/valid.xml\n"),
                request=request,
            )
        if request.url.path == "/broken.xml":
            return httpx.Response(200, text="<html><broken></html>", request=request)
        if request.url.path == "/valid.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://www.bostonfed.org/news-and-events/speeches/"
                    "1996/07/out-of-range-policy-remarks</loc><lastmod>2026-07-25</lastmod></url>"
                    "<url><loc>https://www.bostonfed.org/news-and-events/speeches/"
                    "2026/07/a-stale-policy-remarks</loc><lastmod>2026-07-23</lastmod></url>"
                    "<url><loc>https://www.bostonfed.org/news-and-events/speeches/"
                    "2026/07/policy-remarks</loc><lastmod>2026-07-24</lastmod></url>"
                    "</urlset>"
                ),
                request=request,
            )
        if request.url.path.endswith("/a-stale-policy-remarks"):
            return httpx.Response(404, request=request)
        if request.url.path.endswith("/out-of-range-policy-remarks"):
            raise AssertionError("out-of-range sitemap URL must not be fetched")
        if request.url.path.endswith("/policy-remarks"):
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>Policy Remarks</title>"
                    '<meta name="date" content="2026-07-24T10:00:00-04:00">'
                    '<meta name="author" content="Jane Official"></head><body><main>'
                    + "Monetary policy evidence. " * 40
                    + "</main></body></html>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    spec = require_dataset("federal_reserve.reserve_bank.speeches")
    narrowed_spec = replace(
        spec,
        metadata={**spec.metadata, "official_roots": ("https://www.bostonfed.org",)},
    )
    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            narrowed_spec,
            partition_key="latest",
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    assert batch.facts[0].metadata["speaker_name"] == "Jane Official"


def test_reserve_bank_backfill_finishes_after_last_empty_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text=f"Sitemap: https://{request.url.host}/sitemap.xml\n",
                request=request,
            )
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'),
                request=request,
            )
        return httpx.Response(404, request=request)

    spec = require_dataset("federal_reserve.reserve_bank.speeches")
    narrowed_spec = replace(
        spec,
        metadata={
            **spec.metadata,
            "official_roots": (
                "https://www.bostonfed.org",
                "https://www.dallasfed.org",
            ),
        },
    )
    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            narrowed_spec,
            partition_key="backfill",
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert batch.facts == ()
    assert batch.cursor["source_index"] == 0
    assert batch.cursor["backfill_complete"] is True
