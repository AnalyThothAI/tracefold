from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from tests.postgres_test_utils import repository_session_for_connection
from tests.support.fake_macro_provider import RecordingStructuredMacroModel
from tests.test_macro_thesis import CUTOFF_MS, SESSION
from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    MacroThesisDeepAgent,
)
from tracefold.macro.assets import MACRO_ASSET_DATASETS, MACRO_THESIS_ASSETS
from tracefold.macro.domain import SeriesFact
from tracefold.macro.projection import MacroProjectionService
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.macro.thesis_service import MacroThesisService
from tracefold.macro.thesis_v2 import (
    MacroDraftAssetOutlook,
    MacroDraftCausalEdge,
    MacroDraftMainline,
    MacroDraftMaterialChange,
    MacroDraftModuleAssessment,
    MacroResearchInputV1,
    MacroThesisDraftV2,
    compile_research_input_v1,
)
from tracefold.market.macro_market_domain import MarketObservationFact

ROOT = Path(__file__).resolve().parents[2]
AUTH_HEADERS = {"Authorization": "Bearer e2e-token"}
PERSISTED_ROUTES = (
    "/api/macro/overview",
    "/api/macro/rates-fed",
    "/api/macro/economy-inflation",
    "/api/macro/liquidity-funding",
    "/api/macro/credit",
    "/api/macro/volatility",
    "/api/macro/cross-asset",
    "/api/macro/research",
)


class _E2EDatabase:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with repository_session_for_connection(self.conn) as repos:
            yield repos


@pytest.mark.e2e
def test_macro_real_vertical_seam_with_fake_outer_provider_only(
    e2e_postgres: str,
    e2e_uvicorn: str,
) -> None:
    with psycopg.connect(e2e_postgres, row_factory=dict_row) as conn:
        with repository_session_for_connection(conn) as repos, repos.transaction():
            _seed_material_macro_facts(repos)
        projection = MacroProjectionService(
            db=_E2EDatabase(conn),
            settings=SimpleNamespace(statement_timeout_seconds=30),
            backfill_worker_enabled=True,
            clock_ms=lambda: CUTOFF_MS,
        )
        projection_result = projection.rebuild(now_ms=CUTOFF_MS)
        assert projection_result["modules_computed"] == 6
        assert projection_result["module_rows_written"] == 6
        _assert_projection_keeps_best_effort_failure_local(conn)

        preview_service = MacroThesisService(
            db=_E2EDatabase(conn),
            settings=SimpleNamespace(
                statement_timeout_seconds=30,
                lease_ms=60_000,
                retry_ms=5_000,
                max_attempts=2,
            ),
            agent=None,
            backfill_worker_enabled=True,
            lease_owner="macro-full-stack-preview",
            clock_ms=lambda: CUTOFF_MS + 2_000,
        )
        pack = preview_service._build_pack(
            session_date=SESSION,
            cutoff_ms=CUTOFF_MS,
            now_ms=CUTOFF_MS + 100,
        )
        research_input = compile_research_input_v1(pack)
        candidate = _candidate_for_real_input(research_input)
        model = RecordingStructuredMacroModel.for_mapping(candidate.model_dump(mode="json"))
        agent = MacroThesisDeepAgent(
            model=model,
            model_name=model.model_name,
            clock_ms=lambda: CUTOFF_MS + 2_000,
        )
        service = MacroThesisService(
            db=_E2EDatabase(conn),
            settings=SimpleNamespace(
                statement_timeout_seconds=30,
                lease_ms=60_000,
                retry_ms=5_000,
                max_attempts=2,
            ),
            agent=agent,
            backfill_worker_enabled=True,
            lease_owner="macro-full-stack-owner",
            clock_ms=lambda: CUTOFF_MS + 2_000,
        )
        result = asyncio.run(service.run_due(now_ms=CUTOFF_MS + 100))

        assert result.status == "published", f"{result.error_code}: {result.error_message}"
        assert result.model_calls == 1
        assert result.publication_rows_written == 1
        assert model.invocation_count == 1
        assert model.bound_tool_names == ()
        assert model.request_metadata["macro_attempt_id"].endswith(":attempt:1")
        assert model.request_metadata["macro_research_input_id"] == research_input.input_id
        assert model.response_format is not None
        assert model.response_format["type"] == "json_schema"
        assert model.response_format["json_schema"]["schema"]["title"] == "MacroThesisDraftV2"
        provider_input = json.loads(str(model.request_messages[-1].content))
        assert provider_input == research_input.model_dump(mode="json")
        assert tuple(item.symbol for item in research_input.momentum) == MACRO_THESIS_ASSETS
        assert all(item.source_dataset_id is not None for item in research_input.momentum)
        assert len(candidate.asset_outlooks) == 2
        for material_outlook in candidate.asset_outlooks:
            material_ref = next(
                item
                for item in research_input.exact_evidence
                if item.evidence_ref == material_outlook.supporting_evidence_refs[0]
            )
            assert material_ref.dataset_id == MACRO_ASSET_DATASETS[material_outlook.symbol]
            assert material_outlook.horizon == "1w"

        serving_counts_before = _serving_counts(conn)

    timings = _assert_running_service_performance(e2e_uvicorn)

    with psycopg.connect(e2e_postgres, row_factory=dict_row) as conn:
        assert _serving_counts(conn) == serving_counts_before
    assert model.invocation_count == 1

    _run_real_browser_acceptance(e2e_uvicorn, timings=timings)


def _seed_material_macro_facts(repos: Any) -> None:
    for symbol_index, symbol in enumerate(MACRO_THESIS_ASSETS, start=1):
        dataset_id = MACRO_ASSET_DATASETS[symbol]
        spec = DATASET_REGISTRY[dataset_id]
        values = (100.0 + symbol_index, 103.0 + symbol_index, 106.0 + symbol_index)
        _insert_three_point_facts(
            repos,
            spec=spec,
            values=values,
            received_offset=symbol_index,
        )
        _record_current_source(repos, spec=spec, suffix=symbol.lower())

    required_cross_asset_support = (
        "fred.dcoilwtico",
        "yfinance.es_future.daily",
        "yfinance.nq_future.daily",
        "yfinance.rty_future.daily",
        "yfinance.zb_future.daily",
        "yfinance.zn_future.daily",
        "yfinance.dx_future.daily",
        "yfinance.gc_future.daily",
        "yfinance.cl_future.daily",
        "yfinance.hg_future.daily",
    )
    for support_index, dataset_id in enumerate(required_cross_asset_support, start=50):
        spec = DATASET_REGISTRY[dataset_id]
        _insert_three_point_facts(
            repos,
            spec=spec,
            values=(90.0 + support_index, 92.0 + support_index, 95.0 + support_index),
            received_offset=support_index,
        )
        _record_current_source(
            repos,
            spec=spec,
            suffix=dataset_id.replace(".", "-"),
        )

    history_spec = DATASET_REGISTRY["fred.dgs2"]
    for days_ago, value in ((31, 4.25), (8, 4.35), (1, 4.45)):
        reference_date = SESSION - timedelta(days=days_ago)
        repos.macro.insert_series_fact(
            SeriesFact(
                dataset_id=history_spec.dataset_id,
                series_id=history_spec.series_id,
                reference_date=reference_date,
                vintage_date=reference_date,
                value_numeric=value,
                value_text=None,
                unit=history_spec.unit,
                published_at_ms=CUTOFF_MS - days_ago * 86_400_000,
                received_at_ms=CUTOFF_MS - 9_000 + days_ago,
                source_url=history_spec.source_url,
                raw_data={"fixture": "full-stack-partial-history"},
            )
        )
    _record_current_source(repos, spec=history_spec, suffix="dgs2")
    repos.macro.enqueue_backfill_target(
        history_spec,
        start_date=SESSION - timedelta(days=365 * 5),
        end_date=SESSION,
        now_ms=CUTOFF_MS - 5_000,
        max_attempts=3,
        history_class="trailing_five_years",
    )

    proxy_spec = DATASET_REGISTRY["yfinance.spy.intraday"]
    repos.macro.ensure_target(proxy_spec, now_ms=CUTOFF_MS - 4_000, max_attempts=1)
    failed_target = repos.macro.claim_target(
        clock_kind=proxy_spec.clock_kind,
        lease_owner="macro-full-stack-seed",
        lease_ms=60_000,
        now_ms=CUTOFF_MS - 3_900,
    )
    if failed_target is None or failed_target["dataset_id"] != proxy_spec.dataset_id:
        raise AssertionError("best-effort proxy target was not claimed")
    repos.macro.record_receipt(
        target=failed_target,
        receipt_id="macro-full-stack-proxy-exhausted",
        started_at_ms=CUTOFF_MS - 3_800,
        completed_at_ms=CUTOFF_MS - 3_700,
        status="failed",
        http_status=503,
        rows_seen=0,
        rows_inserted=0,
        response_hash=None,
        error_code="provider_exhausted",
        error_message="synthetic outer-source exhaustion",
        diagnostics={"fixture": "full-stack-terminal-best-effort"},
    )
    assert repos.macro.fail_target(
        target=failed_target,
        lease_owner="macro-full-stack-seed",
        receipt_id="macro-full-stack-proxy-exhausted",
        error_code="provider_exhausted",
        next_due_at_ms=CUTOFF_MS + 86_400_000,
        completed_at_ms=CUTOFF_MS - 3_700,
        unavailable=False,
    )


def _insert_three_point_facts(
    repos: Any,
    *,
    spec: Any,
    values: tuple[float, float, float],
    received_offset: int,
) -> None:
    if spec.fact_family == "series":
        for days_ago, value in zip((31, 8, 1), values, strict=True):
            reference_date = SESSION - timedelta(days=days_ago)
            repos.macro.insert_series_fact(
                SeriesFact(
                    dataset_id=spec.dataset_id,
                    series_id=spec.series_id,
                    reference_date=reference_date,
                    vintage_date=reference_date,
                    value_numeric=value,
                    value_text=None,
                    unit=spec.unit,
                    published_at_ms=CUTOFF_MS - days_ago * 86_400_000,
                    received_at_ms=CUTOFF_MS - 10_000 + received_offset,
                    source_url=spec.source_url,
                    raw_data={"fixture": "full-stack-material-fact"},
                )
            )
        return
    if spec.fact_family != "market_observation":
        raise AssertionError(f"unsupported positive-seam fact family: {spec.fact_family}")
    repos.macro_market.ensure_instrument(spec, now_ms=CUTOFF_MS - 20_000)
    if spec.instrument_id is None:
        raise AssertionError(f"market instrument missing for {spec.dataset_id}")
    for days_ago, value in zip((31, 8, 1), values, strict=True):
        repos.macro_market.insert_observation(
            MarketObservationFact(
                dataset_id=spec.dataset_id,
                instrument_id=spec.instrument_id,
                source_id=spec.source_id,
                field_name="close",
                value_numeric=value,
                unit=spec.unit,
                observed_at_ms=CUTOFF_MS - days_ago * 86_400_000,
                published_at_ms=CUTOFF_MS - days_ago * 86_400_000,
                received_at_ms=CUTOFF_MS - 10_000 + received_offset,
                trust_tier=spec.trust_tier,
                source_url=spec.source_url,
                raw_data={"fixture": "full-stack-material-fact"},
            )
        )


def _record_current_source(repos: Any, *, spec: Any, suffix: str) -> None:
    repos.macro.ensure_target(spec, now_ms=CUTOFF_MS - 20_000, max_attempts=2)
    target = repos.macro.claim_target(
        clock_kind=spec.clock_kind,
        lease_owner="macro-full-stack-seed",
        lease_ms=60_000,
        now_ms=CUTOFF_MS - 19_000,
    )
    if target is None or target["dataset_id"] != spec.dataset_id:
        raise AssertionError(f"source target was not claimed for {spec.dataset_id}")
    receipt_id = f"macro-full-stack-{suffix}"
    repos.macro.record_receipt(
        target=target,
        receipt_id=receipt_id,
        started_at_ms=CUTOFF_MS - 18_000,
        completed_at_ms=CUTOFF_MS - 17_000,
        status="ok",
        http_status=200,
        rows_seen=3,
        rows_inserted=3,
        response_hash=f"sha256:{suffix}",
        error_code=None,
        error_message=None,
        diagnostics={"fixture": "full-stack-current-source"},
    )
    assert repos.macro.complete_target(
        target_key=spec.target_key,
        lease_owner="macro-full-stack-seed",
        receipt_id=receipt_id,
        cursor={"fixture": "full-stack-current-source"},
        next_due_at_ms=CUTOFF_MS + 86_400_000,
        completed_at_ms=CUTOFF_MS - 17_000,
    )


def _candidate_for_real_input(research_input: MacroResearchInputV1) -> MacroThesisDraftV2:
    symbol_by_dataset = {dataset_id: symbol for symbol, dataset_id in MACRO_ASSET_DATASETS.items()}
    material_evidence = tuple(item for item in research_input.exact_evidence if item.dataset_id in symbol_by_dataset)[
        :2
    ]
    if len(material_evidence) != 2:
        raise AssertionError("full-stack input must retain two material asset facts")
    primary_evidence = material_evidence[0]
    primary_symbol = symbol_by_dataset[primary_evidence.dataset_id]
    outlooks = tuple(
        MacroDraftAssetOutlook(
            outlook_id=f"outlook-{symbol.lower()}-1w",
            symbol=symbol,
            horizon="1w",
            outlook_context="mainline",
            direction="bullish",
            causal_transmission=f"The observed momentum supports {symbol} over the declared one-week horizon.",
            supporting_evidence_refs=(evidence.evidence_ref,),
            conflicting_evidence_refs=(),
            confidence=None,
        )
        for evidence in material_evidence
        for symbol in (symbol_by_dataset[evidence.dataset_id],)
    )
    return MacroThesisDraftV2(
        session_date=research_input.session_date,
        cutoff_ms=research_input.cutoff_ms,
        evidence_pack_id=research_input.evidence_pack_id,
        research_input_id=research_input.input_id,
        mainline=MacroDraftMainline(
            stance="call",
            title=f"{primary_symbol} weekly momentum is the material cross-asset signal",
            thesis="Canonical daily facts show a positive weekly impulse without forcing views on unrelated assets.",
            stage="developing",
            horizon="1w",
            confidence=None,
            causal_edges=(
                MacroDraftCausalEdge(
                    edge_id="edge-material-risk-appetite",
                    source=f"{primary_symbol} weekly momentum",
                    mechanism="persistent demand for the observed asset",
                    target=f"one-week {primary_symbol} direction",
                    evidence_refs=(primary_evidence.evidence_ref,),
                    conflicting_evidence_refs=(),
                ),
            ),
            supporting_evidence_refs=(primary_evidence.evidence_ref,),
            conflicting_evidence_refs=(),
            no_call_reason=None,
        ),
        module_assessments=(
            MacroDraftModuleAssessment(
                module_id="cross_asset",
                role="driver",
                analysis=f"Canonical {primary_symbol} facts provide the material directional input.",
                evidence_refs=tuple(item.evidence_ref for item in material_evidence),
            ),
        ),
        material_changes=(
            MacroDraftMaterialChange(
                change_id="change-material-weekly",
                status="strengthened",
                statement=f"{primary_symbol} weekly momentum strengthened in the sealed input.",
                evidence_refs=(primary_evidence.evidence_ref,),
            ),
        ),
        asset_outlooks=outlooks,
        condition_uses=(),
    )


def _assert_projection_keeps_best_effort_failure_local(conn: Any) -> None:
    cross_asset = conn.execute(
        "SELECT payload_json FROM macro_module_current WHERE module_id = 'cross_asset'"
    ).fetchone()["payload_json"]
    proxy = next(
        item for item in cross_asset["evidence"]["dataset_states"] if item["dataset_id"] == "yfinance.spy.intraday"
    )
    history = conn.execute("SELECT payload_json FROM macro_module_current WHERE module_id = 'rates_fed'").fetchone()[
        "payload_json"
    ]
    dgs2 = next(item for item in history["evidence"]["dataset_states"] if item["dataset_id"] == "fred.dgs2")
    spy_daily = next(
        item for item in cross_asset["evidence"]["dataset_states"] if item["dataset_id"] == "nasdaq.spy.daily"
    )
    assert proxy["required_for_current"] is False
    assert proxy["current_health"] == "unavailable"
    assert proxy["current_reason"]["code"] == "source_terminal_stale"
    assert proxy["current_reason"]["recovery"] == "operator_action"
    assert proxy["current_reason"]["next_check_at_ms"] is None
    assert cross_asset["status"]["current_health"]["state"] == "current"
    assert cross_asset["status"]["history_depth"]["state"] == "not_required"
    assert spy_daily["required_for_history"] is False
    assert spy_daily["history_depth"] == "partial"
    assert spy_daily["history_reason"]["code"] == "optional_maximum_history_incomplete"
    assert spy_daily["history_reason"]["impact"] == "none"
    assert dgs2["required_for_current"] is False
    assert dgs2["history_depth"] == "partial"


def _assert_running_service_performance(base_url: str) -> dict[str, dict[str, float]]:
    timings: dict[str, dict[str, float]] = {}
    with httpx.Client(base_url=base_url, headers=AUTH_HEADERS, timeout=5.0) as client:
        for route in PERSISTED_ROUTES:
            cold_started_at = time.perf_counter()
            cold = client.get(route)
            cold_seconds = time.perf_counter() - cold_started_at
            assert cold.status_code == 200, f"{route}: {cold.text}"
            assert cold_seconds <= 5.0

            warm_samples: list[float] = []
            payload_size = len(cold.content)
            for _ in range(20):
                started_at = time.perf_counter()
                response = client.get(route)
                warm_samples.append(time.perf_counter() - started_at)
                assert response.status_code == 200, f"{route}: {response.text}"
                payload_size = max(payload_size, len(response.content))
            ordered = sorted(warm_samples)
            p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
            maximum = ordered[-1]
            assert p95 <= 2.5, f"{route} warm p95={p95:.3f}s"
            assert maximum <= 5.0, f"{route} warm max={maximum:.3f}s"
            timings[route] = {
                "cold_seconds": cold_seconds,
                "warm_p95_seconds": p95,
                "warm_max_seconds": maximum,
                "payload_bytes": float(payload_size),
            }
    return timings


def _serving_counts(conn: Any) -> dict[str, int]:
    tables = (
        "macro_thesis_publications",
        "macro_live_deltas",
        "macro_outcome_replays",
        "macro_module_current",
    )
    return {table: int(conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]) for table in tables}


def _run_real_browser_acceptance(
    base_url: str,
    *,
    timings: dict[str, dict[str, float]],
) -> None:
    env = {
        **os.environ,
        "TRACEFOLD_FULL_STACK_URL": base_url,
        "TRACEFOLD_FULL_STACK_TIMINGS": json.dumps(timings, sort_keys=True),
    }
    result = subprocess.run(
        [
            "npx",
            "playwright",
            "test",
            "--config=playwright.full-stack.config.ts",
        ],
        cwd=str(ROOT / "web"),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"real FastAPI/PostgreSQL/React/Playwright lane failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
