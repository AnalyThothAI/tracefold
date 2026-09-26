import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
)
from tests.support.news_judgment import news_taxonomy
from tracefold.app import serve_database as serve_database_module
from tracefold.app.http.app import create_app
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.program.contracts import EditorialEnvelope
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.platform.config.models import NewsSettings, Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

NEWS_V3_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"


def make_settings(tmp_path) -> Settings:
    """Build settings against the session fixture's already migrated schema."""

    settings = Settings(
        ws_token="secret",
        news=NewsSettings(),
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
    app = create_app(settings=make_settings(tmp_path))

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
            event = parse_opennews_message({"method": "strategy.triggered", "params": fresh})
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
        candidate_feed = client.get("/api/news/feed?admission=candidate&limit=100", headers=headers)
        retired_priority = client.get("/api/news/feed?priority=high", headers=headers)
        retired_priority_sort = client.get("/api/news/feed?sort=priority", headers=headers)
        detail = client.get(f"/api/news/events/{event_ids[0]}", headers=headers)
        missing = client.get("/api/news/events/does-not-exist", headers=headers)
        # #553 retired the whole `?oi=` vocabulary with the lane it named. A market observation is not
        # an Event, so the market monitor reads `/api/news/market` and the feed refuses the parameter
        # by name rather than accepting one that narrows nothing.
        retired_oi = client.get("/api/news/feed?oi=all&limit=10", headers=headers)
        retired_market_admission = client.get("/api/news/feed?admission=telemetry_deterministic", headers=headers)
        market = client.get("/api/news/market?limit=10", headers=headers)
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
        "source_authority": None,
        "subject_code": None,
        "final_decision": None,
        "event_kind": None,
        "admission": None,
        "symbol": None,
        "q": None,
        "limit": 10,
        "outcome": None,
        "hours": None,
        "direction": None,
    }

    assert retired_oi.status_code == 400
    assert retired_oi.json()["error"] == "unsupported_query_param"
    assert retired_market_admission.status_code == 400
    assert retired_market_admission.json()["error"] == "news_feed_admission_invalid"
    # The market surface answers over PostgreSQL alone: no Event, no verdict, no Trading read.
    assert market.status_code == 200
    market_data = market.json()["data"]
    assert {source["market_kind"] for source in market_data["sources"]} == {
        "oi",
        "liquidation",
        "smart_money",
        "unknown_market",
        # #572 PR-2: derived by the chain tape rather than reported by a provider, and summarised
        # beside the four the provider sends whether or not it has anything to report.
        "wallet",
    }
    assert {row["outcome"]["kind"] for row in feed_data["events"]} <= {
        "held_recovery",
        "held_gate",
        "expired_triage_handoff",
        "expired_delivery_handoff",
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
    assert all("title_zh" not in event for event in feed_data["events"])
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
    assert retired_priority.status_code == 400
    assert retired_priority.json() == {"ok": False, "error": "unsupported_query_param", "field": "priority"}
    assert retired_priority_sort.status_code == 400
    assert retired_priority_sort.json() == {"ok": False, "error": "unsupported_query_param", "field": "sort"}

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
    assert status_data["ingest"]["recovery"]["reason"] is None
    assert status_data["broker"]["configured"] is False
    assert status_data["broker"]["connected"] is True
    assert status_data["broker"]["queues"]["news.raw"]["consumers"] == 1
    assert status_data["pipeline"]["events_24h"] >= 1
    assert status_data["delivery"]["delivery_available"] is False
    assert "amqp" not in json.dumps(status_data)
    assert [response.status_code for response in retired] == [404, 404, 404, 404]


def test_api_projects_deterministic_event_assets_from_postgres_to_feed_and_detail(tmp_path, monkeypatch):
    """The public asset comes from `news_event_assets`, not the provider/Gate evidence (#287)."""

    settings = make_settings(tmp_path)
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    event_ids = _seed_news_v3_events(now_ms=now_ms)
    assert event_ids
    event_id = event_ids[0]

    with write_repositories() as repos, repos.transaction():
        repos.conn.execute("DELETE FROM news_event_assets WHERE event_id = %s", (event_id,))
        repos.conn.execute(
            "UPDATE news_events SET admission = 'telemetry_deterministic', grounded_assets = '[]'::jsonb"
            " WHERE event_id = %s",
            (event_id,),
        )
        for seeded_event_id in event_ids:
            repos.conn.execute(
                """
                INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
                SELECT 'BTR', e.event_id, 'perp', e.opened_at_ms
                  FROM news_events e WHERE e.event_id = %s
                ON CONFLICT DO NOTHING
                """,
                (seeded_event_id,),
            )
        repos.instruments.apply_snapshot(
            [
                Instrument(
                    venue="binance.perp",
                    venue_symbol="BTRUSDT",
                    base_symbol="BTR",
                    instrument_class="crypto",
                    quote_asset="USDT",
                )
            ],
            now_ms=now_ms,
        )

    statement_count = [0]
    production_repositories_for_connection = serve_database_module.repositories_for_connection

    class CountingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            statement_count[0] += 1
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def counted_repositories(conn):
        return production_repositories_for_connection(CountingConnection(conn))

    monkeypatch.setattr(serve_database_module, "repositories_for_connection", counted_repositories)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        statement_count[0] = 0
        one_event = client.get("/api/news/feed?limit=1", headers=headers)
        one_event_statement_count = statement_count[0]
        statement_count[0] = 0
        many_events = client.get("/api/news/feed?limit=100", headers=headers)
        many_events_statement_count = statement_count[0]
        feed = client.get("/api/news/feed?symbol=BTR&limit=100", headers=headers)
        detail = client.get(f"/api/news/events/{event_id}", headers=headers)

    assert one_event.status_code == many_events.status_code == feed.status_code == detail.status_code == 200
    assert len(one_event.json()["data"]["events"]) == 1
    assert len(many_events.json()["data"]["events"]) > 1
    assert many_events_statement_count == one_event_statement_count
    feed_event = next(event for event in feed.json()["data"]["events"] if event["event_id"] == event_id)
    detail_event = detail.json()["data"]["event"]
    expected = [{"symbol": "BTR", "base_symbol": "BTR", "venue": "binance.perp", "listed": True}]
    assert feed_event["grounded_assets"] == detail_event["grounded_assets"] == []
    assert feed_event["assets"] == detail_event["assets"] == expected


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


def test_api_retired_market_routes_and_websocket_are_absent(tmp_path):
    """The GMGN lane (Search, Token Case, recent events, live market, WS live journal) is gone (#50)."""

    app = create_app(settings=make_settings(tmp_path))

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


def _historical_v2_editorial(*, source_authority: str) -> dict:
    """One `news_editorial_v2` document, exactly as the worker wrote it before #651 §5.3.

    Written by hand because the Python contract can no longer produce it: `EditorialEnvelope` is v4, it
    carries no `relevance` block at all, and `NewsTaxonomyV1` has no `source_authority` field. That is
    the point — these rows are audit truth that is never rewritten, and the feed's source-authority
    filter has to keep answering over them.
    """

    payload = {
        "editorial_contract_version": "news_editorial_v2",
        "editorial_origin": "model",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
        "taxonomy": news_taxonomy(event_family="market_access", change_state="effective").model_dump(mode="json")
        | {"source_authority": source_authority},
    }
    return payload | {"editorial_sha256": canonical_sha(payload)}


def _verdict_payload(*, judgment_contract_version: str) -> dict:
    """The verdict document the named judgment contract binds its rows to (#675 §1).

    `news_judgment_v3` is the shape the Python contract produces; the v2 shape is hand-written for the
    same reason its editorial is, because `TriageVerdict` drops `magnitude`/`audience` on the way in.
    """

    common = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "crypto", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "confidence": 0.8,
        "headline_zh": "比特币获得新的市场准入",
        "why_zh": "新增入口扩大可交易范围。",
    }
    if judgment_contract_version == "news_judgment_v3":
        return TriageVerdict(**common, fact_kind="state_change", evidence_ref="c1").model_dump(mode="json")
    return common | {"magnitude": 2, "audience": "crypto"}


def _write_model_verdict(
    *,
    event_id: str,
    editorial: dict,
    policy_version: str,
    judgment_contract_version: str,
    override_rule: str,
    now_ms: int,
) -> None:
    verdict = _verdict_payload(judgment_contract_version=judgment_contract_version)
    verdict_sha = canonical_sha(verdict)
    judgment_sha = canonical_sha(
        {
            "judgment_contract_version": judgment_contract_version,
            "verdict": verdict,
            "editorial": editorial,
            "verdict_sha256": verdict_sha,
        }
    )
    runtime_manifest_sha = "b" * 64
    program_sha256 = "a" * 64
    with write_repositories() as repos:
        evidence = repos.news.latest_evidence_snapshot(event_id)
        assert evidence is not None
        trace = {
            "judgment_contract_version": judgment_contract_version,
            "judgment_origin": "model",
            "judgment_sha256": judgment_sha,
            "verdict_sha256": verdict_sha,
            "editorial_sha256": editorial["editorial_sha256"],
            "runtime_manifest_sha": runtime_manifest_sha,
            "program_version": PROGRAM_VERSION,
            "program_sha256": program_sha256,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        }
        with repos.transaction():
            assert repos.news.insert_verdict(
                event_id=event_id,
                stage="triage",
                policy_version=policy_version,
                judgment_contract_version=judgment_contract_version,
                judgment_origin="model",
                rule_baseline_decision="push",
                final_decision="push",
                override_rule=override_rule,
                throttled_by=None,
                verdict=verdict,
                model_editorial=editorial,
                judgment_sha256=judgment_sha,
                runtime_manifest_sha=runtime_manifest_sha,
                model="test-model",
                program_version=PROGRAM_VERSION,
                program_sha256=program_sha256,
                degraded=False,
                error_code=None,
                trace=trace,
                evidence_version=int(evidence["evidence_version"]),
                evidence_sha256=str(evidence["evidence_sha256"]),
                focus_fact_id=str(evidence["focus_fact_id"]),
                now_ms=now_ms,
            )


def test_api_serves_an_unavailable_taxonomy_and_filters_history_by_source_authority(tmp_path):
    """#651 §5.3, end to end over real PostgreSQL.

    Three facts in one pass, because they are one fact: a judgment whose taxonomy Predictor failed alone
    is persistable under the new CHECK, readable through the public Event detail with its classification
    absent and its code-owned authority intact, and indistinguishable to the feed's source-authority
    filter from a `news_editorial_v2` row written before the cut.
    """

    settings = make_settings(tmp_path)
    app = create_app(settings=settings)
    now_ms = int(time.time() * 1000)
    event_ids = _seed_news_v3_events(now_ms=now_ms)
    assert len(event_ids) >= 2
    unavailable_event, historical_event = event_ids[0], event_ids[1]

    unavailable = EditorialEnvelope.issue(
        source_authority="issuer_first_party",
        taxonomy=None,
        taxonomy_error_code="news_program_output_truncated",
    ).model_dump(mode="json")
    _write_model_verdict(
        event_id=unavailable_event,
        editorial=unavailable,
        policy_version=TRIAGE_POLICY_VERSION,
        judgment_contract_version="news_judgment_v3",
        override_rule="fact_kind_state_change",
        now_ms=now_ms,
    )
    _write_model_verdict(
        event_id=historical_event,
        editorial=_historical_v2_editorial(source_authority="reputable_secondary"),
        # The policy the pre-cut worker wrote under: a historical row, not a re-issued one.
        policy_version="news_triage_policy_v13",
        judgment_contract_version="news_judgment_v2",
        override_rule="trade_relevance_realtime",
        now_ms=now_ms,
    )

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        detail = client.get(f"/api/news/events/{unavailable_event}", headers=headers)
        historical_detail = client.get(f"/api/news/events/{historical_event}", headers=headers)
        issuer_feed = client.get("/api/news/feed?source_authority=issuer_first_party&limit=100", headers=headers)
        secondary_feed = client.get("/api/news/feed?source_authority=reputable_secondary&limit=100", headers=headers)

    assert detail.status_code == 200
    # #706: the Triage verdict is history, served under its legacy name and without the retired axes.
    triage = detail.json()["data"]["legacy_verdict"]
    assert {"taxonomy", "taxonomy_status", "taxonomy_error_code"}.isdisjoint(triage)
    assert detail.json()["data"]["event_update"] is None
    assert triage["source_authority"] == "issuer_first_party"
    assert triage["source_authority_zh"]
    assert triage["headline_zh"] == "比特币获得新的市场准入"
    verdict_row = next(row for row in detail.json()["data"]["verdicts"] if row["stage"] == "triage")
    assert verdict_row["policy_version"] == TRIAGE_POLICY_VERSION == "news_triage_policy_v17"
    assert verdict_row["model_editorial"]["taxonomy"] is None
    assert verdict_row["model_editorial"]["taxonomy_status"] == "unavailable"
    assert verdict_row["model_editorial"]["source_authority"] == "issuer_first_party"

    # The v2 row reads as v3 above the storage boundary: the authority is lifted out of the taxonomy,
    # which is present and therefore `available`.
    assert historical_detail.status_code == 200
    historical_triage = historical_detail.json()["data"]["legacy_verdict"]
    assert historical_triage["source_authority"] == "reputable_secondary"
    historical_row = next(row for row in historical_detail.json()["data"]["verdicts"] if row["stage"] == "triage")
    assert historical_row["model_editorial"]["taxonomy_status"] == "available"
    # The stored axis as stored: no vocabulary, no codebook claim, and the authority lifted out.
    assert historical_row["model_editorial"]["taxonomy"]["event_family"] == "market_access"
    assert "source_authority" not in historical_row["model_editorial"]["taxonomy"]

    assert issuer_feed.status_code == 200 and secondary_feed.status_code == 200
    issuer_ids = {event["event_id"] for event in issuer_feed.json()["data"]["events"]}
    secondary_ids = {event["event_id"] for event in secondary_feed.json()["data"]["events"]}
    assert unavailable_event in issuer_ids and historical_event not in issuer_ids
    assert historical_event in secondary_ids and unavailable_event not in secondary_ids
