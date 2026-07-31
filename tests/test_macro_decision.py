from __future__ import annotations

import json
from datetime import UTC, date, datetime

from tracefold.app.http import schemas as api_schemas
from tracefold.macro import (
    NATURAL_CHANGE_REGISTRY,
    module_payloads,
    natural_change_calculation,
    resolve_thesis_session,
)
from tracefold.macro.coverage import COVERAGE_MANIFEST
from tracefold.macro.fed_roles import effective_roster_rows, match_effective_role
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.reasons import macro_reason
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


def _module(
    module_id: str,
    *,
    now_ms: int = NOW_MS,
    **overrides: list[dict],
) -> dict:
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


def test_semantic_history_start_survives_bounded_projection_window() -> None:
    spec = DATASET_REGISTRY["fred.dgs10"]
    current = _series_row(
        dataset_id=spec.dataset_id,
        reference_date=date(2026, 7, 27),
    )
    current["semantic_history_start"] = date(2021, 7, 27)

    assert module_payloads._dataset_history_depth(
        spec,
        [],
        [current],
        active_backfills=[],
    ) == ("complete", "expected_history_window_present")


def test_daily_fact_without_publication_clock_uses_received_time_not_future_close() -> None:
    now_ms = int(datetime(2026, 7, 29, 6, 30, tzinfo=UTC).timestamp() * 1_000)
    received_at_ms = now_ms - 5_000
    row = {
        **_series_row(
            dataset_id="fred.iorb",
            reference_date=date(2026, 7, 29),
            value=3.65,
        ),
        "received_at_ms": received_at_ms,
    }

    module = _module(
        "liquidity_funding",
        now_ms=now_ms,
        series_rows=[row],
    )

    assert module["latest_fact_at_ms"] == received_at_ms
    latest = next(item for item in module["evidence"]["latest_facts"] if item["dataset_id"] == "fred.iorb")
    assert latest["observed_at_ms"] is None
    assert latest["published_at_ms"] is None
    assert latest["received_at_ms"] == received_at_ms


def test_market_latest_fact_exposes_all_source_clocks_and_prefers_observed_time() -> None:
    observed_at_ms = NOW_MS - 20_000
    row = {
        **_market_row("yfinance.spy.intraday", observed_at_ms, 105),
        "published_at_ms": NOW_MS - 10_000,
        "received_at_ms": NOW_MS - 5_000,
    }

    module = _module(
        "cross_asset",
        market_rows=[row],
    )

    assert module["latest_fact_at_ms"] == observed_at_ms
    latest = next(item for item in module["evidence"]["latest_facts"] if item["dataset_id"] == "yfinance.spy.intraday")
    assert latest["observed_at_ms"] == observed_at_ms
    assert latest["published_at_ms"] == NOW_MS - 10_000
    assert latest["received_at_ms"] == NOW_MS - 5_000


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
    assert DATASET_REGISTRY["cboe.cfe.vx.settlement"].module_id == "volatility"
    assert DATASET_REGISTRY["nasdaq.spy.daily"].module_id == "cross_asset"
    assert DATASET_REGISTRY["yfinance.spy.intraday"].module_id == "cross_asset"
    assert all(capability.requirement in {"required", "supporting"} for capability in COVERAGE_MANIFEST.values())
    assert "licensed_unavailable" not in json.dumps(module, sort_keys=True)


def test_official_vx_curve_is_expiry_sorted_owned_only_by_volatility_and_keeps_source_clock() -> None:
    rows = [
        {
            "settlement_id": "vx-sep",
            "fact_schema_version": "market_settlement_v2",
            "dataset_id": "cboe.cfe.vx.settlement",
            "instrument_id": "vx_future",
            "trade_date": date(2026, 7, 27),
            "contract_code": "VXU26",
            "contract_expiration_date": date(2026, 9, 16),
            "settlement_price": 17.2,
            "open_interest": 90_000,
            "volume": 30_000,
            "published_at_ms": NOW_MS - 2_000,
            "received_at_ms": NOW_MS - 1_000,
            "source_url": "https://www.cboe.com/vx.csv",
            "fact_hash": "sha256:vx-sep",
        },
        {
            "settlement_id": "vx-aug",
            "fact_schema_version": "market_settlement_v2",
            "dataset_id": "cboe.cfe.vx.settlement",
            "instrument_id": "vx_future",
            "trade_date": date(2026, 7, 27),
            "contract_code": "VXQ26",
            "contract_expiration_date": date(2026, 8, 19),
            "settlement_price": 16.2,
            "open_interest": 120_000,
            "volume": 45_000,
            "published_at_ms": NOW_MS - 2_000,
            "received_at_ms": NOW_MS - 1_000,
            "source_url": "https://www.cboe.com/vx.csv",
            "fact_hash": "sha256:vx-aug",
        },
    ]

    volatility = _module("volatility", settlement_rows=rows)
    cross_asset = _module("cross_asset", settlement_rows=rows)
    curve = volatility["term_structure"]["official_vx_curve"]

    assert [item["contract_expiration_date"] for item in curve] == [
        "2026-08-19",
        "2026-09-16",
    ]
    assert curve[0]["published_at_ms"] == NOW_MS - 2_000
    assert curve[0]["received_at_ms"] == NOW_MS - 1_000
    assert all(item["dataset_id"] != "cboe.cfe.vx.settlement" for item in cross_asset["evidence"]["latest_facts"])
    assert "term_structure" not in cross_asset


def test_fed_institutional_stance_separates_reader_reason_from_analysis_identity() -> None:
    document_id = "macrodoc_fomc_statement_20260727"
    analysis_id = "macroan_fomc_statement_20260727"
    module = _module(
        "rates_fed",
        document_rows=[
            {
                "document_id": document_id,
                "dataset_id": "federal_reserve.fomc.documents",
                "document_type": "statement",
                "title": "Federal Reserve issues FOMC statement",
                "effective_date": date(2026, 7, 27),
                "published_at_ms": NOW_MS - 10_000,
                "received_at_ms": NOW_MS - 9_000,
                "source_url": "https://www.federalreserve.gov/monetarypolicy/fomc.htm",
                "metadata_json": {},
            }
        ],
        analysis_rows=[
            {
                "analysis_id": analysis_id,
                "dataset_id": "federal_reserve.document.analysis",
                "document_id": document_id,
                "created_at_ms": NOW_MS - 5_000,
                "received_at_ms": NOW_MS - 5_000,
                "source_url": "https://www.federalreserve.gov/monetarypolicy/fomc.htm",
                "policy_relevance": "policy_signal",
                "stance": "hawkish",
                "confidence": 0.86,
                "analysis_json": {
                    "change_from_prior": "more_hawkish",
                    "evidence": [],
                    "rationale": "委员会仍将通胀风险置于宽松风险之前。",
                },
                "model_name": "test-model",
                "prompt_version": "test-prompt",
                "reviewer_disposition": "pass",
            }
        ],
    )

    api_schemas.MacroRatesFedPersistedData.model_validate(module)
    stance = module["fed"]["institutional_stance"]
    assert stance["state"] == "current"
    assert stance["direction"] == "hawkish"
    assert stance["reason"] == "委员会仍将通胀风险置于宽松风险之前。"
    assert stance["analysis_id"] == analysis_id
    assert not stance["reason"].startswith("analysis:")

    unavailable = _module("rates_fed")["fed"]["institutional_stance"]
    assert unavailable["reason"] == "尚未发布通过独立审阅的 FOMC 声明分析。"
    assert unavailable["analysis_id"] is None


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


def test_rates_payload_is_tenor_native_and_matches_the_issue_31_acceptance_sample() -> None:
    rows = []
    nominal = (
        (date(2026, 6, 29), {"2Y": 4.34, "7Y": 4.20, "10Y": 4.38, "30Y": 4.91}),
        (date(2026, 7, 1), {"2Y": 4.31, "7Y": 4.26, "10Y": 4.43, "30Y": 4.96}),
        (date(2026, 7, 22), {"2Y": 4.28, "7Y": 4.43, "10Y": 4.55, "30Y": 5.06}),
        (date(2026, 7, 28), {"2Y": 4.26, "7Y": 4.47, "10Y": 4.61, "30Y": 5.09}),
        (date(2026, 7, 29), {"2Y": 4.22, "7Y": 4.51, "10Y": 4.67, "30Y": 5.20}),
    )
    real = (
        (date(2026, 7, 28), {"10Y": 2.41, "30Y": 2.92}),
        (date(2026, 7, 29), {"10Y": 2.41, "30Y": 2.98}),
    )
    for dataset_id, observations in (
        ("treasury.daily_nominal_curve", nominal),
        ("treasury.daily_real_curve", real),
    ):
        rows.extend(
            _series_row(
                dataset_id=dataset_id,
                reference_date=reference_date,
                value=value,
                series_id=tenor,
            )
            for reference_date, values in observations
            for tenor, value in values.items()
        )

    module = _module("rates_fed", series_rows=rows)
    reversed_module = _module("rates_fed", series_rows=list(reversed(rows)))

    api_schemas.MacroRatesFedPersistedData.model_validate(module)
    assert module == reversed_module
    assert module["schema_version"] == "macro_rates_fed_v6"
    assert "summary" not in module
    assert "contradictions" not in module
    assert "falsifiers" not in module
    assert "classification" not in module["curve"]
    assert "top_changes" not in json.dumps(module, sort_keys=True)

    decision = module["decision"]
    assert decision["state"] == "available"
    assert decision["reference_date"] == "2026-07-29"
    assert decision["headline"] == ("最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）")
    assert decision["session_completeness"]["state"] == "complete"
    matrix = {item["tenor"]: item for item in decision["tenor_matrix"]}
    assert [item["tenor"] for item in decision["tenor_matrix"]] == ["2Y", "10Y", "30Y"]
    assert matrix["2Y"]["current"]["yield_pct"] == 4.22
    assert matrix["10Y"]["current"]["yield_pct"] == 4.67
    assert matrix["30Y"]["current"]["yield_pct"] == 5.20
    assert {
        tenor: next(item for item in row["windows"] if item["window"] == "1d")["change_bp"]
        for tenor, row in matrix.items()
    } == {"2Y": -4.0, "10Y": 6.0, "30Y": 11.0}
    ten_year_one_week = next(item for item in matrix["10Y"]["windows"] if item["window"] == "1w")
    assert (ten_year_one_week["baseline_date"], ten_year_one_week["change_bp"]) == (
        "2026-07-22",
        12.0,
    )
    ten_year_mtd = next(item for item in matrix["10Y"]["windows"] if item["window"] == "mtd")
    assert (ten_year_mtd["baseline_date"], ten_year_mtd["change_bp"]) == ("2026-07-01", 24.0)

    ten_year_30d = next(item for item in matrix["10Y"]["windows"] if item["window"] == "past_30d")
    assert ten_year_30d["baseline_date"] == "2026-06-29"
    assert ten_year_30d["change_bp"] == 29.0
    assert all(":10Y:" in fact_id for fact_id in ten_year_30d["input_fact_ids"])
    assert matrix["10Y"]["current"]["yield_pct"] != 4.51

    spreads = {item["spread_id"]: item for item in decision["spread_summary"]}
    assert (spreads["2s10s"]["value_bp"], spreads["2s10s"]["change_1d_bp"]) == (45.0, 10.0)
    assert (spreads["10s30s"]["value_bp"], spreads["10s30s"]["change_1d_bp"]) == (53.0, 5.0)
    classification = next(item for item in decision["classifications"] if item["window"] == "1d")
    assert classification["state"] == "twist_steepening"
    assert classification["label"] == "扭转式陡峭化"

    decompositions = {item["tenor"]: item for item in decision["decompositions"]}
    assert (
        decompositions["10Y"]["nominal_change_bp"],
        decompositions["10Y"]["real_change_bp"],
        decompositions["10Y"]["breakeven_change_bp"],
    ) == (6.0, 0.0, 6.0)
    assert decompositions["10Y"]["assessment_state"] == "inflation_compensation_dominant"
    assert (
        decompositions["30Y"]["nominal_change_bp"],
        decompositions["30Y"]["real_change_bp"],
        decompositions["30Y"]["breakeven_change_bp"],
    ) == (11.0, 6.0, 5.0)
    assert all("期限溢价" not in item["statement"] for item in decision["explanation"]["bounded_assessments"])
    assert decision["explanation"]["hypotheses"] == []

    assert [item["window"] for item in module["curve"]["nominal_snapshots"][:2]] == [
        "current",
        "previous",
    ]
    assert "10s30s" in module["curve"]["spreads"]
    assert decision["source_policy"]["selection_policy"] == ("treasury_completed_session_primary_fred_history_only")


def test_rates_one_day_uses_the_previous_official_observation_across_a_weekend() -> None:
    rows = [
        _series_row(
            dataset_id="treasury.daily_nominal_curve",
            reference_date=reference_date,
            value=value,
            series_id=tenor,
        )
        for reference_date, values in (
            (date(2026, 7, 31), {"2Y": 4.25, "10Y": 4.60, "30Y": 5.10}),
            (date(2026, 8, 3), {"2Y": 4.21, "10Y": 4.66, "30Y": 5.21}),
        )
        for tenor, value in values.items()
    ]

    decision = _module("rates_fed", series_rows=rows)["decision"]
    matrix = {item["tenor"]: item for item in decision["tenor_matrix"]}

    assert decision["reference_date"] == "2026-08-03"
    assert {
        tenor: (
            next(item for item in row["windows"] if item["window"] == "1d")["baseline_date"],
            next(item for item in row["windows"] if item["window"] == "1d")["change_bp"],
        )
        for tenor, row in matrix.items()
    } == {
        "2Y": ("2026-07-31", -4.0),
        "10Y": ("2026-07-31", 6.0),
        "30Y": ("2026-07-31", 11.0),
    }
    assert {
        tenor: next(item for item in row["windows"] if item["window"] == "mtd")["state"]
        for tenor, row in matrix.items()
    } == {"2Y": "baseline", "10Y": "baseline", "30Y": "baseline"}


def test_rates_decision_fails_closed_when_primary_tenors_or_real_curve_are_unaligned() -> None:
    rows = [
        _series_row(
            dataset_id="treasury.daily_nominal_curve",
            reference_date=reference_date,
            value=value,
            series_id=tenor,
        )
        for tenor, reference_date, value in (
            ("2Y", date(2026, 7, 28), 4.26),
            ("2Y", date(2026, 7, 29), 4.22),
            ("10Y", date(2026, 7, 28), 4.61),
            ("10Y", date(2026, 7, 29), 4.67),
            ("30Y", date(2026, 7, 28), 5.09),
        )
    ]
    rows.extend(
        _series_row(
            dataset_id="treasury.daily_real_curve",
            reference_date=reference_date,
            value=value,
            series_id="10Y",
        )
        for reference_date, value in ((date(2026, 7, 27), 2.40), (date(2026, 7, 28), 2.41))
    )

    module = _module("rates_fed", series_rows=rows)

    assert module["decision"]["state"] == "unaligned"
    assert module["decision"]["headline"] is None
    assert module["decision"]["session_completeness"]["state"] == "unaligned"
    assert all(item["state"] == "unaligned" for item in module["decision"]["spread_summary"])
    assert all(item["state"] == "unaligned" for item in module["decision"]["classifications"])
    ten_year = next(item for item in module["decision"]["decompositions"] if item["tenor"] == "10Y")
    assert ten_year["state"] == "unaligned"
    assert ten_year["breakeven_change_bp"] is None
    assert "未进行跨日拼接" in ten_year["gap"]


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

    api_schemas.MacroLiquidityFundingPersistedData.model_validate(module)
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

    api_schemas.MacroVolatilityPersistedData.model_validate(module)
    groups = module["cross_asset_implied"]["normalized_groups"]
    assert [group["group_id"] for group in groups] == ["cross_asset_implied_volatility"]
    assert [series["symbol"] for series in groups[0]["series"]] == [
        "VXN",
        "GVZ",
        "OVX",
    ]
    assert groups[0]["series"][0]["points"][0] == {
        "date": "2026-07-17",
        "normalized_value": 100.0,
    }
    assert groups[0]["series"][-1]["points"][-1] == {
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

    api_schemas.MacroCreditPersistedData.model_validate(module)
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

    api_schemas.MacroCrossAssetPersistedData.model_validate(module)
    matrix = module["assets"]["return_matrix"]
    assert [row["symbol"] for row in matrix] == [
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
    assert [(row["group_id"], row["group_label"]) for row in matrix] == [
        ("equity", "权益"),
        ("equity", "权益"),
        ("equity", "权益"),
        ("duration_credit", "久期与信用"),
        ("duration_credit", "久期与信用"),
        ("duration_credit", "久期与信用"),
        ("duration_credit", "久期与信用"),
        ("dollar_commodities", "美元与商品"),
        ("dollar_commodities", "美元与商品"),
        ("dollar_commodities", "美元与商品"),
    ]
    assert [group["group_id"] for group in module["assets"]["normalized_groups"]] == [
        "equity",
        "duration_credit",
        "dollar_commodities",
    ]
    assert [[series["symbol"] for series in group["series"]] for group in module["assets"]["normalized_groups"]] == [
        ["SPY", "QQQ", "IWM"],
        ["TLT", "IEF", "LQD", "HYG"],
        ["UUP", "GLD", "USO"],
    ]
    assert {
        series["source"]["source_role"] for group in module["assets"]["normalized_groups"] for series in group["series"]
    } == {"decision_primary"}
    assert {row["symbol"] for row in module["assets"]["source_identity"]} >= {
        "BTC",
        "VIX",
        "WTI",
    }
    assert _dataset_state(module, "yfinance.spy.intraday")["current_health"] == "current"
    assert matrix[0]["latest_source"]["dataset_id"] == "yfinance.spy.intraday"
    assert matrix[0]["latest_source"]["label"] == "SPDR标普500 ETF"
    assert matrix[0]["latest_source"]["source_role"] == "intraday_proxy"
    assert matrix[0]["latest_source"]["fact"]["market_time_ms"] == intraday
    assert matrix[0]["return_source"]["dataset_id"] == "nasdaq.spy.daily"
    assert matrix[0]["return_source"]["source_role"] == "decision_primary"
    assert matrix[0]["return_source"]["fact"]["change_1w_pct"] == 5.0
    assert matrix[0]["identity_policy"] == "separate_source_facts_no_blend"
    assert matrix[0]["selection_policy"] == "intraday_latest_and_daily_returns_exact"
    assert set(module["assets"]) == {
        "normalized_groups",
        "return_matrix",
        "source_identity",
    }


def test_cross_asset_matrix_keeps_exact_source_roles_without_browser_fallback() -> None:
    daily = int(datetime(2026, 7, 24, 20, tzinfo=UTC).timestamp() * 1_000)
    module = _module(
        "cross_asset",
        market_rows=[_market_row("nasdaq.spy.daily", daily, 105)],
    )

    matrix = module["assets"]["return_matrix"]
    assert len(matrix) == 10
    assert [row["display_order"] for row in matrix] == list(range(1, 11))
    spy = matrix[0]
    assert spy["symbol"] == "SPY"
    assert spy["latest_source"]["dataset_id"] == "yfinance.spy.intraday"
    assert spy["latest_source"]["fact"] is None
    assert spy["return_source"]["dataset_id"] == "nasdaq.spy.daily"
    assert spy["return_source"]["fact"]["latest_value"] == 105
    assert module["futures"]["return_matrix"][0]["latest_source"]["dataset_id"] == ("yfinance.es_future.intraday")
    assert module["futures"]["return_matrix"][0]["latest_source"]["fact"] is None


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

    spy = module["assets"]["return_matrix"][0]
    state = _dataset_state(module, "yfinance.spy.intraday")
    assert spy["latest_source"]["fact"]["latest_value"] == 110
    assert spy["latest_source"]["fact"]["market_time_ms"] == later
    assert spy["selection_policy"] == "intraday_latest_and_daily_returns_exact"
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
    assert state["current_reason"]["code"] == "last_expected_bar_present"
    assert state["required_for_current"] is False
    assert module["status"]["current_health"]["state"] == "unavailable"


def test_next_checkpoints_never_emit_reasonless_critical_placeholders() -> None:
    healthy = {
        "dataset_id": "fred.dgs2",
        "label": "2Y Treasury",
        "current_health": "current",
        "history_depth": "complete",
        "critical": True,
        "current_reason": macro_reason(
            code="within_freshness_budget",
            message="当前事实位于 freshness budget 内。",
            impact="none",
            retryable=False,
            recovery="none",
        ),
        "history_reason": macro_reason(
            code="configured_history_range_complete",
            message="历史范围完整。",
            impact="none",
            retryable=False,
            recovery="none",
        ),
    }
    assert module_payloads._next_checkpoints([healthy]) == []

    scheduled = {
        **healthy,
        "current_reason": macro_reason(
            code="within_freshness_budget",
            message="当前事实位于 freshness budget 内；下一窗口已调度。",
            impact="none",
            retryable=False,
            recovery="none",
            next_check_at_ms=NOW_MS + 1_000,
        ),
    }
    checkpoint = module_payloads._next_checkpoints([scheduled])[0]
    assert checkpoint["reason"]["code"] == "within_freshness_budget"
    assert checkpoint["next_check_at_ms"] == NOW_MS + 1_000


def test_running_backfill_without_durable_schedule_has_no_next_check() -> None:
    module = _module(
        "rates_fed",
        target_states=[
            {
                "dataset_id": "fred.dgs2",
                "partition_key": "2021-07-27..2026-07-27",
                "clock_kind": "backfill",
                "status": "claimed",
                "cursor_json": {"start_date": "2021-07-27"},
            }
        ],
    )

    execution = module["status"]["backfill_execution"]
    assert execution["state"] == "running"
    assert execution["next_check_at_ms"] is None
    assert execution["reason"]["code"] == "history_backfill_running"
    assert execution["reason"]["next_check_at_ms"] is None


def test_empty_rates_decision_does_not_invent_analysis_or_hypotheses() -> None:
    module = _module("rates_fed")

    assert module["decision"]["headline"] is None
    assert module["decision"]["state"] == "incomplete"
    assert module["decision"]["explanation"]["facts"] == []
    assert module["decision"]["explanation"]["bounded_assessments"] == []
    assert module["decision"]["explanation"]["hypotheses"] == []
    assert all(item["reason"] is not None for item in module["next_checkpoints"])


def test_release_payload_preserves_latest_fields_and_bounds_twelve_observations() -> None:
    periods = [
        *(f"2025-M{month:02d}" for month in range(1, 13)),
        "2026-M01",
        "2026-M02",
    ]
    releases = [
        {
            "release_fact_id": f"release:{period}",
            "fact_hash": f"sha256:{index:064x}",
            "dataset_id": "bls.cpi.release",
            "reference_period": period,
            "scheduled_at_ms": NOW_MS + index * 1_000,
            "actual_value": 300.0 + index,
            "estimate_value": 299.5 + index,
            "prior_value": 299.0 + index,
            "revised_prior_value": None,
            "unit": "index",
            "published_at_ms": NOW_MS + index * 1_000,
            "received_at_ms": NOW_MS + index * 1_000 + 100,
            "source_url": f"https://example.com/releases/{period}",
        }
        for index, period in enumerate(periods, start=1)
    ]
    releases.append(
        {
            **releases[-2],
            "release_fact_id": "release:2026-M01:revision",
            "fact_hash": "sha256:" + "f" * 64,
            "revised_prior_value": 311.25,
            "received_at_ms": releases[-2]["received_at_ms"] + 500,
        }
    )

    module = _module("economy_inflation", release_rows=releases)
    reversed_module = _module(
        "economy_inflation",
        release_rows=list(reversed(releases)),
    )
    api_schemas.MacroEconomyInflationPersistedData.model_validate(module)
    summary = module["inflation"]["official_releases"][0]
    observations = summary["observations"]

    assert len(observations) == 12
    assert [item["reference_period"] for item in observations] == list(reversed(periods[2:]))
    assert summary["reference_period"] == observations[0]["reference_period"] == "2026-M02"
    assert summary["actual_value"] == observations[0]["actual_value"]
    assert (
        next(item for item in observations if item["reference_period"] == "2026-M01")["revised_prior_value"] == 311.25
    )
    assert reversed_module["inflation"]["official_releases"] == module["inflation"]["official_releases"]


def test_natural_change_metrics_follow_non_curve_dataset_cadence() -> None:
    monthly_rows = [
        _series_row(
            dataset_id="fred.cpiaucsl",
            reference_date=date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1),
            value=300 + month,
        )
        for month in range(1, 15)
    ]

    economy = _module("economy_inflation", series_rows=monthly_rows)
    cpi_change = next(change for change in economy["summary"]["top_changes"] if change["dataset_id"] == "fred.cpiaucsl")

    assert cpi_change["cadence"] == "monthly"
    assert set(cpi_change["metrics"]) == {"mom_pct", "three_month_annualized_pct", "yoy_pct"}
    assert cpi_change["metric_unit"] == "percent"


def test_natural_change_does_not_relabel_out_of_window_or_missing_period_data() -> None:
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
    bitcoin = next(item for item in module["assets"]["source_identity"] if item["symbol"] == "BTC")
    bitcoin_sources = {item["dataset_id"]: item for item in bitcoin["sources"]}

    assert receipt["selected_dataset_id"] == "binance.btcusdt.spot"
    assert receipt["selection_policy"] == "decision_primary_only_no_fallback"
    assert receipt["identity_policy"] == "separate_source_facts_no_blend"
    assert bitcoin_sources["binance.btcusdt.spot"]["source_role"] == "decision_primary"
    assert bitcoin_sources["yfinance.btc_yahoo.intraday"]["source_role"] == "intraday_proxy"
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
