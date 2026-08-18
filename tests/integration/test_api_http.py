import json
import math
import time
from contextlib import contextmanager
from decimal import Decimal
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
from tracefold.news.events import admit_item
from tracefold.news.opennews import parse_opennews_message
from tracefold.platform.config.settings import NewsSettings, Settings

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


_SCHEMA_STATE = {"prepared": False}


def make_settings(tmp_path, *, reset: bool = True) -> Settings:
    """Reset the schema unless the test only exercises validation/auth paths (reset=False reuses head)."""

    if reset or not _SCHEMA_STATE["prepared"]:
        prepare_postgres_database()
        _SCHEMA_STATE["prepared"] = True
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


def test_api_bootstrap_exposes_frontend_runtime_config_without_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"ws_token": "secret"}


def test_api_rejects_protected_reads_without_token(tmp_path):
    app = create_app(settings=make_settings(tmp_path, reset=False))

    with TestClient(app) as client:
        response = client.get("/api/news/feed")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}


def _seed_news_v3_events(*, now_ms: int) -> list[str]:
    hits = json.loads(NEWS_V3_FIXTURE.read_text(encoding="utf-8"))
    event_ids: list[str] = []
    with write_repositories() as repos, repos.transaction():
        for offset, hit in enumerate(sorted(hits, key=lambda h: str(h.get("ts") or ""))[:40]):
            # pin the fixture into the live 24 h window (the corpus ages; the status counters are windowed)
            fresh = {**hit, "ts": (now_ms - 3600_000 + offset * 1000) / 1000}
            event = parse_opennews_message(
                {"method": "strategy.triggered", "params": fresh}, strategy_ids=frozenset({"1018", "1352", "1353"})
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
    with write_repositories() as repos, repos.transaction():
        repos.news.update_broker_snapshot(
            snapshot={"connected": True, "queues": {"news.raw": {"messages": 0, "consumers": 1}}, "error_code": None},
            now_ms=now_ms,
        )
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
        "outcome": None,
        "hours": None,
    }
    assert {row["outcome"]["kind"] for row in feed_data["events"]} <= {
        "held_recovery",
        "held_gate",
        "queued_publish",
        "queued_triage",
        "dropped",
        "throttled",
        "degraded_dropped",
        "pending_delivery",
        "delivered",
        "delivery_failed",
    }
    assert 0 < len(feed_data["events"]) <= 10
    assert all("title_zh" in event for event in feed_data["events"])
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
    assert status_data["broker"]["connected"] is True
    assert status_data["broker"]["queues"]["news.raw"]["consumers"] == 1
    assert status_data["pipeline"]["events_24h"] >= 1
    assert status_data["delivery"]["delivery_available"] is False
    assert status_data["control"] == {"paused": False, "mutes": []}
    assert "amqp" not in json.dumps(status_data)
    assert [response.status_code for response in retired] == [404, 404, 404, 404]


def test_api_notification_routes_are_not_registered(tmp_path):
    app = create_app(settings=make_settings(tmp_path, reset=False))

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
    app = create_app(settings=make_settings(tmp_path, reset=False))

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


def test_api_retired_market_routes_and_websocket_are_absent(tmp_path):
    """The GMGN lane (Search, Token Case, recent events, live market, WS live journal) is gone (#50)."""

    app = create_app(settings=make_settings(tmp_path, reset=False))

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        responses = [
            client.get("/api/recent", headers=headers),
            client.get("/api/events/by-ids?ids=x", headers=headers),
            client.get("/api/search?q=btc", headers=headers),
            client.get("/api/search/inspect?q=btc", headers=headers),
            client.get("/api/token-case?target_type=chain_token&target_id=x", headers=headers),
            client.get("/api/target-posts?target_type=chain_token&target_id=x", headers=headers),
            client.get("/api/live-market?target_type=Asset&target_id=x", headers=headers),
            client.get("/api/token-images/" + "0" * 64, headers=headers),
        ]
        websocket_missing = False
        try:
            with client.websocket_connect("/ws"):
                pass
        except Exception:
            websocket_missing = True

    assert [response.status_code for response in responses] == [404] * len(responses)
    assert websocket_missing
