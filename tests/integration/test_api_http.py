import json
import math
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

from fastapi.testclient import TestClient

from tests.integration.test_token_radar_idempotency import _run_radar_projection, _SingleConnectionDB
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tracefold.app.http.app import create_app
from tracefold.app.http.responses import _json
from tracefold.app.repositories import repositories_for_connection
from tracefold.app.workers import _ingest_service_for_repos
from tracefold.market import (
    Author,
    Content,
    MarketTick,
    MarketTickPersistenceService,
    Source,
    TwitterEvent,
    market_tick_id,
    parse_gmgn_token_payload,
    rebuild_all_token_radar_for_maintenance,
)
from tracefold.news import NewsBriefDraft, NewsFeedEntry, OpenNewsEvent, opennews_source
from tracefold.news.brief import brief_fingerprint
from tracefold.platform.config.settings import Settings

PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
TOKEN_RADAR_TEST_REBUILD_OFFSET_MS = 60_000


def _opennews_event(
    *,
    provider_record_id: str,
    title: str,
    description: str,
    published_at_ms: int,
    reporting_origin: str,
    provider_metadata: dict[str, object] | None = None,
) -> OpenNewsEvent:
    return OpenNewsEvent(
        provider_record_id=provider_record_id,
        observation_kind="report",
        provider_metadata=dict(provider_metadata or {}),
        entry=NewsFeedEntry(
            guid=provider_record_id,
            link=f"https://example.test/{provider_record_id}",
            title=title,
            description=description,
            published_at_ms=published_at_ms,
            reporting_origin=reporting_origin,
            raw={},
        ),
    )


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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        image_id = seed_ready_token_image(client, content=b"fake-png")
        response = client.get(f"/api/token-images/{image_id}")

    assert response.status_code == 200
    assert response.content == b"fake-png"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_token_images_rejects_invalid_image_ids(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        responses = [client.get(f"/api/token-images/{image_id}") for image_id in ("a" * 63, "A" * 64, "g" * 64)]

    assert [response.status_code for response in responses] == [404, 404, 404]


def test_token_images_returns_404_for_missing_and_error_rows(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        source_url = "https://gmgn.ai/external-res/error-token.png"
        with write_repositories() as repos:
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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        image_id = seed_ready_token_image(client, storage_path="missing.png", write_file=False)
        response = client.get(f"/api/token-images/{image_id}")

    assert response.status_code == 404


def test_token_images_rejects_storage_path_traversal(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=make_settings(tmp_path))

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
    with write_repositories() as repos:
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
    with write_repositories() as repos:
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


def make_settings(tmp_path) -> Settings:
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


def ingest_event(event: TwitterEvent):
    with write_repositories() as repos:
        return _ingest_service_for_repos(
            repos,
            event_anchor_active_window_ms=300_000,
        ).ingest_event(event)


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
    del client
    base_now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    conn = connect_postgres_test(read_only=False)
    try:
        for attempt in range(1_000):
            frontier = conn.execute(
                """
                SELECT target_type, target_id, window_key, venue
                FROM radar_projection_frontiers
                WHERE status = 'dirty'
                ORDER BY deadline_at_ms, target_type, target_id,
                         window_key, venue
                LIMIT 1
                """
            ).fetchone()
            conn.commit()
            if frontier is None:
                return
            result = _run_radar_projection(
                conn,
                window=str(frontier["window_key"]),
                now_ms=base_now_ms + attempt,
                venue=str(frontier["venue"]),
            )
            assert result["projection_status"] in {
                "published",
                "unchanged",
                "deleted",
            }
        raise AssertionError("radar projection frontiers did not drain")
    finally:
        conn.close()


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
    ingest_event(event)
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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "ws_token": "secret",
        "replay_limit": 100,
    }


def test_api_rejects_protected_reads_without_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/recent")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def test_recent_uses_stable_cursor_and_rejects_filter_drift(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = 1_800_000_000_000
    for event_id, received_at_ms in (
        ("recent-3", now_ms),
        ("recent-2", now_ms),
        ("recent-1", now_ms - 1),
    ):
        ingest_event(
            make_event(
                event_id,
                handle="cursor-source",
                received_at_ms=received_at_ms,
            )
        )

    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        first = client.get(
            "/api/recent",
            params={"handles": "cursor-source", "limit": 2},
            headers=headers,
        )
        cursor = first.json()["data"]["page"]["next_cursor"]
        second = client.get(
            "/api/recent",
            params={
                "handles": "cursor-source",
                "limit": 2,
                "cursor": cursor,
            },
            headers=headers,
        )
        drifted = client.get(
            "/api/recent",
            params={"handles": "other-source", "limit": 2, "cursor": cursor},
            headers=headers,
        )

    assert first.status_code == 200
    assert [row["event_id"] for row in first.json()["data"]["events"]] == [
        "recent-3",
        "recent-2",
    ]
    assert first.json()["data"]["page"] == {
        "returned_count": 2,
        "has_more": True,
        "next_cursor": cursor,
    }
    assert second.status_code == 200
    assert [row["event_id"] for row in second.json()["data"]["events"]] == [
        "recent-1",
    ]
    assert second.json()["data"]["page"] == {
        "returned_count": 1,
        "has_more": False,
        "next_cursor": None,
    }
    assert drifted.status_code == 400
    assert drifted.json() == {
        "ok": False,
        "error": "invalid_cursor",
        "field": "cursor",
    }


def test_api_removed_token_signal_reads_are_not_registered(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        snapshots = client.get("/api/token-signal-snapshots", headers={"Authorization": "Bearer secret"})
        outcomes = client.get("/api/token-signal-outcomes", headers={"Authorization": "Bearer secret"})
        evaluations = client.get("/api/token-signal-evaluations", headers={"Authorization": "Bearer secret"})

    assert snapshots.status_code == 404
    assert outcomes.status_code == 404
    assert evaluations.status_code == 404


def test_api_search_rejects_removed_filter_params(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/search", params={"symbol": "PEPE"}, headers={"Authorization": "Bearer secret"})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "unsupported_query_param", "field": "symbol"}


def test_api_search_rejects_malformed_cursor(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/search",
            params={"q": "PEPE", "cursor": "not-a-cursor"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_cursor"}


def test_api_news_exposes_exact_worldmonitor_read_contract(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(
                    _opennews_event(
                        provider_record_id="story-1",
                        title="Central bank raises interest rate after policy shock",
                        description="Officials published a detailed policy response.",
                        published_at_ms=now_ms - 60_000,
                        reporting_origin="example-news",
                        provider_metadata={
                            "score": 76,
                            "signal": "long",
                            "grade": "A",
                            "coins": [{"symbol": "BTC", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="story-2",
                        title="Major earthquake strikes coastal region",
                        description="Emergency services reported widespread damage.",
                        published_at_ms=now_ms - 50_000,
                        reporting_origin="example-news",
                    ),
                    _opennews_event(
                        provider_record_id="story-3",
                        title="Cyber attack disrupts regional infrastructure",
                        description="Operators reported a sustained service disruption.",
                        published_at_ms=now_ms - 40_000,
                        reporting_origin="second-news",
                    ),
                ),
                observed_at_ms=now_ms,
            )
            repos.news.rebuild_stories(now_ms=now_ms)
            candidates = repos.news.brief_candidates()
            fingerprint = brief_fingerprint(candidates)
            claim = repos.news.claim_brief_run(
                fingerprint=fingerprint,
                story_count=len(candidates),
                source_count=2,
                now_ms=now_ms,
                max_attempts=3,
                lease_owner="test-runtime",
            )
            assert claim is not None
            repos.news.publish_brief(
                run_id=claim["run_id"],
                lease_owner=claim["lease_owner"],
                fingerprint=fingerprint,
                stories=candidates,
                draft=NewsBriefDraft(
                    lead="今日全球政策重点发生变化 [1]",
                    lines=tuple(
                        f"第{index}条：{story['representative_title']} [{index}]"
                        for index, story in enumerate(candidates, 1)
                    ),
                    provider="fixture",
                    model="fixture-model",
                    raw_response="{}",
                ),
                validation={
                    "citation_index_lock": True,
                    "citation_closure": True,
                    "proper_noun_grounding": True,
                    "no_cross_story_stitching": True,
                    "story_count": len(candidates),
                    "lead_fallback": False,
                    "line_fallbacks": [],
                    "grounding_failures": [],
                    "model_line_coverage": len(candidates),
                    "final_story_coverage": len(candidates),
                },
                now_ms=now_ms,
            )

        headers = {"Authorization": "Bearer secret"}
        sources_response = client.get("/api/news/sources", headers=headers)
        feed_response = client.get("/api/news/feed", headers=headers)
        latest_feed_response = client.get(
            "/api/news/feed",
            params={"sort": "latest"},
            headers=headers,
        )
        feed_etag = feed_response.headers["etag"]
        unchanged_feed = client.get(
            "/api/news/feed",
            headers={**headers, "If-None-Match": feed_etag},
        )
        stories = feed_response.json()["data"]["stories"]
        story = next(
            item for item in stories if item["title"] == "Central bank raises interest rate after policy shock"
        )
        story_id = story["story_id"]
        detail_response = client.get(f"/api/news/stories/{story_id}", headers=headers)
        brief_response = client.get("/api/news/brief", headers=headers)
        status_response = client.get("/api/news/status", headers=headers)
        unchanged_brief = client.get(
            "/api/news/brief",
            headers={**headers, "If-None-Match": brief_response.headers["etag"]},
        )
        retired_responses = [
            client.get("/api/news/stories", headers=headers),
            client.get("/api/news/brief/history", headers=headers),
            client.post(f"/api/news/stories/{story_id}/analysis-requests", headers=headers),
            client.get("/api/news", headers=headers),
            client.get("/api/news/items/news-1", headers=headers),
            client.get("/api/news/sources/status", headers=headers),
        ]

    assert sources_response.status_code == 200
    source = sources_response.json()["data"]["items"][0]
    assert source["source_id"] == "news-opennews"
    assert source["source_kind"] == "opennews"
    assert source["last_success_at_ms"] == now_ms
    assert sources_response.json()["data"]["page"] == {
        "returned_count": 1,
        "has_more": False,
        "next_cursor": None,
    }

    assert feed_response.status_code == 200
    assert latest_feed_response.status_code == 200
    assert latest_feed_response.json()["data"]["sort"] == "latest"
    assert feed_response.headers["cache-control"] == "private, no-cache"
    assert unchanged_feed.status_code == 304
    assert story["title"] in {
        "Central bank raises interest rate after policy shock",
        "Major earthquake strikes coastal region",
        "Cyber attack disrupts regional infrastructure",
    }
    assert story["source_count"] == 1
    assert story["provider_evidence"] == {
        "item_id": story["representative_item_id"],
        "url": "https://example.test/story-1",
        "provider_metadata": {
            "score": 76,
            "signal": "long",
            "grade": "A",
            "coins": [{"symbol": "BTC", "market_type": "spot"}],
        },
    }
    assert set(story["provider_evidence"]) == {
        "item_id",
        "url",
        "provider_metadata",
    }
    assert (
        next(item for item in stories if item["title"] == "Major earthquake strikes coastal region")[
            "provider_evidence"
        ]
        is None
    )
    assert set(story["importance_factors"]) >= {
        "severity_points",
        "source_points",
        "corroboration_points",
        "recency_points",
        "total",
    }

    assert detail_response.status_code == 200
    detail = detail_response.json()["data"]
    assert detail["story_id"] == story_id
    assert detail["representative_item_id"]
    assert detail["scoring_item_id"]
    assert detail["provider_evidence"] == story["provider_evidence"]
    assert "current" not in detail["members"][0]
    assert "active" not in detail
    assert detail["members"][0]["reporting_origin"]
    assert detail["members"][0]["provider_record_id"]
    assert detail["members"][0]["provider_metadata"] == story["provider_evidence"]["provider_metadata"]
    assert detail["members_page"] == {
        "returned_count": 1,
        "has_more": False,
        "next_cursor": None,
    }
    assert "analysis" not in detail
    assert "revisions" not in detail

    assert brief_response.status_code == 200
    brief = brief_response.json()["data"]
    assert brief["state"] == "ready"
    assert len(brief["publication"]["selected_story_ids"]) == 3
    assert brief["publication"]["locale"] == "zh-CN"
    assert brief["publication"]["validation"]["citation_index_lock"] is True
    assert unchanged_brief.status_code == 304
    assert status_response.status_code == 200
    disabled_push = status_response.json()["data"]["layers"]["push"]
    assert {
        key: disabled_push[key]
        for key in (
            "status",
            "reasons",
            "enabled",
            "feishu_webhook_url_configured",
            "feishu_signing_secret_configured",
            "initialized",
            "baseline_at_ms",
            "pending_count",
            "retry_count",
            "terminal_count",
            "sent_count",
            "latest_sent_at_ms",
        )
    } == {
        "status": "disabled",
        "reasons": [],
        "enabled": False,
        "feishu_webhook_url_configured": False,
        "feishu_signing_secret_configured": False,
        "initialized": False,
        "baseline_at_ms": None,
        "pending_count": 0,
        "retry_count": 0,
        "terminal_count": 0,
        "sent_count": 0,
        "latest_sent_at_ms": None,
    }
    assert [response.status_code for response in retired_responses] == [404] * 6


def test_api_news_status_reports_enabled_push_without_secrets_or_payloads(tmp_path):
    settings = make_settings(tmp_path)
    settings.news.push = type(settings.news.push)(
        enabled=True,
        feishu_webhook_url=("https://open.feishu.cn/open-apis/bot/v2/hook/status-test-hook"),
        feishu_signing_secret="status-test-signing-secret",
    )
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(
                    _opennews_event(
                        provider_record_id="status-story",
                        title="Central bank announces a policy decision",
                        description="Officials published the decision.",
                        published_at_ms=now_ms - 1_000,
                        reporting_origin="authority",
                    ),
                ),
                observed_at_ms=now_ms,
            )
            repos.news.rebuild_stories(now_ms=now_ms)
            repos.news.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=now_ms,
                error_code=None,
                gap_unclosed=False,
                gap_boundary_provider_record_id=None,
                expected_gap_version=0,
            )

        headers = {"Authorization": "Bearer secret"}
        warming_response = client.get("/api/news/status", headers=headers)

        with write_repositories() as repos, repos.transaction():
            repos.news.initialize_push_baseline(now_ms=now_ms + 1)
            for story_id in ("a" * 64, "b" * 64, "c" * 64, "d" * 64):
                assert repos.news.insert_push_candidate(
                    story_id=story_id,
                    selected_item_id=f"selected-{story_id[0]}",
                    provider_score=90,
                    threshold_observed_at_ms=now_ms,
                    source_payload={
                        "card": "leaked-card",
                        "webhook": ("https://open.feishu.cn/open-apis/bot/v2/hook/status-test-hook"),
                    },
                    suppressed=False,
                    now_ms=now_ms + 2,
                )
            repos.conn.execute(
                """
                UPDATE news_push_deliveries
                   SET status = CASE story_id
                         WHEN %s THEN 'retry_wait'
                         WHEN %s THEN 'terminal'
                         WHEN %s THEN 'sent'
                         ELSE status
                       END,
                       delivery_attempts = CASE
                         WHEN story_id IN (%s, %s, %s) THEN 1
                         ELSE delivery_attempts
                       END,
                       next_attempt_at_ms = CASE
                         WHEN story_id = %s THEN %s
                         WHEN story_id IN (%s, %s) THEN NULL
                         ELSE next_attempt_at_ms
                       END,
                       last_error = CASE
                         WHEN story_id = %s THEN %s
                         WHEN story_id = %s THEN 'feishu_business_rejected'
                         ELSE NULL
                       END,
                       sent_at_ms = CASE WHEN story_id = %s THEN %s ELSE NULL END,
                       updated_at_ms = CASE WHEN story_id = %s THEN %s ELSE %s END
                """,
                (
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "b" * 64,
                    now_ms + 60_000,
                    "c" * 64,
                    "d" * 64,
                    "b" * 64,
                    "status-test-signing-secret https://example.test leaked-card",
                    "c" * 64,
                    "d" * 64,
                    now_ms + 3,
                    "b" * 64,
                    now_ms + 5,
                    now_ms + 4,
                ),
            )

        degraded_response = client.get("/api/news/status", headers=headers)
        settings.news.push.enabled = False
        disabled_history_response = client.get("/api/news/status", headers=headers)

    assert warming_response.status_code == 200
    warming = warming_response.json()["data"]
    assert warming["status"] == "warming"
    assert {name: layer["status"] for name, layer in warming["layers"].items()} == {
        "ingest": "ready",
        "story": "ready",
        "brief": "ready",
        "push": "warming",
    }
    assert warming["layers"]["push"]["initialized"] is False

    assert degraded_response.status_code == 200
    degraded = degraded_response.json()["data"]
    push = degraded["layers"]["push"]
    assert degraded["status"] == "degraded"
    assert push["status"] == "degraded"
    assert push["enabled"] is True
    assert push["feishu_webhook_url_configured"] is True
    assert push["feishu_signing_secret_configured"] is True
    assert push["initialized"] is True
    assert push["baseline_at_ms"] == now_ms + 1
    assert push["pending_count"] == 1
    assert push["retry_count"] == 1
    assert push["terminal_count"] == 1
    assert push["sent_count"] == 1
    assert push["latest_sent_at_ms"] == now_ms + 3
    assert push["latest_error"] == "news_story_push_delivery_error"
    assert push["reasons"] == [
        "push_delivery_retry_wait",
        "push_delivery_terminal",
    ]
    rendered = json.dumps(degraded)
    assert "status-test-signing-secret" not in rendered
    assert "status-test-hook" not in rendered
    assert "leaked-card" not in rendered

    assert disabled_history_response.status_code == 200
    disabled_history = disabled_history_response.json()["data"]
    disabled_push = disabled_history["layers"]["push"]
    assert disabled_history["status"] == "ready"
    assert disabled_push["status"] == "disabled"
    assert disabled_push["enabled"] is False
    assert disabled_push["initialized"] is True
    assert disabled_push["baseline_at_ms"] == now_ms + 1
    assert disabled_push["pending_count"] == 1
    assert disabled_push["retry_count"] == 1
    assert disabled_push["terminal_count"] == 1
    assert disabled_push["sent_count"] == 1
    assert disabled_push["latest_sent_at_ms"] == now_ms + 3


def test_api_news_feed_category_filter_is_server_authoritative(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(
                    _opennews_event(
                        provider_record_id="rates",
                        title="Federal Reserve changes interest rate policy",
                        description="Officials announced a new interest rate decision.",
                        published_at_ms=now_ms - 30_000,
                        reporting_origin="authority",
                    ),
                    _opennews_event(
                        provider_record_id="earthquake",
                        title="Major earthquake strikes coastal region",
                        description="Emergency services reported widespread damage.",
                        published_at_ms=now_ms - 20_000,
                        reporting_origin="authority",
                    ),
                    _opennews_event(
                        provider_record_id="ceasefire",
                        title="Ceasefire talks begin between regional governments",
                        description="Diplomats began formal negotiations.",
                        published_at_ms=now_ms - 10_000,
                        reporting_origin="standard",
                    ),
                ),
                observed_at_ms=now_ms,
            )
            repos.news.rebuild_stories(now_ms=now_ms)

        headers = {"Authorization": "Bearer secret"}
        complete = client.get("/api/news/feed", headers=headers)
        economic = client.get(
            "/api/news/feed",
            params={"category": "economic"},
            headers=headers,
        )
        unsupported = client.get(
            "/api/news/feed",
            params={"view": "priority"},
            headers=headers,
        )

    assert complete.status_code == 200
    stories = complete.json()["data"]["stories"]
    assert {story["category"] for story in stories} == {
        "disaster",
        "diplomatic",
        "economic",
    }
    assert len(stories) == 3
    assert economic.status_code == 200
    assert [story["title"] for story in economic.json()["data"]["stories"]] == [
        "Federal Reserve changes interest rate policy"
    ]
    assert unsupported.status_code == 400
    assert unsupported.json() == {
        "ok": False,
        "error": "unsupported_query_param",
        "field": "view",
    }


def test_api_exposes_recent_search_and_token_read_models(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
        ingest_event(event)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        headers = {"Authorization": "Bearer secret"}
        recent = client.get("/api/recent?limit=5", headers=headers)
        search = client.get("/api/search", params={"q": "$PEPE", "limit": 5, "window": "24h"}, headers=headers)
        search_inspect = client.get(
            "/api/search/inspect",
            params={"q": "$PEPE", "limit": 5, "window": "24h"},
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

    assert account_alerts.status_code == 404


def test_token_radar_public_payload_excludes_unresolved_rows(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        rebuild_now_ms = now_ms + TOKEN_RADAR_TEST_REBUILD_OFFSET_MS
        ingest_event(
            make_token_event(
                "event-pepe-diagnostics",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
        )
        ingest_event(
            make_event(
                "event-unknown-diagnostics",
                text="$NEWTOKEN soon",
                received_at_ms=now_ms - 500,
            ),
        )
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        response = client.get(
            "/api/token-radar",
            params={"window": "5m", "limit": 20},
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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        ingested = ingest_event(
            make_token_event(
                "event-pepe-live-overlay",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
        )
        resolution = next(item for item in ingested.token_resolutions if item["resolution_status"] == "EXACT")
        with write_repositories() as repos:
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
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)

    with TestClient(app) as client:
        with write_repositories() as repos:
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

        ingest_event(
            make_event("event-aapl-1", handle="toly", text="$AAPL breakout", received_at_ms=now_ms - 10_000),
        )
        ingest_event(
            make_event("event-aapl-2", handle="elonmusk", text="$AAPL still bid", received_at_ms=now_ms - 5_000),
        )
        ingest_event(
            make_event("event-rklb-1", handle="toly", text="$RKLB launch cadence", received_at_ms=now_ms - 3_000),
        )
        ingest_event(
            make_token_event(
                "event-pepe-stock-radar-exclusion",
                symbol="PEPE",
                address=PEPE,
                text=f"$PEPE {PEPE}",
                received_at_ms=now_ms - 1_000,
            ),
        )

        conn = connect_postgres_test(read_only=False)
        try:
            rebuild_all_token_radar_for_maintenance(
                db=_SingleConnectionDB(conn),
                now_ms=now_ms,
            )
        finally:
            conn.close()

        response = client.get(
            "/api/stocks-radar",
            params={"window": "1h", "limit": 10},
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


def test_api_notification_routes_are_not_registered(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        responses = [
            client.get("/api/notifications", headers=headers),
            client.post("/api/notifications/id/read", headers=headers),
            client.post("/api/notifications/author/toly/read", headers=headers),
            client.get("/api/notification-deliveries", headers=headers),
        ]

    assert [response.status_code for response in responses] == [404, 404, 404, 404]


def test_api_deletes_social_enrichment_and_harness_routes(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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


def test_api_token_radar_rejects_removed_scope(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        responses = [
            client.get("/api/token-radar", params={"window": "5m", "scope": scope}, headers=headers)
            for scope in ("all", "matched")
        ]

    assert [response.status_code for response in responses] == [400, 400]
    assert [response.json() for response in responses] == [
        {"ok": False, "error": "unsupported_query_param", "field": "scope"},
        {"ok": False, "error": "unsupported_query_param", "field": "scope"},
    ]


def test_api_live_market_returns_missing_without_durable_current_row(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        target = seed_resolved_asset_with_event(client, symbol="HANSA")
        response = client.get(
            "/api/token-case",
            params={
                "target_type": target["target_type"],
                "target_id": target["target_id"],
                "window": "24h",
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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/token-case",
            params={"target_type": "Asset", "target_id": "asset:solana:token:missing"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "target_not_found"}


def test_api_token_case_requires_auth(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/token-case", params={"target_type": "Asset", "target_id": "asset:x"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def test_api_token_case_rejects_invalid_window_and_removed_scope(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
    assert bad_scope.json() == {"ok": False, "error": "unsupported_query_param", "field": "scope"}


def test_api_token_case_matches_search_inspect_token_result_shape(tmp_path, monkeypatch):
    now_ms = 1_778_562_000_000
    monkeypatch.setattr("tracefold.app.http.routes_search._now_ms", lambda: now_ms)
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        target = seed_resolved_asset_with_event(client, symbol="HANSA", now_ms=now_ms - 60_000)
        token_case = client.get(
            "/api/token-case",
            params={
                "target_type": target["target_type"],
                "target_id": target["target_id"],
                "window": "24h",
            },
            headers={"Authorization": "Bearer secret"},
        )
        inspect = client.get(
            "/api/search/inspect",
            params={"q": "$HANSA", "window": "24h"},
            headers={"Authorization": "Bearer secret"},
        )

    assert token_case.status_code == 200
    assert inspect.status_code == 200
    assert inspect.json()["data"]["token_result"] == token_case.json()["data"]


def test_api_target_posts_returns_full_post_pages_and_requires_target_identity(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
            ingest_event(event)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        asset_flow = client.get(
            "/api/token-radar",
            params={"window": "5m", "limit": 5},
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]["targets"][0]
        target_type = asset_flow["factor_snapshot"]["subject"]["target_type"]
        target_id = asset_flow["factor_snapshot"]["subject"]["target_id"]

        missing = client.get("/api/target-posts?window=5m", headers={"Authorization": "Bearer secret"})
        first_page = client.get(
            "/api/target-posts",
            params={"target_type": target_type, "target_id": target_id, "window": "5m", "limit": 2},
            headers={"Authorization": "Bearer secret"},
        )
        second_page = client.get(
            "/api/target-posts",
            params={
                "target_type": target_type,
                "target_id": target_id,
                "window": "5m",
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
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=make_settings(tmp_path))

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
            ingest_event(event)
        rebuild_token_radar(client, now_ms=rebuild_now_ms)

        asset_flow = client.get(
            "/api/token-radar",
            params={"window": "5m", "limit": 5},
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]["targets"][0]
        target_type = asset_flow["factor_snapshot"]["subject"]["target_type"]
        target_id = asset_flow["factor_snapshot"]["subject"]["target_id"]

        missing = client.get("/api/target-social-timeline?window=5m", headers={"Authorization": "Bearer secret"})
        response = client.get(
            "/api/target-social-timeline",
            params={"target_type": target_type, "target_id": target_id, "window": "5m", "limit": 2},
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
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/target-social-timeline",
            params={"target_type": "Asset", "target_id": "asset:eip155:1:erc20:0xpepe", "window": "5m", "bucket": "1m"},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "unsupported_query_param", "field": "bucket"}


def test_api_rejects_removed_1m_window(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/token-radar",
            params={"window": "1m", "limit": 5},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "invalid_window", "field": "window"}


def test_api_target_posts_requires_target_identity(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert "handles" not in data
    assert data["ok"] is False
    assert data["reasons"] == ["runtime_missing"]
    assert data["workers_runtime"] == {
        "runtime_id": None,
        "runtime_version": None,
        "state": "unavailable",
        "started_at_ms": None,
        "heartbeat_at_ms": None,
        "heartbeat_stale_after_ms": 15_000,
        "fatal_code": None,
        "unavailable_reason": "runtime_missing",
    }
    assert "workers" not in data
    assert "worker_lanes" not in data
    assert "collector" not in data
    assert "enrichment" not in data
    assert "notifications" not in data

    assert "snapshot_gate" not in data


def test_api_status_remains_queryable_when_readiness_is_degraded(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        client.app.state.service.db.api_pool.close()
        readiness = client.get("/readyz")
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    body = response.json()
    assert readiness.status_code == 503
    assert readiness.json()["reasons"] == ["database_unavailable"]
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["data"]["ok"] is False
    assert body["data"]["reasons"] == [
        "database_unavailable",
        "runtime_status_query_failed",
    ]
    assert body["data"]["db"]["ok"] is False
    assert body["data"]["workers_runtime"]["state"] == "unavailable"
    assert "news" not in body["data"]


def test_api_rejects_removed_narrative_product_surfaces(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

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
    app = create_app(settings=make_settings(tmp_path))
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
    assert "author_watched" not in by_handle["toly"]
    assert "author_watched" not in by_handle["random_dude"]
    assert by_handle["toly"]["source_provider"] == "gmgn"
    assert by_handle["toly"]["channel"] == "twitter_monitor_basic"


def test_social_events_by_ids_skips_missing(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
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
    app = create_app(settings=make_settings(tmp_path))
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
    app = create_app(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/api/events/by-ids",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "ids_required"


def _seed_social_event_batch(app) -> None:
    with write_repositories() as repos, repos.transaction():
        repos.evidence.insert_event(
            make_event("event-watched", handle="toly", text="$PEPE watched", received_at_ms=1_700_000_000_000),
        )
        repos.evidence.insert_event(
            make_event(
                "event-public",
                handle="random_dude",
                text="$PEPE public",
                received_at_ms=1_700_000_010_000,
            ),
        )
