from __future__ import annotations

from tests.factories_token_radar import (
    VERSA_BASE_CA,
    insert_base_versa_asset,
    make_gmgn_payload_event,
    make_token_event,
    open_token_radar_runtime,
)
from tracefold.market import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    TokenRadarProjector,
    TokenRadarPublisher,
)


def test_versa_symbol_and_ca_build_one_intent(tmp_path):
    conn, repos, ingest = open_token_radar_runtime(tmp_path)
    insert_base_versa_asset(conn, observed_at_ms=1_777_799_000_000)
    event = make_token_event(
        text=f"很不错的一个项目，挺有格局的dev， $VERSA {VERSA_BASE_CA}",
        received_at_ms=1_777_800_000_000,
    )

    result = ingest.ingest_event(event)

    intents = repos.token_intents.intents_for_event("event-versa")
    resolutions = repos.intent_resolutions.resolutions_for_event("event-versa")
    assert len(intents) == 1
    assert intents[0]["display_symbol"] == "VERSA"
    assert intents[0]["address_hint"].lower() == VERSA_BASE_CA
    assert len(resolutions) == 1
    assert resolutions[0]["resolution_status"] == "EXACT"
    assert resolutions[0]["target_type"] == "Asset"
    assert resolutions[0]["target_id"] == f"asset:eip155:8453:erc20:{VERSA_BASE_CA}"
    assert result.token_intents[0]["intent_id"] == intents[0]["intent_id"]
    assert result.token_resolutions[0]["resolution_id"] == resolutions[0]["resolution_id"]


def test_unresolved_attention_never_projects_as_driver(tmp_path):
    _, repos, ingest = open_token_radar_runtime(tmp_path)
    results = [
        ingest.ingest_event(
            make_token_event(
                event_id=f"event-hanta-{index}",
                text="$HANTA new burst",
                received_at_ms=1_777_800_000_000 + index,
                author_handle=f"voice{index}",
            ),
        )
        for index in range(7)
    ]

    _publisher(repos).publish_rank_set(
        window="5m",
        now_ms=1_777_800_060_000,
        limit=20,
    )
    rows = repos.token_radar.latest_current_rows(
        window="5m",
        venue=TOKEN_RADAR_DEFAULT_VENUE,
        limit=20,
        projection_version=TOKEN_RADAR_PROJECTION_VERSION,
    )

    assert all(
        resolution["resolution_status"] != "EXACT" for result in results for resolution in result.token_resolutions
    )
    assert rows == []


def test_address_like_payload_symbol_does_not_mask_missing_real_symbol(tmp_path):
    _, repos, ingest = open_token_radar_runtime(tmp_path)
    address = "3iqrRNGG111111111111111111111111111111wNpump"
    event = make_gmgn_payload_event(
        symbol=address,
        chain="sol",
        address=address,
        received_at_ms=1_777_800_000_000,
    )

    result = ingest.ingest_event(event)
    _rebuild_resolved_current_rows(repos, now_ms=1_777_800_060_000)
    rows = repos.token_radar.latest_current_rows(
        window="5m",
        venue=TOKEN_RADAR_DEFAULT_VENUE,
        limit=20,
        projection_version=TOKEN_RADAR_PROJECTION_VERSION,
    )

    assert result.token_resolutions[0]["resolution_status"] == "EXACT"
    assert rows[0]["target_type"] == "Asset"
    assert rows[0]["factor_snapshot_json"]["subject"]["symbol"] is None
    assert rows[0]["factor_snapshot_json"]["subject"]["address"] == address


def test_gmgn_payload_identity_does_not_project_market_snapshot_into_radar(tmp_path):
    _, repos, ingest = open_token_radar_runtime(tmp_path)
    event = make_gmgn_payload_event(
        symbol="PEPE",
        chain="eth",
        address="0x6982508145454ce325ddbe47a25d4ec3d2311933",
        received_at_ms=1_777_800_000_000,
    )

    result = ingest.ingest_event(event)
    _rebuild_resolved_current_rows(repos, now_ms=1_777_800_060_000)
    rows = repos.token_radar.latest_current_rows(
        window="5m",
        venue=TOKEN_RADAR_DEFAULT_VENUE,
        limit=20,
        projection_version=TOKEN_RADAR_PROJECTION_VERSION,
    )

    assert result.token_resolutions[0]["resolution_status"] == "EXACT"
    assert rows[0]["factor_snapshot_json"]["subject"]["symbol"] == "PEPE"
    enriched_events = repos.enriched_events.list_by_event_id(event.event_id)
    assert enriched_events
    assert {item["capture_method"] for item in enriched_events} == {"unavailable"}
    assert all(item["tick_id"] is None for item in enriched_events)
    factor_snapshot = rows[0]["factor_snapshot_json"]
    assert factor_snapshot["market"]["event_anchor"] is None
    assert factor_snapshot["market"]["decision_latest"] is None
    assert factor_snapshot["market"]["readiness"]["anchor_status"] == "missing"
    assert factor_snapshot["market"]["readiness"]["latest_status"] == "missing"
    assert factor_snapshot["data_health"]["market"] == "missing"
    assert "market_metadata_missing" in factor_snapshot["gates"]["risk_reasons"]


def _rebuild_resolved_current_rows(repos, *, now_ms: int) -> None:
    with repos.transaction():
        repos.token_radar_dirty_targets.enqueue_recent_resolved_targets(
            since_ms=now_ms - 5 * 60 * 1000,
            now_ms=now_ms,
            limit=20,
            reason="golden_corpus_projection",
        )
    _publisher(repos).rebuild_dirty_targets(
        lease_ms=120_000,
        retry_ms=30_000,
        max_attempts=3,
        windows=("5m",),
        now_ms=now_ms,
        limit=20,
        rank_limit=20,
        lease_owner="golden_corpus_projection",
    )


def _publisher(repos) -> TokenRadarPublisher:
    return TokenRadarPublisher(repos=repos, projector=TokenRadarProjector(repos=repos))
