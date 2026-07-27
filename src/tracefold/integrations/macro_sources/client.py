from __future__ import annotations

import csv
import gzip
import hashlib
import io
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from functools import partial
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import requests
from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from tracefold.macro import (
    DatasetSpec,
    DocumentFact,
    FetchBatch,
    MacroSourceError,
    MacroSourceUnavailable,
    ReleaseFact,
    SeriesFact,
)
from tracefold.market import (
    MarketObservationFact,
    MarketPositionFact,
    MarketSettlementFact,
)


class MacroSourceClient:
    """Fetch one registry target without owning scheduling or persistence."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        user_agent: str = "TracefoldMacro/1.0 research@localhost",
        fred_enabled: bool = True,
        cboe_enabled: bool = True,
        cftc_enabled: bool = True,
        nasdaq_public_enabled: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"User-Agent": user_agent, "Accept": "text/csv,application/json"}
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers=headers,
            transport=transport,
        )
        self._timeout_seconds = float(timeout_seconds)
        self._fred_session: requests.Session | None = None
        if transport is None:
            self._fred_session = requests.Session()
            self._fred_session.headers.update(headers)
        self._fred_enabled = bool(fred_enabled)
        self._cboe_enabled = bool(cboe_enabled)
        self._cftc_enabled = bool(cftc_enabled)
        self._nasdaq_public_enabled = bool(nasdaq_public_enabled)

    def close(self) -> None:
        self._client.close()
        if self._fred_session is not None:
            self._fred_session.close()

    def fetch(
        self,
        spec: DatasetSpec,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch:
        received_at_ms = int(now_ms if now_ms is not None else time.time() * 1_000)
        if spec.adapter_id == "fred_csv" and not self._fred_enabled:
            raise MacroSourceUnavailable("fred_disabled")
        if spec.adapter_id == "cfe_settlement" and not self._cboe_enabled:
            raise MacroSourceUnavailable("cboe_disabled")
        if spec.adapter_id == "cftc_tff" and not self._cftc_enabled:
            raise MacroSourceUnavailable("cftc_disabled")
        if spec.adapter_id == "nasdaq_history" and not self._nasdaq_public_enabled:
            raise MacroSourceUnavailable("nasdaq_public_disabled")
        if spec.adapter_id == "fred_csv":
            return self._fetch_fred(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "treasury_curve_xml":
            return self._fetch_treasury_curve(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "nasdaq_history":
            return self._fetch_nasdaq_history(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "binance_spot":
            return self._fetch_binance_spot(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "cfe_settlement":
            return self._fetch_cfe_settlement(spec, partition_key, received_at_ms)
        if spec.adapter_id == "cftc_tff":
            return self._fetch_cftc_tff(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "bls_release":
            return self._fetch_bls_release(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "fed_board_speech_archive":
            return self._fetch_board_speech_archive(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "fed_fomc_calendar":
            return self._fetch_fed_fomc_calendar(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "fed_reserve_bank_sitemaps":
            return self._fetch_reserve_bank_speeches(spec, partition_key, cursor, received_at_ms)
        if spec.adapter_id == "unavailable":
            reason = str(spec.metadata.get("unavailable_reason") or "source_not_configured")
            raise MacroSourceUnavailable(reason)
        raise MacroSourceError(f"unsupported_macro_adapter:{spec.adapter_id}")

    def _fetch_fred(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        params: dict[str, str] = {"id": spec.series_id}
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        cursor_date = _optional_date(cursor.get("reference_date")) or start_date
        if cursor_date is not None:
            params["cosd"] = str(cursor_date if start_date else cursor_date - timedelta(days=7))
        if end_date is not None:
            params["coed"] = str(end_date)
        response: httpx.Response | requests.Response
        if self._fred_session is None:
            response = self._client.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params=params,
            )
        else:
            response = self._fred_session.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params=params,
                timeout=self._timeout_seconds,
            )
        _require_success(response, source_id=spec.source_id)
        rows = list(csv.DictReader(io.StringIO(response.text)))
        facts: list[SeriesFact] = []
        latest_date: date | None = cursor_date
        vintage_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        for row in rows:
            reference_date = _optional_date(row.get("DATE") or row.get("observation_date"))
            if reference_date is None:
                continue
            raw_value = row.get(spec.series_id)
            value = _finite_float(raw_value)
            if value is None:
                continue
            facts.append(
                SeriesFact(
                    dataset_id=spec.dataset_id,
                    series_id=spec.series_id,
                    reference_date=reference_date,
                    vintage_date=vintage_date,
                    value_numeric=value,
                    value_text=None,
                    unit=spec.unit,
                    published_at_ms=None,
                    received_at_ms=received_at_ms,
                    source_url=str(response.url),
                    raw_data={"date": str(reference_date), "value": raw_value},
                )
            )
            latest_date = max(latest_date or reference_date, reference_date)
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor=_series_cursor(
                latest_date,
                start_date=start_date,
                end_date=end_date,
                backfill_complete=True if start_date is not None and end_date is not None else None,
            ),
        )

    def _fetch_nasdaq_history(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        cursor_date = _optional_date(cursor.get("reference_date")) or start_date
        lower_bound = start_date or (
            cursor_date - timedelta(days=7) if cursor_date is not None else received_date - timedelta(days=1_825)
        )
        upper_bound = end_date or received_date
        response = self._client.get(
            spec.source_url,
            params={
                "assetclass": "etf",
                "fromdate": lower_bound.isoformat(),
                "todate": upper_bound.isoformat(),
                "limit": "5000",
            },
            headers={"Accept": "application/json, text/plain, */*"},
        )
        _require_success(response, source_id=spec.source_id)
        payload = response.json()
        status_code = (
            payload.get("status", {}).get("rCode")
            if isinstance(payload, dict) and isinstance(payload.get("status"), dict)
            else None
        )
        rows = (
            payload.get("data", {}).get("tradesTable", {}).get("rows", [])
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict)
            else []
        )
        if status_code != 200 or not isinstance(rows, list):
            raise MacroSourceError("nasdaq_history_payload_invalid")
        facts: list[MarketObservationFact] = []
        latest_date: date | None = cursor_date
        for row in rows:
            if not isinstance(row, dict):
                continue
            observed_date = _optional_us_date(row.get("date"))
            close = _finite_float(str(row.get("close") or "").replace("$", "").replace(",", ""))
            if observed_date is None or close is None or spec.instrument_id is None:
                continue
            facts.append(
                MarketObservationFact(
                    dataset_id=spec.dataset_id,
                    instrument_id=spec.instrument_id,
                    source_id=spec.source_id,
                    field_name="close",
                    value_numeric=close,
                    unit=spec.unit,
                    observed_at_ms=_date_close_ms(observed_date),
                    published_at_ms=None,
                    received_at_ms=received_at_ms,
                    trust_tier=spec.trust_tier,
                    source_url=str(response.url),
                    raw_data=dict(row),
                )
            )
            latest_date = max(latest_date or observed_date, observed_date)
        facts.sort(key=lambda fact: fact.observed_at_ms)
        if not facts:
            raise MacroSourceError("nasdaq_history_no_valid_rows")
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor=_series_cursor(
                latest_date,
                start_date=start_date,
                end_date=end_date,
                backfill_complete=True if start_date is not None and end_date is not None else None,
            ),
        )

    def _fetch_treasury_curve(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        tenors = spec.metadata.get("tenors")
        curve_type = str(spec.metadata.get("curve_type") or "")
        if not isinstance(tenors, dict) or not tenors or not curve_type:
            raise MacroSourceError("treasury_curve_metadata_invalid")
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        first_year = (start_date or received_date).year
        last_year = (end_date or received_date).year
        year = max(
            first_year,
            min(_optional_int(cursor.get("year")) or first_year, last_year),
        )
        facts: list[SeriesFact] = []
        latest_date = _optional_date(cursor.get("reference_date"))
        vintage_date = received_date
        response = self._client.get(
            spec.source_url,
            params={"data": curve_type, "field_tdr_date_value": str(year)},
            headers={"Accept": "application/xml,text/xml"},
        )
        _require_success(response, source_id=spec.source_id)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise MacroSourceError("treasury_curve_xml_invalid") from exc
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            properties = entry.find(".//{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties")
            if properties is None:
                continue
            values = {_xml_local_name(child.tag): str(child.text or "").strip() for child in properties}
            reference_date = _optional_date(values.get("NEW_DATE"))
            if reference_date is None:
                continue
            if start_date is not None and reference_date < start_date:
                continue
            if end_date is not None and reference_date > end_date:
                continue
            for source_field, tenor in tenors.items():
                value = _finite_float(values.get(str(source_field)))
                if value is None:
                    continue
                facts.append(
                    SeriesFact(
                        dataset_id=spec.dataset_id,
                        series_id=str(tenor),
                        reference_date=reference_date,
                        vintage_date=vintage_date,
                        value_numeric=value,
                        value_text=None,
                        unit=spec.unit,
                        published_at_ms=None,
                        received_at_ms=received_at_ms,
                        source_url=str(response.url),
                        raw_data={
                            "curve_type": curve_type,
                            "source_field": str(source_field),
                            "tenor": str(tenor),
                            "date": reference_date.isoformat(),
                            "value": values.get(str(source_field)),
                        },
                    )
                )
            latest_date = max(latest_date or reference_date, reference_date)
        facts.sort(key=lambda fact: (fact.reference_date, fact.series_id))
        completed = year >= last_year
        next_year = year if completed else year + 1
        next_cursor = _series_cursor(
            latest_date,
            start_date=start_date,
            end_date=end_date,
            backfill_complete=completed if start_date is not None and end_date is not None else None,
        )
        next_cursor["year"] = next_year
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor=next_cursor,
        )

    def _fetch_binance_spot(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        params: dict[str, Any] = {"symbol": spec.series_id, "interval": "1d", "limit": 1_000}
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        observed_at_ms = _optional_int(cursor.get("observed_at_ms"))
        if observed_at_ms is None and start_date is not None:
            observed_at_ms = _date_open_ms(start_date)
        if observed_at_ms is not None:
            params["startTime"] = max(
                0,
                observed_at_ms if start_date else observed_at_ms - 7 * 86_400_000,
            )
        if end_date is not None:
            params["endTime"] = _date_end_ms(end_date)
        response = self._client.get(spec.source_url, params=params)
        _require_success(response, source_id=spec.source_id)
        payload = response.json()
        if not isinstance(payload, list):
            raise MacroSourceError("binance_spot_payload_invalid")
        facts: list[MarketObservationFact] = []
        latest_ms = observed_at_ms
        for raw in payload:
            if not isinstance(raw, list) or len(raw) < 7 or spec.instrument_id is None:
                continue
            close = _finite_float(raw[4])
            close_at_ms = _optional_int(raw[6])
            if close is None or close_at_ms is None or close_at_ms > received_at_ms:
                continue
            facts.append(
                MarketObservationFact(
                    dataset_id=spec.dataset_id,
                    instrument_id=spec.instrument_id,
                    source_id=spec.source_id,
                    field_name="close",
                    value_numeric=close,
                    unit=spec.unit,
                    observed_at_ms=close_at_ms,
                    published_at_ms=close_at_ms,
                    received_at_ms=received_at_ms,
                    trust_tier=spec.trust_tier,
                    source_url=str(response.url),
                    raw_data={"open_time_ms": raw[0], "close": raw[4], "close_time_ms": raw[6]},
                )
            )
            latest_ms = max(latest_ms or close_at_ms, close_at_ms)
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor={
                **({"observed_at_ms": latest_ms} if latest_ms is not None else {}),
                **({"start_date": start_date.isoformat()} if start_date is not None else {}),
                **({"end_date": end_date.isoformat()} if end_date is not None else {}),
                **(
                    {"backfill_complete": len(payload) < 1_000}
                    if start_date is not None and end_date is not None
                    else {}
                ),
            },
        )

    def _fetch_cfe_settlement(
        self,
        spec: DatasetSpec,
        partition_key: str,
        received_at_ms: int,
    ) -> FetchBatch:
        target_date = _optional_date(partition_key)
        if target_date is None:
            target_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        saw_published_file = False
        for offset in range(0, 8):
            candidate = target_date - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            attempted = self._client.get(spec.source_url, params={"dt": candidate.isoformat()})
            if attempted.status_code != 200:
                if attempted.status_code not in {403, 404}:
                    _require_success(attempted, source_id=spec.source_id)
                continue
            saw_published_file = True
            facts: list[MarketSettlementFact] = []
            for raw_row in csv.DictReader(io.StringIO(attempted.text)):
                row = {_normalize_column(key): value for key, value in raw_row.items()}
                contract_code = _first_text(row, "symbol", "contract", "futuresymbol", "product")
                product = _first_text(row, "product")
                settlement = _first_float(
                    row,
                    "settlementprice",
                    "settle",
                    "price",
                    "finalsettlement",
                )
                if (
                    not contract_code
                    or settlement is None
                    or spec.instrument_id is None
                    or (
                        str(product or "").upper() != spec.series_id
                        and not contract_code.upper().startswith(spec.series_id)
                    )
                ):
                    continue
                facts.append(
                    MarketSettlementFact(
                        dataset_id=spec.dataset_id,
                        instrument_id=spec.instrument_id,
                        source_id=spec.source_id,
                        trade_date=candidate,
                        contract_code=contract_code.upper(),
                        settlement_price=settlement,
                        open_interest=_first_float(row, "openinterest", "oi"),
                        volume=_first_float(row, "volume", "totalvolume"),
                        unit=spec.unit,
                        published_at_ms=None,
                        received_at_ms=received_at_ms,
                        source_url=str(attempted.url),
                        raw_data=dict(raw_row),
                    )
                )
            if facts:
                return _batch(
                    spec,
                    partition_key,
                    tuple(facts),
                    attempted,
                    cursor={"trade_date": str(candidate)},
                )
        if not saw_published_file:
            raise MacroSourceError("cfe_settlement_file_not_published")
        raise MacroSourceError("cfe_settlement_no_valid_rows")

    def _fetch_cftc_tff(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        contracts = spec.metadata.get("contracts")
        if not isinstance(contracts, dict) or not contracts:
            raise MacroSourceError("cftc_tff_contracts_missing")
        contract_labels = {str(code): str(label) for code, label in contracts.items()}
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        cursor_date = _optional_date(cursor.get("reference_date"))
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        lower_bound = start_date or (
            cursor_date - timedelta(days=35) if cursor_date is not None else received_date - timedelta(days=730)
        )
        contract_clause = ",".join(f"'{code}'" for code in contract_labels)
        where = [f"cftc_contract_market_code in ({contract_clause})"]
        if lower_bound is not None:
            where.append(f"report_date_as_yyyy_mm_dd >= '{lower_bound.isoformat()}T00:00:00'")
        if end_date is not None:
            where.append(f"report_date_as_yyyy_mm_dd <= '{end_date.isoformat()}T23:59:59'")
        response = self._client.get(
            spec.source_url,
            params={
                "$where": " AND ".join(where),
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": "50000",
            },
        )
        _require_success(response, source_id=spec.source_id)
        payload = response.json()
        if not isinstance(payload, list):
            raise MacroSourceError("cftc_tff_payload_invalid")
        facts: list[MarketPositionFact] = []
        latest_date = cursor_date
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            contract_code = str(raw.get("cftc_contract_market_code") or "")
            if contract_code not in contract_labels:
                continue
            report_date = _optional_date(raw.get("report_date_as_yyyy_mm_dd"))
            open_interest = _finite_float(raw.get("open_interest_all"))
            leveraged_long = _finite_float(raw.get("lev_money_positions_long"))
            leveraged_short = _finite_float(raw.get("lev_money_positions_short"))
            leveraged_long_pct = _finite_float(raw.get("pct_of_oi_lev_money_long"))
            leveraged_short_pct = _finite_float(raw.get("pct_of_oi_lev_money_short"))
            asset_manager_long_pct = _finite_float(raw.get("pct_of_oi_asset_mgr_long"))
            asset_manager_short_pct = _finite_float(raw.get("pct_of_oi_asset_mgr_short"))
            dealer_long_pct = _finite_float(raw.get("pct_of_oi_dealer_long_all"))
            dealer_short_pct = _finite_float(raw.get("pct_of_oi_dealer_short_all"))
            numeric_fields = (
                open_interest,
                leveraged_long,
                leveraged_short,
                leveraged_long_pct,
                leveraged_short_pct,
                asset_manager_long_pct,
                asset_manager_short_pct,
                dealer_long_pct,
                dealer_short_pct,
            )
            if report_date is None or any(value is None for value in numeric_fields):
                continue
            facts.append(
                MarketPositionFact(
                    dataset_id=spec.dataset_id,
                    contract_code=contract_code,
                    contract_name=contract_labels[contract_code],
                    report_date=report_date,
                    open_interest=float(open_interest),
                    leveraged_long=float(leveraged_long),
                    leveraged_short=float(leveraged_short),
                    leveraged_net_pct_oi=round(
                        float(leveraged_long_pct) - float(leveraged_short_pct),
                        4,
                    ),
                    asset_manager_net_pct_oi=round(
                        float(asset_manager_long_pct) - float(asset_manager_short_pct),
                        4,
                    ),
                    dealer_net_pct_oi=round(
                        float(dealer_long_pct) - float(dealer_short_pct),
                        4,
                    ),
                    published_at_ms=received_at_ms,
                    received_at_ms=received_at_ms,
                    source_url=str(response.url),
                    raw_data=dict(raw),
                )
            )
            latest_date = max(latest_date or report_date, report_date)
        facts.sort(key=lambda fact: (fact.contract_code, fact.report_date))
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor=_series_cursor(
                latest_date,
                start_date=start_date,
                end_date=end_date,
                backfill_complete=True if start_date is not None and end_date is not None else None,
            ),
        )

    def _fetch_bls_release(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        start_year = (start_date or received_date.replace(year=received_date.year - 1)).year
        end_year = (end_date or received_date).year
        response = self._client.post(
            spec.source_url,
            json={
                "seriesid": [spec.series_id],
                "startyear": str(start_year),
                "endyear": str(end_year),
                "calculations": False,
                "annualaverage": False,
            },
        )
        _require_success(response, source_id=spec.source_id)
        payload = response.json()
        series = payload.get("Results", {}).get("series", []) if isinstance(payload, dict) else []
        raw_rows = series[0].get("data", []) if series and isinstance(series[0], dict) else []
        rows = [
            row
            for row in raw_rows
            if isinstance(row, dict)
            and str(row.get("period") or "").startswith("M")
            and str(row.get("period")) != "M13"
            and _finite_float(row.get("value")) is not None
        ]
        rows.sort(key=lambda row: (str(row.get("year")), str(row.get("period"))))
        facts: list[ReleaseFact] = []
        latest_reference = str(cursor.get("reference_period") or "")
        for index, row in enumerate(rows):
            reference_period = f"{row['year']}-{row['period']}"
            if start_date is None and index < max(0, len(rows) - 2):
                continue
            previous = rows[index - 1] if index > 0 else None
            actual = _finite_float(row.get("value"))
            prior = _finite_float(previous.get("value")) if previous is not None else None
            facts.append(
                ReleaseFact(
                    dataset_id=spec.dataset_id,
                    release_id=f"BLS:{spec.series_id}:{reference_period}",
                    series_id=spec.series_id,
                    reference_period=reference_period,
                    scheduled_at_ms=None,
                    published_at_ms=received_at_ms,
                    received_at_ms=received_at_ms,
                    actual_value=actual,
                    prior_value=prior,
                    revised_prior_value=None,
                    estimate_value=None,
                    unit=spec.unit,
                    importance_tier=int(spec.metadata.get("importance_tier") or 2),
                    source_url=str(response.url),
                    raw_data=dict(row),
                )
            )
            latest_reference = max(latest_reference, reference_period)
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor={
                "reference_period": latest_reference,
                **({"start_date": start_date.isoformat()} if start_date else {}),
                **({"end_date": end_date.isoformat()} if end_date else {}),
                **({"backfill_complete": True} if start_date is not None and end_date is not None else {}),
            },
        )

    def _fetch_fed_fomc_calendar(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        first_year = (start_date or received_date).year
        last_year = (end_date or received_date).year
        year = max(
            first_year,
            min(_optional_int(cursor.get("year")) or first_year, last_year),
        )
        calendar_url = (
            spec.source_url
            if year >= received_date.year - 5
            else f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
        )
        document_links: dict[str, str] = {}
        response = self._client.get(
            calendar_url,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        _require_success(response, source_id=spec.source_id)
        for href, label in _html_links(response.text):
            absolute_url = urljoin(str(response.url), href)
            document_date = _date_from_fed_url(absolute_url)
            if document_date is None or document_date.year != year:
                continue
            if start_date is not None and document_date < start_date:
                continue
            if end_date is not None and document_date > end_date:
                continue
            if not _is_fomc_document_link(absolute_url, label):
                continue
            document_links[absolute_url] = label
        documents: list[DocumentFact] = []
        latest_published_at_ms = _optional_int(cursor.get("published_at_ms")) or 0
        document_items = sorted(document_links.items())
        with ThreadPoolExecutor(max_workers=min(8, len(document_items) or 1)) as executor:
            fetched_documents = executor.map(
                partial(
                    self._fetch_fomc_document,
                    spec,
                    calendar_url=calendar_url,
                    received_at_ms=received_at_ms,
                ),
                document_items,
            )
            for document in fetched_documents:
                if document is None:
                    continue
                documents.append(document)
                latest_published_at_ms = max(
                    latest_published_at_ms,
                    document.published_at_ms,
                )
        documents.sort(key=lambda item: (item.published_at_ms, item.document_id))
        completed = year >= last_year
        return _batch(
            spec,
            partition_key,
            tuple(documents),
            response,
            cursor={
                "published_at_ms": latest_published_at_ms,
                "year": year if completed else year + 1,
                **({"backfill_complete": completed} if start_date is not None and end_date is not None else {}),
                **({"start_date": start_date.isoformat()} if start_date else {}),
                **({"end_date": end_date.isoformat()} if end_date else {}),
            },
        )

    def _fetch_fomc_document(
        self,
        spec: DatasetSpec,
        document_item: tuple[str, str],
        *,
        calendar_url: str,
        received_at_ms: int,
    ) -> DocumentFact | None:
        url, link_label = document_item
        is_pdf = url.lower().endswith(".pdf")
        document_response = self._client.get(
            url,
            headers={"Accept": ("application/pdf" if is_pdf else "text/html,application/xhtml+xml")},
        )
        _require_success(document_response, source_id=spec.source_id)
        content_text = (
            _extract_pdf_text(document_response.content) if is_pdf else _extract_official_body(document_response.text)
        )
        if not content_text:
            return None
        effective_date = _date_from_fed_url(url)
        if effective_date is None:
            return None
        published_date = _release_date_from_body(content_text) or effective_date
        published_at_ms = min(_date_release_ms(published_date), received_at_ms)
        title = (
            link_label or _document_label(url)
            if is_pdf
            else _official_page_title(document_response.text) or link_label or _document_label(url)
        )
        content_hash = hashlib.sha256(content_text.encode()).hexdigest()
        document_id = "macrodoc_" + hashlib.sha256(f"{spec.dataset_id}|{url}|{content_hash}".encode()).hexdigest()
        return DocumentFact(
            document_id=document_id,
            dataset_id=spec.dataset_id,
            document_type=_document_type(f"{title} {url}"),
            title=title,
            effective_date=effective_date,
            published_at_ms=published_at_ms,
            received_at_ms=max(received_at_ms, published_at_ms),
            source_url=url,
            content_text=content_text[:100_000],
            metadata={
                "calendar_url": calendar_url,
                "link_label": link_label,
                "content_hash": f"sha256:{content_hash}",
                "body_source": "official_pdf" if is_pdf else "official_page",
                "fomc_role_records": ([] if is_pdf else _extract_fomc_role_records(document_response.text)),
            },
        )

    def _fetch_board_speech_archive(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        lower_bound = start_date or received_date - timedelta(days=366)
        upper_bound = end_date or received_date
        year = min(
            _optional_int(cursor.get("year")) or upper_bound.year,
            upper_bound.year,
        )
        year = max(year, lower_bound.year)
        url_after = str(cursor.get("url_after") or "")
        archive_url = f"https://www.federalreserve.gov/newsevents/{year}-speeches.htm"
        archive_response = self._client.get(
            archive_url,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        _require_success(archive_response, source_id=spec.source_id)
        discovered: dict[str, tuple[date, str]] = {}
        for href, label in _html_links(archive_response.text):
            source_url = urljoin(str(archive_response.url), href)
            parsed = urlparse(source_url)
            if (
                parsed.netloc.lower().removeprefix("www.") != "federalreserve.gov"
                or "/newsevents/speech/" not in parsed.path.lower()
            ):
                continue
            published_date = _date_from_url_or_text(source_url)
            if published_date is None or not (lower_bound <= published_date <= upper_bound):
                continue
            discovered.setdefault(source_url, (published_date, label))
        candidates = sorted(url for url in discovered if not url_after or url > url_after)
        selected = candidates[:60]
        documents: list[DocumentFact] = []
        latest_published_at_ms = _optional_int(cursor.get("published_at_ms")) or 0
        for source_url in selected:
            published_date, link_label = discovered[source_url]
            page_response = self._client.get(
                source_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            _require_success(page_response, source_id=spec.source_id)
            content_text = _extract_official_body(page_response.text)
            if len(content_text) < 500:
                continue
            title = _official_page_title(page_response.text) or link_label
            speaker_name = _official_speech_speaker(
                page_response.text,
                title,
                content_text,
            )
            content_hash = hashlib.sha256(content_text.encode()).hexdigest()
            document_id = (
                "macrodoc_" + hashlib.sha256(f"{spec.dataset_id}|{source_url}|{content_hash}".encode()).hexdigest()
            )
            published_at_ms = min(_date_release_ms(published_date), received_at_ms)
            documents.append(
                DocumentFact(
                    document_id=document_id,
                    dataset_id=spec.dataset_id,
                    document_type="speech",
                    title=title,
                    effective_date=published_date,
                    published_at_ms=published_at_ms,
                    received_at_ms=max(received_at_ms, published_at_ms),
                    source_url=source_url,
                    content_text=content_text[:100_000],
                    metadata={
                        "archive_url": archive_url,
                        "speaker_name": speaker_name,
                        "content_hash": f"sha256:{content_hash}",
                        "body_source": "official_board_page",
                    },
                )
            )
            latest_published_at_ms = max(latest_published_at_ms, published_at_ms)
        if len(candidates) > len(selected):
            next_year = year
            next_url_after = selected[-1]
            completed = False
        elif year > lower_bound.year:
            next_year = year - 1
            next_url_after = ""
            completed = False
        else:
            next_year = year
            next_url_after = ""
            completed = True
        documents.sort(key=lambda item: (item.published_at_ms, item.document_id))
        return _batch(
            spec,
            partition_key,
            tuple(documents),
            archive_response,
            cursor={
                "year": next_year,
                "url_after": next_url_after,
                "published_at_ms": latest_published_at_ms,
                "reference_date": (upper_bound.isoformat() if completed else lower_bound.isoformat()),
                **({"backfill_complete": completed} if start_date is not None and end_date is not None else {}),
                **({"start_date": start_date.isoformat()} if start_date else {}),
                **({"end_date": end_date.isoformat()} if end_date else {}),
            },
        )

    def _fetch_reserve_bank_speeches(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        raw_roots = spec.metadata.get("official_roots")
        if not isinstance(raw_roots, (tuple, list)) or not raw_roots:
            raise MacroSourceError("fed_reserve_bank_roots_missing")
        roots = tuple(str(value).rstrip("/") for value in raw_roots)
        received_date = datetime.fromtimestamp(received_at_ms / 1_000, tz=UTC).date()
        start_date = _optional_date(cursor.get("start_date"))
        end_date = _optional_date(cursor.get("end_date"))
        lower_bound = start_date or received_date - timedelta(days=366)
        upper_bound = end_date or received_date
        source_index = (_optional_int(cursor.get("source_index")) or 0) % len(roots)
        url_after = str(cursor.get("url_after") or "")
        latest_published_at_ms = _optional_int(cursor.get("published_at_ms")) or 0
        documents: list[DocumentFact] = []
        response: httpx.Response | None = None
        visited_sources = 0
        next_source_index = source_index
        next_url_after = url_after
        completed_cycle = False
        while visited_sources < len(roots) and not documents:
            root = roots[next_source_index]
            candidates, discovery_response = self._reserve_bank_speech_candidates(
                root,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                url_after=next_url_after,
            )
            response = response or discovery_response
            page_limit = 60
            selected = candidates[:page_limit]
            with ThreadPoolExecutor(max_workers=min(8, len(selected) or 1)) as executor:
                fetched_documents = executor.map(
                    partial(
                        self._fetch_reserve_bank_document,
                        spec,
                        root=root,
                        lower_bound=lower_bound,
                        upper_bound=upper_bound,
                        received_at_ms=received_at_ms,
                    ),
                    selected,
                )
                for document in fetched_documents:
                    if document is None:
                        continue
                    documents.append(document)
                    latest_published_at_ms = max(
                        latest_published_at_ms,
                        document.published_at_ms,
                    )
            if len(candidates) > page_limit:
                next_url_after = selected[-1]
            else:
                completed_cycle = next_source_index == len(roots) - 1
                next_source_index = (next_source_index + 1) % len(roots)
                next_url_after = ""
                visited_sources += 1
                if completed_cycle:
                    break
        if response is None:
            raise MacroSourceError("fed_reserve_bank_discovery_failed")
        documents.sort(key=lambda item: (item.published_at_ms, item.document_id))
        return _batch(
            spec,
            partition_key,
            tuple(documents),
            response,
            cursor={
                "source_index": next_source_index,
                "url_after": next_url_after,
                "published_at_ms": latest_published_at_ms,
                "reference_date": (upper_bound.isoformat() if completed_cycle else lower_bound.isoformat()),
                **({"backfill_complete": completed_cycle} if start_date is not None and end_date is not None else {}),
                **({"start_date": start_date.isoformat()} if start_date else {}),
                **({"end_date": end_date.isoformat()} if end_date else {}),
            },
        )

    def _fetch_reserve_bank_document(
        self,
        spec: DatasetSpec,
        candidate_url: str,
        *,
        root: str,
        lower_bound: date,
        upper_bound: date,
        received_at_ms: int,
    ) -> DocumentFact | None:
        with self._client.stream(
            "GET",
            candidate_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/pdf"},
        ) as page_response:
            if page_response.status_code in {404, 410}:
                return None
            _require_success(page_response, source_id=spec.source_id)
            content_type = str(page_response.headers.get("content-type") or "").lower()
            is_pdf = "application/pdf" in content_type or candidate_url.lower().endswith(".pdf")
            content = _read_bounded_stream(
                page_response,
                max_bytes=25_000_000 if is_pdf else 5_000_000,
                error_code="fed_reserve_bank_speech_body_too_large",
            )
            encoding = page_response.encoding or "utf-8"
        page_text = content.decode(encoding, errors="replace") if not is_pdf else ""
        content_text = _extract_pdf_text(content) if is_pdf else _extract_official_body(page_text)
        if len(content_text) < 500:
            return None
        published_date = _official_speech_date(page_text, candidate_url)
        if published_date is None or not (lower_bound <= published_date <= upper_bound):
            return None
        title = _official_page_title(page_text) or _document_label(candidate_url)
        speaker_name = _official_speech_speaker(page_text, title, content_text)
        content_hash = hashlib.sha256(content_text.encode()).hexdigest()
        document_id = (
            "macrodoc_" + hashlib.sha256(f"{spec.dataset_id}|{candidate_url}|{content_hash}".encode()).hexdigest()
        )
        published_at_ms = min(_date_release_ms(published_date), received_at_ms)
        return DocumentFact(
            document_id=document_id,
            dataset_id=spec.dataset_id,
            document_type="speech",
            title=title,
            effective_date=published_date,
            published_at_ms=published_at_ms,
            received_at_ms=max(received_at_ms, published_at_ms),
            source_url=candidate_url,
            content_text=content_text[:100_000],
            metadata={
                "official_root": root,
                "speaker_name": speaker_name,
                "content_hash": f"sha256:{content_hash}",
                "body_source": ("official_reserve_bank_pdf" if is_pdf else "official_reserve_bank_page"),
            },
        )

    def _reserve_bank_speech_candidates(
        self,
        root: str,
        *,
        lower_bound: date,
        upper_bound: date,
        url_after: str,
    ) -> tuple[list[str], httpx.Response]:
        robots_response = self._client.get(f"{root}/robots.txt", headers={"Accept": "text/plain"})
        sitemap_urls = (
            re.findall(r"(?im)^sitemap:\s*(\S+)", robots_response.text) if robots_response.status_code < 400 else []
        )
        if not sitemap_urls:
            sitemap_urls = [f"{root}/sitemap.xml"]
        discovered: dict[str, date | None] = {}
        response: httpx.Response | None = None
        queue = list(dict.fromkeys(sitemap_urls))
        seen_sitemaps: set[str] = set()
        while queue and len(seen_sitemaps) < 40:
            sitemap_url = queue.pop(0)
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            sitemap_response = self._client.get(
                sitemap_url,
                headers={"Accept": "application/xml,text/xml,application/gzip"},
            )
            if sitemap_response.status_code >= 400:
                continue
            try:
                nested, pages = _parse_sitemap(sitemap_response)
            except MacroSourceError:
                continue
            response = response or sitemap_response
            queue.extend(url for url in nested if _same_official_host(root, url))
            for page_url, last_modified in pages:
                if not _same_official_host(root, page_url) or not _looks_like_speech_url(page_url):
                    continue
                candidate_year = _year_from_url(page_url)
                if candidate_year is not None and not (lower_bound.year <= candidate_year <= upper_bound.year):
                    continue
                candidate_date = _date_from_url_or_text(page_url) or last_modified
                if candidate_date is not None and not (lower_bound <= candidate_date <= upper_bound):
                    continue
                discovered[page_url] = candidate_date
        if response is None:
            fallback = self._client.get(root, headers={"Accept": "text/html,application/xhtml+xml"})
            _require_success(fallback, source_id="federal_reserve_banks")
            response = fallback
            for href, label in _html_links(fallback.text):
                page_url = urljoin(str(fallback.url), href)
                if (
                    _same_official_host(root, page_url)
                    and _looks_like_speech_url(page_url)
                    and _looks_like_speech_label(label)
                ):
                    discovered[page_url] = _date_from_url_or_text(page_url)
        candidates = sorted(url for url in discovered if not url_after or url > url_after)
        return candidates, response


def _batch(
    spec: DatasetSpec,
    partition_key: str,
    facts: tuple[
        SeriesFact | ReleaseFact | DocumentFact | MarketObservationFact | MarketPositionFact | MarketSettlementFact,
        ...,
    ],
    response: httpx.Response | requests.Response,
    *,
    cursor: dict[str, Any],
) -> FetchBatch:
    return FetchBatch(
        dataset_id=spec.dataset_id,
        partition_key=partition_key,
        facts=facts,
        cursor=cursor,
        response_hash="sha256:" + hashlib.sha256(response.content).hexdigest(),
        source_url=str(response.url),
        http_status=response.status_code,
    )


def _series_cursor(
    latest_date: date | None,
    *,
    start_date: date | None,
    end_date: date | None,
    backfill_complete: bool | None = None,
) -> dict[str, Any]:
    return {
        **({"reference_date": latest_date.isoformat()} if latest_date is not None else {}),
        **({"start_date": start_date.isoformat()} if start_date is not None else {}),
        **({"end_date": end_date.isoformat()} if end_date is not None else {}),
        **({"backfill_complete": backfill_complete} if backfill_complete is not None else {}),
    }


def _require_success(
    response: httpx.Response | requests.Response,
    *,
    source_id: str,
) -> None:
    try:
        response.raise_for_status()
    except (httpx.HTTPError, requests.RequestException) as exc:
        raise MacroSourceError(f"{source_id}_http_error:{response.status_code}") from exc


def _read_bounded_stream(
    response: httpx.Response,
    *,
    max_bytes: int,
    error_code: str,
) -> bytes:
    content_length = _optional_int(response.headers.get("content-length"))
    if content_length is not None and content_length > max_bytes:
        raise MacroSourceError(error_code)
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise MacroSourceError(error_code)
    return bytes(body)


def _finite_float(value: Any) -> float | None:
    if value is None or str(value).strip() in {"", ".", "nan", "NaN"}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _optional_us_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def _date_close_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, 21, tzinfo=UTC).timestamp() * 1_000)


def _date_open_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1_000)


def _date_end_ms(value: date) -> int:
    return _date_open_ms(value) + 86_400_000 - 1


def _normalize_column(value: str | None) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _finite_float(row.get(key))
        if parsed is not None:
            return parsed
    return None


class _FedHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.all_text: list[str] = []
        self.main_text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._main_depth = 0
        self._title_depth = 0
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): str(value or "") for key, value in attrs}
        if tag in {"script", "style", "nav", "header", "footer", "noscript"}:
            self._skip_depth += 1
        if tag == "main" or attributes.get("id", "").lower() in {
            "content",
            "contentwrapper",
            "article",
            "articlecontent",
        }:
            self._main_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "a":
            self._link_href = attributes.get("href") or None
            self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href:
            label = " ".join(" ".join(self._link_parts).split())
            self.links.append((self._link_href, label))
            self._link_href = None
            self._link_parts = []
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in {"script", "style", "nav", "header", "footer", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._title_depth:
            self.title_parts.append(value)
        if self._link_href is not None:
            self._link_parts.append(value)
        if self._skip_depth:
            return
        self.all_text.append(value)
        if self._main_depth:
            self.main_text.append(value)


def _parse_fed_html(value: str) -> _FedHtmlParser:
    parser = _FedHtmlParser()
    parser.feed(value)
    parser.close()
    return parser


def _extract_official_body(value: str) -> str:
    parsed = _parse_fed_html(value)
    preferred = parsed.main_text if len(" ".join(parsed.main_text)) >= 200 else parsed.all_text
    return " ".join(" ".join(preferred).split())


def _official_page_title(value: str) -> str | None:
    parsed = _parse_fed_html(value)
    title = " ".join(parsed.title_parts).strip()
    if not title:
        return None
    return re.sub(r"\s*[-|]\s*Federal Reserve Board.*$", "", title, flags=re.IGNORECASE).strip() or None


def _html_links(value: str) -> list[tuple[str, str]]:
    return _parse_fed_html(value).links


class _FomcParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._parts: list[str] | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "nav", "header", "footer", "noscript"}:
            self._skip_depth += 1
        if tag == "p" and self._parts is None:
            self._parts = []
        elif tag == "br" and self._parts is not None:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._parts is not None:
            lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
            paragraph = "\n".join(line for line in lines if line)
            if paragraph:
                self.paragraphs.append(paragraph)
            self._parts = None
        if tag in {"script", "style", "nav", "header", "footer", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._parts is None:
            return
        value = " ".join(data.split())
        if value:
            self._parts.append(value + " ")


def _extract_fomc_role_records(value: str) -> list[dict[str, Any]]:
    parser = _FomcParagraphParser()
    parser.feed(value)
    parser.close()
    attendance_index = next(
        (
            index
            for index, paragraph in enumerate(parser.paragraphs)
            if paragraph.splitlines()[0].strip().lower() in {"attendance", "present:"}
        ),
        None,
    )
    if attendance_index is None:
        return []
    heading_lines = parser.paragraphs[attendance_index].splitlines()
    if len(heading_lines) > 1:
        member_lines = heading_lines[1:]
        group_start = attendance_index + 1
    else:
        if attendance_index + 1 >= len(parser.paragraphs):
            return []
        member_lines = parser.paragraphs[attendance_index + 1].splitlines()
        group_start = attendance_index + 2
    groups = parser.paragraphs[group_start : group_start + 2]
    records = [_member_role_record(line) for line in member_lines]
    if groups and "alternate members of the committee" in groups[0].lower():
        names = re.split(
            r",?\s+Alternate Members of the Committee",
            groups[0],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        records.extend(
            {
                "official_name": name,
                "role_title": "FOMC alternate member",
                "organization": "Federal Open Market Committee",
                "fomc_voter": False,
            }
            for name in _split_official_names(names)
        )
    if len(groups) > 1 and "presidents of the federal reserve banks" in groups[1].lower():
        names = re.split(
            r",?\s+Presidents of the Federal Reserve Banks",
            groups[1],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        records.extend(
            {
                "official_name": name,
                "role_title": "FOMC nonvoting participant",
                "organization": "Federal Reserve System",
                "fomc_voter": False,
            }
            for name in _split_official_names(names)
        )
    return [record for record in records if record["official_name"]]


def _member_role_record(value: str) -> dict[str, Any]:
    match = re.match(
        r"^(?P<name>.+?)(?:,\s*(?P<title>Chairman|Chair|Vice Chair))?$",
        " ".join(value.split()),
        flags=re.IGNORECASE,
    )
    name = _clean_fomc_official_name(match.group("name") if match else value)
    title = str(match.group("title") or "FOMC member") if match else "FOMC member"
    return {
        "official_name": name,
        "role_title": title,
        "organization": "Federal Open Market Committee",
        "fomc_voter": True,
    }


def _split_official_names(value: str) -> list[str]:
    normalized = re.sub(r",?\s+and\s+", ",", " ".join(value.split()), flags=re.IGNORECASE)
    names = [_clean_fomc_official_name(name) for name in normalized.split(",")]
    return [name for name in names if name]


def _clean_fomc_official_name(value: Any) -> str:
    # Attendance footnote anchors appear as bare leading digits in the
    # Board minutes HTML (for example, "2 Loretta J. Mester").
    normalized = " ".join(str(value or "").split()).strip(" ,")
    return re.sub(r"^\d+\s*", "", normalized).strip(" ,")


def _parse_sitemap(response: httpx.Response) -> tuple[list[str], list[tuple[str, date | None]]]:
    content = response.content
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError as exc:
            raise MacroSourceError("fed_reserve_bank_sitemap_gzip_invalid") from exc
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise MacroSourceError("fed_reserve_bank_sitemap_xml_invalid") from exc
    nested: list[str] = []
    pages: list[tuple[str, date | None]] = []
    root_name = _xml_local_name(root.tag).lower()
    for child in root:
        values = {_xml_local_name(node.tag).lower(): " ".join(str(node.text or "").split()) for node in child}
        location = values.get("loc", "")
        if not location:
            continue
        if root_name == "sitemapindex":
            nested.append(location)
        else:
            pages.append((location, _optional_date(values.get("lastmod"))))
    return nested, pages


def _same_official_host(root: str, candidate: str) -> bool:
    root_host = urlparse(root).netloc.lower().removeprefix("www.")
    candidate_host = urlparse(candidate).netloc.lower().removeprefix("www.")
    return bool(root_host and candidate_host == root_host)


def _looks_like_speech_url(value: str) -> bool:
    parsed = urlparse(value)
    path = parsed.path.lower().rstrip("/")
    if not path or path.endswith(("/speech", "/speeches", "/remarks")):
        return False
    return any(
        token in path
        for token in (
            "/speech/",
            "/speeches/",
            "/remarks/",
            "/from-the-president/",
            "/president/speech",
            "/president/remarks",
        )
    )


def _looks_like_speech_label(value: str) -> bool:
    normalized = value.lower()
    return any(token in normalized for token in ("speech", "remarks", "economic outlook", "monetary policy"))


class _HtmlMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = {key.lower(): " ".join(str(value or "").split()) for key, value in attrs}
        key = (attributes.get("property") or attributes.get("name") or attributes.get("itemprop") or "").lower()
        content = attributes.get("content", "")
        if key and content:
            self.meta[key] = content


def _html_metadata(value: str) -> dict[str, str]:
    parser = _HtmlMetadataParser()
    parser.feed(value)
    parser.close()
    return parser.meta


def _official_speech_date(value: str, source_url: str) -> date | None:
    metadata = _html_metadata(value)
    for key in (
        "article:published_time",
        "datepublished",
        "date",
        "publishdate",
        "publication_date",
        "dc.date",
    ):
        parsed = _date_from_url_or_text(metadata.get(key, ""))
        if parsed is not None:
            return parsed
    json_date = re.search(
        r'["\']datePublished["\']\s*:\s*["\']([^"\']+)',
        value,
        flags=re.IGNORECASE,
    )
    if json_date is not None:
        parsed = _date_from_url_or_text(json_date.group(1))
        if parsed is not None:
            return parsed
    return _date_from_url_or_text(source_url) or _date_from_url_or_text(_extract_official_body(value)[:2_000])


def _official_speech_speaker(value: str, title: str, content_text: str) -> str | None:
    metadata = _html_metadata(value)
    for key in ("author", "article:author", "byl", "dc.creator"):
        author = " ".join(metadata.get(key, "").split())
        if author and len(author) <= 200:
            return author
    json_author = re.search(
        r'["\']author["\']\s*:\s*(?:\{.*?["\']name["\']\s*:\s*)?["\']([^"\']+)',
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if json_author is not None:
        return " ".join(json_author.group(1).split())[:200]
    role_name = re.search(
        r"\b(?:Chair|Vice Chair(?:\s+for\s+Supervision)?|Governor)\s+"
        r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,4})"
        r"(?=\s+(?:At|Before|For|Thank|Good|Today|It)\b)",
        content_text[:2_000],
    )
    if role_name is not None:
        return " ".join(role_name.group(1).split())[:200]
    parsed = _speech_speaker(title, content_text)
    if parsed:
        return parsed
    for pattern in (
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},?\s+20\d{2}\s+"
        r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,4})\s*,\s*"
        r"(?:President|First Vice President|Manager|Executive Vice President)\b",
        r"(?:Remarks|Speech|Address)\s+by\s+([A-Z][A-Za-z.' -]{3,100}?)(?:\s+at\s+|\s+on\s+|,)",
        r"\bBy\s+([A-Z][A-Za-z.' -]{3,100}?)(?:,\s+(?:President|Chief Executive|Federal Reserve)|\n)",
        r"\b([A-Z][A-Za-z.' -]{3,100}?),\s+President(?:\s+and\s+CEO)?\b",
    ):
        match = re.search(pattern, f"{title}\n{content_text[:2_000]}", flags=re.IGNORECASE)
        if match is not None:
            return " ".join(match.group(1).split())[:200]
    return None


def _date_from_url_or_text(value: str) -> date | None:
    text = str(value or "")
    for pattern in (
        r"(?<!\d)((?:19|20)\d{2})[-_/](\d{1,2})[-_/](\d{1,2})(?!\d)",
        r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)",
    ):
        match = re.search(pattern, text)
        if match is None:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    month_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_match is None:
        return None
    try:
        return datetime.strptime(
            f"{month_match.group(1)} {month_match.group(2)} {month_match.group(3)}",
            "%B %d %Y",
        ).date()
    except ValueError:
        return None


def _year_from_url(value: str) -> int | None:
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", urlparse(value).path)
    return int(match.group(1)) if match is not None else None


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", maxsplit=1)[-1]


def _date_from_fed_url(value: str) -> date | None:
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", value)
    if match is None:
        match = re.search(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})", value)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _release_date_from_body(value: str) -> date | None:
    match = re.search(
        r"(?:Release Date|For release at).*?"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),\s+(20\d{2})",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}, {match.group(3)}",
            "%B %d, %Y",
        ).date()
    except ValueError:
        return None


def _extract_pdf_text(payload: bytes) -> str:
    if not payload.startswith(b"%PDF-"):
        raise MacroSourceError("fomc_pdf_signature_invalid")
    if len(payload) > 25_000_000:
        raise MacroSourceError("fomc_pdf_too_large")
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise MacroSourceError("fomc_pdf_encrypted")
        if len(reader.pages) > 200:
            raise MacroSourceError("fomc_pdf_page_limit")
        pages: list[str] = []
        total_content_bytes = 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None:
                total_content_bytes += len(contents.get_data())
            if total_content_bytes > 50_000_000:
                raise MacroSourceError("fomc_pdf_content_too_large")
            text = page.extract_text(extraction_mode="layout")
            if text:
                pages.append(text)
    except MacroSourceError:
        raise
    except (PdfReadError, ValueError, TypeError, KeyError, OSError) as exc:
        raise MacroSourceError("fomc_pdf_extract_failed") from exc
    normalized = re.sub(r"[ \t]+", " ", "\n".join(pages))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) < 200:
        raise MacroSourceError("fomc_pdf_no_valid_text")
    return normalized[:100_000]


def _date_release_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, 18, tzinfo=UTC).timestamp() * 1_000)


def _is_fomc_document_link(url: str, label: str) -> bool:
    normalized_url = url.lower()
    normalized_label = label.lower()
    if not normalized_url.startswith("https://www.federalreserve.gov/"):
        return False
    if not normalized_url.endswith((".htm", ".html", ".pdf")):
        return False
    semantic_label = any(
        token in normalized_label
        for token in (
            "statement",
            "implementation note",
            "minutes",
            "projection materials",
            "economic projections",
        )
    )
    semantic_url = any(
        token in normalized_url
        for token in (
            "/pressreleases/monetary",
            "fomcminutes",
            "fomcprojtabl",
            "fomcprojectionmaterials",
        )
    )
    return semantic_label or semantic_url


def _speech_speaker(title: str, content_text: str) -> str | None:
    value = f"{title} {content_text[:500]}"
    match = re.search(
        r"(?:Speech|Remarks)\s+by\s+(.+?)(?:\s+on\s+|\s+at\s+|,\s+(?:Chair|Vice Chair|Governor)|$)",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).split())[:200] if match else None


def _document_label(url: str) -> str:
    if "fomcminutes" in url.lower():
        return "FOMC Minutes"
    if "projection" in url.lower() or "fomcprojtabl" in url.lower():
        return "Summary of Economic Projections"
    if url.lower().rstrip("/").endswith("a1.htm"):
        return "FOMC Implementation Note"
    return "FOMC Statement"


def _document_type(title: str) -> str:
    normalized = title.lower()
    if "implementation" in normalized or normalized.rstrip("/").endswith("a1.htm"):
        return "implementation"
    if "minutes" in normalized:
        return "minutes"
    if "economic projections" in normalized or "projection materials" in normalized or "fomcprojtabl" in normalized:
        return "sep"
    if "speech" in normalized:
        return "speech"
    if "calendar" in normalized:
        return "calendar"
    return "statement"


__all__ = ["MacroSourceClient", "MacroSourceError", "MacroSourceUnavailable"]
