from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tracefold.app.http.app import create_app
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
    return {
        "schema_version": "macro_module_v1",
        "module_id": "rates_fed",
        "label": "利率与美联储",
        "readiness": "degraded",
        "judgment_cutoff_ms": None,
        "latest_fact_at_ms": 1_000,
        "current_state": {
            "headline": "收益率曲线仍倒挂",
            "dominant_change": "2年期收益率一周下行",
            "feature_count": 1,
            "interpretation": "短端对宽松预期更敏感。",
        },
        "top_changes": [{"dataset_id": "fred.dgs2", "short_change": -0.1}],
        "features": [{"feature_id": "rates.curve_2s10s", "value_numeric": -0.2}],
        "charts": [{"title": "美国国债收益率曲线", "series": []}],
        "contradictions": ["实际利率仍高"],
        "falsifiers": ["2年期收益率重新显著上行"],
        "next_checkpoints": [{"dataset_id": "fred.effr", "state": "current"}],
        "gaps": [
            {
                "dataset_id": "cme.rates.futures.curves",
                "state": "unavailable",
                "reason": "licensed_data_not_configured",
            }
        ],
        "dataset_states": [{"dataset_id": "fred.dgs2", "state": "current"}],
        "raw_evidence": [{"dataset_id": "fred.dgs2", "fact_ref": "macro_fact_1"}],
    }


def test_macro_overview_and_typed_module_routes_are_persisted_only(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                readiness="degraded",
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
                schema_version="macro_evidence_pack_v1",
                compiler_version="macro_decision_compiler_v1",
                payload={"modules": []},
                payload_hash="sha256:pack",
                created_at_ms=1_100,
            )
            repos.macro.insert_daily_judgment(
                session_date=date(2026, 7, 27),
                evidence_pack_id="mep_test",
                judgment_cutoff_ms=1_000,
                latest_fact_at_ms=900,
                judgment={
                    "schema_version": "macro_daily_judgment_v1",
                    "overall_state": "分项压力、尚未共振",
                    "asset_directions": {},
                },
                memo_text="# 每日宏观判断",
                schema_version="macro_daily_judgment_v1",
                compiler_version="macro_decision_compiler_v1",
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
    assert overview_data["modules"][0]["readiness"] == "degraded"
    assert rates.status_code == 200
    assert rates.json()["data"]["current_state"]["headline"] == "收益率曲线仍倒挂"
    assert rates.json()["data"]["gaps"][0]["dataset_id"] == "cme.rates.futures.curves"
    assert all(response.status_code == 200 for response in remaining)
    assert all(response.json()["data"]["readiness"] == "blocked" for response in remaining)


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
