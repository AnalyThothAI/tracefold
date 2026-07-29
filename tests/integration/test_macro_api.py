from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tests.test_macro_thesis import CUTOFF_MS, SESSION, _draft, _modules
from tracefold.app.http import routes_macro
from tracefold.app.http.app import create_app
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.reasons import macro_reason
from tracefold.macro.thesis import (
    MacroThesisReviewV1,
    build_publication,
    compile_evidence_pack_v3,
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
    target_states: list[dict] | None = None,
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
        target_states=target_states or [],
    )
    payload["summary"]["headline"] = "收益率曲线等待官方回填" if module_id == "rates_fed" else f"{module_id} 已持久化"
    payload["status"]["current_health"]["state"] = current_health_state
    if current_health_state == "current":
        health = payload["status"]["current_health"]
        health["current_datasets"] = health["tracked_datasets"]
    return payload


def _insert_thesis(repos) -> dict:
    modules = list(_modules())
    modules[0]["evidence"]["reconciliation_receipts"] = [
        {
            "concept_id": "test.rates",
            "state": "complete",
            "selection_policy": "decision_primary_only_no_fallback",
            "selected_dataset_id": "fred.dgs2",
            "identity_policy": "separate_source_facts_no_blend",
            "observations": [
                {
                    "dataset_id": "fred.dgs2",
                    "source_role": "decision_primary",
                    "reference": "2026-07-27",
                    "value": 4.3,
                    "unit": "percent",
                    "fact_ref": "fact:rates_fed",
                }
            ],
            "comparisons": [],
        }
    ]
    pack = compile_evidence_pack_v3(
        session_date=SESSION,
        cutoff_ms=CUTOFF_MS,
        sealed_at_ms=CUTOFF_MS + 1_000,
        modules=modules,
        prior_publication=None,
    )
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


def _insert_modules(
    repos,
    *,
    all_current: bool = False,
    omit: frozenset[str] = frozenset(),
) -> None:
    for module_id in MACRO_MODULE_IDS:
        if module_id in omit:
            continue
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


def test_each_module_uses_its_exact_persisted_payload_contract() -> None:
    malformed_paths = {
        "rates_fed": ("curve",),
        "economy_inflation": ("inflation",),
        "liquidity_funding": ("funding",),
        "credit": ("confirmations",),
        "volatility": ("cross_asset_implied",),
        "cross_asset": ("assets",),
    }

    for module_id, path in malformed_paths.items():
        payload = _module_payload(module_id)
        payload[path[0]]["unexpected_same_version_field"] = True
        validated, reason = routes_macro._module_payload(
            module_id,
            {"payload_json": payload},
        )

        assert validated is None
        assert reason is not None
        assert reason["code"] == "macro_module_schema_mismatch"


def test_same_version_nested_malformed_module_degrades_locally(
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
            _insert_modules(repos, all_current=True)
            malformed = _module_payload("credit", current_health_state="current")
            del malformed["confirmations"]["return_matrix"][0]["latest_source"]["dataset_id"]
            repos.macro.upsert_module_current(
                module_id="credit",
                current_health_state="current",
                history_depth_state=malformed["status"]["history_depth"]["state"],
                fact_cutoff_ms=CUTOFF_MS,
                payload=malformed,
                payload_hash="sha256:credit-same-version-malformed",
                updated_at_ms=CUTOFF_MS + 200,
            )

        overview = client.get("/api/macro/overview", headers=AUTH)
        credit = client.get("/api/macro/credit", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)
        research = client.get("/api/macro/research", headers=AUTH)

    assert overview.status_code == 200
    assert credit.status_code == 200
    assert rates.status_code == 200
    assert research.status_code == 200
    overview_modules = overview.json()["data"]["modules"]
    credit_summary = next(item for item in overview_modules if item["module_id"] == "credit")
    assert credit_summary["availability"] == "unavailable"
    assert credit_summary["reason"]["code"] == "macro_module_schema_mismatch"
    assert sum(item["availability"] == "available" for item in overview_modules) == 5
    assert credit.json()["data"]["schema_version"] == "macro_module_unavailable_v1"
    assert credit.json()["data"]["reason"]["code"] == "macro_module_schema_mismatch"
    assert rates.json()["data"]["schema_version"] == "macro_rates_fed_v5"


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
    assert overview_data["schema_version"] == "macro_overview_v7"
    assert overview_data["transport"]["state"] == "current"
    assert overview_data["session_date"] == "2026-07-27"
    assert overview_data["displayed_session_date"] == "2026-07-27"
    assert overview_data["thesis_state"] == "published"
    assert overview_data["thesis_reason"] is None
    assert overview_data["thesis"]["publication_id"] == publication["publication_id"]
    assert overview_data["mainline_presentation"]["title"]["origin"] == "publication"
    assert "status" not in overview_data["live_delta"]
    assert overview_data["live_delta"]["mainline_validity"] == "insufficient"
    assert overview_data["live_delta"]["scopes"][0]["label"] == "整体主线"
    assert all(scope["label"] for scope in overview_data["live_delta"]["scopes"])
    assert {
        item["source_reason_code"] for scope in overview_data["live_delta"]["scopes"] for item in scope["items"]
    } == {"post_cutoff_fact_missing"}
    assert len(overview_data["thesis"]["assets"]) == 12
    assert len(overview_data["asset_presentation"]) == 12
    assert overview_data["claim_presentation"][0]["asset_implications"]
    assert "causal_channel" not in overview_data["asset_presentation"][0]["horizons"][0]
    assert "reader_rationale" in overview_data["asset_presentation"][0]["horizons"][0]
    assert overview_data["outcome_replay"]["schema_version"] == "macro_outcome_replay_read_v1"
    assert overview_data["outcome_replay"]["horizons"][0]["reason"]["next_check_at_ms"]
    assert len(overview_data["modules"]) == 6
    assert overview_data["modules"][0]["module_id"] == "rates_fed"
    assert overview_data["modules"][0]["role"] == "driver"
    assert overview_data["modules"][0]["coverage_state"] == "complete"
    assert overview_data["modules"][0]["current_health_state"] == "current"
    assert overview_data["data_quality"]["current_health_state"] == "degraded"
    assert research.status_code == 200
    research_data = research.json()["data"]
    assert research_data["schema_version"] == "macro_thesis_detail_v3"
    assert research_data["thesis"]["publication_id"] == publication["publication_id"]
    assert research_data["state"] == "current"
    assert research_data["displayed_session_date"] == "2026-07-27"
    assert research_data["appendix"]["publication_id"] == publication["publication_id"]
    assert research_data["mainline_presentation"] == overview_data["mainline_presentation"]
    assert research_data["appendix"]["data_quality"]["current_health_state"] == "current"
    assert len(research_data["appendix"]["source_lineage"]) == 6
    rates_lineage = research_data["appendix"]["source_lineage"][0]
    assert rates_lineage["observed_at_ms"] == CUTOFF_MS - 1_000
    assert rates_lineage["published_at_ms"] is None
    assert rates_lineage["received_at_ms"] == CUTOFF_MS - 1_000
    rates_data = rates.json()["data"]
    assert "analysis" not in rates_data["thesis_context"]
    assert rates_data["thesis_context"]["reader_narrative"]["text"]
    assert research_data["appendix"]["reconciliation_receipts"][0]["concept_id"] == "test.rates"
    assert set(research_data["history"][0]) == {
        "publication_id",
        "session_date",
        "cutoff_ms",
        "published_at_ms",
        "title",
        "stance",
        "confidence",
        "horizon",
    }
    assert rates.status_code == 200
    assert rates.json()["data"]["summary"]["headline"] == "收益率曲线等待官方回填"
    assert rates.json()["data"]["schema_version"] == "macro_rates_fed_v5"
    assert rates.json()["data"]["thesis_context"]["cutoff_ms"] == publication["cutoff_ms"]
    assert rates.json()["data"]["thesis_context"]["annotations"]
    assert "cme_policy_probabilities" not in rates.json()["data"]["policy_pricing"]
    assert all(response.status_code == 200 for response in remaining)
    assert [response.json()["data"]["schema_version"] for response in remaining] == [
        "macro_economy_inflation_v5",
        "macro_liquidity_funding_v5",
        "macro_credit_v7",
        "macro_volatility_v6",
        "macro_cross_asset_v6",
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
    assert overview_data["schema_version"] == "macro_overview_v7"
    assert overview_data["session_date"] == "2026-07-27"
    assert overview_data["cutoff_ms"] == cutoff_ms
    assert overview_data["thesis_state"] == "missing"
    assert overview_data["thesis_reason"]["code"] == "macro_thesis_run_missing"
    assert overview_data["thesis"] is None
    assert overview_data["displayed_session_date"] is None
    assert overview_data["fallback"]["state"] == "none"
    assert overview_data["asset_presentation"] == []
    assert overview_data["claim_presentation"] == []
    assert overview_data["live_delta"] is None
    assert rates.status_code == 200
    assert "judgment" not in rates.json()["data"]["status"]


def test_macro_module_reads_degrade_locally_without_persisted_projection(
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
            _insert_modules(
                repos,
                all_current=True,
                omit=frozenset({"rates_fed"}),
            )
            before = len(repos.macro.all_modules_current())
        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)
        with client.app.state.service.repositories() as repos:
            after = len(repos.macro.all_modules_current())

    assert before == after == 5
    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert len(overview_data["modules"]) == 6
    unavailable = [module for module in overview_data["modules"] if module["availability"] == "unavailable"]
    assert [module["module_id"] for module in unavailable] == ["rates_fed"]
    assert unavailable[0]["reason"]["code"] == "macro_module_not_materialized"
    assert sum(module["availability"] == "available" for module in overview_data["modules"]) == 5
    assert rates.status_code == 200
    assert rates.json()["data"]["schema_version"] == "macro_module_unavailable_v1"
    assert rates.json()["data"]["reason"]["code"] == "macro_module_not_materialized"


def test_available_degraded_module_exposes_typed_impact_and_real_next_check(
    tmp_path,
    monkeypatch,
) -> None:
    read_at_ms = int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000)
    next_check_at_ms = read_at_ms + 60_000
    monkeypatch.setattr(routes_macro, "_now_ms", lambda: read_at_ms)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_thesis(repos)
            payload = _module_payload("rates_fed")
            states = payload["evidence"]["dataset_states"]
            affected = states[0]
            affected["current_health"] = "degraded"
            affected["current_reason"] = macro_reason(
                code="expected_fact_delayed",
                message="预期事实尚未到达，当前事实已延迟。",
                impact="limited",
                affected_dataset_ids=(affected["dataset_id"],),
                retryable=True,
                recovery="automatic",
                next_action="等待下一次采集并重新投影该 Dataset。",
                next_check_at_ms=next_check_at_ms,
            )
            for state in states[1:]:
                state["current_health"] = "current"
            payload["status"]["current_health"].update(
                {
                    "state": "degraded",
                    "current_datasets": len(states) - 1,
                    "tracked_datasets": len(states),
                }
            )
            payload["status"]["history_depth"].update(
                {
                    "state": "not_required",
                    "complete_datasets": 0,
                    "tracked_datasets": 0,
                }
            )
            payload["status"]["backfill_execution"].update(
                {
                    "state": "not_required",
                    "reason": None,
                    "next_check_at_ms": None,
                }
            )
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                current_health_state="degraded",
                history_depth_state="not_required",
                fact_cutoff_ms=CUTOFF_MS,
                payload=payload,
                payload_hash="sha256:rates-degraded",
                updated_at_ms=CUTOFF_MS + 100,
            )

        overview = client.get("/api/macro/overview", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)

    assert overview.status_code == 200
    assert rates.status_code == 200
    overview_reason = next(
        module["reason"] for module in overview.json()["data"]["modules"] if module["module_id"] == "rates_fed"
    )
    route_reason = rates.json()["data"]["reason"]
    assert overview_reason == route_reason
    assert route_reason["code"] == "macro_module_current_degraded"
    assert route_reason["impact"] == "limited"
    assert route_reason["affected_dataset_ids"] == [affected["dataset_id"]]
    assert route_reason["affected_claim_ids"] == ["claim-rates"]
    assert route_reason["retryable"] is True
    assert route_reason["recovery"] == "automatic"
    assert route_reason["next_action"] == "等待下一次采集并重新投影该 Dataset。"
    assert route_reason["next_check_at_ms"] == next_check_at_ms


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
            current_pack = compile_evidence_pack_v3(
                session_date=date(2026, 7, 28),
                cutoff_ms=CUTOFF_MS + 86_400_000,
                sealed_at_ms=CUTOFF_MS + 86_401_000,
                modules=_modules(),
                prior_publication=publication,
            )
            repos.macro_thesis.insert_evidence_pack(current_pack)
            repos.macro_thesis.ensure_run(
                pack=current_pack,
                due_at_ms=current_pack.cutoff_ms,
                max_attempts=2,
                now_ms=current_pack.cutoff_ms + 100,
            )
            assert repos.macro_thesis.mark_configuration_error_before_attempt(
                session_date=date(2026, 7, 28),
                error_code="macro_thesis_configuration_error",
                error_message="sk-secret https://private.example/error",
                now_ms=current_pack.cutoff_ms + 200,
            )
        overview = client.get("/api/macro/overview", headers=AUTH)
        current = client.get("/api/macro/research", headers=AUTH)
        historical = client.get(
            "/api/macro/research",
            params={"session_date": "2026-07-27"},
            headers=AUTH,
        )

    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["session_date"] == "2026-07-28"
    assert overview_data["displayed_session_date"] == "2026-07-27"
    assert overview_data["thesis_state"] == "config_error"
    assert overview_data["thesis_reason"]["code"] == "macro_thesis_configuration_error"
    assert overview_data["thesis"]["publication_id"] == publication["publication_id"]
    assert overview_data["fallback"]["state"] == "available"
    assert overview_data["fallback"]["publication_id"] == publication["publication_id"]
    assert overview_data["run"]["session_date"] == "2026-07-28"
    assert overview_data["run"]["status"] == "config_error"
    assert "error_message" not in overview_data["run"]
    assert "sk-secret" not in overview.text
    assert "private.example" not in overview.text
    assert current.status_code == 200
    current_data = current.json()["data"]
    assert current_data["state"] == "failed"
    assert current_data["requested_session_date"] == "2026-07-28"
    assert current_data["displayed_session_date"] == "2026-07-27"
    assert current_data["thesis"]["publication_id"] == publication["publication_id"]
    assert current_data["fallback"]["state"] == "available"
    assert current_data["live_delta"] is None
    assert current_data["outcome_replay"] is None
    assert current_data["appendix"]["publication_id"] == publication["publication_id"]
    assert current_data["run"]["reason"]["retryable"] is False
    assert current_data["run"]["reason"]["recovery"] == "operator_action"
    assert current_data["run"]["updated_at_ms"] == current_pack.cutoff_ms + 200
    assert historical.status_code == 200
    historical_data = historical.json()["data"]
    assert historical_data["state"] == "historical"
    assert historical_data["thesis"]["publication_id"] == publication["publication_id"]
    assert historical_data["live_delta"] is None
    assert historical_data["outcome_replay"] is None
    assert historical_data["appendix"]["publication_id"] == publication["publication_id"]
    assert historical_data["appendix"]["data_quality"]["current_health_state"] == "current"


def test_disabled_backfill_worker_is_explicitly_paused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        routes_macro,
        "_now_ms",
        lambda: int(datetime(2026, 7, 27, 14, tzinfo=UTC).timestamp() * 1_000),
    )
    targets = [
        {
            "dataset_id": "fred.dgs2",
            "partition_key": "2021-07-27..2026-07-27",
            "clock_kind": "backfill",
            "status": "backfilling",
            "cursor_json": {"start_date": "2021-07-27"},
            "next_due_at_ms": CUTOFF_MS + 1_000,
        },
        {
            "dataset_id": "fred.dgs10",
            "partition_key": "2021-07-27..2026-07-27",
            "clock_kind": "backfill",
            "status": "current",
        },
    ]
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            payload = _module_payload("rates_fed", target_states=targets)
            repos.macro.upsert_module_current(
                module_id="rates_fed",
                current_health_state=payload["status"]["current_health"]["state"],
                history_depth_state=payload["status"]["history_depth"]["state"],
                fact_cutoff_ms=CUTOFF_MS,
                payload=payload,
                payload_hash="sha256:rates-paused",
                updated_at_ms=CUTOFF_MS + 100,
            )
        rates = client.get("/api/macro/rates-fed", headers=AUTH)

    assert rates.status_code == 200
    execution = rates.json()["data"]["status"]["backfill_execution"]
    assert execution["state"] == "paused"
    assert execution["worker_enabled"] is False
    assert execution["reason"]["code"] == "history_backfill_worker_disabled"
    assert execution["reason"]["affected_dataset_ids"] == ["fred.dgs2"]


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
