from __future__ import annotations

import csv
import hashlib
import html
import io
import math
import time
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

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
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "text/csv,application/json"},
            transport=transport,
        )
        self._fred_enabled = bool(fred_enabled)
        self._cboe_enabled = bool(cboe_enabled)
        self._cftc_enabled = bool(cftc_enabled)
        self._nasdaq_public_enabled = bool(nasdaq_public_enabled)

    def close(self) -> None:
        self._client.close()

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
        if spec.adapter_id == "fed_rss":
            return self._fetch_fed_rss(spec, partition_key, cursor, received_at_ms)
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
        response = self._client.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params=params)
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
            cursor=_series_cursor(latest_date, start_date=start_date, end_date=end_date),
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
            cursor_date - timedelta(days=7)
            if cursor_date is not None
            else received_date - timedelta(days=1_825)
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
            cursor=_series_cursor(latest_date, start_date=start_date, end_date=end_date),
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
            if close is None or close_at_ms is None:
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
                    received_at_ms=max(received_at_ms, close_at_ms),
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
        response: httpx.Response | None = None
        trade_date = target_date
        for offset in range(0, 8):
            candidate = target_date - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            attempted = self._client.get(spec.source_url, params={"dt": candidate.isoformat()})
            if attempted.status_code == 200:
                response = attempted
                trade_date = candidate
                break
            if attempted.status_code not in {403, 404}:
                _require_success(attempted, source_id=spec.source_id)
        if response is None:
            raise MacroSourceError("cfe_settlement_file_not_published")
        facts: list[MarketSettlementFact] = []
        for raw_row in csv.DictReader(io.StringIO(response.text)):
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
                    trade_date=trade_date,
                    contract_code=contract_code.upper(),
                    settlement_price=settlement,
                    open_interest=_first_float(row, "openinterest", "oi"),
                    volume=_first_float(row, "volume", "totalvolume"),
                    unit=spec.unit,
                    published_at_ms=None,
                    received_at_ms=received_at_ms,
                    source_url=str(response.url),
                    raw_data=dict(raw_row),
                )
            )
        if not facts:
            raise MacroSourceError("cfe_settlement_no_valid_rows")
        return _batch(
            spec,
            partition_key,
            tuple(facts),
            response,
            cursor={"trade_date": str(trade_date)},
        )

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
            cursor_date - timedelta(days=35)
            if cursor_date is not None
            else received_date - timedelta(days=730)
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
            cursor=_series_cursor(latest_date, start_date=start_date, end_date=end_date),
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
        series = (
            payload.get("Results", {}).get("series", [])
            if isinstance(payload, dict)
            else []
        )
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
            },
        )

    def _fetch_fed_rss(
        self,
        spec: DatasetSpec,
        partition_key: str,
        cursor: dict[str, Any],
        received_at_ms: int,
    ) -> FetchBatch:
        response = self._client.get(spec.source_url)
        _require_success(response, source_id=spec.source_id)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise MacroSourceError("fed_rss_xml_invalid") from exc
        documents: list[DocumentFact] = []
        latest_published_at_ms = _optional_int(cursor.get("published_at_ms")) or 0
        for item in root.findall(".//item"):
            title = _xml_text(item, "title")
            url = _xml_text(item, "link")
            raw_content = _xml_text(item, "description")
            if not title or not url or not raw_content:
                continue
            published_at_ms = _rss_date_ms(_xml_text(item, "pubDate"), received_at_ms)
            content_text = _plain_text(raw_content)
            if not content_text:
                continue
            guid = _xml_text(item, "guid") or url
            content_hash = hashlib.sha256(content_text.encode()).hexdigest()
            document_id = "macrodoc_" + hashlib.sha256(
                f"{spec.dataset_id}|{guid}|{content_hash}".encode()
            ).hexdigest()
            effective_date = datetime.fromtimestamp(published_at_ms / 1_000, tz=UTC).date()
            documents.append(
                DocumentFact(
                    document_id=document_id,
                    dataset_id=spec.dataset_id,
                    document_type=_document_type(title),
                    title=title,
                    effective_date=effective_date,
                    published_at_ms=published_at_ms,
                    received_at_ms=max(received_at_ms, published_at_ms),
                    source_url=url,
                    content_text=content_text[:50_000],
                    metadata={"feed_guid": guid, "feed_url": str(response.url)},
                )
            )
            latest_published_at_ms = max(latest_published_at_ms, published_at_ms)
        return _batch(
            spec,
            partition_key,
            tuple(documents),
            response,
            cursor={"published_at_ms": latest_published_at_ms},
        )


def _batch(
    spec: DatasetSpec,
    partition_key: str,
    facts: tuple[
        SeriesFact
        | ReleaseFact
        | DocumentFact
        | MarketObservationFact
        | MarketSettlementFact,
        ...,
    ],
    response: httpx.Response,
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
) -> dict[str, Any]:
    return {
        **({"reference_date": latest_date.isoformat()} if latest_date is not None else {}),
        **({"start_date": start_date.isoformat()} if start_date is not None else {}),
        **({"end_date": end_date.isoformat()} if end_date is not None else {}),
    }


def _require_success(response: httpx.Response, *, source_id: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MacroSourceError(f"{source_id}_http_error:{response.status_code}") from exc


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


def _xml_text(item: Element, tag: str) -> str:
    node = item.find(tag)
    return str(node.text or "").strip() if node is not None else ""


def _rss_date_ms(value: str, fallback_ms: int) -> int:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return fallback_ms
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return min(int(parsed.timestamp() * 1_000), fallback_ms)


def _plain_text(value: str) -> str:
    text = html.unescape(value)
    output = []
    inside_tag = False
    for char in text:
        if char == "<":
            inside_tag = True
            output.append(" ")
        elif char == ">":
            inside_tag = False
            output.append(" ")
        elif not inside_tag:
            output.append(char)
    return " ".join("".join(output).split())


def _document_type(title: str) -> str:
    normalized = title.lower()
    if "minutes" in normalized:
        return "minutes"
    if "economic projections" in normalized:
        return "sep"
    if "speech" in normalized:
        return "speech"
    if "calendar" in normalized:
        return "calendar"
    return "statement"


__all__ = ["MacroSourceClient", "MacroSourceError", "MacroSourceUnavailable"]
