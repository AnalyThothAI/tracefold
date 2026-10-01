"""Real PostgreSQL replay, membership and concurrent collector mutation proofs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import RosterMember, TapeCursor
from tracefold.news.market_observations import MarketObservation
from tracefold.news.storage.collectors import ChainTapeState, OpenNewsIncident, OpenNewsState, WalletRosterState

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]
WALLETS = tuple("0x" + str(n) * 40 for n in range(1, 4))


def observation() -> MarketObservation:
    return MarketObservation(
        observation_id="p1-oi",
        kind="oi",
        source_id="opennews",
        source_item_key="p1-oi",
        source_strategy_id="1019",
        provider_metadata={"strategies": [{"id": "1019"}]},
        provider_params={"record": 1},
        title="OI",
        raw_first_line="OI",
        description="OI observation",
        ingest_mode="live",
        parse_status="parsed",
        parse_error=None,
        event_at_ms=1000,
        received_at_ms=1001,
        available_at_ms=1002,
        provider="opennews",
        source_venue="binance",
        raw_instrument="BTC",
        symbol="BTC",
        parser_version="oi_signal_v1",
        source_contract_version="oi_v1",
        oi_event_id="oi-p1",
        measurement_definition="oi/window",
        measurement_window_ms=300000,
        direction="rise",
        oi_change_bps=500,
        oi_value_usd=1000000,
        whale_long_profit_bps=1000,
        whale_oi_ratio_bps=2000,
    )


@pytest.mark.parametrize(
    "metadata", [{}, {"strategies": [{"id": "1019"}]}, {"strategies": [{"id": "1019"}, {"id": "1019"}]}]
)
def test_same_frame_keeps_xmin_facts_and_outbox_identity(metadata):
    with closing(connect_postgres_test()) as conn:
        news = repositories_for_connection(conn).news
        with conn.transaction():
            assert news.insert_market_observation(
                observation().model_copy(update={"provider_metadata": metadata}), now_ms=1002
            )
        before = conn.execute("SELECT xmin::text,updated_at_ms FROM news_market_observations").fetchone()
        with conn.transaction():
            assert not news.insert_market_observation(
                observation().model_copy(update={"provider_metadata": metadata}), now_ms=2000
            )
        assert conn.execute("SELECT xmin::text,updated_at_ms FROM news_market_observations").fetchone() == before
        assert conn.execute("SELECT count(*) AS n FROM news_trade_events").fetchone()["n"] == 1
        with conn.transaction():
            assert not news.insert_market_observation(
                observation().model_copy(
                    update={
                        "provider_metadata": {"strategies": [{"id": "1019"}, {"id": "second"}]},
                        "oi_value_usd": 1,
                    }
                ),
                now_ms=3000,
            )
        row = conn.execute("SELECT provider_metadata,oi_value_usd FROM news_market_observations").fetchone()
        assert len(row["provider_metadata"]["strategies"]) == 2 and row["oi_value_usd"] == 1000000
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_trade_events").fetchone()["n"] == 1
        detail = news.market_item(item_id="p1-oi")
        assert detail["group_key"] == "oi|opennews|binance|BTC|oi/window"
        assert detail["provider_params"] == {"record": 1}
        assert news.market_group_timeline(group_key=detail["group_key"])[0]["item_id"] == "p1-oi"


@pytest.mark.parametrize("rejoin_delay, inherited", [(1799999, True), (1800000, False)])
def test_membership_intervals_reproduce_versions_and_rejoin_coverage(rejoin_delay, inherited):
    with closing(connect_postgres_test()) as conn:
        news = repositories_for_connection(conn).news
        with conn.transaction():
            first = news.chain_tape_store_roster([RosterMember(wallet=w, handle=w) for w in WALLETS[:2]], now_ms=1000)
            news.chain_tape_save_state(
                cursor=TapeCursor(1, -1),
                roster_version=first.roster_version,
                outcome="success",
                error=None,
                now_ms=1000,
                succeeded=True,
            )
            news.chain_tape_record_coverage(
                from_ms=1000, through_ms=2000, through_block=2, through_log=1, gap_at_ms=None, wallets=WALLETS[:2]
            )
            same = news.chain_tape_store_roster(
                [RosterMember(wallet=w, handle="updated") for w in WALLETS[:2]], now_ms=2500
            )
            assert same.roster_version == first.roster_version
            removed = news.chain_tape_store_roster([RosterMember(wallet=WALLETS[0], handle="updated")], now_ms=3000)
            cutoff = 3000 + rejoin_delay
            news.chain_tape_record_coverage(
                from_ms=None, through_ms=cutoff, through_block=3, through_log=1, gap_at_ms=None, wallets=[WALLETS[0]]
            )
            back = news.chain_tape_store_roster(
                [RosterMember(wallet=w, handle="back") for w in WALLETS[:2]], now_ms=cutoff
            )
        assert {m["wallet"] for m in news.chain_tape_members(first.roster_version)} == set(WALLETS[:2])
        assert {m["wallet"] for m in news.chain_tape_members(removed.roster_version)} == {WALLETS[0]}
        joined = next(m for m in news.chain_tape_members(back.roster_version) if m["wallet"] == WALLETS[1])
        assert joined["monitoring_from_ms"] == (1000 if inherited else None)
        assert conn.execute("SELECT count(*) AS n FROM news_market_wallets").fetchone()["n"] == 3


def test_concurrent_incidents_keep_ids_broker_state_and_recovery():
    causes = (
        "network_connect",
        "authentication",
        "provider_close",
        "protocol_error",
        "idle_timeout",
        "broker_backpressure",
        "broker_unavailable",
        "process_outage",
    )

    def open_one(index):
        with closing(connect_postgres_test()) as conn:
            news = repositories_for_connection(conn).news
            return news.open_incident(cause_class=causes[index], now_ms=1000 + index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(open_one, range(8)))
    assert len(set(identities)) == 8
    with closing(connect_postgres_test()) as conn:
        news = repositories_for_connection(conn).news
        assert len(news.open_incidents()) == 8
        news.close_open_incidents(cause_classes=None, now_ms=2000)
        assert len(news.pending_recovery_incidents()) == 8
        news.update_broker_snapshot(snapshot={"connected": True}, now_ms=2100)
        for index in range(25):
            news.open_incident(cause_class="triage_circuit_open", now_ms=3000 + index)
            news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=4000 + index)
        row = conn.execute("SELECT state,incidents FROM news_collectors WHERE collector_id='opennews'").fetchone()
        assert len(row["incidents"]) == 28
        assert row["state"]["next_incident_id"] == max(i["incident_id"] for i in row["incidents"]) + 1
        assert row["state"]["broker_snapshot"]["connected"] is True
        assert news.complete_recovery(
            incident_id=min(identities),
            status="recovered",
            recovered_count=1,
            error_code=None,
            recovery_from_at_ms=1000,
            recovery_to_at_ms=2000,
            now_ms=5000,
        )
        assert len(news.pending_recovery_incidents()) == 7


@pytest.mark.parametrize("failure", ["duplicate_id", "duplicate_open_cause", "invalid_clock", "invalid_state"])
def test_collector_validation_rolls_back_invalid_document(failure):
    with closing(connect_postgres_test()) as conn:
        news = repositories_for_connection(conn).news
        news.open_incident(cause_class="network_connect", now_ms=1000)
        before = conn.execute(
            "SELECT state,incidents,updated_at_ms FROM news_collectors WHERE collector_id='opennews'"
        ).fetchone()
        with (
            pytest.raises(ValidationError),
            news.mutate_collector("opennews", OpenNewsState, now_ms=2000) as (state, incidents),
        ):
            state.broker_snapshot["connected"] = True
            if failure == "invalid_state":
                state.next_incident_id = 0
            elif failure == "invalid_clock":
                incidents.root[0].closed_at_ms = 999
            else:
                incidents.root.append(
                    OpenNewsIncident(
                        incident_id=1 if failure == "duplicate_id" else 2,
                        cause_class="authentication" if failure == "duplicate_id" else "network_connect",
                        opened_at_ms=1500,
                        updated_at_ms=1500,
                    )
                )
        assert (
            conn.execute(
                "SELECT state,incidents,updated_at_ms FROM news_collectors WHERE collector_id='opennews'"
            ).fetchone()
            == before
        )


@pytest.mark.parametrize(
    ("collector_id", "model", "field", "invalid"),
    [
        ("chain_tape", ChainTapeState, "high_water_block", -1),
        ("chain_tape", ChainTapeState, "high_water_tx_index", -2),
        ("chain_tape", ChainTapeState, "noise_through_tx_index", -2),
        ("chain_tape", ChainTapeState, "roster_version", -1),
        ("chain_tape", ChainTapeState, "ignored_inbound_total", -1),
        ("chain_tape", ChainTapeState, "last_outcome", "invalid"),
        ("chain_tape", ChainTapeState, "last_success_at_ms", 0),
        ("chain_tape", ChainTapeState, "next_attempt_at_ms", -1),
        ("wallet_roster", WalletRosterState, "consecutive_failures", -1),
    ],
)
def test_typed_collector_preserves_cursor_and_retry_constraints(collector_id, model, field, invalid):
    with closing(connect_postgres_test()) as conn:
        news = repositories_for_connection(conn).news
        before = conn.execute(
            "SELECT state,updated_at_ms FROM news_collectors WHERE collector_id=%s", (collector_id,)
        ).fetchone()
        with pytest.raises(ValidationError), news.mutate_collector(collector_id, model, now_ms=2000) as (state, _):
            setattr(state, field, invalid)
        assert (
            conn.execute(
                "SELECT state,updated_at_ms FROM news_collectors WHERE collector_id=%s", (collector_id,)
            ).fetchone()
            == before
        )
