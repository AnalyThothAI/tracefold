from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tracefold.app.http import routes_macro
from tracefold.app.http.app import create_app
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.platform.config.settings import Settings

AUTH = {"Authorization": "Bearer secret"}


def _settings(tmp_path) -> Settings:
    prepare_postgres_database()
    settings = Settings(
        ws_token="secret",
        storage=postgres_settings_storage(),
        workers=disabled_workers_settings(),
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings


def _module_payload() -> dict:
    payload = build_typed_module_payload(
        module_id="rates_fed",
        now_ms=1_000,
        series_rows=[],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[],
    )
    payload["summary"]["headline"] = "收益率曲线等待官方回填"
    return payload


def test_macro_overview_and_typed_module_routes_are_persisted_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 27, 13, tzinfo=UTC).timestamp() * 1_000),
    )
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                data_health_state="invalid",
                fact_cutoff_ms=1_000,
                payload=_module_payload(),
                payload_hash="sha256:module",
                updated_at_ms=1_100,
            )
            repos.macro.insert_evidence_pack(
                evidence_pack_id="mep_test",
                session_date=date(2026, 7, 27),
                judgment_cutoff_ms=1_000,
                latest_fact_at_ms=900,
                schema_version="macro_evidence_pack_v2",
                compiler_version="macro_professional_coverage_compiler_v2",
                payload={"schema_version": "macro_evidence_pack_v2", "modules": []},
                payload_hash="sha256:pack",
                created_at_ms=1_100,
            )
            repos.macro.insert_daily_judgment(
                session_date=date(2026, 7, 27),
                evidence_pack_id="mep_test",
                judgment_cutoff_ms=1_000,
                latest_fact_at_ms=900,
                judgment={
                    "schema_version": "macro_daily_judgment_v2",
                    "overall_state": "分项压力、尚未共振",
                    "asset_directions": {},
                },
                memo_text="# 每日宏观判断",
                schema_version="macro_daily_judgment_v2",
                compiler_version="macro_professional_coverage_compiler_v2",
                payload_hash="sha256:judgment",
                published_at_ms=1_100,
            )

        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)
        remaining = [
            client.get(path, headers=AUTH)
            for path in (
                "/api/macro/economy-inflation",
                "/api/macro/liquidity-funding",
                "/api/macro/credit",
                "/api/macro/volatility",
                "/api/macro/cross-asset",
            )
        ]

    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["daily_judgment"]["overall_state"] == "分项压力、尚未共振"
    assert len(overview_data["modules"]) == 6
    assert overview_data["modules"][0]["module_id"] == "rates_fed"
    assert overview_data["modules"][0]["coverage_state"] == "licensed_unavailable"
    assert overview_data["modules"][0]["data_health_state"] == "invalid"
    assert overview_data["modules"][0]["judgment_state"] == "current"
    assert rates.status_code == 200
    assert rates.json()["data"]["summary"]["headline"] == "收益率曲线等待官方回填"
    assert rates.json()["data"]["schema_version"] == "macro_rates_fed_v2"
    assert rates.json()["data"]["policy_pricing"]["cme_policy_probabilities"]["state"] == ("licensed_unavailable")
    assert all(response.status_code == 200 for response in remaining)
    assert [response.json()["data"]["schema_version"] for response in remaining] == [
        "macro_economy_inflation_v2",
        "macro_liquidity_funding_v2",
        "macro_credit_v2",
        "macro_volatility_v2",
        "macro_cross_asset_v2",
    ]
    assert all(response.json()["data"]["status"]["judgment"]["state"] == "current" for response in remaining)


def test_macro_routes_do_not_present_prior_session_judgment_as_current(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 27, 13, tzinfo=UTC).timestamp() * 1_000),
    )
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            repos.macro.insert_evidence_pack(
                evidence_pack_id="mep_prior",
                session_date=date(2026, 7, 24),
                judgment_cutoff_ms=1_000,
                latest_fact_at_ms=900,
                schema_version="macro_evidence_pack_v2",
                compiler_version="macro_professional_coverage_compiler_v2",
                payload={"schema_version": "macro_evidence_pack_v2", "modules": []},
                payload_hash="sha256:prior-pack",
                created_at_ms=1_100,
            )
            repos.macro.insert_daily_judgment(
                session_date=date(2026, 7, 24),
                evidence_pack_id="mep_prior",
                judgment_cutoff_ms=1_000,
                latest_fact_at_ms=900,
                judgment={
                    "schema_version": "macro_daily_judgment_v2",
                    "overall_state": "历史判断",
                    "asset_directions": {},
                },
                memo_text="# 历史判断",
                schema_version="macro_daily_judgment_v2",
                compiler_version="macro_professional_coverage_compiler_v2",
                payload_hash="sha256:prior-judgment",
                published_at_ms=1_100,
            )
        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)

    assert overview.status_code == 200
    assert overview.json()["data"]["judgment_state"] == "missing"
    assert overview.json()["data"]["daily_judgment"] is None
    assert rates.json()["data"]["status"]["judgment"]["state"] == "missing"


def test_macro_hard_cut_rejects_generic_windows_and_removed_routes(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        window = client.get(
            "/api/macro/rates-fed",
            params={"window": "90d"},
            headers=AUTH,
        )
        retired = [
            client.get(path, headers=AUTH)
            for path in (
                "/api/macro/evidence/overview",
                "/api/macro/rates-inflation",
                "/api/macro/growth-labor",
            )
        ]

    assert window.status_code == 400
    assert window.json() == {
        "ok": False,
        "error": "unsupported_query_param",
        "field": "window",
    }
    assert all(response.status_code == 404 for response in retired)
