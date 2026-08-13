from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tracefold.app.database import WorkerDatabase
from tracefold.app.http import routes_macro
from tracefold.app.http.app import create_app
from tracefold.app.repositories import repositories_for_connection
from tracefold.macro.dependencies import MODULE_DATASET_DEPENDENCIES
from tracefold.macro.domain import MACRO_MODULE_IDS, SeriesFact
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.projection import rebuild_all_macro_modules_for_maintenance
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.platform.config.settings import Settings

AUTH = {"Authorization": "Bearer secret"}
CUTOFF_MS = int(datetime(2026, 7, 28, 12, 50, tzinfo=UTC).timestamp() * 1_000)


def test_rates_curve_material_facts_project_to_v8_postgres_and_public_api(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    now_ms = int(datetime(2026, 7, 30, 12, tzinfo=UTC).timestamp() * 1_000)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            for dataset_id, observations in (
                (
                    "treasury.daily_nominal_curve",
                    (
                        (date(2026, 7, 28), {"2Y": 4.26, "7Y": 4.47, "10Y": 4.61, "30Y": 5.09}),
                        (date(2026, 7, 29), {"2Y": 4.22, "7Y": 4.51, "10Y": 4.67, "30Y": 5.20}),
                    ),
                ),
                (
                    "treasury.daily_real_curve",
                    (
                        (date(2026, 7, 28), {"10Y": 2.41, "30Y": 2.92}),
                        (date(2026, 7, 29), {"10Y": 2.41, "30Y": 2.98}),
                    ),
                ),
            ):
                spec = DATASET_REGISTRY[dataset_id]
                for reference_date, values in observations:
                    for tenor, value in values.items():
                        repos.macro.insert_series_fact(
                            SeriesFact(
                                dataset_id=dataset_id,
                                series_id=tenor,
                                reference_date=reference_date,
                                vintage_date=reference_date,
                                value_numeric=value,
                                value_text=None,
                                unit=spec.unit,
                                published_at_ms=now_ms - 60_000,
                                received_at_ms=now_ms - 30_000,
                                source_url=spec.source_url,
                                raw_data={"fixture": "issue-31-rates-vertical-seam"},
                            )
                        )
        worker_db = WorkerDatabase.create(client.app.state.service.settings)
        try:
            result = rebuild_all_macro_modules_for_maintenance(
                db=worker_db,
                now_ms=now_ms,
            )
        finally:
            worker_db.worker_pool.close()
        assert result["modules_computed"] == 6
        with write_repositories() as repos:
            persisted = repos.macro.module_current("rates_fed")
        response = client.get("/api/macro/rates-fed", headers=AUTH)

    assert persisted is not None
    stored = persisted["payload_json"]
    assert stored["schema_version"] == "macro_rates_fed_v8"
    assert "summary" not in stored
    assert "top_changes" not in stored
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["document_analysis_runtime"] == {
        "state": "disabled",
        "enabled": False,
        "configured": False,
        "worker_active": False,
        "model": "gpt-5.4-mini",
    }
    assert data["fed"]["meeting_calendar"] == {"revision_id": None, "meetings": []}
    assert data["treasury_auctions"] == {"recent_results": []}
    assert data["decision"]["headline"] == ("最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）")
    matrix = {item["tenor"]: item for item in data["decision"]["tenor_matrix"]}
    assert matrix["10Y"]["current"]["yield_pct"] == 4.67
    assert next(item for item in matrix["10Y"]["windows"] if item["window"] == "1d")["change_bp"] == 6.0
    assert {item["spread_id"]: item["change_1d_bp"] for item in data["decision"]["spread_summary"]} == {
        "2s10s": 10.0,
        "10s30s": 5.0,
    }
    assert {
        item["tenor"]: (
            item["nominal_change_bp"],
            item["real_change_bp"],
            item["breakeven_change_bp"],
        )
        for item in data["decision"]["decompositions"]
    } == {"10Y": (6.0, 0.0, 6.0), "30Y": (11.0, 6.0, 5.0)}
    assert data["decision"]["classifications"][0]["state"] == "twist_steepening"


def test_module_reason_uses_only_required_current_sources_and_preserves_terminal_recovery() -> None:
    payload = _module_payload("cross_asset")
    payload["status"]["current_health"] = {
        "state": "degraded",
        "tracked_datasets": 2,
        "current_datasets": 1,
        "degraded_datasets": 1,
        "unavailable_datasets": 0,
    }
    payload["evidence"]["dataset_states"] = [
        {
            "dataset_id": "nasdaq.spy.daily",
            "required_for_current": True,
            "current_health": "degraded",
            "current_reason": {
                "code": "source_terminal_stale",
                "recovery": "operator_action",
            },
        },
        {
            "dataset_id": "yfinance.spy.intraday",
            "required_for_current": False,
            "current_health": "unavailable",
            "current_reason": {
                "code": "source_terminal_stale",
                "recovery": "operator_action",
            },
        },
    ]

    reason = routes_macro._available_module_reason(payload)

    assert reason is not None
    assert reason["affected_dataset_ids"] == ["nasdaq.spy.daily"]
    assert reason["retryable"] is False
    assert reason["recovery"] == "operator_action"
    assert reason["next_check_at_ms"] is None


def test_terminal_required_target_reason_reaches_module_and_overview_http(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _insert_modules(repos)
            spec = DATASET_REGISTRY["fred.effr"]
            repos.macro.ensure_target(
                spec,
                now_ms=CUTOFF_MS - 2_000,
                max_attempts=1,
                reactivate_unavailable=False,
            )
            target = repos.macro.claim_target(
                clock_kind=spec.clock_kind,
                lease_owner="macro-api-stale",
                lease_ms=60_000,
                now_ms=CUTOFF_MS - 1_900,
            )
            assert target is not None
            assert repos.macro.fail_target(
                target=target,
                lease_owner="macro-api-stale",
                error_code="provider_exhausted",
                next_due_at_ms=CUTOFF_MS + 60_000,
                completed_at_ms=CUTOFF_MS - 1_700,
                unavailable=True,
            )
            payload = build_typed_module_payload(
                module_id="rates_fed",
                now_ms=CUTOFF_MS,
                series_rows=[],
                market_rows=[],
                position_rows=[],
                settlement_rows=[],
                release_rows=[],
                document_rows=[],
                target_states=repos.macro.target_states(),
            )
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                current_health_state=payload["status"]["current_health"]["state"],
                history_depth_state=payload["status"]["history_depth"]["state"],
                fact_cutoff_ms=CUTOFF_MS,
                payload=payload,
                payload_hash="sha256:terminal-required-target",
                updated_at_ms=CUTOFF_MS,
            )
        module = client.get("/api/macro/rates-fed", headers=AUTH)
        overview = client.get("/api/macro/overview", headers=AUTH)

    assert module.status_code == overview.status_code == 200
    module_reason = module.json()["data"]["reason"]
    overview_reason = next(
        item["reason"] for item in overview.json()["data"]["modules"] if item["module_id"] == "rates_fed"
    )
    for reason in (module_reason, overview_reason):
        assert "fred.effr" in reason["affected_dataset_ids"]
        assert reason["retryable"] is False
        assert reason["recovery"] == "operator_action"
        assert reason["next_check_at_ms"] is None


def test_claimable_required_targets_publish_automatic_next_check_over_http(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    next_check_at_ms = CUTOFF_MS + 60_000
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _insert_modules(repos)
            acquired_specs = tuple(
                DATASET_REGISTRY[dataset_id]
                for dataset_id in MODULE_DATASET_DEPENDENCIES["rates_fed"]
                if DATASET_REGISTRY[dataset_id].clock_kind != "derived"
            )
            for spec in acquired_specs:
                repos.macro.ensure_target(
                    spec,
                    now_ms=CUTOFF_MS - 2_000,
                    max_attempts=2,
                    reactivate_unavailable=False,
                )
            repos.macro.conn.execute(
                """
                UPDATE macro_acquisition_targets
                SET status = 'delayed',
                    next_due_at_ms = %s,
                    updated_at_ms = %s
                WHERE dataset_id = ANY(%s)
                """,
                (
                    next_check_at_ms,
                    CUTOFF_MS - 1_000,
                    [spec.dataset_id for spec in acquired_specs],
                ),
            )
            payload = build_typed_module_payload(
                module_id="rates_fed",
                now_ms=CUTOFF_MS,
                series_rows=[],
                market_rows=[],
                position_rows=[],
                settlement_rows=[],
                release_rows=[],
                document_rows=[],
                target_states=repos.macro.target_states(),
            )
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                current_health_state=payload["status"]["current_health"]["state"],
                history_depth_state=payload["status"]["history_depth"]["state"],
                fact_cutoff_ms=CUTOFF_MS,
                payload=payload,
                payload_hash="sha256:claimable-required-targets",
                updated_at_ms=CUTOFF_MS,
            )
        module = client.get("/api/macro/rates-fed", headers=AUTH)
        overview = client.get("/api/macro/overview", headers=AUTH)

    for reason in (
        module.json()["data"]["reason"],
        next(item["reason"] for item in overview.json()["data"]["modules"] if item["module_id"] == "rates_fed"),
    ):
        assert reason["retryable"] is True
        assert reason["recovery"] == "automatic"
        assert reason["next_check_at_ms"] == next_check_at_ms


def _settings(tmp_path) -> Settings:
    prepare_postgres_database()
    settings = Settings(
        ws_token="secret",
        storage=postgres_settings_storage(),
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings


@contextmanager
def write_repositories():
    conn = connect_postgres_test(read_only=False)
    try:
        yield repositories_for_connection(conn)
    finally:
        conn.close()


def _module_payload(module_id: str) -> dict:
    return build_typed_module_payload(
        module_id=module_id,
        now_ms=CUTOFF_MS,
        series_rows=[],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[],
    )


def _insert_modules(repos) -> None:
    for module_id in MACRO_MODULE_IDS:
        payload = _module_payload(module_id)
        repos.macro.upsert_module_current(
            module_id=module_id,
            current_health_state=payload["status"]["current_health"]["state"],
            history_depth_state=payload["status"]["history_depth"]["state"],
            fact_cutoff_ms=CUTOFF_MS,
            payload=payload,
            payload_hash=f"sha256:{module_id}",
            updated_at_ms=CUTOFF_MS + 100,
        )


def _current_time(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 28, 14, 0, tzinfo=UTC).timestamp() * 1_000),
    )


def test_each_module_rejects_same_version_extra_fields_locally() -> None:
    for module_id in MACRO_MODULE_IDS:
        payload = _module_payload(module_id)
        payload["unexpected_same_version_field"] = True
        validated, reason = routes_macro._module_payload(
            module_id,
            {"payload_json": payload},
        )
        assert validated is None
        assert reason is not None
        assert reason["code"] == "macro_module_schema_mismatch"


def test_overview_serves_six_current_modules_without_research(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _insert_modules(repos)
        overview = client.get("/api/macro/overview", headers=AUTH)
        research = client.get("/api/macro/research", headers=AUTH)

    assert overview.status_code == 200
    data = overview.json()["data"]
    assert data["schema_version"] == "macro_overview_v9"
    assert len(data["modules"]) == 6
    assert "thesis" not in data
    assert "run" not in data
    assert "recovery" not in data
    assert research.status_code == 404


def test_module_reads_revalidate_unchanged_current_payload_with_etag(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _insert_modules(repos)

        first = client.get(
            "/api/macro/rates-fed",
            headers={**AUTH, "Accept-Encoding": "gzip"},
        )
        unchanged = client.get(
            "/api/macro/rates-fed",
            headers={
                **AUTH,
                "If-None-Match": f'"different", {first.headers["etag"]}',
            },
        )
        wildcard = client.get(
            "/api/macro/rates-fed",
            headers={**AUTH, "If-None-Match": "*"},
        )

    assert first.status_code == 200
    assert first.headers["etag"].startswith('W/"')
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["content-encoding"] == "gzip"
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == first.headers["etag"]
    assert unchanged.headers["vary"] == "Accept-Encoding"
    assert wildcard.status_code == 304
    assert wildcard.content == b""
