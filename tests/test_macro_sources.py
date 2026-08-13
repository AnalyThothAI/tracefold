from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest

import tracefold.integrations.macro_sources.client as macro_source_client
from tracefold.integrations.macro_sources import (
    MacroSourceClient,
    MacroSourceError,
    MacroSourceUnavailable,
)
from tracefold.macro import DATASET_REGISTRY, DocumentFact, ReleaseFact, SeriesFact, require_dataset
from tracefold.macro.registry import MACRO_ACQUISITION_ADAPTER_IDS
from tracefold.market import MarketObservationFact, MarketPositionFact, MarketSettlementFact

NOW_MS = int(datetime(2026, 7, 27, 12, tzinfo=UTC).timestamp() * 1_000)
_REMOVED_ACQUISITION_ADAPTER_ID = min(MACRO_ACQUISITION_ADAPTER_IDS)


def _fetch_macro_continuations(
    client: MacroSourceClient,
    spec,
    *,
    cursor: dict[str, object],
    stop_on_facts: bool = True,
    max_turns: int = 20,
):
    batches = []
    next_cursor = cursor
    for _ in range(max_turns):
        batch = client.fetch(
            spec,
            partition_key="latest",
            cursor=next_cursor,
            now_ms=NOW_MS,
        )
        batches.append(batch)
        assert batch.diagnostics["request_count"] <= 4
        assert batch.diagnostics["decoded_bytes"] <= 25_000_000
        if (stop_on_facts and batch.facts) or batch.completion == "complete":
            return batch, batches
        next_cursor = batch.cursor
    raise AssertionError("macro continuation did not converge")


@pytest.mark.parametrize(
    ("registry_contract", "expected_drift"),
    [
        (
            MACRO_ACQUISITION_ADAPTER_IDS | {"unwired_test_adapter"},
            "missing=unwired_test_adapter",
        ),
        (
            MACRO_ACQUISITION_ADAPTER_IDS - {_REMOVED_ACQUISITION_ADAPTER_ID},
            f"extra={_REMOVED_ACQUISITION_ADAPTER_ID}",
        ),
    ],
)
def test_macro_source_client_constructor_fails_fast_on_dispatch_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    registry_contract: frozenset[str],
    expected_drift: str,
) -> None:
    monkeypatch.setattr(
        macro_source_client,
        "MACRO_ACQUISITION_ADAPTER_IDS",
        registry_contract,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="macro_source_adapter_contract_mismatch") as exc_info:
        MacroSourceClient()

    assert expected_drift in str(exc_info.value)


def test_macro_source_client_dispatches_the_exact_registry_adapter_contract() -> None:
    representative_specs = {}
    for spec in DATASET_REGISTRY.values():
        if spec.adapter_id in MACRO_ACQUISITION_ADAPTER_IDS:
            representative_specs.setdefault(spec.adapter_id, spec)
    assert frozenset(representative_specs) == MACRO_ACQUISITION_ADAPTER_IDS

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        for adapter_id in sorted(MACRO_ACQUISITION_ADAPTER_IDS):
            request_count = len(requests)
            try:
                client.fetch(
                    representative_specs[adapter_id],
                    partition_key="latest",
                    cursor={},
                    now_ms=NOW_MS,
                )
            except MacroSourceError as exc:
                assert "unsupported_macro_adapter" not in str(exc)
            assert len(requests) > request_count
    finally:
        client.close()


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


def test_fred_csv_accepts_valid_gzip_response() -> None:
    payload = b"DATE,DGS10\n2026-07-01,4.20\n2026-07-02,4.25\n"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(payload),
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("fred.dgs10"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert [fact.value_numeric for fact in batch.facts] == [4.20, 4.25]
    assert requests[0].url.params["cosd"] == "2026-07-13"


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


def test_federal_reserve_speech_adapter_continues_at_three_documents() -> None:
    requests: list[httpx.Request] = []
    links = "".join(
        f'<a href="/newsevents/speech/example2026072{index}a.htm">Policy {index}</a>' for index in range(1, 6)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/newsevents/2026-speeches.htm"):
            return httpx.Response(200, text=links, request=request)
        return httpx.Response(
            200,
            text=(
                "<html><title>Policy Outlook - Federal Reserve Board</title><main>"
                "<p>Chair Example Q. Official. "
                + "The policy outlook depends on inflation and employment evidence. " * 12
                + "</p></main></html>"
            ),
            request=request,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        first = client.fetch(
            require_dataset("federal_reserve.board.speeches"),
            partition_key="latest",
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
            now_ms=NOW_MS,
        )
        second = client.fetch(
            require_dataset("federal_reserve.board.speeches"),
            partition_key="latest",
            cursor=first.cursor,
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(first.facts) == 3
    assert first.completion == "continuation"
    assert first.diagnostics["request_count"] == 4
    assert len(second.facts) == 2
    assert second.completion == "complete"
    assert second.diagnostics["request_count"] == 3
    assert len(requests) == 7


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
        first = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
        second = client.fetch(
            require_dataset("federal_reserve.fomc.documents"),
            partition_key="latest",
            cursor=first.cursor,
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert first.completion == "continuation"
    assert second.completion == "complete"
    facts = first.facts + second.facts
    assert {fact.document_type for fact in facts} == {
        "statement",
        "implementation",
        "minutes",
        "sep",
    }
    assert all(len(fact.content_text) > 200 for fact in facts)


def test_fomc_schedule_adapter_emits_official_meeting_dates_without_fetching_documents() -> None:
    calendar = """
    <html><main>
      <div class="panel panel-default">
        <div class="panel-heading"><h4><a id="current">2026 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>July</strong></div>
          <div class="fomc-meeting__date">28-29</div>
          <a href="/newsevents/pressreleases/monetary20260729a.htm">Statement</a>
        </div>
        <div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">15-16*</div>
        </div>
      </div>
      <div class="panel panel-default">
        <div class="panel-heading"><h4><a id="future">2027 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>January</strong></div>
          <div class="fomc-meeting__date">26-27</div>
        </div>
      </div>
    </main></html>
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=calendar, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("federal_reserve.fomc.schedule"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(requests) == 1
    assert [type(fact) for fact in batch.facts] == [ReleaseFact, ReleaseFact, ReleaseFact]
    revisions = {fact.release_id.split(":", maxsplit=3)[1] for fact in batch.facts}
    assert len(revisions) == 1
    assert [
        (
            ":".join(fact.release_id.split(":")[2:]),
            fact.series_id,
            fact.reference_period,
            fact.scheduled_at_ms,
            fact.actual_value,
        )
        for fact in batch.facts
    ] == [
        ("2026-07-28:2026-07-29", "FOMC_MEETING", "2026-07-28..2026-07-29", None, None),
        ("2026-09-15:2026-09-16", "FOMC_MEETING_SEP", "2026-09-15..2026-09-16", None, None),
        ("2027-01-26:2027-01-27", "FOMC_MEETING", "2027-01-26..2027-01-27", None, None),
    ]
    assert all(fact.release_id.startswith("FOMC_CALENDAR:") for fact in batch.facts)
    assert batch.cursor == {
        "calendar_years": [2026, 2027],
        "latest_meeting_end": "2027-01-27",
    }


def test_treasury_auction_adapter_emits_typed_demand_metrics_for_completed_results() -> None:
    response_payload = {
        "data": [
            {
                "record_date": "2026-07-27",
                "cusip": "91282ABC1",
                "security_type": "Note",
                "security_term": "10-Year",
                "auction_date": "2026-07-27",
                "closing_time_comp": "01:00 PM",
                "bid_to_cover_ratio": "2.67",
                "high_yield": "4.321",
                "offering_amt": "42000000000",
                "comp_accepted": "40000000000",
                "indirect_bidder_accepted": "28000000000",
                "direct_bidder_accepted": "5000000000",
                "primary_dealer_accepted": "7000000000",
                "pdf_filenm_comp_results": "R_20260727_1.pdf",
            },
            {
                "record_date": "2026-08-03",
                "cusip": "91282FUT1",
                "security_type": "Note",
                "security_term": "10-Year",
                "auction_date": "2026-08-03",
                "closing_time_comp": "01:00 PM",
                "bid_to_cover_ratio": "null",
                "high_yield": "null",
                "offering_amt": "42000000000",
                "comp_accepted": "null",
                "indirect_bidder_accepted": "null",
                "direct_bidder_accepted": "null",
                "primary_dealer_accepted": "null",
                "pdf_filenm_comp_results": "null",
            },
        ],
        "meta": {"total-count": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("treasury.auction.results"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert [(fact.series_id, fact.actual_value, fact.unit) for fact in batch.facts] == [
        ("10_YEAR:bid_to_cover", 2.67, "ratio"),
        ("10_YEAR:direct_award_share", 12.5, "percent"),
        ("10_YEAR:high_yield", 4.321, "percent"),
        ("10_YEAR:indirect_award_share", 70.0, "percent"),
        ("10_YEAR:offering_amount", 42_000_000_000.0, "usd"),
        ("10_YEAR:primary_dealer_award_share", 17.5, "percent"),
    ]
    assert {fact.release_id for fact in batch.facts} == {"TREASURY_AUCTION:91282ABC1:2026-07-27"}
    assert all(fact.scheduled_at_ms == 1785171600000 for fact in batch.facts)
    assert all(fact.published_at_ms is None for fact in batch.facts)
    assert batch.cursor == {"auction_date": "2026-07-27", "record_date": "2026-07-27"}


def test_treasury_bill_preserves_each_source_rate_as_a_distinct_typed_fact() -> None:
    response_payload = {
        "data": [
            {
                "record_date": "2026-08-10",
                "cusip": "912797QJ2",
                "security_type": "Bill",
                "security_term": "13-Week",
                "auction_date": "2026-08-10",
                "closing_time_comp": "11:30 AM",
                "bid_to_cover_ratio": "2.880000",
                "high_discnt_rate": "3.735000",
                "high_investment_rate": "3.823000",
                "high_yield": "3.901000",
                "offering_amt": "85000000000",
                "comp_accepted": "null",
                "pdf_filenm_comp_results": "R_20260810_1.pdf",
            }
        ],
        "meta": {"total-count": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        batch = client.fetch(
            require_dataset("treasury.auction.results"),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    rate_facts = [
        fact
        for fact in batch.facts
        if fact.series_id
        in {
            "13_WEEK:high_discount_rate",
            "13_WEEK:high_investment_rate",
            "13_WEEK:high_yield",
        }
    ]
    assert all(isinstance(fact, ReleaseFact) for fact in rate_facts)
    assert [
        (
            fact.series_id,
            fact.actual_value,
            fact.raw_data["source_field"],
            fact.raw_data["source_value"],
        )
        for fact in rate_facts
    ] == [
        ("13_WEEK:high_discount_rate", 3.735, "high_discnt_rate", "3.735000"),
        ("13_WEEK:high_investment_rate", 3.823, "high_investment_rate", "3.823000"),
        ("13_WEEK:high_yield", 3.901, "high_yield", "3.901000"),
    ]


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
        batch, batches = _fetch_macro_continuations(
            client,
            require_dataset("federal_reserve.reserve_bank.speeches"),
            cursor={},
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
    assert [item.diagnostics["request_count"] for item in batches] == [1, 2]


def test_missing_cfe_daily_file_is_retryable_not_permanently_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        first = client.fetch(
            require_dataset("cboe.cfe.vx.settlement"),
            partition_key="2026-07-27",
            cursor={},
            now_ms=NOW_MS,
        )
        assert first.completion == "continuation"
        with pytest.raises(MacroSourceError, match="file_not_published") as error:
            client.fetch(
                require_dataset("cboe.cfe.vx.settlement"),
                partition_key="2026-07-27",
                cursor=first.cursor,
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


def test_nasdaq_malformed_success_json_is_a_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{", request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MacroSourceError, match=r"^nasdaq_history_payload_invalid$"):
            client.fetch(
                require_dataset("nasdaq.spy.daily"),
                partition_key="latest",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("dataset_id", "error_code"),
    [
        ("binance.btcusdt.spot", "binance_spot_payload_invalid"),
        ("cftc.tff.rates_positions", "cftc_tff_payload_invalid"),
        ("bls.unemployment.release", "bls_release_payload_invalid"),
        ("yfinance.spy.intraday", "yfinance_history_payload_invalid"),
        ("treasury.auction.results", "treasury_auction_json_invalid"),
    ],
)
def test_json_providers_translate_malformed_success_payloads(
    dataset_id: str,
    error_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{", request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MacroSourceError) as exc_info:
            client.fetch(
                require_dataset(dataset_id),
                partition_key="latest",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()

    assert str(exc_info.value) == error_code


def test_yfinance_intraday_emits_market_time_facts_and_switches_to_incremental_period() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _yahoo_chart_response(
            request,
            timestamps=(
                int(datetime(2026, 7, 27, 23, 50, tzinfo=UTC).timestamp()),
                int(datetime(2026, 7, 27, 23, 55, tzinfo=UTC).timestamp()),
            ),
            opens=(735.0, 738.0),
            highs=(736.0, 739.0),
            lows=(734.0, 737.0),
            closes=(735.34, 738.93),
            volumes=(1_000.0, 2_000.0),
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
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
    assert calls[0].url.params["range"] == "1mo"
    assert calls[1].url.params["range"] == "1d"
    assert all(batch.diagnostics["request_count"] == 1 for batch in (initial, incremental))
    assert initial.diagnostics["decoded_bytes"] > 0
    assert incremental.response_hash == initial.response_hash


@pytest.mark.parametrize(
    "dataset_id",
    ("yfinance.es_future.intraday", "yfinance.btc_yahoo.intraday"),
)
def test_yfinance_high_session_initial_fetch_stays_within_the_fact_budget(
    dataset_id: str,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        row_count = {"1mo": 5_001, "5d": 1_440, "1d": 288}[request.url.params["range"]]
        first_timestamp = int(datetime(2026, 7, 21, tzinfo=UTC).timestamp())
        timestamps = tuple(first_timestamp + index * 300 for index in range(row_count))
        prices = tuple(6_300.0 + index for index in range(row_count))
        return _yahoo_chart_response(
            request,
            timestamps=timestamps,
            opens=prices,
            highs=prices,
            lows=prices,
            closes=prices,
            volumes=prices,
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    try:
        initial = client.fetch(
            require_dataset(dataset_id),
            partition_key="latest",
            cursor={},
            now_ms=NOW_MS,
        )
        incremental = client.fetch(
            require_dataset(dataset_id),
            partition_key="latest",
            cursor=initial.cursor,
            now_ms=NOW_MS,
        )
    finally:
        client.close()

    assert len(initial.facts) == 1_440
    assert len(incremental.facts) == 288
    assert [call.url.params["range"] for call in calls] == ["5d", "1d"]
    assert all(call.url.params["interval"] == "5m" for call in calls)


def test_yfinance_intraday_registry_has_one_code_owned_window_per_market_clock() -> None:
    intraday = {
        spec.dataset_id: spec
        for spec in DATASET_REGISTRY.values()
        if spec.adapter_id == "yfinance_history" and spec.frequency == "intraday"
    }
    high_session_dataset_ids = {
        "yfinance.btc_yahoo.intraday",
        "yfinance.cl_future.intraday",
        "yfinance.dx_future.intraday",
        "yfinance.es_future.intraday",
        "yfinance.gc_future.intraday",
        "yfinance.hg_future.intraday",
        "yfinance.nq_future.intraday",
        "yfinance.rty_future.intraday",
        "yfinance.zb_future.intraday",
        "yfinance.zn_future.intraday",
    }
    monthly_dataset_ids = {
        "yfinance.dxy.intraday",
        "yfinance.gld.intraday",
        "yfinance.hyg.intraday",
        "yfinance.ief.intraday",
        "yfinance.iwm.intraday",
        "yfinance.lqd.intraday",
        "yfinance.qqq.intraday",
        "yfinance.spy.intraday",
        "yfinance.tlt.intraday",
        "yfinance.uso.intraday",
        "yfinance.vix_index.intraday",
    }

    assert set(intraday) == high_session_dataset_ids | monthly_dataset_ids
    assert all(intraday[dataset_id].metadata["initial_period"] == "5d" for dataset_id in high_session_dataset_ids)
    assert all(intraday[dataset_id].metadata["initial_period"] == "1mo" for dataset_id in monthly_dataset_ids)
    assert all(spec.metadata["incremental_period"] == "1d" for spec in intraday.values())


@pytest.mark.parametrize(
    ("metadata_key", "error_code"),
    (
        ("bar_interval", "yfinance_history_bar_interval_missing"),
        ("initial_period", "yfinance_history_initial_period_missing"),
        ("incremental_period", "yfinance_history_incremental_period_missing"),
    ),
)
def test_yfinance_history_requires_code_owned_window_metadata(
    metadata_key: str,
    error_code: str,
) -> None:
    spec = require_dataset("yfinance.es_future.intraday")
    metadata = dict(spec.metadata)
    metadata.pop(metadata_key)
    malformed_spec = replace(spec, metadata=metadata)

    client = MacroSourceClient(
        transport=httpx.MockTransport(
            lambda request: _yahoo_chart_response(
                request,
                timestamps=(int(datetime(2026, 7, 27, 23, 55, tzinfo=UTC).timestamp()),),
                opens=(6_300.0,),
                highs=(6_300.0,),
                lows=(6_300.0,),
                closes=(6_300.0,),
                volumes=(1_000.0,),
            )
        )
    )
    try:
        with pytest.raises(MacroSourceError, match=error_code):
            client.fetch(
                malformed_spec,
                partition_key="latest",
                cursor={},
                now_ms=NOW_MS,
            )
    finally:
        client.close()


def test_yfinance_daily_continuous_proxy_uses_five_year_then_monthly_period() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _yahoo_chart_response(
            request,
            timestamps=(int(datetime(2026, 7, 24, tzinfo=UTC).timestamp()),),
            opens=(6_300.0,),
            highs=(6_350.0,),
            lows=(6_290.0,),
            closes=(6_340.5,),
            volumes=(1_000.0,),
        )

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
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

    assert calls[0].url.params["range"] == "5y"
    assert calls[0].url.params["interval"] == "1d"
    assert calls[0].url.params["includePrePost"] == "false"
    assert calls[1].url.params["range"] == "1mo"
    assert calls[2].url.params["range"] == "5y"
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


def test_source_client_exposes_the_exact_enabled_adapter_set_for_startup_reconciliation() -> None:
    client = MacroSourceClient(
        fred_enabled=False,
        cboe_enabled=False,
        cftc_enabled=False,
        nasdaq_daily_enabled=False,
        yfinance_enabled=False,
    )
    try:
        assert client.enabled_adapter_ids == MACRO_ACQUISITION_ADAPTER_IDS - {
            "fred_csv",
            "cfe_settlement",
            "cftc_tff",
            "nasdaq_history",
            "yfinance_history",
        }
    finally:
        client.close()


def _yahoo_chart_response(
    request: httpx.Request,
    *,
    timestamps: tuple[int, ...],
    opens: tuple[float, ...],
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    closes: tuple[float, ...],
    volumes: tuple[float, ...],
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "chart": {
                "result": [
                    {
                        "timestamp": list(timestamps),
                        "indicators": {
                            "quote": [
                                {
                                    "open": list(opens),
                                    "high": list(highs),
                                    "low": list(lows),
                                    "close": list(closes),
                                    "volume": list(volumes),
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        },
        request=request,
    )


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
        batch, batches = _fetch_macro_continuations(
            client,
            narrowed_spec,
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
        )
    finally:
        client.close()

    assert len(batch.facts) == 1
    assert batch.facts[0].metadata["speaker_name"] == "Jane Official"
    assert [item.diagnostics["request_count"] for item in batches] == [1, 1, 3]


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
        batch, batches = _fetch_macro_continuations(
            client,
            narrowed_spec,
            cursor={"start_date": "2026-07-01", "end_date": "2026-07-27"},
            stop_on_facts=False,
        )
    finally:
        client.close()

    assert batch.facts == ()
    assert batch.cursor["source_index"] == 0
    assert batch.cursor["backfill_complete"] is True
    assert [item.diagnostics["request_count"] for item in batches] == [1, 1, 1, 1]
