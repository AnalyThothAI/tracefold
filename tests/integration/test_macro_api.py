from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tests.test_macro_thesis import (
    CUTOFF_MS,
    SESSION,
    _modules,
    _pack,
    _publication,
    _research_input,
)
from tracefold.app.http import routes_macro
from tracefold.app.http.app import create_app
from tracefold.macro.assets import MACRO_THESIS_ASSETS
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module
from tracefold.macro.thesis import (
    MacroAssetOutlook,
    MacroCausalEdge,
    MacroCondition,
    MacroHorizonOutlook,
    MacroMainline,
    MacroModuleRole,
    MacroNarrativeSection,
    MacroThesisBodyDraft,
    MacroThesisClaim,
    MacroThesisReviewV1,
    build_publication,
    payload_hash,
)
from tracefold.macro.thesis_v2 import (
    evaluate_live_delta_v2,
    evaluate_outcome_replay_v2,
)
from tracefold.platform.config.settings import Settings

AUTH = {"Authorization": "Bearer secret"}


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
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            spec = DATASET_REGISTRY["fred.effr"]
            repos.macro.ensure_target(spec, now_ms=CUTOFF_MS - 2_000, max_attempts=1)
            target = repos.macro.claim_target(
                clock_kind=spec.clock_kind,
                lease_owner="macro-api-stale",
                lease_ms=60_000,
                now_ms=CUTOFF_MS - 1_900,
            )
            assert target is not None
            repos.macro.record_receipt(
                target=target,
                receipt_id="macro-api-stale-effr",
                started_at_ms=CUTOFF_MS - 1_800,
                completed_at_ms=CUTOFF_MS - 1_700,
                status="failed",
                http_status=503,
                rows_seen=0,
                rows_inserted=0,
                response_hash=None,
                error_code="provider_exhausted",
                error_message="synthetic terminal source",
                diagnostics={},
            )
            assert repos.macro.fail_target(
                target=target,
                lease_owner="macro-api-stale",
                receipt_id="macro-api-stale-effr",
                error_code="provider_exhausted",
                next_due_at_ms=CUTOFF_MS + 60_000,
                completed_at_ms=CUTOFF_MS - 1_700,
                unavailable=False,
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
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            acquired_specs = tuple(spec for spec in datasets_for_module("rates_fed") if spec.clock_kind != "derived")
            for spec in acquired_specs:
                repos.macro.ensure_target(spec, now_ms=CUTOFF_MS - 2_000, max_attempts=2)
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
        workers=disabled_workers_settings(),
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings


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


def _insert_v2_publication(repos) -> dict:
    pack = _pack()
    research_input = _research_input(pack=pack)
    publication = _publication()
    repos.macro_thesis.insert_evidence_pack(pack)
    repos.macro_thesis.insert_research_input(research_input)
    repos.macro_thesis.ensure_run(
        pack=pack,
        due_at_ms=CUTOFF_MS,
        max_attempts=2,
        now_ms=CUTOFF_MS + 100,
    )
    repos.macro_thesis.bind_research_input(
        session_date=SESSION,
        research_input=research_input,
        now_ms=CUTOFF_MS + 150,
    )
    repos.macro_thesis.claim_run(
        session_date=SESSION,
        lease_owner="api-owner",
        lease_ms=60_000,
        now_ms=CUTOFF_MS + 200,
    )
    repos.macro_thesis.publish_v2(
        publication=publication,
        lease_owner="api-owner",
    )
    repos.macro_thesis.insert_live_delta(
        evaluate_live_delta_v2(
            publication=publication,
            modules=_modules(),
            evaluated_at_ms=CUTOFF_MS + 400,
        )
    )
    repos.macro_thesis.insert_outcome_replay(
        evaluate_outcome_replay_v2(
            publication=publication,
            market_rows=[],
            evaluated_at_ms=CUTOFF_MS + 400,
        )
    )
    return publication.model_dump(mode="json")


def _legacy_publication():
    pack = _pack()
    refs = {module_id: f"macro-module:{SESSION.isoformat()}:{module_id}" for module_id in MACRO_MODULE_IDS}
    falsifier = MacroCondition(
        condition_id="legacy-falsifier",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="lte",
        threshold=-25,
        effect="invalidation_triggered",
        rationale="Legacy immutable condition.",
    )
    checkpoint = MacroCondition(
        condition_id="legacy-checkpoint",
        module_id="rates_fed",
        dataset_id="fred.dgs2",
        metric_name="change_1w_bp",
        operator="gte",
        threshold=20,
        effect="confirming",
        rationale="Legacy immutable checkpoint.",
    )
    draft = MacroThesisBodyDraft(
        mainline=MacroMainline(
            stance="call",
            title="Legacy v1 Thesis",
            thesis="This is an immutable explicit archive fixture.",
            stage="developing",
            confidence="medium",
            horizon="1w",
            claims=(
                MacroThesisClaim(
                    claim_id="legacy-claim",
                    statement="Rates were the material driver.",
                    causal_edges=(
                        MacroCausalEdge(
                            source="Rates",
                            mechanism="Discount rate",
                            target="Risk assets",
                            evidence_refs=(refs["rates_fed"],),
                            conflicting_evidence_refs=(),
                        ),
                    ),
                    supporting_evidence_refs=(refs["rates_fed"],),
                    conflicting_evidence_refs=(),
                    conditions=(),
                ),
            ),
            supporting_evidence_refs=(refs["rates_fed"],),
            conflicting_evidence_refs=(),
            falsifiers=(falsifier,),
            checkpoints=(checkpoint,),
        ),
        module_assessments=tuple(
            MacroModuleRole(
                module_id=module_id,
                role="driver" if module_id == "rates_fed" else "uncertain",
                analysis=f"Legacy {module_id} assessment.",
                claim_ids=("legacy-claim",) if module_id == "rates_fed" else (),
                supporting_evidence_refs=(refs[module_id],),
                conflicting_evidence_refs=(),
            )
            for module_id in MACRO_MODULE_IDS
        ),
        asset_outlooks=tuple(
            MacroAssetOutlook(
                symbol=symbol,
                outlook_1w=MacroHorizonOutlook(
                    horizon="1w",
                    direction="no_call",
                    causal_channel="Legacy evidence was insufficient.",
                    confidence="low",
                ),
                outlook_1m=MacroHorizonOutlook(
                    horizon="1m",
                    direction="no_call",
                    causal_channel="Legacy evidence was insufficient.",
                    confidence="low",
                ),
            )
            for symbol in MACRO_THESIS_ASSETS
        ),
        narrative_sections=(
            MacroNarrativeSection(
                section_id="legacy-mainline",
                title="Legacy narrative",
                markdown="Immutable archive content.",
                evidence_refs=(refs["rates_fed"],),
            ),
        ),
    )
    draft_hash = payload_hash(draft.model_dump(mode="json"))
    review = MacroThesisReviewV1(
        draft_hash=draft_hash,
        disposition="pass",
        findings=("Legacy reviewer audit.",),
        invocation_id="legacy-review",
        model_name="legacy-model",
        prompt_version="legacy-review-v1",
    )
    publication = build_publication(
        evidence_pack=pack,
        draft=draft,
        review=review,
        research_provenance={
            "invocation_id": "legacy-research",
            "model_name": "legacy-model",
            "prompt_version": "legacy-research-v1",
        },
        published_at_ms=CUTOFF_MS + 300,
    )
    return pack, publication


def _insert_v1_publication(repos) -> dict:
    pack, publication = _legacy_publication()
    research_input = _research_input(pack=pack)
    repos.macro_thesis.insert_evidence_pack(pack)
    repos.macro_thesis.insert_research_input(research_input)
    repos.macro_thesis.ensure_run(
        pack=pack,
        due_at_ms=CUTOFF_MS,
        max_attempts=2,
        now_ms=CUTOFF_MS + 100,
    )
    repos.macro_thesis.bind_research_input(
        session_date=SESSION,
        research_input=research_input,
        now_ms=CUTOFF_MS + 150,
    )
    repos.macro_thesis.claim_run(
        session_date=SESSION,
        lease_owner="legacy-owner",
        lease_ms=60_000,
        now_ms=CUTOFF_MS + 200,
    )
    repos.macro_thesis.conn.execute(
        """
        INSERT INTO macro_thesis_reviews(
          review_id, session_date, review_sequence, draft_hash, disposition,
          review_json, invocation_id, model_name, prompt_version, created_at_ms
        )
        VALUES (%s, %s, 1, %s, 'pass', %s, %s, %s, %s, %s)
        """,
        (
            f"{SESSION.isoformat()}:{publication.review.invocation_id}",
            SESSION,
            publication.review.draft_hash,
            Jsonb(publication.review.model_dump(mode="json")),
            publication.review.invocation_id,
            publication.review.model_name,
            publication.review.prompt_version,
            CUTOFF_MS + 250,
        ),
    )
    repos.macro_thesis.conn.execute(
        """
        INSERT INTO macro_thesis_publications(
          publication_id, session_date, cutoff_ms, evidence_pack_id,
          schema_version, thesis_json, thesis_hash, reviewer_invocation_id,
          reviewer_draft_hash, published_at_ms
        )
        VALUES (%s, %s, %s, %s, 'macro_thesis_v1', %s, %s, %s, %s, %s)
        """,
        (
            publication.publication_id,
            publication.session_date,
            publication.cutoff_ms,
            publication.evidence_pack_id,
            Jsonb(publication.model_dump(mode="json")),
            publication.content_hash,
            publication.review.invocation_id,
            publication.review.draft_hash,
            publication.published_at_ms,
        ),
    )
    repos.macro_thesis.conn.execute(
        """
        UPDATE macro_thesis_runs
        SET status = 'published',
            publication_id = %s,
            leased_until_ms = NULL,
            lease_owner = NULL,
            updated_at_ms = %s
        WHERE session_date = %s
          AND status = 'running'
          AND lease_owner = 'legacy-owner'
        """,
        (publication.publication_id, publication.published_at_ms, SESSION),
    )
    return publication.model_dump(mode="json")


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


def test_current_routes_return_current_session_state_without_prior_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
        overview = client.get("/api/macro/overview", headers=AUTH)
        research = client.get("/api/macro/research", headers=AUTH)

    assert overview.status_code == 200
    data = overview.json()["data"]
    assert data["schema_version"] == "macro_overview_v8"
    assert data["session_date"] == SESSION.isoformat()
    assert data["thesis_state"] == "missing"
    assert data["thesis"] is None
    assert data["live_delta"] is None
    assert data["outcome_replay"] is None
    assert "fallback" not in data
    assert research.json()["data"]["schema_version"] == "macro_thesis_detail_v4"
    assert research.json()["data"]["state"] == "missing"


def test_current_v2_publication_serves_sparse_thesis_and_current_module_context(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            publication = _insert_v2_publication(repos)
        overview = client.get("/api/macro/overview", headers=AUTH)
        research = client.get("/api/macro/research", headers=AUTH)
        rates = client.get("/api/macro/rates-fed", headers=AUTH)

    assert overview.status_code == research.status_code == rates.status_code == 200
    overview_data = overview.json()["data"]
    research_data = research.json()["data"]
    rates_data = rates.json()["data"]
    assert overview_data["thesis_state"] == "published"
    assert overview_data["thesis"]["schema_version"] == "macro_thesis_v2"
    assert len(overview_data["thesis"]["asset_outlooks"]) == 1
    assert overview_data["thesis"]["assets"][0]["symbol"] == "SPY"
    assert len(overview_data["thesis"]["assets"]) == 12
    assert research_data["state"] == "published"
    assert research_data["live_delta"]["schema_version"] == "macro_live_delta_v2"
    assert research_data["outcome_replay"]["schema_version"] == "macro_outcome_replay_v2"
    assert rates_data["thesis_context"]["state"] == "published"
    assert rates_data["thesis_context"]["assessment"]["role"] == "driver"
    assert rates_data["thesis_context"]["session_date"] == SESSION.isoformat()
    assert publication["publication_id"] == overview_data["thesis"]["publication_id"]


def test_explicit_v2_archive_is_frozen_omits_deltas_and_keeps_external_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            publication = _insert_v2_publication(repos)
        archive = client.get(
            f"/api/macro/research?session_date={SESSION.isoformat()}",
            headers=AUTH,
        )
        missing = client.get(
            "/api/macro/research?session_date=2026-07-21",
            headers=AUTH,
        )

    archive_data = archive.json()["data"]
    assert archive_data["schema_version"] == "macro_thesis_archive_detail_v2"
    assert archive_data["state"] == "historical"
    assert archive_data["thesis"]["publication_id"] == publication["publication_id"]
    assert "live_delta" not in archive_data
    assert "outcome_replay" not in archive_data
    assert len(archive_data["recovery"]) == 12
    assert archive_data["recovery"][0]["scope_kind"] == "asset"
    assert missing.json()["data"]["state"] == "missing"
    assert missing.json()["data"]["thesis"] is None
    assert missing.json()["data"]["recovery"] == []


def test_current_v1_is_not_published_but_remains_explicit_archive(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            publication = _insert_v1_publication(repos)
        overview = client.get("/api/macro/overview", headers=AUTH)
        current = client.get("/api/macro/research", headers=AUTH)
        archive = client.get(
            f"/api/macro/research?session_date={SESSION.isoformat()}",
            headers=AUTH,
        )

    overview_data = overview.json()["data"]
    current_data = current.json()["data"]
    archive_data = archive.json()["data"]
    assert overview_data["thesis_state"] == "not_published"
    assert overview_data["thesis"] is None
    assert overview_data["thesis_reason"]["code"] == "macro_thesis_current_contract_not_published"
    assert current_data["state"] == "not_published"
    assert current_data["thesis"] is None
    assert archive_data["state"] == "historical"
    assert archive_data["thesis"]["schema_version"] == "macro_thesis_v1"
    assert archive_data["thesis"]["publication_id"] == publication["publication_id"]
    assert len(archive_data["recovery"]) >= 12
    assert archive_data["recovery"][0]["publication"]["dataset_id"] == "nasdaq.spy.daily"


def test_macro_http_reads_are_persisted_only_and_write_zero_rows(
    tmp_path,
    monkeypatch,
) -> None:
    _current_time(monkeypatch)
    app = create_app(settings=_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            _insert_modules(repos)
            _insert_v2_publication(repos)
        with client.app.state.service.repositories() as repos:
            before = {
                "runs": repos.macro_thesis.conn.execute("SELECT count(*) AS count FROM macro_thesis_runs").fetchone()[
                    "count"
                ],
                "publications": repos.macro_thesis.conn.execute(
                    "SELECT count(*) AS count FROM macro_thesis_publications"
                ).fetchone()["count"],
                "live": repos.macro_thesis.conn.execute("SELECT count(*) AS count FROM macro_live_deltas").fetchone()[
                    "count"
                ],
            }
        for path in (
            "/api/macro/overview",
            "/api/macro/rates-fed",
            "/api/macro/research",
            f"/api/macro/research?session_date={SESSION.isoformat()}",
        ):
            assert client.get(path, headers=AUTH).status_code == 200
        with client.app.state.service.repositories() as repos:
            after = {
                "runs": repos.macro_thesis.conn.execute("SELECT count(*) AS count FROM macro_thesis_runs").fetchone()[
                    "count"
                ],
                "publications": repos.macro_thesis.conn.execute(
                    "SELECT count(*) AS count FROM macro_thesis_publications"
                ).fetchone()["count"],
                "live": repos.macro_thesis.conn.execute("SELECT count(*) AS count FROM macro_live_deltas").fetchone()[
                    "count"
                ],
            }
    assert after == before
