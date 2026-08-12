import json
import math
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Any

from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tracefold.app.http import routes_news
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
)
from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    enrich_token_radar,
    reduce_token_radar,
    token_radar_text_fingerprint,
)
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository
from tracefold.news import (
    NewsBriefSource,
    NewsBriefStoryLine,
    NewsBriefSynthesisResult,
    NewsFeedEntry,
    NewsFeedFetch,
    OpenNewsEvent,
)
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.projection import NewsProjectionSnapshot, compute_news_story_projection
from tracefold.news.sources import opennews_source, public_rss_sources
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
        ),
    )


def _rebuild_news_projection(repos: Any, *, now_ms: int) -> dict[str, Any]:
    payload = repos.news.load_story_projection(now_ms=now_ms)
    snapshot = NewsProjectionSnapshot(
        input_fingerprint=str(payload["input_fingerprint"]),
        scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
        current_input_fingerprint=(
            str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") else None
        ),
        rows=tuple(dict(row) for row in payload["rows"]),
    )
    if snapshot.unchanged:
        return {
            "projection_status": "unchanged_input",
            "items": len(snapshot.rows),
            "stories": 0,
            "rows_written": 0,
        }
    projection = compute_news_story_projection(snapshot)
    return dict(
        repos.news.publish_story_projection(
            snapshot=snapshot,
            projection=projection,
            now_ms=now_ms,
        )
    )


def _sync_public_news_sources(repos: Any, *, now_ms: int) -> None:
    rss_sources = public_rss_sources()
    repos.news.sync_sources((*rss_sources, opennews_source()), now_ms=now_ms)
    sources_by_id = {source.source_id: source for source in rss_sources}
    claim_token = "00000000-0000-0000-0000-000000000038"
    for index in range(len(rss_sources)):
        claimed = repos.news.claim_due_rss_source(
            now_ms=now_ms,
            claim_token=claim_token,
            lease_expires_at_ms=now_ms + 10_000,
        )
        assert claimed is not None
        if index == 0:
            assert repos.news.record_rss_fetch(
                source=sources_by_id[str(claimed["source_id"])],
                claim_token=claim_token,
                fetch=NewsFeedFetch(status_code=200),
                finished_at_ms=now_ms + 1,
            ) == {
                "items_inserted": 0,
                "items_updated": 0,
                "items_deactivated": 0,
            }
        else:
            assert repos.news.record_rss_failure(
                source_id=str(claimed["source_id"]),
                claim_token=claim_token,
                finished_at_ms=now_ms + index + 1,
                error_code="news_rss_http_503",
                status_code=503,
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
        repository = TokenRadarCurrentRepository(conn)
        with conn.transaction():
            reduced = reduce_token_radar(
                repository.load_material_inputs(now_ms=base_now_ms),
                now_ms=base_now_ms,
            )
            reduced = enrich_token_radar(
                reduced,
                repository.load_presentation_facts(
                    list(reduced.selected_keys),
                    now_ms=base_now_ms,
                ),
                now_ms=base_now_ms,
            )
            result = repository.publish(reduced, evaluation_at_ms=base_now_ms)
        assert result["status"] in {"published", "unchanged", "recovered"}
    finally:
        conn.close()


def token_radar_revision(*, target_id: str, event_index: int, now_ms: int) -> RadarEvidenceRevision:
    source_event_at_ms = now_ms - (3 - event_index) * 60_000
    event_id = f"event-{target_id}-{event_index}"
    return RadarEvidenceRevision(
        event_id=event_id,
        intent_id=f"intent-{target_id}-{event_index}",
        resolution_id=f"resolution-{target_id}-{event_index}",
        source_event_at_ms=source_event_at_ms,
        received_at_ms=source_event_at_ms + 1_000,
        event_created_at_ms=source_event_at_ms + 2_000,
        action="tweet",
        author_key=f"author-{target_id}-{event_index}",
        text_fingerprint=token_radar_text_fingerprint(f"independent text {target_id} {event_index}"),
        resolution_status="EXACT",
        target_type="Asset",
        target_id=target_id,
        resolution_decision_at_ms=source_event_at_ms + 2_000,
        resolution_created_at_ms=source_event_at_ms + 3_000,
    )


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


def test_api_search_resolves_robinhood_contracts_by_canonical_chain(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            ethereum = repos.registry.upsert_chain_asset(
                chain_id="eip155:1",
                address=PEPE,
                observed_at_ms=now_ms,
            )
            robinhood = repos.registry.upsert_chain_asset(
                chain_id="robinhood",
                address=PEPE,
                observed_at_ms=now_ms,
            )
        prefixed = client.get(
            "/api/search",
            params={"q": f"robinhood:{PEPE}"},
            headers={"Authorization": "Bearer secret"},
        )
        unprefixed = client.get(
            "/api/search",
            params={"q": PEPE},
            headers={"Authorization": "Bearer secret"},
        )

    assert prefixed.status_code == 200
    assert [candidate["target_id"] for candidate in prefixed.json()["data"]["target_candidates"]] == [
        robinhood["asset_id"]
    ]
    assert unprefixed.status_code == 200
    assert {candidate["target_id"] for candidate in unprefixed.json()["data"]["target_candidates"]} == {
        ethereum["asset_id"],
        robinhood["asset_id"],
    }


def test_api_news_exposes_exact_worldmonitor_read_contract(tmp_path):
    settings = make_settings(tmp_path)
    settings.news.rss_enabled = True
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _sync_public_news_sources(repos, now_ms=now_ms)
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
                            "coins": [
                                {
                                    "symbol": "BTC",
                                    "market_type": "spot",
                                    "match": "Bitcoin",
                                    "score": 91,
                                    "signal": "bullish",
                                    "grade": "A+",
                                }
                            ],
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
                        provider_record_id="story-1-corroboration",
                        title="Central bank raises interest rate after policy shock",
                        description="A second wire independently confirmed the policy decision.",
                        published_at_ms=now_ms - 70_000,
                        reporting_origin="second-wire",
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
            _rebuild_news_projection(repos, now_ms=now_ms)
            candidate = repos.news.peek_brief_candidate(now_ms=now_ms)
            assert candidate is not None
            prepared = repos.news.prepare_brief_run(
                slot_at_ms=candidate["slot_at_ms"],
                lease_owner="test-runtime",
                lease_token="test-lease",
                now_ms=now_ms,
            )
            assert prepared is not None and prepared["completed_without_model"] is False
            claim = prepared["claim"]
            stories = prepared["top_stories"]
            assert repos.news.start_brief_model(
                slot_at_ms=claim["slot_at_ms"],
                lease_owner=claim["lease_owner"],
                lease_token=claim["lease_token"],
                now_ms=now_ms + 1,
            )
            publication_id = repos.news.publish_brief(
                claim=claim,
                result=NewsBriefSynthesisResult(
                    brief_kind="l1",
                    quality="ok",
                    world_brief="Central bank policy and global emergencies lead the public brief [1].",
                    brief_story_lines=tuple(
                        NewsBriefStoryLine(n=index, text=f"{story['primary_title']} [{index}]")
                        for index, story in enumerate(stories, 1)
                    ),
                    sources=tuple(
                        NewsBriefSource(
                            title=story["primary_title"],
                            source=story["primary_source"],
                            url=story["primary_link"] or "",
                            published_at_ms=story["primary_published_at_ms"],
                        )
                        for story in stories
                    ),
                    provider="fixture",
                    model="fixture-model",
                    validation={
                        "failure_code": None,
                        "stripped_citations": 0,
                        "line_fallbacks": [],
                    },
                ),
                now_ms=now_ms + 2,
            )
            assert publication_id is not None

        headers = {"Authorization": "Bearer secret"}
        sources_response = client.get("/api/news/sources", headers=headers)
        sources_cursor = sources_response.json()["data"]["page"]["next_cursor"]
        sources_second_response = client.get(
            "/api/news/sources",
            params={"cursor": sources_cursor},
            headers=headers,
        )
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
            client.post(f"/api/news/stories/{story_id}/analysis-requests", headers=headers),
            client.get("/api/news", headers=headers),
            client.get("/api/news/items/news-1", headers=headers),
            client.get("/api/news/sources/status", headers=headers),
        ]

    assert sources_response.status_code == 200
    assert sources_second_response.status_code == 200
    source_items = [
        *sources_response.json()["data"]["items"],
        *sources_second_response.json()["data"]["items"],
    ]
    assert len(source_items) == 180
    assert source_items[0]["source_kind"] == "opennews"
    opennews = next(item for item in source_items if item["source_kind"] == "opennews")
    assert opennews["source_id"] == "news-opennews"
    assert opennews["last_success_at_ms"] == now_ms
    rss_items = [item for item in source_items if item["source_kind"] == "rss"]
    assert len(rss_items) == 179
    assert all(item["feed_url"].startswith("https://") for item in rss_items)
    assert sum(item["last_success_at_ms"] is not None for item in rss_items) == 1
    assert sources_response.json()["data"]["page"] == {
        "returned_count": 100,
        "has_more": True,
        "next_cursor": sources_cursor,
    }
    assert sources_second_response.json()["data"]["page"] == {
        "returned_count": 80,
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
    assert story["source_count"] == 2
    assert story["provider_evidence"] == {
        "item_id": story["representative_item_id"],
        "url": "https://example.test/story-1",
        "provider_metadata": {
            "score": 76,
            "signal": "long",
            "grade": "A",
            "assets": [
                {
                    "symbol": "BTC",
                    "market_type": "spot",
                    "match": "Bitcoin",
                    "score": 91,
                    "signal": "bullish",
                    "grade": "A+",
                }
            ],
        },
    }
    assert "coins" not in story["provider_evidence"]["provider_metadata"]
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
    assert "coins" not in detail["members"][0]["provider_metadata"]
    assert detail["members_page"] == {
        "returned_count": 2,
        "has_more": False,
        "next_cursor": None,
    }
    assert "analysis" not in detail
    assert "revisions" not in detail

    assert brief_response.status_code == 200
    brief = brief_response.json()["data"]
    assert brief["state"] == "current"
    assert len(brief["publication"]["top_stories"]) == 3
    assert brief["publication"]["locale"] == "en"
    assert brief["publication"]["brief_kind"] == "l1"
    assert brief["publication"]["validation"] == {
        "failure_code": None,
        "stripped_citations": 0,
        "line_fallbacks": [],
    }
    assert len(brief["publication"]["top_stories"]) == len(brief["publication"]["brief_story_lines"])
    assert len(brief["publication"]["top_stories"]) == len(brief["publication"]["sources"])
    assert set(brief) == {
        "state",
        "slot_at_ms",
        "next_due_at_ms",
        "publication",
        "latest_run",
    }
    assert brief["publication"]["slot_at_ms"] == brief["slot_at_ms"]
    assert brief["latest_run"]["status"] == "completed"
    assert unchanged_brief.status_code == 304
    assert status_response.status_code == 200
    status_layers = status_response.json()["data"]["layers"]
    assert status_layers["ingest"]["rss"] == {
        "enabled": True,
        "source_count": 179,
        "successful_source_count": 1,
        "failed_source_count": 178,
        "claimed_source_count": 0,
        "next_due_at_ms": now_ms + 1_800_001,
        "latest_success_at_ms": now_ms + 1,
    }
    assert status_layers["ingest"]["opennews"]["source_id"] == "news-opennews"
    disabled_push = status_layers["push"]
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
    assert [response.status_code for response in retired_responses] == [404] * 5


def test_api_news_status_treats_default_disabled_rss_as_intentional(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.news.rss_enabled is False
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
                        provider_record_id="opennews-only-status",
                        title="OpenNews primary publishes without RSS enabled",
                        description="The primary lane remains independently usable.",
                        published_at_ms=now_ms - 1_000,
                        reporting_origin="authority",
                    ),
                ),
                observed_at_ms=now_ms,
            )
            repos.news.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=now_ms,
                error_code=None,
            )
            _rebuild_news_projection(repos, now_ms=now_ms)

        response = client.get("/api/news/status", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    ingest = response.json()["data"]["layers"]["ingest"]
    assert ingest["status"] == "ready"
    assert ingest["reasons"] == []
    assert ingest["rss"] == {
        "enabled": False,
        "source_count": 0,
        "successful_source_count": 0,
        "failed_source_count": 0,
        "claimed_source_count": 0,
        "next_due_at_ms": None,
        "latest_success_at_ms": None,
    }


def test_api_news_canonicalizes_opennews_wire_headline_and_wrapper_origin(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)
    source = opennews_source()
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "twitter-canonical-wire",
                "text": (
                    "Iran announces verified ceasefire talks&lt;br&gt;"
                    "Officials said the announcement followed two days of negotiations "
                    "and published a detailed timetable.&lt;br&gt;https://example.test/noise"
                ),
                "newsType": "Twitter",
                "engineType": "news",
                "source": "ReutersWorld",
                "link": "https://example.test/twitter-canonical-wire",
                "ts": now_ms,
            },
        }
    )
    assert event is not None

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(event,),
                observed_at_ms=now_ms,
            )
            _rebuild_news_projection(repos, now_ms=now_ms)

        headers = {"Authorization": "Bearer secret"}
        feed = client.get("/api/news/feed", headers=headers).json()["data"]
        detail = client.get(
            f"/api/news/stories/{feed['stories'][0]['story_id']}",
            headers=headers,
        ).json()["data"]

    assert {
        "story_title": feed["stories"][0]["title"],
        "item_title": detail["members"][0]["title"],
        "description": detail["members"][0]["description"],
        "reporting_origin": detail["members"][0]["reporting_origin"],
    } == {
        "story_title": "Iran announces verified ceasefire talks",
        "item_title": "Iran announces verified ceasefire talks",
        "description": (
            "Officials said the announcement followed two days of negotiations "
            "and published a detailed timetable. https://example.test/noise"
        ),
        "reporting_origin": "reutersworld",
    }


def test_api_news_strips_wire_controls_and_prefers_bounded_explicit_description(tmp_path):
    app = create_app(settings=make_settings(tmp_path))
    now_ms = int(time.time() * 1000)
    source = opennews_source()
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-controls-and-description",
                "text": (
                    "Central bank approves policy\x00 today<br>"
                    "This remaining block must not replace an explicit provider description."
                ),
                "description": (
                    "<p>Officials confirmed the decision &amp; published implementation details " + ("x" * 500) + "</p>"
                ),
                "newsType": "Reuters",
                "engineType": "news",
                "ts": now_ms,
            },
        }
    )
    assert event is not None

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(event,),
                observed_at_ms=now_ms,
            )
            _rebuild_news_projection(repos, now_ms=now_ms)
        headers = {"Authorization": "Bearer secret"}
        feed = client.get("/api/news/feed", headers=headers).json()["data"]
        detail = client.get(
            f"/api/news/stories/{feed['stories'][0]['story_id']}",
            headers=headers,
        ).json()["data"]

    assert {
        "title": detail["members"][0]["title"],
        "description": detail["members"][0]["description"],
        "description_length": len(detail["members"][0]["description"]),
    } == {
        "title": "Central bank approves policy today",
        "description": ("Officials confirmed the decision & published implementation details " + ("x" * 332)),
        "description_length": 400,
    }


def test_api_news_status_reports_enabled_push_without_secrets_or_payloads(tmp_path):
    settings = make_settings(tmp_path)
    settings.news.push = type(settings.news.push)(
        enabled=True,
        feishu_webhook_url=("https://open.feishu.cn/open-apis/bot/v2/hook/status-test-hook"),
    )
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _sync_public_news_sources(repos, now_ms=now_ms)
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
            _rebuild_news_projection(repos, now_ms=now_ms)
            repos.news.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=now_ms,
                error_code=None,
            )

        headers = {"Authorization": "Bearer secret"}
        warming_response = client.get("/api/news/status", headers=headers)

        with write_repositories() as repos, repos.transaction():
            repos.news.initialize_push_baseline(now_ms=now_ms + 1)

        unsigned_ready_response = client.get("/api/news/status", headers=headers)

        with write_repositories() as repos, repos.transaction():
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
        "brief": "warming",
        "push": "warming",
    }
    assert warming["layers"]["push"]["initialized"] is False
    assert warming["layers"]["push"]["feishu_signing_secret_configured"] is False
    assert warming["layers"]["push"]["reasons"] == ["push_baseline_uninitialized"]
    assert warming["layers"]["push"]["translation_24h"] == {
        "attempted": 0,
        "succeeded": 0,
        "success_ratio": None,
        "latency_p95_ms": None,
        "failure_counts": {},
        "slo_met": None,
    }
    assert warming["layers"]["push"]["delivery_24h"] == {
        "completed": 0,
        "latency_p95_ms": None,
        "over_120s": 0,
        "slo_met": None,
    }

    assert unsigned_ready_response.status_code == 200
    unsigned_ready = unsigned_ready_response.json()["data"]
    assert unsigned_ready["status"] == "warming"
    assert unsigned_ready["layers"]["push"]["status"] == "ready"
    assert unsigned_ready["layers"]["push"]["reasons"] == []
    assert unsigned_ready["layers"]["push"]["feishu_signing_secret_configured"] is False

    assert degraded_response.status_code == 200
    degraded = degraded_response.json()["data"]
    push = degraded["layers"]["push"]
    assert degraded["status"] == "degraded"
    assert push["status"] == "degraded"
    assert push["enabled"] is True
    assert push["feishu_webhook_url_configured"] is True
    assert push["feishu_signing_secret_configured"] is False
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
    assert disabled_history["status"] == "warming"
    assert disabled_push["status"] == "disabled"
    assert disabled_push["enabled"] is False
    assert disabled_push["initialized"] is True
    assert disabled_push["baseline_at_ms"] == now_ms + 1
    assert disabled_push["pending_count"] == 1
    assert disabled_push["retry_count"] == 1
    assert disabled_push["terminal_count"] == 1
    assert disabled_push["sent_count"] == 1
    assert disabled_push["latest_sent_at_ms"] == now_ms + 3


def test_api_news_notification_separates_current_eligibility_from_durable_delivery(
    tmp_path,
    monkeypatch,
):
    clock = {"now_ms": 1_800_000_000_000}
    monkeypatch.setattr(routes_news, "_now_ms", lambda: clock["now_ms"])
    settings = make_settings(tmp_path)
    settings.news.push = type(settings.news.push)(
        enabled=True,
        feishu_webhook_url=("https://open.feishu.cn/open-apis/bot/v2/hook/http-notification-test"),
    )
    app = create_app(settings=settings)
    now_ms = clock["now_ms"]
    baseline_at_ms = now_ms - 2 * 60 * 60 * 1_000
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            repos.news.sync_sources((source,), now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(
                    _opennews_event(
                        provider_record_id="push-state-high",
                        title="Bitcoin exchange announces a new custody product",
                        description="The exchange published its custody launch details.",
                        published_at_ms=now_ms - 2_000,
                        reporting_origin="exchange",
                        provider_metadata={
                            "score": 91,
                            "coins": [{"symbol": "BTC", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="push-state-low",
                        title="Regional ministry updates an agricultural subsidy",
                        description="The ministry published its annual subsidy notice.",
                        published_at_ms=now_ms - 1_000,
                        reporting_origin="ministry",
                        provider_metadata={"score": 70},
                    ),
                    _opennews_event(
                        provider_record_id="push-state-assetless",
                        title="Space agency publishes a telescope maintenance calendar",
                        description="The agency listed routine observatory maintenance dates.",
                        published_at_ms=now_ms - 800,
                        reporting_origin="space-agency",
                        provider_metadata={"score": 92},
                    ),
                    _opennews_event(
                        provider_record_id="push-state-cl-only",
                        title="Customs authority revises regional paperwork requirements",
                        description="The authority clarified forms for cross-border declarations.",
                        published_at_ms=now_ms - 600,
                        reporting_origin="customs-authority",
                        provider_metadata={
                            "score": 93,
                            "coins": [
                                {"symbol": "CL", "market_type": "cex"},
                                {"symbol": "XYZ-CL", "market_type": "cex"},
                            ],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="push-state-malformed-assets",
                        title="Museum announces an extended exhibition schedule",
                        description="The museum extended public viewing dates for its collection.",
                        published_at_ms=now_ms - 400,
                        reporting_origin="museum",
                        provider_metadata={
                            "score": 89,
                            "coins": [
                                {"market_type": "spot"},
                                {"symbol": "BTC"},
                                "invalid",
                            ],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="push-state-baseline",
                        title="Shipping authority publishes a port inspection schedule",
                        description="The authority published its inspection timetable.",
                        published_at_ms=baseline_at_ms,
                        reporting_origin="shipping-authority",
                        provider_metadata={
                            "score": 94,
                            "coins": [{"symbol": "ETH", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="push-state-stale",
                        title="Energy producer announces a completed facility inspection",
                        description="The producer completed the scheduled facility review.",
                        published_at_ms=(now_ms - 15 * 60 * 1_000 - 1),
                        reporting_origin="energy-producer",
                        provider_metadata={
                            "score": 95,
                            "coins": [{"symbol": "SOL", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="push-state-boundary",
                        title="Treasury announces a digital asset custody consultation",
                        description="The consultation covers institutional custody standards.",
                        published_at_ms=now_ms - 15 * 60 * 1_000,
                        reporting_origin="treasury",
                        provider_metadata={
                            "score": 96,
                            "coins": [{"symbol": "XRP", "market_type": "spot"}],
                        },
                    ),
                ),
                observed_at_ms=now_ms,
            )
            repos.conn.execute(
                """
                UPDATE news_push_state
                   SET baseline_at_ms = %s, updated_at_ms = %s
                 WHERE singleton_key = 'current'
                """,
                (baseline_at_ms, now_ms),
            )
            _rebuild_news_projection(repos, now_ms=now_ms)

        headers = {"Authorization": "Bearer secret"}
        initial_response = client.get("/api/news/feed", headers=headers)
        initial = initial_response.json()["data"]["stories"]
        high = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 91)
        low = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 70)
        assetless = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 92)
        cl_only = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 93)
        malformed_assets = next(
            story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 89
        )
        baseline = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 94)
        stale = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 95)
        boundary = next(story for story in initial if story["provider_evidence"]["provider_metadata"]["score"] == 96)

        assert high["notification"] == {
            "eligible": True,
            "ineligible_reason": None,
            "delivery_state": "not_created",
        }
        assert low["notification"] == {
            "eligible": False,
            "ineligible_reason": "score_threshold",
            "delivery_state": "not_created",
        }
        assert assetless["notification"] == {
            "eligible": False,
            "ineligible_reason": "no_asset",
            "delivery_state": "not_created",
        }
        assert cl_only["notification"] == {
            "eligible": False,
            "ineligible_reason": "cl_family_only",
            "delivery_state": "not_created",
        }
        assert "assets" not in malformed_assets["provider_evidence"]["provider_metadata"]
        assert malformed_assets["notification"]["ineligible_reason"] == "no_asset"
        assert baseline["notification"]["ineligible_reason"] == "baseline"
        assert stale["notification"]["ineligible_reason"] == "stale"
        assert boundary["notification"] == {
            "eligible": True,
            "ineligible_reason": None,
            "delivery_state": "not_created",
        }

        with write_repositories() as repos, repos.transaction():
            assert repos.news.insert_push_candidate(
                story_id=high["story_id"],
                selected_item_id=high["provider_evidence"]["item_id"],
                provider_score=91,
                threshold_observed_at_ms=now_ms,
                source_payload={"provider_evidence": high["provider_evidence"]},
                suppressed=False,
                now_ms=now_ms,
            )

        pending = client.get(f"/api/news/stories/{high['story_id']}", headers=headers).json()["data"]
        assert pending["notification"] == {
            "eligible": True,
            "ineligible_reason": None,
            "delivery_state": "pending",
        }

        expected_by_ledger_status = {
            "retry_wait": "pending",
            "sent": "sent",
            "suppressed": "suppressed",
            "terminal": "failed",
        }
        for ledger_status, expected in expected_by_ledger_status.items():
            with write_repositories() as repos, repos.transaction():
                repos.conn.execute(
                    "UPDATE news_push_deliveries SET status = %s WHERE story_id = %s",
                    (ledger_status, high["story_id"]),
                )
            detail = client.get(f"/api/news/stories/{high['story_id']}", headers=headers).json()["data"]
            assert detail["notification"]["delivery_state"] == expected

        with write_repositories() as repos, repos.transaction():
            repos.conn.execute(
                "UPDATE news_push_deliveries SET status = 'sent' WHERE story_id = %s",
                (high["story_id"],),
            )
            repos.conn.execute(
                """
                UPDATE news_items
                   SET provider_metadata = '{}'::jsonb,
                       provider_score_updated_at_ms = %s
                 WHERE item_id = %s
                """,
                (now_ms + 1, high["provider_evidence"]["item_id"]),
            )
        low_with_historical_ledger = client.get(
            f"/api/news/stories/{high['story_id']}",
            headers=headers,
        ).json()["data"]
        assert low_with_historical_ledger["provider_evidence"] is None
        assert low_with_historical_ledger["notification"] == {
            "eligible": False,
            "ineligible_reason": "score_threshold",
            "delivery_state": "sent",
        }

        with write_repositories() as repos, repos.transaction():
            assert repos.news.insert_push_candidate(
                story_id="f" * 64,
                selected_item_id=boundary["provider_evidence"]["item_id"],
                provider_score=96,
                threshold_observed_at_ms=now_ms,
                source_payload={"provider_evidence": boundary["provider_evidence"]},
                suppressed=False,
                now_ms=now_ms,
            )
            repos.conn.execute(
                "UPDATE news_push_deliveries SET status = 'sent' WHERE story_id = %s",
                ("f" * 64,),
            )
        rebound = client.get(
            f"/api/news/stories/{boundary['story_id']}",
            headers=headers,
        ).json()["data"]
        assert rebound["notification"]["delivery_state"] == "sent"

        settings.news.push = type(settings.news.push)()
        disabled = client.get(
            f"/api/news/stories/{boundary['story_id']}",
            headers=headers,
        ).json()["data"]
        assert disabled["notification"] == {
            "eligible": False,
            "ineligible_reason": "disabled",
            "delivery_state": "sent",
        }

        settings.news.push = type(settings.news.push)(
            enabled=True,
            feishu_webhook_url=("https://open.feishu.cn/open-apis/bot/v2/hook/http-notification-test"),
        )
        at_boundary = client.get("/api/news/feed", headers=headers)
        boundary_etag = at_boundary.headers["etag"]
        boundary_before_expiry = next(
            story for story in at_boundary.json()["data"]["stories"] if story["story_id"] == boundary["story_id"]
        )
        assert boundary_before_expiry["notification"] == {
            "eligible": True,
            "ineligible_reason": None,
            "delivery_state": "sent",
        }
        clock["now_ms"] += 1
        revalidated = client.get(
            "/api/news/feed",
            headers={**headers, "If-None-Match": boundary_etag},
        )
        assert revalidated.status_code == 200
        assert revalidated.headers["etag"] != boundary_etag
        aged_boundary = next(
            story for story in revalidated.json()["data"]["stories"] if story["story_id"] == boundary["story_id"]
        )
        assert aged_boundary["notification"] == {
            "eligible": False,
            "ineligible_reason": "stale",
            "delivery_state": "sent",
        }

        with write_repositories() as repos, repos.transaction():
            repos.conn.execute(
                """
                UPDATE news_items
                   SET provider_metadata = '{}'::jsonb,
                       provider_score_updated_at_ms = %s
                 WHERE item_id = %s
                """,
                (now_ms + 1, boundary["provider_evidence"]["item_id"]),
            )
        rebound_without_current_evidence = client.get(
            f"/api/news/stories/{boundary['story_id']}",
            headers=headers,
        ).json()["data"]
        assert rebound_without_current_evidence["notification"] == {
            "eligible": False,
            "ineligible_reason": "score_threshold",
            "delivery_state": "sent",
        }


def test_api_news_status_reports_workers_push_lag_and_translation_slo(tmp_path):
    settings = make_settings(tmp_path)
    settings.news.push = type(settings.news.push)(
        enabled=True,
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/status-test-hook",
    )
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    source = opennews_source()

    with TestClient(app) as client:
        with write_repositories() as repos, repos.transaction():
            _sync_public_news_sources(repos, now_ms=now_ms)
            repos.news.record_opennews_events(
                source=source,
                events=(
                    _opennews_event(
                        provider_record_id="status-runtime-push",
                        title="Bitcoin issuer announces a spot fund decision",
                        description="The issuer published the decision.",
                        published_at_ms=now_ms - 1_000,
                        reporting_origin="issuer",
                        provider_metadata={"score": 91},
                    ),
                ),
                observed_at_ms=now_ms,
            )
            _rebuild_news_projection(repos, now_ms=now_ms)
            repos.news.update_opennews_live_status(
                source_id=source.source_id,
                connected=True,
                now_ms=now_ms,
                error_code=None,
            )
            repos.news.initialize_push_baseline(now_ms=now_ms)
            repos.conn.execute(
                """
                INSERT INTO workers_runtime (
                  singleton_key, runtime_id, runtime_version, lifecycle_state,
                  started_at_ms, heartbeat_at_ms, fatal_code
                ) VALUES (true, %s, 'test-runtime', 'starting', %s, %s, NULL)
                """,
                (
                    "00000000-0000-0000-0000-000000000001",
                    now_ms - 20_000,
                    now_ms,
                ),
            )

        headers = {"Authorization": "Bearer secret"}
        starting = client.get("/api/news/status", headers=headers).json()["data"]
        assert starting["operating_state"] == "recovering"
        assert "workers_runtime_starting" in starting["layers"]["story"]["reasons"]

        with write_repositories() as repos, repos.transaction():
            repos.conn.execute(
                """
                UPDATE workers_runtime
                   SET lifecycle_state = 'running', heartbeat_at_ms = %s
                 WHERE singleton_key
                """,
                (now_ms - 16_000,),
            )
        stale = client.get("/api/news/status", headers=headers).json()["data"]
        assert stale["operating_state"] == "stalled"
        assert "workers_runtime_heartbeat_stale" in stale["layers"]["story"]["reasons"]

        with write_repositories() as repos, repos.transaction():
            repos.conn.execute("DELETE FROM workers_runtime")
            evidence = next(iter(repos.news.story_push_contexts().values()))
            selected = evidence["provider_evidence"]
            for index, duration_ms in enumerate((1_000, 4_000), start=1):
                story_id = f"{index:x}" * 64
                assert repos.news.insert_push_candidate(
                    story_id=story_id,
                    selected_item_id=f"translation-sample-{index}",
                    provider_score=91,
                    threshold_observed_at_ms=now_ms - 130_000,
                    source_payload={"provider_evidence": selected},
                    suppressed=False,
                    now_ms=now_ms - 130_000,
                )
                repos.conn.execute(
                    """
                    UPDATE news_push_deliveries
                       SET status = %s,
                           translation_status = %s,
                           next_attempt_at_ms = NULL,
                           sent_at_ms = %s,
                           updated_at_ms = %s,
                           delivery_payload = jsonb_build_object(
                             'presentation', jsonb_build_object(
                               'prompt_version', 'title_zh_v2',
                               'headline_mode', cast(%s AS text),
                               'fallback_code', cast(%s AS text),
                               'translation_attempted_at_ms', cast(%s AS bigint),
                               'translation_duration_ms', cast(%s AS bigint)
                             )
                           ),
                           payload_fingerprint = repeat(%s, 64)
                     WHERE story_id = %s
                    """,
                    (
                        "sent" if index == 1 else "terminal",
                        "translated" if index == 1 else "unavailable",
                        now_ms - 500 if index == 1 else None,
                        now_ms - 500,
                        "translated" if index == 1 else "fallback_original",
                        None if index == 1 else "news_push_translation_rate_limited",
                        now_ms - 1_000,
                        duration_ms,
                        str(index),
                        story_id,
                    ),
                )

        translation = client.get("/api/news/status", headers=headers).json()["data"]

    assert translation["operating_state"] == "recovering"
    push = translation["layers"]["push"]
    assert push["translation_24h"] == {
        "attempted": 2,
        "succeeded": 1,
        "success_ratio": 0.5,
        "latency_p95_ms": 3850,
        "failure_counts": {"news_push_translation_rate_limited": 1},
        "slo_met": False,
    }
    assert push["delivery_24h"] == {
        "completed": 2,
        "latency_p95_ms": 129500,
        "over_120s": 2,
        "slo_met": False,
    }
    assert "push_translation_success_slo_breached" in push["reasons"]
    assert "push_translation_latency_slo_breached" in push["reasons"]
    assert "push_delivery_latency_slo_breached" in push["reasons"]

    with write_repositories() as repos, repos.transaction():
        repos.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = 'pending_delivery',
                   next_attempt_at_ms = %s,
                   delivery_payload = NULL,
                   payload_fingerprint = NULL,
                   translation_status = 'pending'
            WHERE story_id = %s
            """,
            (now_ms + 3_600_000, "1" * 64),
        )
    with TestClient(app) as client:
        overdue = client.get(
            "/api/news/status",
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]
    assert overdue["operating_state"] == "stalled"
    assert "push_delivery_stalled" in overdue["layers"]["push"]["reasons"]


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
            _rebuild_news_projection(repos, now_ms=now_ms)

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


def test_api_news_feed_priority_search_reporting_origin_and_cursor_filters_are_server_authoritative(tmp_path):
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
                        provider_record_id="policy-primary",
                        title="Bitcoin reserve policy approved by central bank",
                        description="Market monitor officials released policy documents.",
                        published_at_ms=now_ms - 40_000,
                        reporting_origin="Reuters",
                        provider_metadata={
                            "score": 71,
                            "source": "Wire Alpha",
                            "coins": [{"symbol": "BTC", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="policy-corroboration",
                        title="Bitcoin reserve policy approved by central bank",
                        description="A unique corroboration marker from a second newsroom.",
                        published_at_ms=now_ms - 30_000,
                        reporting_origin="Bloomberg",
                        provider_metadata={"score": 65, "source": "Wire Beta"},
                    ),
                    _opennews_event(
                        provider_record_id="threshold",
                        title="Regional power grid reports planned maintenance",
                        description="Routine work will begin overnight.",
                        published_at_ms=now_ms - 20_000,
                        reporting_origin="Utility Times",
                        provider_metadata={
                            "score": 70,
                            "source": "Boundary Desk",
                            "coins": [{"symbol": "BOUNDARYCOIN", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="software",
                        title="Software consortium releases open source database update",
                        description="Market monitor teams published the release notes.",
                        published_at_ms=now_ms - 10_000,
                        reporting_origin="Bloomberg",
                        provider_metadata={
                            "score": 82,
                            "source": "Provider Desk",
                            "coins": [{"symbol": "ETH", "market_type": "spot"}],
                        },
                    ),
                    _opennews_event(
                        provider_record_id="unscored",
                        title="Local library extends weekend opening hours",
                        description="The updated public schedule starts next month.",
                        published_at_ms=now_ms - 5_000,
                        reporting_origin="Community News",
                    ),
                ),
                observed_at_ms=now_ms,
            )
            _rebuild_news_projection(repos, now_ms=now_ms)
            # Feed filters bind to the published Story membership closure. An
            # acquisition expiry can precede the next atomic Story rebuild.
            repos.conn.execute("UPDATE news_items SET active=false WHERE provider_record_id='software'")
            # A repeated provider report may also update an Item origin before
            # the minute-cadence Story writer republishes that closure.
            repos.conn.execute("UPDATE news_items SET reporting_origin='reuters' WHERE provider_record_id='software'")

        headers = {"Authorization": "Bearer secret"}
        complete = client.get("/api/news/feed", headers=headers)
        priority = client.get(
            "/api/news/feed",
            params={"provider_score_gt": 70, "sort": "latest"},
            headers=headers,
        )
        by_title = client.get("/api/news/feed", params={"q": "BITCOIN RESERVE"}, headers=headers)
        by_description = client.get(
            "/api/news/feed",
            params={"q": "corroboration marker"},
            headers=headers,
        )
        by_reporting_origin = client.get("/api/news/feed", params={"q": "utility times"}, headers=headers)
        by_provider_source = client.get("/api/news/feed", params={"q": "provider desk"}, headers=headers)
        by_coin = client.get("/api/news/feed", params={"q": "boundarycoin"}, headers=headers)
        exact_origin = client.get(
            "/api/news/feed",
            params={"reporting_origin": " BLOOMBERG ", "sort": "latest"},
            headers=headers,
        )
        first_page = client.get(
            "/api/news/feed",
            params={
                "provider_score_gt": 70,
                "reporting_origin": "bloomberg",
                "q": "market monitor",
                "sort": "latest",
                "limit": 1,
            },
            headers=headers,
        )
        cursor = first_page.json()["data"]["next_cursor"]
        second_page = client.get(
            "/api/news/feed",
            params={
                "provider_score_gt": 70,
                "reporting_origin": "bloomberg",
                "q": "market monitor",
                "sort": "latest",
                "limit": 1,
                "cursor": cursor,
            },
            headers=headers,
        )
        mismatches = (
            client.get(
                "/api/news/feed",
                params={
                    "provider_score_gt": 69,
                    "reporting_origin": "bloomberg",
                    "q": "market monitor",
                    "sort": "latest",
                    "limit": 1,
                    "cursor": cursor,
                },
                headers=headers,
            ),
            client.get(
                "/api/news/feed",
                params={
                    "provider_score_gt": 70,
                    "reporting_origin": "reuters",
                    "q": "market monitor",
                    "sort": "latest",
                    "limit": 1,
                    "cursor": cursor,
                },
                headers=headers,
            ),
            client.get(
                "/api/news/feed",
                params={
                    "provider_score_gt": 70,
                    "reporting_origin": "bloomberg",
                    "q": "release notes",
                    "sort": "latest",
                    "limit": 1,
                    "cursor": cursor,
                },
                headers=headers,
            ),
        )

    assert complete.status_code == 200
    complete_data = complete.json()["data"]
    assert len(complete_data["stories"]) == 4
    assert complete_data["filters"] == {
        "category": None,
        "level": None,
        "source_id": None,
        "reporting_origin": None,
        "provider_score_gt": None,
        "q": None,
    }
    assert {facet["value"]: facet["count"] for facet in complete_data["facets"]["reporting_origins"]} == {
        "bloomberg": 2,
        "community news": 1,
        "reuters": 1,
        "utility times": 1,
    }
    assert complete_data["facets"]["page"]["reporting_origins_has_more"] is False

    assert priority.status_code == 200
    priority_data = priority.json()["data"]
    assert priority_data["filters"]["provider_score_gt"] == 70
    assert [story["provider_evidence"]["provider_metadata"]["score"] for story in priority_data["stories"]] == [
        82,
        71,
    ]
    assert all(story["provider_evidence"]["provider_metadata"]["score"] > 70 for story in priority_data["stories"])
    assert {facet["value"] for facet in priority_data["facets"]["reporting_origins"]} == {
        "bloomberg",
        "reuters",
    }

    expected_policy_title = "Bitcoin reserve policy approved by central bank"
    assert [story["title"] for story in by_title.json()["data"]["stories"]] == [expected_policy_title]
    assert [story["title"] for story in by_description.json()["data"]["stories"]] == [expected_policy_title]
    assert [story["title"] for story in by_reporting_origin.json()["data"]["stories"]] == [
        "Regional power grid reports planned maintenance"
    ]
    assert [story["title"] for story in by_provider_source.json()["data"]["stories"]] == [
        "Software consortium releases open source database update"
    ]
    assert [story["title"] for story in by_coin.json()["data"]["stories"]] == [
        "Regional power grid reports planned maintenance"
    ]

    assert exact_origin.status_code == 200
    assert exact_origin.json()["data"]["filters"]["reporting_origin"] == "bloomberg"
    assert [story["title"] for story in exact_origin.json()["data"]["stories"]] == [
        "Software consortium releases open source database update",
        expected_policy_title,
    ]
    assert exact_origin.json()["data"]["facets"]["reporting_origins"] == [
        {"value": "bloomberg", "label": "bloomberg", "count": 2},
        {"value": "reuters", "label": "reuters", "count": 1},
    ]
    assert first_page.status_code == 200
    assert cursor
    assert second_page.status_code == 200
    first_story_id = first_page.json()["data"]["stories"][0]["story_id"]
    second_story_id = second_page.json()["data"]["stories"][0]["story_id"]
    assert first_story_id != second_story_id
    assert second_page.json()["data"]["next_cursor"] is None
    assert second_page.json()["data"]["has_more"] is False
    assert [response.status_code for response in mismatches] == [400, 400, 400]
    assert [response.json() for response in mismatches] == [
        {"ok": False, "error": "news_feed_cursor_filter_mismatch", "field": "cursor"},
        {"ok": False, "error": "news_feed_cursor_filter_mismatch", "field": "cursor"},
        {"ok": False, "error": "news_feed_cursor_filter_mismatch", "field": "cursor"},
    ]


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
        radar = client.get("/api/token-radar", headers=headers)
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
    assert "current_radar" not in inspect_data["token_result"]
    legacy_market_field = "market_overlay"
    assert legacy_market_field not in inspect_data["token_result"]
    assert "radar_item" not in inspect_data["token_result"]
    assert "agent_brief" not in inspect_data["token_result"]
    assert "discussion_digest" not in inspect_data["token_result"]
    assert "narrative_admission" not in inspect_data["token_result"]

    assert radar.status_code == 200
    assert radar.json()["data"] == {
        "schema_version": "token_radar_snapshot_v4",
        "state": "current",
        "stale_reason": None,
        "state_changed_at_ms": rebuild_now_ms,
        "social_evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }

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
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "schema_version": "token_radar_snapshot_v4",
        "state": "current",
        "stale_reason": None,
        "state_changed_at_ms": rebuild_now_ms,
        "social_evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }


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


def test_api_stocks_radar_is_not_registered(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/stocks-radar",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 404


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


def test_api_token_radar_rejects_all_product_queries_but_keeps_auth_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        names = ("window", "venue", "limit", "scope", "arbitrary")
        responses = [client.get("/api/token-radar", params={name: "value"}, headers=headers) for name in names]
        auth_query = client.get("/api/token-radar", params={"token": "secret"})

    assert [response.status_code for response in responses] == [400] * len(names)
    assert [response.json() for response in responses] == [
        {"ok": False, "error": "unsupported_query_param", "field": name} for name in names
    ]
    assert auth_query.status_code == 200
    assert auth_query.json()["data"] == {
        "schema_version": "token_radar_snapshot_v4",
        "state": "unavailable",
        "stale_reason": None,
        "state_changed_at_ms": 0,
        "social_evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }


def test_api_token_radar_serves_exact_packet_with_stable_etag(tmp_path):
    now_ms = int(time.time() * 1_000)
    settings = make_settings(tmp_path)
    reduced = enrich_token_radar(
        reduce_token_radar(
            [
                token_radar_revision(
                    target_id="asset:test",
                    event_index=index,
                    now_ms=now_ms,
                )
                for index in range(3)
            ],
            now_ms=now_ms,
        ),
        [
            {
                "target_type": "Asset",
                "target_id": "asset:test",
                "symbol": "TEST",
                "name": "Test Token",
                "logo_url": None,
                "chain": "eip155:1",
                "exchange": None,
                "address": "0xtest",
                "signal_price_usd": "1",
                "price_usd": "1.25",
                "price_observed_at_ms": now_ms - 30_000,
                "market_cap_usd": "1000000",
                "market_cap_observed_at_ms": now_ms - 45_000,
            }
        ],
        now_ms=now_ms,
    )
    with write_repositories() as repos, repos.transaction():
        result = TokenRadarCurrentRepository(repos.conn).publish(
            reduced,
            evaluation_at_ms=now_ms,
        )
    assert result["status"] == "published"
    app = create_app(settings=settings)

    with TestClient(app) as client:
        current = client.get(
            "/api/token-radar",
            headers={"Authorization": "Bearer secret"},
        )
        current_not_modified = client.get(
            "/api/token-radar",
            headers={
                "Authorization": "Bearer secret",
                "If-None-Match": current.headers["etag"],
            },
        )
        with write_repositories() as repos, repos.transaction():
            failure_writes = TokenRadarCurrentRepository(repos.conn).record_failure(
                error_code="token_radar_source_unavailable",
                evaluation_at_ms=now_ms + 1,
            )
        stale = client.get(
            "/api/token-radar",
            headers={
                "Authorization": "Bearer secret",
                "If-None-Match": current.headers["etag"],
            },
        )
        stale_not_modified = client.get(
            "/api/token-radar",
            headers={
                "Authorization": "Bearer secret",
                "If-None-Match": stale.headers["etag"],
            },
        )
        with write_repositories() as repos, repos.transaction():
            recovered_result = TokenRadarCurrentRepository(repos.conn).publish(
                reduced,
                evaluation_at_ms=now_ms + 2,
            )
        recovered = client.get(
            "/api/token-radar",
            headers={
                "Authorization": "Bearer secret",
                "If-None-Match": stale.headers["etag"],
            },
        )

    assert current.status_code == 200
    assert current.json() == {
        "ok": True,
        "data": {
            "schema_version": "token_radar_snapshot_v4",
            "state": "current",
            "stale_reason": None,
            "state_changed_at_ms": now_ms,
            "social_evidence_as_of_ms": reduced.snapshot["social_evidence_as_of_ms"],
            "eligible_total": 1,
            "items": reduced.snapshot["items"],
        },
    }
    assert current.headers["cache-control"] == "private, no-cache"
    assert current.headers["etag"].startswith('"')
    assert current_not_modified.status_code == 304
    assert not current_not_modified.content
    assert current_not_modified.headers["etag"] == current.headers["etag"]

    assert failure_writes == 1
    assert stale.status_code == 200
    assert stale.json()["data"] == {
        **current.json()["data"],
        "state": "stale",
        "stale_reason": "source_unavailable",
        "state_changed_at_ms": now_ms + 1,
    }
    assert stale.headers["etag"] != current.headers["etag"]
    assert stale_not_modified.status_code == 304
    assert not stale_not_modified.content
    assert stale_not_modified.headers["etag"] == stale.headers["etag"]

    assert recovered_result == {"status": "recovered", "rows_written": 1}
    assert recovered.status_code == 200
    assert recovered.json()["data"] == {
        **current.json()["data"],
        "state_changed_at_ms": now_ms + 2,
    }
    assert recovered.headers["etag"] != stale.headers["etag"]


def test_api_token_radar_local_p95_budgets_at_maximum_public_size(tmp_path):
    now_ms = int(time.time() * 1_000)
    settings = make_settings(tmp_path)
    reduced = reduce_token_radar(
        [
            token_radar_revision(
                target_id=f"asset:perf:{target_index}",
                event_index=event_index,
                now_ms=now_ms,
            )
            for target_index in range(50)
            for event_index in range(3)
        ],
        now_ms=now_ms,
    )
    reduced = enrich_token_radar(
        reduced,
        [
            {
                "target_type": "Asset",
                "target_id": f"asset:perf:{target_index}",
                "symbol": f"P{target_index}",
                "name": f"Performance Token {target_index}",
                "logo_url": f"/api/token-images/{target_index:064x}",
                "chain": "eip155:1",
                "exchange": None,
                "address": f"0x{target_index:040x}",
                "signal_price_usd": None,
                "price_usd": "1.25",
                "price_observed_at_ms": now_ms,
                "market_cap_usd": "1250000",
                "market_cap_observed_at_ms": now_ms,
            }
            for target_index in range(50)
        ],
        now_ms=now_ms,
    )
    assert len(reduced.snapshot["items"]) == 50
    with write_repositories() as repos, repos.transaction():
        TokenRadarCurrentRepository(repos.conn).publish(reduced, evaluation_at_ms=now_ms)

    app = create_app(settings=settings)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        first = client.get("/api/token-radar", headers=headers)
        assert first.status_code == 200
        cached_headers = {**headers, "If-None-Match": first.headers["etag"]}
        for _ in range(20):
            assert client.get("/api/token-radar", headers=headers).status_code == 200
            assert client.get("/api/token-radar", headers=cached_headers).status_code == 304

        success_ms: list[float] = []
        cached_ms: list[float] = []
        for _ in range(200):
            started = time.perf_counter()
            response = client.get("/api/token-radar", headers=headers)
            success_ms.append((time.perf_counter() - started) * 1_000)
            assert response.status_code == 200

            started = time.perf_counter()
            response = client.get("/api/token-radar", headers=cached_headers)
            cached_ms.append((time.perf_counter() - started) * 1_000)
            assert response.status_code == 304

    assert sorted(success_ms)[189] <= 100
    assert sorted(cached_ms)[189] <= 50


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
    assert "current_radar" not in body["data"]
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
        target_type = ""
        target_id = ""
        for index in range(3):
            event = make_token_event(
                f"event-pepe-post-{index}",
                symbol="PEPE",
                address=PEPE,
                handle=f"voice{index}",
                text=f"$PEPE post {index}",
                received_at_ms=base_ms - index * 1_000,
            )
            ingested = ingest_event(event)
            resolution = next(row for row in ingested.token_resolutions if row["resolution_status"] == "EXACT")
            target_type = str(resolution["target_type"])
            target_id = str(resolution["target_id"])

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
        exact_trigger = client.get(
            "/api/target-posts",
            params={
                "target_type": target_type,
                "target_id": target_id,
                "event_id": "event-pepe-post-0",
                "range": "all_history",
                "limit": 1,
            },
            headers={"Authorization": "Bearer secret"},
        )
        incompatible_exact = client.get(
            "/api/target-posts",
            params={
                "target_type": target_type,
                "target_id": target_id,
                "event_id": "event-pepe-post-0",
                "cursor": "cursor",
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
    assert exact_trigger.status_code == 200
    exact_data = exact_trigger.json()["data"]
    assert exact_data["returned_count"] == 1
    assert exact_data["total_count"] == 1
    assert exact_data["has_more"] is False
    assert exact_data["next_cursor"] is None
    assert [item["event_id"] for item in exact_data["items"]] == ["event-pepe-post-0"]
    assert incompatible_exact.status_code == 400
    assert incompatible_exact.json() == {
        "ok": False,
        "error": "incompatible_query_params",
        "field": "cursor",
    }


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
        target_type = ""
        target_id = ""
        for index in range(3):
            event = make_token_event(
                f"event-pepe-timeline-{index}",
                symbol="PEPE",
                address=PEPE,
                handle=f"voice{index}",
                text=f"$PEPE timeline mcap liquidity {index}",
                received_at_ms=base_ms - index * 30_000,
            )
            ingested = ingest_event(event)
            resolution = next(row for row in ingested.token_resolutions if row["resolution_status"] == "EXACT")
            target_type = str(resolution["target_type"])
            target_id = str(resolution["target_id"])

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
    assert response.json() == {
        "ok": False,
        "error": "unsupported_query_param",
        "field": "window",
    }


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
    assert set(data) == {"measured_at_ms", "runtime", "providers"}
    assert data["runtime"]["ok"] is False
    assert data["runtime"]["reasons"] == ["runtime_missing"]
    assert data["runtime"]["workers_runtime"] == {
        "runtime_id": None,
        "runtime_version": None,
        "state": "unavailable",
        "started_at_ms": None,
        "heartbeat_at_ms": None,
        "heartbeat_stale_after_ms": 15_000,
        "fatal_code": None,
        "unavailable_reason": "runtime_missing",
    }
    assert data["providers"]["status"] in {"ok", "degraded"}
    assert "workers" not in data["runtime"]
    assert "worker_lanes" not in data["runtime"]
    assert "collector" not in data["runtime"]
    assert "enrichment" not in data["runtime"]
    assert "notifications" not in data["runtime"]
    assert "snapshot_gate" not in data["runtime"]


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
    assert body["data"]["runtime"]["ok"] is False
    assert body["data"]["runtime"]["reasons"] == [
        "database_unavailable",
        "runtime_status_query_failed",
    ]
    assert body["data"]["runtime"]["db"]["ok"] is False
    assert body["data"]["runtime"]["workers_runtime"]["state"] == "unavailable"
    assert body["data"]["providers"] == {
        "status": "unavailable",
        "reasons": ["database_unavailable"],
        "items": [],
    }
    assert "news" not in body["data"]


def test_api_status_separates_provider_freshness_circuit_and_unowned_backlog_from_readiness(
    tmp_path,
):
    settings = make_settings(tmp_path)
    settings.providers.binance.enabled = False
    with write_repositories() as repos, repos.transaction():
        repos.asset_profile_refresh_targets.enqueue_targets(
            [
                {
                    "provider": "binance_web3_profile",
                    "target_type": "Asset",
                    "target_id": "asset:dex:sol:test-provider-status",
                    "chain_id": "sol",
                    "address": "test-provider-status",
                    "symbol": "TEST",
                    "payload_hash": "sha256:provider-status",
                    "source_watermark_ms": 1,
                    "heat_tier": "hot",
                    "priority": 10,
                }
            ],
            reason="provider_status_test",
            now_ms=1,
            due_at_ms=1,
        )
        repos.provider_circuits.open(
            provider="okx_dex_search",
            error="provider status test",
            now_ms=1,
            retry_ms=60_000,
        )

    app = create_app(settings=settings)
    with TestClient(app) as client:
        readiness = client.get("/readyz")
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})

    assert readiness.status_code == 200
    data = response.json()["data"]
    assert data["providers"]["status"] == "degraded"
    providers = {item["provider"]: item for item in data["providers"]["items"]}
    assert providers["gmgn_direct_ws"]["freshness"] == "no_evidence"
    assert providers["gmgn_direct_ws"]["reasons"] == ["source_stale"]
    assert providers["okx_dex_search"]["circuit_status"] == "open"
    assert providers["okx_dex_search"]["reasons"] == ["circuit_open"]
    assert "last_error" not in providers["okx_dex_search"]
    assert providers["binance_web3_profile"]["owned"] is False
    assert providers["binance_web3_profile"]["has_backlog"] is True
    assert providers["binance_web3_profile"]["reasons"] == ["unowned_backlog"]


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
