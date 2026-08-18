import json
import math
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient

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
)
from tracefold.news.events import admit_item
from tracefold.news.opennews import parse_opennews_message
from tracefold.platform.config.settings import NewsSettings, Settings

PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
NEWS_V3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
OPENNEWS_STRATEGY_IDS = frozenset({"1018", "1019"})


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
        news=NewsSettings(opennews_strategy_ids=("1018", "1019")),
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


def _seed_news_v3_events(*, now_ms: int) -> list[str]:
    hits = json.loads(NEWS_V3_FIXTURE.read_text(encoding="utf-8"))
    event_ids: list[str] = []
    with write_repositories() as repos, repos.transaction():
        for hit in sorted(hits, key=lambda h: str(h.get("ts") or ""))[:40]:
            event = parse_opennews_message(
                {"method": "strategy.triggered", "params": hit}, strategy_ids=frozenset({"1018", "1352", "1353"})
            )
            if event is None:
                continue
            result = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=now_ms,
                trace_id="api-test",
                watchlist_symbols=frozenset({"BTC"}),
                now_ms=now_ms,
            )
            if result.event_created:
                event_ids.append(result.event_id)
    return event_ids


def test_api_news_v3_exposes_feed_event_detail_and_status(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    event_ids = _seed_news_v3_events(now_ms=now_ms)
    assert event_ids

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        feed = client.get("/api/news/feed?limit=10", headers=headers)
        candidate_feed = client.get("/api/news/feed?admission=candidate&sort=priority&limit=100", headers=headers)
        detail = client.get(f"/api/news/events/{event_ids[0]}", headers=headers)
        missing = client.get("/api/news/events/does-not-exist", headers=headers)
        bad_admission = client.get("/api/news/feed?admission=bogus", headers=headers)
        bad_param = client.get("/api/news/feed?story_id=1", headers=headers)
        status = client.get("/api/news/status", headers=headers)
        retired = [
            client.get("/api/news/brief", headers=headers),
            client.get("/api/news/sources", headers=headers),
            client.get(f"/api/news/stories/{'1' * 64}", headers=headers),
            client.get("/api/radar", headers=headers),
        ]

    assert feed.status_code == 200
    feed_data = feed.json()["data"]
    assert feed_data["filters"] == {
        "family": None,
        "admission": None,
        "priority": None,
        "decision": None,
        "symbol": None,
        "q": None,
        "sort": "latest",
        "limit": 10,
    }
    assert 0 < len(feed_data["events"]) <= 10
    assert all(event["display_title"] for event in feed_data["events"])
    assert {event["event_id"] for event in feed_data["events"]} <= set(event_ids)
    if len(event_ids) > 10:
        assert feed_data["next_cursor"]
        with TestClient(app) as client:
            page_two = client.get(
                f"/api/news/feed?limit=10&cursor={feed_data['next_cursor']}", headers={"Authorization": "Bearer secret"}
            )
        assert page_two.status_code == 200
        assert {e["event_id"] for e in page_two.json()["data"]["events"]}.isdisjoint(
            {e["event_id"] for e in feed_data["events"]}
        )

    assert candidate_feed.status_code == 200
    assert all(event["admission"] == "candidate" for event in candidate_feed.json()["data"]["events"])

    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["event"]["event_id"] == event_ids[0]
    assert detail_data["members"] and detail_data["verdicts"] == [] and detail_data["deliveries"] == []
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "news_event_not_found"}
    assert bad_admission.status_code == 400
    assert bad_admission.json() == {"ok": False, "error": "news_feed_admission_invalid", "field": "admission"}
    assert bad_param.status_code == 400
    assert bad_param.json()["error"] == "unsupported_query_param"

    assert status.status_code == 200
    status_data = status.json()["data"]
    assert status_data["state"] == "unavailable"
    assert status_data["ingest"]["token_configured"] is False
    assert status_data["broker"]["configured"] is False
    assert status_data["pipeline"]["events_24h"] >= 1
    assert status_data["delivery"]["delivery_available"] is False
    assert status_data["control"] == {"paused": False, "mutes": []}
    assert "amqp" not in json.dumps(status_data)
    assert [response.status_code for response in retired] == [404, 404, 404, 404]


def test_api_exposes_recent_search_and_token_read_models(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        now_ms = int(time.time() * 1000)
        event = make_token_event(
            "event-1",
            symbol="PEPE",
            address=PEPE,
            text=f"$PEPE ignition {PEPE}",
            received_at_ms=now_ms - 1_000,
        )
        ingest_event(event)

        headers = {"Authorization": "Bearer secret"}
        recent = client.get("/api/recent?limit=5", headers=headers)
        search = client.get("/api/search", params={"q": "$PEPE", "limit": 5, "window": "24h"}, headers=headers)
        search_inspect = client.get(
            "/api/search/inspect",
            params={"q": "$PEPE", "limit": 5, "window": "24h"},
            headers=headers,
        )
        radar = client.get("/api/token-radar", headers=headers)
        stocks_radar = client.get("/api/stocks-radar", headers=headers)
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

    assert radar.status_code == 404
    assert stocks_radar.status_code == 404
    assert account_alerts.status_code == 404


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
