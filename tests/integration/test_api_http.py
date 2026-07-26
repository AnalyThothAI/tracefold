import json
import math
import time
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

from fastapi.testclient import TestClient

from tests.notification_helpers import insert_notification_row
from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import runtime_workers_settings, workers_settings_with_enabled
from tracefold.app.http.app import create_app
from tracefold.app.http.responses import _json
from tracefold.app.worker_manifest import all_worker_manifests
from tracefold.market import (
    Author,
    Content,
    MarketTick,
    MarketTickPersistenceService,
    Source,
    TwitterEvent,
    market_tick_id,
    parse_gmgn_token_payload,
)
from tracefold.news import NewsFeedEntry, NewsSourceDefinition
from tracefold.platform.config.settings import Settings

PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
TOKEN_RADAR_TEST_REBUILD_OFFSET_MS = 60_000


def test_api_json_response_encodes_decimal_payloads():
    response = _json({"ok": True, "data": {"price": Decimal("1.23")}})

    assert json.loads(response.body) == {"ok": True, "data": {"price": 1.23}}


def test_api_json_response_replaces_non_finite_float_payloads_with_null():
    response = _json(
        {
            "ok": True,
            "data": {
                "score": math.nan,
                "nested": [{"value": math.inf}, {"value": -math.inf}, {"value": 1.0}],
            },
        }
    )

    assert json.loads(response.body) == {
        "ok": True,
        "data": {
            "score": None,
            "nested": [{"value": None}, {"value": None}, {"value": 1.0}],
        },
    }


def test_token_images_serves_ready_local_file_without_auth(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        image_id = seed_ready_token_image(client, content=b"fake-png")
        response = client.get(f"/api/token-images/{image_id}")

    assert response.status_code == 200
    assert response.content == b"fake-png"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_token_images_rejects_invalid_image_ids(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        responses = [client.get(f"/api/token-images/{image_id}") for image_id in ("a" * 63, "A" * 64, "g" * 64)]

    assert [response.status_code for response in responses] == [404, 404, 404]


def test_token_images_returns_404_for_missing_and_error_rows(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        source_url = "https://gmgn.ai/external-res/error-token.png"
        with client.app.state.service.repositories() as repos:
            repos.token_image_assets.upsert_pending_sources(
                [
                    {
                        "source_url": source_url,
                        "source_provider": "gmgn_dex_profile",
                        "source_kind": "asset_profile.logo_url",
                        "raw_ref_json": {"source": "test"},
                    }
                ],
                now_ms=1_779_000_000_000,
            )
            repos.token_image_assets.mark_error(
                source_url,
                error="upstream failed",
                now_ms=1_779_000_000_100,
                retry_ms=30_000,
            )

        missing = client.get(f"/api/token-images/{'0' * 64}")
        error = client.get(f"/api/token-images/{_sha256(source_url)}")

    assert missing.status_code == 404
    assert error.status_code == 404


def test_token_images_returns_404_when_ready_file_is_missing(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        image_id = seed_ready_token_image(client, storage_path="missing.png", write_file=False)
        response = client.get(f"/api/token-images/{image_id}")

    assert response.status_code == 404


def test_token_images_rejects_storage_path_traversal(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        runtime = client.app.state.service
        cache_root = runtime.settings.app_home / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / "outside.png").write_bytes(b"leaked")
        image_id = insert_ready_token_image_row(client, storage_path="../outside.png")

        response = client.get(f"/api/token-images/{image_id}")

    assert response.status_code == 404
    assert response.content != b"leaked"


def test_old_token_image_proxy_route_is_absent(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/token-image",
            params={"url": "https://gmgn.ai/external-res/token-alpha.png"},
        )

    assert response.status_code == 404


def seed_ready_token_image(
    client: TestClient,
    *,
    source_url: str = "https://gmgn.ai/external-res/token-alpha.png",
    storage_path: str = "token-alpha.png",
    content: bytes = b"fake-png",
    media_type: str = "image/png",
    write_file: bool = True,
) -> str:
    runtime = client.app.state.service
    cache_dir = runtime.settings.app_home / "cache" / "token-images"
    if write_file:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / storage_path).write_bytes(content)
    with runtime.repositories() as repos:
        repos.token_image_assets.upsert_pending_sources(
            [
                {
                    "source_url": source_url,
                    "source_provider": "gmgn_dex_profile",
                    "source_kind": "asset_profile.logo_url",
                    "raw_ref_json": {"source": "test"},
                }
            ],
            now_ms=1_779_000_000_000,
        )
        row = repos.token_image_assets.mark_ready(
            source_url,
            media_type=media_type,
            file_extension=".png",
            content_sha256="a" * 64,
            byte_size=len(content),
            storage_path=storage_path,
            now_ms=1_779_000_000_100,
        )
    return str(row["image_id"])


def insert_ready_token_image_row(
    client: TestClient,
    *,
    source_url: str = "https://gmgn.ai/external-res/traversal-token.png",
    storage_path: str,
) -> str:
    image_id = _sha256(source_url)
    with client.app.state.service.repositories() as repos:
        repos.conn.execute(
            """
            INSERT INTO token_image_assets(
              image_id, source_url, source_url_hash, source_provider, source_kind, status,
              media_type, file_extension, content_sha256, byte_size, storage_path,
              public_url, raw_ref_json, failure_count, next_refresh_at_ms,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, %s, 'gmgn_dex_profile', 'asset_profile.logo_url', 'ready',
              'image/png', '.png', %s, 6, %s, %s, '{}'::jsonb, 0, %s, %s, %s
            )
            """,
            (
                image_id,
                source_url,
                image_id,
                "b" * 64,
                storage_path,
                f"/api/token-images/{image_id}",
                1_779_000_000_000,
                1_779_000_000_000,
                1_779_000_000_000,
            ),
        )
        repos.conn.commit()
    return image_id


def _sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def make_settings(tmp_path, *, enabled_workers: tuple[str, ...] = ("token_radar_projection",)) -> Settings:
    prepare_postgres_database()
    settings = Settings(
        handles=("toly", "elonmusk"),
        ws_token="secret",
        storage=postgres_settings_storage(),
        workers=workers_settings_with_enabled(*enabled_workers),
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings


def make_event(
    event_id: str,
    handle: str = "toly",
    text: str | None = None,
    received_at_ms: int | None = None,
) -> TwitterEvent:
    return TwitterEvent(
        event_id=event_id,
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_basic",
        ),
        action="tweet",
        original_action=None,
        tweet_id=event_id,
        internal_id=event_id,
        timestamp=1,
        received_at_ms=received_at_ms if received_at_ms is not None else int(time.time() * 1000),
        author=Author(handle=handle, name=handle, avatar=None, followers=100, tags=[]),
        content=Content(text=text or f"{handle} text", media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        matched_handles=[handle],
        raw={"id": event_id},
    )


def make_token_event(
    event_id: str,
    *,
    symbol: str,
    address: str,
    handle: str = "toly",
    text: str | None = None,
    received_at_ms: int | None = None,
) -> TwitterEvent:
    snapshot = parse_gmgn_token_payload(
        {
            "tt": "ca",
            "t": {
                "a": address,
                "c": "eth",
                "mc": "60490.341996",
                "p": "1.0",
                "s": symbol,
            },
        }
    )
    return replace(
        make_event(event_id, handle=handle, text=text or f"${symbol} launch", received_at_ms=received_at_ms),
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_token",
        ),
        token_snapshot=snapshot,
    )


def rebuild_token_radar(client: TestClient, *, now_ms: int | None = None) -> None:
    worker_entry = client.app.state.service.scheduler.workers["token_radar_projection"]
    worker = getattr(worker_entry, "worker", worker_entry)
    assert worker is not None
    deadline = time.monotonic() + 10.0
    drained_once = False
    base_now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    interval_ms = (
        int(
            max(
                float(getattr(worker.settings, "interval_seconds", 1.0)),
                float(getattr(worker.settings, "cold_interval_seconds", 1.0)),
            )
            * 1000
        )
        + 1
    )
    attempt = 0
    while True:
        worker.rebuild_once(now_ms=base_now_ms + attempt * interval_ms)
        attempt += 1
        pending, leased = _token_radar_dirty_queue_counts(client)
        while leased:
            if time.monotonic() >= deadline:
                raise AssertionError("token radar projection dirty leases did not drain")
            time.sleep(0.05)
            pending, leased = _token_radar_dirty_queue_counts(client)
        if pending == 0:
            if drained_once:
                return
            drained_once = True
            continue
        if time.monotonic() >= deadline:
            raise AssertionError("token radar projection dirty queues did not drain")
        time.sleep(0.05)


def _token_radar_dirty_queue_counts(client: TestClient) -> tuple[int, int]:
    runtime = client.app.state.service
    with runtime.db.api_pool.connection() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS pending,
              COUNT(*) FILTER (WHERE lease_owner IS NOT NULL) AS leased
            FROM token_radar_dirty_targets
            """
        ).fetchone()
    return int(row["pending"] or 0), int(row["leased"] or 0)


def seed_resolved_asset_with_event(
    client: TestClient,
    *,
    symbol: str = "HANSA",
    address: str = PEPE,
    event_id: str = "event-token-case-1",
    now_ms: int | None = None,
) -> dict[str, object]:
    event = make_token_event(
        event_id,
        symbol=symbol,
        address=address,
        text=f"${symbol} ignition {address}",
        received_at_ms=now_ms if now_ms is not None else int(time.time() * 1000),
    )
    client.app.state.service.ingest.ingest_event(event, is_watched=True)
    search = client.get(
        "/api/search",
        params={"q": f"${symbol}", "limit": 5, "window": "24h"},
        headers={"Authorization": "Bearer secret"},
    )
    assert search.status_code == 200
    candidates = search.json()["data"]["target_candidates"]
    resolved = [candidate for candidate in candidates if candidate["status"] == "resolved"]
    assert len(resolved) == 1
    return resolved[0]


def test_api_bootstrap_exposes_frontend_runtime_config_without_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get("/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "ws_token": "secret",
        "handles": ["toly", "elonmusk"],
        "replay_limit": 100,
    }


def test_api_rejects_protected_reads_without_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get("/api/recent")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def test_api_removed_token_signal_reads_are_not_registered(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        snapshots = client.get("/api/token-signal-snapshots", headers={"Authorization": "Bearer secret"})
        outcomes = client.get("/api/token-signal-outcomes", headers={"Authorization": "Bearer secret"})
        evaluations = client.get("/api/token-signal-evaluations", headers={"Authorization": "Bearer secret"})

    assert snapshots.status_code == 404
    assert outcomes.status_code == 404
    assert evaluations.status_code == 404


def test_api_search_rejects_removed_filter_params(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get("/api/search", params={"symbol": "PEPE"}, headers={"Authorization": "Bearer secret"})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "unsupported_query_param", "field": "symbol"}


def test_api_search_rejects_malformed_cursor(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/search",
            params={"q": "PEPE", "cursor": "not-a-cursor"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_cursor"}


def test_api_status_exposes_flat_worker_status(tmp_path):
    app = create_app(
        settings=make_settings(
            tmp_path,
            enabled_workers=("event_anchor_backfill", "token_radar_projection"),
        ),
        start_collector=False,
    )

    with TestClient(app) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert "_".join(("anchor", "price")) not in data
    assert "resolution_refresh" not in data
    assert "token_radar_projection" not in data
    assert "worker_lanes" not in data
    workers = data["workers"]
    for name in (
        "market_tick_stream",
        "market_tick_poll",
        "event_anchor_backfill",
        "resolution_refresh",
        "token_radar_projection",
    ):
        assert set(workers[name]) >= {
            "enabled",
            "running",
            "last_started_at_ms",
            "last_finished_at_ms",
            "last_result",
            "last_error",
        }
    assert "event_anchor_backfill" in workers
    assert workers["event_anchor_backfill"]["enabled"] is True


def test_api_news_exposes_only_story_and_source_contracts(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    source_definition = NewsSourceDefinition(
        source_id="source-story-test",
        name="Example News",
        feed_url="https://example.test/feed.xml",
        source_domain="example.test",
        source_role="original_publisher",
        trust_tier="authoritative",
        source_chain_id="example",
        coverage_tags=("politics", "economy"),
    )

    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            repos.news.sync_sources((source_definition,), now_ms=1_000)
            repos.news.record_fetch_success(
                source=source_definition,
                entries=(
                    NewsFeedEntry(
                        guid="story-1",
                        link="https://example.test/story-1",
                        title="Global policy response confirmed",
                        summary="Officials published the policy response.",
                        published_at_ms=2_000,
                    ),
                ),
                started_at_ms=3_000,
                finished_at_ms=3_100,
                status_code=200,
                etag="etag-1",
                last_modified=None,
                not_modified=False,
            )
            repos.news.project_pending_revisions(now_ms=3_200, limit=100)
            repos.news.conn.execute(
                """
                UPDATE news_stories
                   SET brief_eligible = true,
                       impact_score = 90,
                       priority_score = 90
                """
            )
            repos.news.plan_global_brief(
                now_ms=3_300,
                candidate_limit=100,
                debounce_ms=0,
                critical_debounce_ms=0,
            )

        headers = {"Authorization": "Bearer secret"}
        sources_response = client.get("/api/news/sources", headers=headers)
        stories_response = client.get("/api/news/stories", headers=headers)
        story_id = stories_response.json()["data"]["items"][0]["story_id"]
        detail_response = client.get(f"/api/news/stories/{story_id}", headers=headers)
        request_response = client.post(
            f"/api/news/stories/{story_id}/analysis-requests",
            headers=headers,
        )
        missing_request_response = client.post(
            "/api/news/stories/missing-story/analysis-requests",
            headers=headers,
        )
        brief_response = client.get("/api/news/brief", headers=headers)
        brief_history_response = client.get("/api/news/brief/history", headers=headers)
        retired_responses = [
            client.get("/api/news", headers=headers),
            client.get("/api/news/items/news-1", headers=headers),
            client.get("/api/news/sources/status", headers=headers),
        ]

    assert sources_response.status_code == 200
    [source] = sources_response.json()["data"]["items"]
    assert source["source_id"] == "source-story-test"
    assert source["last_success_at_ms"] == 3_100
    assert source["last_http_status"] == 200
    assert stories_response.status_code == 200
    [story] = stories_response.json()["data"]["items"]
    assert story["title"] == "Global policy response confirmed"
    assert story["evidence_posture"] == "single_origin_reported"
    assert story["source_count"] == 1
    assert detail_response.status_code == 200
    assert detail_response.json()["data"]["memberships"][0]["epistemic_use"] == "fact_evidence"
    assert request_response.status_code == 202
    assert request_response.json()["data"]["status"] == "insufficient"
    assert missing_request_response.status_code == 404
    assert brief_response.status_code == 200
    assert brief_response.json()["data"]["current"] is None
    assert brief_response.json()["data"]["fallback"]["status"] == "publishable"
    assert brief_history_response.status_code == 200
    assert brief_history_response.json()["data"]["items"] == []
    assert [response.status_code for response in retired_responses] == [404, 404, 404]


def test_api_news_story_cursor_search_and_filters_compose(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    authoritative = NewsSourceDefinition(
        source_id="authority",
        name="Authority News",
        feed_url="https://authority.test/feed.xml",
        source_domain="authority.test",
        source_role="original_publisher",
        trust_tier="authoritative",
        source_chain_id="authority",
    )
    standard = NewsSourceDefinition(
        source_id="standard",
        name="Standard News",
        feed_url="https://standard.test/feed.xml",
        source_domain="standard.test",
        source_role="original_publisher",
        trust_tier="standard",
        source_chain_id="standard",
    )

    with TestClient(app) as client:
        with client.app.state.service.repositories() as repos, repos.transaction():
            repos.news.sync_sources((authoritative, standard), now_ms=1_000)
            repos.news.record_fetch_success(
                source=authoritative,
                entries=(
                    NewsFeedEntry(
                        guid="alpha",
                        link="https://authority.test/alpha",
                        title="Alpha government approves fiscal package",
                        published_at_ms=2_000,
                    ),
                    NewsFeedEntry(
                        guid="beta",
                        link="https://authority.test/beta",
                        title="Beta central bank cuts rates by 25 basis points",
                        published_at_ms=3_000,
                    ),
                ),
                started_at_ms=4_000,
                finished_at_ms=4_100,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repos.news.record_fetch_success(
                source=standard,
                entries=(
                    NewsFeedEntry(
                        guid="gamma",
                        link="https://standard.test/gamma",
                        title="Gamma conflict ceasefire announced",
                        published_at_ms=5_000,
                    ),
                ),
                started_at_ms=6_000,
                finished_at_ms=6_100,
                status_code=200,
                etag=None,
                last_modified=None,
                not_modified=False,
            )
            repos.news.project_pending_revisions(now_ms=6_200, limit=100)

        headers = {"Authorization": "Bearer secret"}
        first = client.get("/api/news/stories", params={"limit": 1}, headers=headers)
        first_data = first.json()["data"]
        second = client.get(
            "/api/news/stories",
            params={"limit": 1, "cursor": first_data["next_cursor"]},
            headers=headers,
        )
        searched = client.get(
            "/api/news/stories",
            params={"q": "Gamma"},
            headers=headers,
        )
        trusted = client.get(
            "/api/news/stories",
            params={
                "evidence_posture": "single_origin_reported",
                "source": "authority",
            },
            headers=headers,
        )
        unverified = client.get(
            "/api/news/stories",
            params={
                "evidence_posture": "single_origin_reported",
                "source": "standard",
            },
            headers=headers,
        )
        invalid_cursor = client.get(
            "/api/news/stories",
            params={"cursor": "not-a-cursor"},
            headers=headers,
        )
        invalid_verification = client.get(
            "/api/news/stories",
            params={"evidence_posture": "invented"},
            headers=headers,
        )

    assert first.status_code == second.status_code == 200
    assert first_data["next_cursor"]
    first_id = first_data["items"][0]["story_id"]
    second_id = second.json()["data"]["items"][0]["story_id"]
    assert first_id != second_id
    assert [item["title"] for item in searched.json()["data"]["items"]] == ["Gamma conflict ceasefire announced"]
    assert {item["representative_evidence"]["source_id"] for item in trusted.json()["data"]["items"]} == {"authority"}
    assert {item["evidence_posture"] for item in trusted.json()["data"]["items"]} == {"single_origin_reported"}
    assert [item["evidence_posture"] for item in unverified.json()["data"]["items"]] == ["single_origin_reported"]
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["error"] == "news_story_cursor_invalid"
    assert invalid_verification.status_code == 400
    assert invalid_verification.json()["error"] == "news_story_evidence_posture_invalid"


def test_api_exposes_recent_search_and_token_read_models(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        rebuild_now_ms = now_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        event = make_token_event(
            "event-1",
            symbol="PEPE",
            address=PEPE,
            text=f"$PEPE ignition {PEPE}",
            received_at_ms=now_ms - 1_000,
        )
        client.app.state.service.ingest.ingest_event(event, is_watched=True)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        headers = {"Authorization": "Bearer secret"}
        recent = client.get("/api/recent?limit=5", headers=headers)
        search = client.get("/api/search", params={"q": "$PEPE", "limit": 5, "window": "24h"}, headers=headers)
        search_inspect = client.get(
            "/api/search/inspect",
            params={"q": "$PEPE", "limit": 5, "window": "24h", "scope": "all"},
            headers=headers,
        )
        asset_flow = client.get("/api/token-radar?window=5m&limit=5", headers=headers)
        account_alerts = client.get("/api/account-alerts?window=24h&limit=5", headers=headers)

    assert recent.status_code == 200
    assert recent.json()["data"]["events"][0]["event_id"] == "event-1"
    assert "token_intents" in recent.json()["data"]["items"][0]
    assert "token_resolutions" in recent.json()["data"]["items"][0]
    assert "harness" not in recent.json()["data"]["items"][0]
    assert "enrichment" not in recent.json()["data"]["items"][0]

    assert search.status_code == 200
    search_data = search.json()["data"]
    assert search_data["items"][0]["event"]["event_id"] == "event-1"
    assert search_data["page"]["returned_count"] == 1
    assert "total_count" not in search_data
    assert search_data["query"]["window"] == "24h"

    assert search_inspect.status_code == 200
    inspect_data = search_inspect.json()["data"]
    assert inspect_data["query"]["result_kind"] == "token_result"
    assert set(inspect_data["resolver"]) == {
        "target_candidates",
        "selected_target",
        "reasons",
    }
    assert inspect_data["resolver"]["target_candidates"]
    assert inspect_data["token_result"]["posts"]["items"][0]["event_id"] == "event-1"
    assert inspect_data["token_result"]["timeline"]["market_candles"]["target_type"] == "Asset"
    assert inspect_data["token_result"]["profile"]["status"] == "pending"
    assert inspect_data["token_result"]["profile"]["provider"] is None
    assert inspect_data["token_result"]["market_live"]["status"] in {"missing", "unsupported", "ready"}
    current_radar = inspect_data["token_result"]["current_radar"]
    assert current_radar is not None
    assert set(current_radar) == {
        "intent",
        "radar",
        "resolution",
        "quality",
        "factor_snapshot",
    }
    assert current_radar["factor_snapshot"]["subject"]["symbol"] == "PEPE"
    assert current_radar["radar"]["lane"] == "resolved"
    assert current_radar["radar"]["rank"] >= 1
    assert current_radar["factor_snapshot"]["composite"]["recommended_decision"] in {
        "discard",
        "watch",
        "high_alert",
    }
    legacy_market_field = "market_overlay"
    assert legacy_market_field not in inspect_data["token_result"]
    assert "radar_item" not in inspect_data["token_result"]
    assert "agent_brief" not in inspect_data["token_result"]
    assert "discussion_digest" not in inspect_data["token_result"]
    assert "narrative_admission" not in inspect_data["token_result"]

    assert asset_flow.status_code == 200
    radar_row = asset_flow.json()["data"]["targets"][0]
    assert radar_row["factor_snapshot"]["subject"]["symbol"] == "PEPE"
    assert {"target", "attention", "market", "score", "data_health", "source_event_ids"}.isdisjoint(radar_row)
    assert radar_row["profile"]["status"] == "pending"
    assert radar_row["profile"]["provider"] is None
    assert "discussion_digest" not in radar_row
    assert "narrative_admission" not in radar_row

    assert account_alerts.status_code == 200
    assert account_alerts.json()["data"]["items"][0]["event_id"] == "event-1"
    assert account_alerts.json()["data"]["items"][0]["token_resolution_status"] == "EXACT"


def test_token_radar_public_payload_excludes_unresolved_rows(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        rebuild_now_ms = now_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        runtime = client.app.state.service
        runtime.ingest.ingest_event(
            make_token_event(
                "event-pepe-diagnostics",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
            is_watched=True,
        )
        runtime.ingest.ingest_event(
            make_event(
                "event-unknown-diagnostics",
                text="$NEWTOKEN soon",
                received_at_ms=now_ms - 500,
            ),
            is_watched=True,
        )
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        response = client.get(
            "/api/token-radar",
            params={"window": "5m", "scope": "all", "limit": 20},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    public_rows = [*data["targets"], *data["attention"]]
    assert public_rows
    assert all(row["factor_snapshot"]["subject"]["target_id"] for row in public_rows)
    assert "NEWTOKEN" not in {row["factor_snapshot"]["subject"]["symbol"] for row in public_rows}
    assert data["projection"]["unresolved"]["identity_missing_count"] == 0
    assert "NEWTOKEN" not in data["projection"]["unresolved"]["sample_symbols"]


def test_live_market_reads_durable_current_without_gateway(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        runtime = client.app.state.service
        ingested = runtime.ingest.ingest_event(
            make_token_event(
                "event-pepe-live-overlay",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
            is_watched=True,
        )
        resolution = next(item for item in ingested.token_resolutions if item["resolution_status"] == "EXACT")
        with runtime.repositories() as repos:
            market_target = repos.registry.chain_token_market_target(str(resolution["target_id"]))
            assert market_target is not None
            chain, _, token_address = str(market_target["target_id"]).rpartition(":")
            tick = MarketTick(
                tick_id=market_tick_id(
                    target_type="chain_token",
                    target_id=str(market_target["target_id"]),
                    source_provider="gmgn_dex_quote",
                    observed_at_ms=now_ms,
                ),
                target_type="chain_token",
                target_id=str(market_target["target_id"]),
                chain=chain,
                token_address=token_address,
                exchange=None,
                instrument=None,
                pricefeed_id=None,
                source_tier="tier3_inline",
                source_provider="gmgn_dex_quote",
                observed_at_ms=now_ms,
                received_at_ms=now_ms,
                price_usd=Decimal("0.123"),
                liquidity_usd=Decimal("45000"),
                volume_24h_usd=Decimal("9000"),
                market_cap_usd=Decimal("123000"),
                holders=321,
                created_at_ms=now_ms,
                raw_payload_json={"source": "test"},
            )
            with repos.transaction():
                MarketTickPersistenceService(repos).persist_ticks([tick], now_ms=now_ms)
        live_market = client.get(
            "/api/live-market",
            params={"target_type": resolution["target_type"], "target_id": resolution["target_id"]},
            headers={"Authorization": "Bearer secret"},
        )

    assert live_market.status_code == 200
    payload = live_market.json()["data"]
    assert payload["status"] == "live"
    assert payload["price_usd"] == 0.123
    assert payload["market_cap_usd"] == 123_000
    assert payload["provider"] == "gmgn_dex_quote"


def test_stocks_radar_returns_us_equity_market_instruments_with_unavailable_quote_state(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    now_ms = int(time.time() * 1000)

    with TestClient(app) as client:
        runtime = client.app.state.service
        with runtime.repositories() as repos:
            repos.registry.upsert_us_equity_symbol(
                symbol="AAPL",
                exchange="NASDAQ",
                security_name="Apple Inc. Common Stock",
                instrument_type="equity",
                source="test",
                source_updated_at_ms=now_ms,
                raw_payload={"Symbol": "AAPL"},
                observed_at_ms=now_ms,
            )
            repos.registry.upsert_us_equity_symbol(
                symbol="RKLB",
                exchange="NASDAQ",
                security_name="Rocket Lab USA, Inc. Common Stock",
                instrument_type="equity",
                source="test",
                source_updated_at_ms=now_ms,
                raw_payload={"Symbol": "RKLB"},
                observed_at_ms=now_ms,
            )

        runtime.ingest.ingest_event(
            make_event("event-aapl-1", handle="toly", text="$AAPL breakout", received_at_ms=now_ms - 10_000),
            is_watched=True,
        )
        runtime.ingest.ingest_event(
            make_event("event-aapl-2", handle="elonmusk", text="$AAPL still bid", received_at_ms=now_ms - 5_000),
            is_watched=False,
        )
        runtime.ingest.ingest_event(
            make_event("event-rklb-1", handle="toly", text="$RKLB launch cadence", received_at_ms=now_ms - 3_000),
            is_watched=True,
        )
        runtime.ingest.ingest_event(
            make_token_event(
                "event-pepe-stock-radar-exclusion",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
            is_watched=True,
        )

        response = client.get(
            "/api/stocks-radar",
            params={"window": "1h", "scope": "all", "limit": 10},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    rows = data["rows"]
    symbols = {row["target"]["symbol"] for row in rows}
    assert symbols == {"AAPL", "RKLB"}
    assert all(row["target"]["target_type"] == "MarketInstrument" for row in rows)
    assert all(row["target"]["target_id"].startswith("market_instrument:us_equity:") for row in rows)
    assert "PEPE" not in symbols
    assert data["health"] == {
        "returned_count": 2,
        "quote_ready_count": 0,
        "quote_unavailable_count": 2,
    }
    by_symbol = {row["target"]["symbol"]: row for row in rows}
    assert by_symbol["AAPL"]["attention"]["mentions"] == 2
    assert by_symbol["AAPL"]["attention"]["unique_authors"] == 2
    assert by_symbol["AAPL"]["quote"]["status"] == "unavailable"
    assert by_symbol["AAPL"]["quote"]["error"] == "quote_read_model_unavailable"
    assert by_symbol["AAPL"]["quote"]["provider"] is None
    assert by_symbol["AAPL"]["row_health"] == ["quote_unavailable"]
    assert by_symbol["RKLB"]["quote"]["status"] == "unavailable"
    assert by_symbol["RKLB"]["quote"]["error"] == "quote_read_model_unavailable"
    assert by_symbol["RKLB"]["quote"]["provider_symbol"] == "RKLB"
    assert by_symbol["RKLB"]["row_health"] == ["quote_unavailable"]


def test_api_exposes_notification_list_summary_and_read_state(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        runtime = client.app.state.service
        with runtime.repositories() as repos, repos.transaction():
            first = insert_notification_row(
                repos.notifications,
                dedup_key="activity:event-1",
                rule_id="watched_account_activity",
                severity="info",
                title="activity",
                body="new post",
                entity_type="account",
                entity_key="account:toly",
                author_handle="toly",
                event_id="event-1",
                source_table="events",
                source_id="event-1",
                occurrence_at_ms=1_700_000_000_000,
                payload={"event_id": "event-1"},
                channels=["in_app"],
            )
            insert_notification_row(
                repos.notifications,
                dedup_key="watched-token:pepe",
                rule_id="watched_account_token_alert",
                severity="high",
                title="@toly mentioned $PEPE",
                body="First-seen watched-account token mention",
                entity_type="token",
                entity_key="token:eth:pepe",
                author_handle="toly",
                symbol="PEPE",
                chain="eth",
                address=PEPE,
                source_table="account_token_alerts",
                source_id="alert-1",
                occurrence_at_ms=1_700_000_060_000,
                payload={
                    "alert_id": "alert-1",
                    "author_handle": "toly",
                    "symbol": "PEPE",
                    "is_first_seen_global": True,
                },
                channels=["in_app"],
            )
        assert first is not None

        headers = {"Authorization": "Bearer secret"}
        listed = client.get("/api/notifications?limit=10", headers=headers)
        read = client.post(f"/api/notifications/{first['notification_id']}/read", headers=headers)
        unread = client.get("/api/notifications?unread_only=true&limit=10", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["data"]["summary"]["unread_count"] == 2
    assert listed.json()["data"]["summary"]["high_unread_count"] == 1
    assert listed.json()["data"]["summary"]["account_unread_counts"] == {"toly": 2}
    assert listed.json()["data"]["items"][0]["rule_id"] == "watched_account_token_alert"
    assert listed.json()["data"]["items"][0]["payload"] == {
        "alert_id": "alert-1",
        "author_handle": "toly",
        "symbol": "PEPE",
        "is_first_seen_global": True,
    }
    assert listed.json()["data"]["items"][0]["channels"] == ["in_app"]

    assert read.status_code == 200
    assert read.json()["data"]["updated"] is True
    assert unread.status_code == 200
    assert [item["notification_id"] for item in unread.json()["data"]["items"]] != [first["notification_id"]]
    assert unread.json()["data"]["summary"]["unread_count"] == 1


def test_api_marks_author_notifications_read(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        runtime = client.app.state.service
        with runtime.repositories() as repos, repos.transaction():
            for suffix in ("1", "2"):
                insert_notification_row(
                    repos.notifications,
                    dedup_key=f"activity:toly:{suffix}",
                    rule_id="watched_account_activity",
                    severity="info",
                    title="activity",
                    body="new post",
                    entity_type="account",
                    entity_key="account:toly",
                    author_handle="toly",
                    event_id=f"event-toly-{suffix}",
                    source_table="events",
                    source_id=f"event-toly-{suffix}",
                    occurrence_at_ms=1_700_000_000_000 + int(suffix),
                    payload={"event_id": f"event-toly-{suffix}"},
                    channels=["in_app"],
                )
            insert_notification_row(
                repos.notifications,
                dedup_key="activity:elon",
                rule_id="watched_account_activity",
                severity="info",
                title="activity",
                body="new post",
                entity_type="account",
                entity_key="account:elonmusk",
                author_handle="elonmusk",
                event_id="event-elon",
                source_table="events",
                source_id="event-elon",
                occurrence_at_ms=1_700_000_000_010,
                payload={"event_id": "event-elon"},
                channels=["in_app"],
            )

        headers = {"Authorization": "Bearer secret"}
        read = client.post("/api/notifications/author/toly/read", headers=headers)
        listed = client.get("/api/notifications?limit=10", headers=headers)

    assert read.status_code == 200
    assert read.json()["data"]["updated_count"] == 2
    assert listed.status_code == 200
    assert listed.json()["data"]["summary"]["unread_count"] == 1
    assert listed.json()["data"]["summary"]["account_unread_counts"] == {"elonmusk": 1}


def test_api_exposes_notification_delivery_audit(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        runtime = client.app.state.service
        with runtime.repositories() as repos, repos.transaction():
            notification = insert_notification_row(
                repos.notifications,
                dedup_key="watched-token:pepe",
                rule_id="watched_account_token_alert",
                severity="high",
                title="@toly mentioned $PEPE",
                body="First-seen watched-account token mention",
                entity_type="token",
                entity_key="token:eth:pepe",
                symbol="PEPE",
                source_table="account_token_alerts",
                source_id="alert-1",
                occurrence_at_ms=1_700_000_060_000,
                payload={"alert_id": "alert-1", "symbol": "PEPE"},
                channels=["in_app", "pushdeer"],
            )
            assert notification is not None
            repos.notifications.enqueue_delivery(
                notification_id=notification["notification_id"],
                channel_id="pushdeer",
                provider="apprise",
                max_attempts=5,
                next_run_at_ms=1_700_000_060_000,
            )

        response = client.get("/api/notification-deliveries", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"][0]["channel_id"] == "pushdeer"
    assert body["data"]["items"][0]["provider"] == "apprise"
    assert body["data"]["items"][0]["status"] == "pending"


def test_api_deletes_social_enrichment_and_harness_routes(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        deleted = [
            client.get("/api/social-events?window=1h&limit=5", headers=headers),
            client.get("/api/attention-seeds?window=1h&limit=5", headers=headers),
            client.get("/api/harness-snapshots?window=1h&horizon=6h&limit=5", headers=headers),
            client.get("/api/harness-outcomes?window=1h&horizon=6h&limit=5", headers=headers),
            client.get("/api/harness-credits?window=1h&horizon=6h&limit=5", headers=headers),
            client.get("/api/harness-health", headers=headers),
            client.get("/api/harness-score-buckets?horizon=6h", headers=headers),
        ]

    assert [response.status_code for response in deleted] == [404, 404, 404, 404, 404, 404, 404]


def test_api_asset_flow_scope_filters_watched_mentions(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        rebuild_now_ms = now_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        watched_event = make_token_event(
            "event-watched",
            symbol="PEPE",
            address="0x6982508145454ce325ddbe47a25d4ec3d2311933",
            text="$PEPE watched",
            received_at_ms=now_ms - 1_000,
        )
        public_event = make_token_event(
            "event-public",
            symbol="BONK",
            address="0x44b28991b167582f18ba0259e0173176ca125505",
            handle="anon",
            text="$BONK public",
            received_at_ms=now_ms - 900,
        )
        client.app.state.service.ingest.ingest_event(watched_event, is_watched=True)
        client.app.state.service.ingest.ingest_event(public_event, is_watched=False)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        headers = {"Authorization": "Bearer secret"}
        all_flow = client.get("/api/token-radar", params={"window": "5m", "scope": "all"}, headers=headers)
        watched_flow = client.get("/api/token-radar", params={"window": "5m", "scope": "matched"}, headers=headers)

    assert all_flow.status_code == 200
    assert {item["factor_snapshot"]["subject"]["symbol"] for item in all_flow.json()["data"]["targets"]} == {
        "PEPE",
        "BONK",
    }

    assert watched_flow.status_code == 200
    assert watched_flow.json()["data"]["scope"] == "matched"
    assert [item["factor_snapshot"]["subject"]["symbol"] for item in watched_flow.json()["data"]["targets"]] == ["PEPE"]


def test_api_live_market_returns_missing_without_durable_current_row(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/live-market",
            params={"target_type": "Asset", "target_id": "asset:missing"},
            headers={"Authorization": "Bearer secret"},
        )
        missing = client.get("/api/live-market", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["target_type"] == "Asset"
    assert data["target_id"] == "asset:missing"
    assert data["status"] == "missing"
    assert missing.status_code == 400
    assert missing.json() == {"ok": False, "error": "target_required", "field": "target_id"}


def test_api_token_case_returns_dossier_for_resolved_asset(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        target = seed_resolved_asset_with_event(client, symbol="HANSA")
        response = client.get(
            "/api/token-case",
            params={
                "target_type": target["target_type"],
                "target_id": target["target_id"],
                "window": "24h",
                "scope": "all",
                "posts_limit": 2,
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["target"]["target_type"] == "Asset"
    assert "market_live" in body["data"]
    assert body["data"]["current_radar"] is None
    assert body["data"]["timeline"]["market_candles"]["target_type"] == "Asset"
    assert "radar_item" not in body["data"]
    legacy_market_field = "market_overlay"
    assert legacy_market_field not in body["data"]
    assert "agent_brief" not in body["data"]
    assert "discussion_digest" not in body["data"]
    assert "narrative_admission" not in body["data"]
    assert body["data"]["posts"]["items"][0]["post_quality"]["contributions"]
    assert "semantic" not in body["data"]["posts"]["items"][0]


def test_api_token_case_returns_404_when_target_not_found(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/token-case",
            params={"target_type": "Asset", "target_id": "asset:solana:token:missing"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "target_not_found"}


def test_api_token_case_requires_auth(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get("/api/token-case", params={"target_type": "Asset", "target_id": "asset:x"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def test_api_token_case_rejects_invalid_window_and_scope(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        bad_window = client.get(
            "/api/token-case",
            params={"target_type": "Asset", "target_id": "asset:x", "window": "7d"},
            headers={"Authorization": "Bearer secret"},
        )
        bad_scope = client.get(
            "/api/token-case",
            params={"target_type": "Asset", "target_id": "asset:x", "scope": "private"},
            headers={"Authorization": "Bearer secret"},
        )

    assert bad_window.status_code == 400
    assert bad_window.json() == {"ok": False, "error": "invalid_window", "field": "window"}
    assert bad_scope.status_code == 400
    assert bad_scope.json() == {"ok": False, "error": "invalid_scope", "field": "scope"}


def test_api_token_case_matches_search_inspect_token_result_shape(tmp_path, monkeypatch):
    now_ms = 1_778_562_000_000
    monkeypatch.setattr("tracefold.app.http.routes_search._now_ms", lambda: now_ms)
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        target = seed_resolved_asset_with_event(client, symbol="HANSA", now_ms=now_ms - 60_000)
        token_case = client.get(
            "/api/token-case",
            params={
                "target_type": target["target_type"],
                "target_id": target["target_id"],
                "window": "24h",
                "scope": "all",
            },
            headers={"Authorization": "Bearer secret"},
        )
        inspect = client.get(
            "/api/search/inspect",
            params={"q": "$HANSA", "window": "24h", "scope": "all"},
            headers={"Authorization": "Bearer secret"},
        )

    assert token_case.status_code == 200
    assert inspect.status_code == 200
    assert inspect.json()["data"]["token_result"] == token_case.json()["data"]


def test_api_target_posts_returns_full_post_pages_and_requires_target_identity(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        base_ms = int(time.time() * 1000)
        rebuild_now_ms = base_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        for index in range(3):
            event = make_token_event(
                f"event-pepe-post-{index}",
                symbol="PEPE",
                address=PEPE,
                handle=f"voice{index}",
                text=f"$PEPE post {index}",
                received_at_ms=base_ms - index * 1_000,
            )
            client.app.state.service.ingest.ingest_event(event, is_watched=index == 0)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        asset_flow = client.get(
            "/api/token-radar",
            params={"window": "5m", "limit": 5, "scope": "all"},
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]["targets"][0]
        target_type = asset_flow["factor_snapshot"]["subject"]["target_type"]
        target_id = asset_flow["factor_snapshot"]["subject"]["target_id"]

        missing = client.get("/api/target-posts?window=5m", headers={"Authorization": "Bearer secret"})
        first_page = client.get(
            "/api/target-posts",
            params={"target_type": target_type, "target_id": target_id, "window": "5m", "scope": "all", "limit": 2},
            headers={"Authorization": "Bearer secret"},
        )
        second_page = client.get(
            "/api/target-posts",
            params={
                "target_type": target_type,
                "target_id": target_id,
                "window": "5m",
                "scope": "all",
                "limit": 2,
                "cursor": first_page.json()["data"]["next_cursor"],
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 400
    assert missing.json() == {"ok": False, "error": "target_required", "field": "target_id"}
    assert first_page.status_code == 200
    first_body = first_page.json()["data"]
    assert first_body["total_count"] == 3
    assert first_body["returned_count"] == 2
    assert first_body["has_more"] is True
    assert first_body["query"]["target_type"] == target_type
    assert "semantic" not in first_body["items"][0]
    assert "sort" not in first_body["query"]
    assert "catalyst_score" not in first_body["items"][0]
    assert "catalyst_components" not in first_body["items"][0]
    assert first_body["query"]["target_id"] == target_id
    assert first_body["items"][0]["post_quality"]["score_version"] == "post_quality_v1"
    assert first_body["items"][0]["post_quality"]["contributions"]
    assert "score" not in first_body["items"][0]
    assert second_page.status_code == 200
    assert second_page.json()["data"]["returned_count"] == 1


def test_api_target_posts_rejects_malformed_cursor(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/target-posts",
            params={
                "target_type": "Asset",
                "target_id": "asset:eip155:1:erc20:0xpepe",
                "window": "5m",
                "cursor": "abcde",
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_cursor"}


def test_api_target_posts_rejects_retired_sort_query(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/target-posts",
            params={
                "target_type": "Asset",
                "target_id": "asset:eip155:1:erc20:0xpepe",
                "window": "5m",
                "sort": "recent",
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "unsupported_query_param",
        "field": "sort",
    }


def test_api_target_social_timeline_returns_buckets_authors_and_posts(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        base_ms = int(time.time() * 1000)
        rebuild_now_ms = base_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        for index in range(3):
            event = make_token_event(
                f"event-pepe-timeline-{index}",
                symbol="PEPE",
                address=PEPE,
                handle=f"voice{index}",
                text=f"$PEPE timeline mcap liquidity {index}",
                received_at_ms=base_ms - index * 30_000,
            )
            client.app.state.service.ingest.ingest_event(event, is_watched=index == 0)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        asset_flow = client.get(
            "/api/token-radar",
            params={"window": "5m", "limit": 5, "scope": "all"},
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]["targets"][0]
        target_type = asset_flow["factor_snapshot"]["subject"]["target_type"]
        target_id = asset_flow["factor_snapshot"]["subject"]["target_id"]

        missing = client.get("/api/target-social-timeline?window=5m", headers={"Authorization": "Bearer secret"})
        response = client.get(
            "/api/target-social-timeline",
            params={"target_type": target_type, "target_id": target_id, "window": "5m", "scope": "all", "limit": 2},
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 400
    assert missing.json() == {"ok": False, "error": "target_required", "field": "target_id"}
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["query"]["bucket"] == "30s"
    assert data["query"]["target_type"] == target_type
    assert data["query"]["target_id"] == target_id
    assert data["summary"]["posts"] == 2
    assert data["buckets"]
    assert data["authors"]
    assert data["returned_count"] == 2
    assert data["has_more"] is True


def test_api_target_social_timeline_rejects_manual_bucket_param(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/target-social-timeline",
            params={"target_type": "Asset", "target_id": "asset:eip155:1:erc20:0xpepe", "window": "5m", "bucket": "1m"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "unsupported_query_param", "field": "bucket"}


def test_api_rejects_removed_1m_window(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/token-radar",
            params={"window": "1m", "limit": 5, "scope": "all"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_window", "field": "window"}


def test_api_target_posts_requires_target_identity(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/target-posts",
            params={"window": "5m"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "target_required", "field": "target_id"}


def test_api_status_exposes_operational_state(tmp_path):
    settings = make_settings(tmp_path)
    settings.workers = runtime_workers_settings()
    app = create_app(settings=settings, start_collector=False)

    with TestClient(app) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["handles"] == ["toly", "elonmusk"]
    manifest_names = {manifest.name for manifest in all_worker_manifests()}
    assert manifest_names.issubset(data["workers"])
    assert "worker_lanes" not in data
    assert "collector" not in data
    assert "enrichment" not in data
    assert "notifications" not in data

    collector = data["workers"]["collector"]
    assert collector["enabled"] is False
    assert collector["running"] is False
    assert collector["effective_status"] == "intentionally_not_started"
    assert "queue_depth" not in collector
    assert "details" not in collector
    assert data["snapshot_gate"] == {
        "immediate_complete": 0,
        "debounced_complete": 0,
        "debounced_timeout": 0,
        "non_tw_channel": 0,
    }


def test_api_status_remains_queryable_when_readiness_is_degraded(tmp_path):
    settings = make_settings(tmp_path)
    settings.workers = runtime_workers_settings()
    app = create_app(settings=settings, start_collector=False)

    with TestClient(app) as client:
        client.app.state.service.db.api_pool.close()
        readiness = client.get("/readyz")
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    body = response.json()
    assert readiness.status_code == 503
    assert readiness.json()["reasons"] == ["database_unhealthy"]
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["data"]["ok"] is False
    assert "database_unhealthy" not in body["data"]["reasons"]
    assert body["data"]["db"]["ok"] is True


def test_api_rejects_removed_narrative_product_surfaces(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        narrative_flow = client.get("/api/narrative-flow?window=1h&limit=5", headers=headers)
        account_narratives = client.get("/api/account-narratives?window=24h&limit=5", headers=headers)
        seeds = client.get("/api/narrative-seeds?window=24h&limit=5", headers=headers)
        flow = client.get("/api/narrative-token-flow?seed_id=seed&window=1h&limit=5", headers=headers)
        frontier = client.get("/api/attention-frontier?window=1h&limit=5", headers=headers)

    assert narrative_flow.status_code == 404
    assert account_narratives.status_code == 404
    assert seeds.status_code == 404
    assert flow.status_code == 404
    assert frontier.status_code == 404


def test_social_events_by_ids_returns_full_records(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    ids = ["event-watched", "event-public"]
    with TestClient(app) as client:
        _seed_social_event_batch(client.app)
        response = client.get(
            "/api/events/by-ids",
            params={"ids": ",".join(ids)},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    events = response.json()["data"]["events"]
    assert {event["event_id"] for event in events} == set(ids)
    by_handle = {event["author_handle"]: event for event in events}
    assert by_handle["toly"]["author_watched"] is True
    assert by_handle["random_dude"]["author_watched"] is False
    assert by_handle["toly"]["source_provider"] == "gmgn"
    assert by_handle["toly"]["channel"] == "twitter_monitor_basic"


def test_social_events_by_ids_skips_missing(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        _seed_social_event_batch(client.app)
        response = client.get(
            "/api/events/by-ids",
            params={"ids": "event-watched,nonexistent-id"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    body = response.json()
    assert [event["event_id"] for event in body["data"]["events"]] == ["event-watched"]
    assert body["data"]["not_found"] == ["nonexistent-id"]


def test_social_events_by_ids_rejects_too_many(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    huge = ",".join(f"id-{i}" for i in range(201))
    with TestClient(app) as client:
        response = client.get(
            "/api/events/by-ids",
            params={"ids": huge},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "too_many_ids"


def test_social_events_by_ids_requires_ids(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        response = client.get(
            "/api/events/by-ids",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "ids_required"


def _seed_social_event_batch(app) -> None:
    with app.state.service.repositories() as repos, repos.transaction():
        repos.evidence.insert_event(
            make_event("event-watched", handle="toly", text="$PEPE watched", received_at_ms=1_700_000_000_000),
            is_watched=True,
        )
        repos.evidence.insert_event(
            make_event(
                "event-public",
                handle="random_dude",
                text="$PEPE public",
                received_at_ms=1_700_000_010_000,
            ),
            is_watched=False,
        )
