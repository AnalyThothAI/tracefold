from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tests.test_macro_thesis import CUTOFF_MS, SESSION, _draft, _modules, _pack
from tracefold.app.http import routes_macro
from tracefold.app.http.app import create_app
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.thesis import (
    MacroThesisReviewV1,
    build_publication,
    evaluate_live_delta,
    payload_hash,
    pending_outcome_replay,
)
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


def _module_payload(
    module_id="rates_fed",
    *,
    current_health_state: str = "unavailable",
) -> dict:
    payload = build_typed_module_payload(
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
    payload["summary"]["headline"] = "收益率曲线等待官方回填" if module_id == "rates_fed" else f"{module_id} 已持久化"
    payload["status"]["current_health"]["state"] = current_health_state
    if current_health_state == "current":
        health = payload["status"]["current_health"]
        health["current_datasets"] = health["tracked_datasets"]
    return payload


def _insert_thesis(repos) -> dict:
    pack = _pack()
    draft = _draft()
    draft_hash = payload_hash(draft.model_dump(mode="json"))
    review = MacroThesisReviewV1(
        draft_hash=draft_hash,
        disposition="pass",
        findings=("独立复核通过",),
        invocation_id="api-review-1",
        model_name="openai/gpt-5.4-mini",
        prompt_version="macro-thesis-review-v1",
    )
    publication = build_publication(
        evidence_pack=pack,
        draft=draft,
        review=review,
        research_provenance={
            "invocation_id": "api-research-1",
            "model_name": "openai/gpt-5.4-mini",
            "prompt_version": "macro-thesis-research-v1",
        },
        published_at_ms=CUTOFF_MS + 300,
    )
    repos.macro_thesis.insert_evidence_pack(pack)
    repos.macro_thesis.ensure_run(
        pack=pack,
        due_at_ms=CUTOFF_MS,
        max_attempts=2,
        now_ms=CUTOFF_MS + 100,
    )
    repos.macro_thesis.claim_run(
        session_date=SESSION,
        lease_owner="api-owner",
        lease_ms=60_000,
        now_ms=CUTOFF_MS + 200,
    )
    repos.macro_thesis.record_review(
        session_date=SESSION,
        review=review,
        review_sequence=1,
        created_at_ms=CUTOFF_MS + 250,
    )
    repos.macro_thesis.publish(publication=publication, lease_owner="api-owner")
    repos.macro_thesis.insert_live_delta(
        evaluate_live_delta(
            publication=publication,
            modules=_modules(),
            evaluated_at_ms=CUTOFF_MS + 400,
        )
    )
    repos.macro_thesis.insert_outcome_replay(
        pending_outcome_replay(
            publication=publication,
            evaluated_at_ms=CUTOFF_MS + 400,
        )
    )
    return publication.model_dump(mode="json")


def _insert_modules(repos, *, all_current: bool = False) -> None:
    for module_id in MACRO_MODULE_IDS:
        health_state = "current" if all_current or module_id == "rates_fed" else "unavailable"
        payload = _module_payload(
            module_id,
            current_health_state=health_state,
        )
        repos.macro.upsert_module_current(
            module_id=module_id,
            current_health_state=health_state,
            history_depth_state=payload["status"]["history_depth"]["state"],
            fact_cutoff_ms=CUTOFF_MS,
            payload=payload,
            payload_hash=f"sha256:{module_id}",
            updated_at_ms=CUTOFF_MS + 100,
        )


def test_macro_overview_modules_and_research_read_one_persisted_thesis(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000),
    )
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            publication = _insert_thesis(repos)

        overview = client.get("/api/macro/overview", headers=AUTH)
        research = client.get("/api/macro/research", headers=AUTH)
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
    assert overview_data["schema_version"] == "macro_overview_v5"
    assert overview_data["transport"]["state"] == "current"
    assert overview_data["session_date"] == "2026-07-27"
    assert overview_data["thesis_state"] == "published"
    assert overview_data["thesis"]["publication_id"] == publication["publication_id"]
    assert overview_data["live_delta"]["status"] == "insufficient"
    assert {item["reason_code"] for item in overview_data["live_delta"]["items"]} == {"post_cutoff_fact_missing"}
    assert len(overview_data["thesis"]["assets"]) == 12
    assert len(overview_data["modules"]) == 6
    assert overview_data["modules"][0]["module_id"] == "rates_fed"
    assert overview_data["modules"][0]["role"] == "driver"
    assert overview_data["modules"][0]["coverage_state"] == "complete"
    assert overview_data["modules"][0]["current_health_state"] == "current"
    assert overview_data["data_quality"]["current_health_state"] == "degraded"
    assert research.status_code == 200
    assert research.json()["data"]["thesis"]["publication_id"] == publication["publication_id"]
    assert research.json()["data"]["state"] == "current"
    assert rates.status_code == 200
    assert rates.json()["data"]["summary"]["headline"] == "收益率曲线等待官方回填"
    assert rates.json()["data"]["schema_version"] == "macro_rates_fed_v4"
    assert "cme_policy_probabilities" not in rates.json()["data"]["policy_pricing"]
    assert all(response.status_code == 200 for response in remaining)
    assert [response.json()["data"]["schema_version"] for response in remaining] == [
        "macro_economy_inflation_v4",
        "macro_liquidity_funding_v4",
        "macro_credit_v5",
        "macro_volatility_v4",
        "macro_cross_asset_v5",
    ]


def test_macro_overview_exposes_current_session_when_thesis_is_missing(
    tmp_path,
    monkeypatch,
) -> None:
    read_at_ms = int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000)
    cutoff_ms = int(datetime(2026, 7, 27, 12, 50, tzinfo=UTC).timestamp() * 1_000)
    monkeypatch.setattr(routes_macro, "_now_ms", lambda: read_at_ms)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos, all_current=True)
        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)

    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["schema_version"] == "macro_overview_v5"
    assert overview_data["session_date"] == "2026-07-27"
    assert overview_data["cutoff_ms"] == cutoff_ms
    assert overview_data["thesis_state"] == "missing"
    assert overview_data["thesis"] is None
    assert overview_data["live_delta"] is None
    assert rates.status_code == 200
    assert "judgment" not in rates.json()["data"]["status"]


def test_macro_module_reads_fail_closed_without_persisted_projection(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000),
    )
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos:
            before = len(repos.macro.all_modules_current())
        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)
        with client.app.state.service.repositories() as repos:
            after = len(repos.macro.all_modules_current())

    assert before == after == 0
    assert overview.status_code == 503
    assert overview.json() == {
        "ok": False,
        "error": "macro_module_not_materialized:rates_fed",
    }
    assert rates.status_code == 503
    assert rates.json() == {
        "ok": False,
        "error": "macro_module_not_materialized:rates_fed",
    }


def test_prior_thesis_is_historical_and_never_presented_as_current(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 28, 14, tzinfo=UTC).timestamp() * 1_000),
    )
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            publication = _insert_thesis(repos)
        overview = client.get("/api/macro/overview", headers=AUTH)
        historical = client.get(
            "/api/macro/research",
            params={"session_date": "2026-07-27"},
            headers=AUTH,
        )

    assert overview.status_code == 200
    assert overview.json()["data"]["session_date"] == "2026-07-28"
    assert overview.json()["data"]["thesis_state"] == "missing"
    assert overview.json()["data"]["thesis"] is None
    assert historical.status_code == 200
    assert historical.json()["data"]["state"] == "historical"
    assert historical.json()["data"]["thesis"]["publication_id"] == publication["publication_id"]


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
