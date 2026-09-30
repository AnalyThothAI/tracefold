"""Real intake receipts distinguish relay backlog without guessing from source age."""

import asyncio
from types import SimpleNamespace

import pytest

from tests.integration.test_trading_analysis_closure import _selection
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app import trading_analysis
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.root import TradingRepository

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("recorded_offset", "expected", "publication_reason"),
    [(-1, "backlog", "source_stale"), (0, "prospective", None), (1001, "unknown", "input_incomplete")],
)
def test_relay_records_public_outbox_boundary_once_and_blocks_backlog(
    tmp_path, postgres_clone_dsn, monkeypatch, recorded_offset, expected, publication_reason
):
    started = 1_800_000_000_000
    now = started + 1000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: started)
    runner = trading_analysis.AnalysisRunner(
        settings=Settings(), market_data=object(), assessor=None, program_sha="e" * 64, raw_root=tmp_path
    )
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: now)
    acknowledgements = []
    event = {
        "event_id": 1,
        "kind": "oi",
        "source_fact_key": "receipt-source",
        "source_revision": "v1",
        "source_recorded_at_ms": started + recorded_offset,
        "payload_sha256": "a" * 64,
        "payload": {
            "kind": "oi",
            "provider_event_at_ms": now,
            "oi_change_bps": 350,
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
        },
    }

    # Only the News-owned public projection is available to this App mapper.
    class PublicNews:
        def unacknowledged_trade_events(self, *, limit):
            return [event]

        def acknowledge_trade_event(self, **kwargs):
            acknowledgements.append(kwargs)

    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    trading = TradingRepository(conn)
    repos = SimpleNamespace(news=PublicNews(), trading=trading)

    async def db_call(fn, *, transaction=False):
        if transaction:
            with conn.transaction():
                return fn(repos)
        return fn(repos)

    async def selected(*args, **kwargs):
        return _selection()

    monkeypatch.setattr(runner, "_db_async", db_call)
    monkeypatch.setattr(trading_analysis, "_select_from_connection", selected)
    try:
        assert asyncio.run(runner.relay_once()) == 1
        assert len(acknowledgements) == 1
        [row] = conn.execute("SELECT case_id,intake_context FROM trading_cases").fetchall()
        context = row["intake_context"]
        assert context == {
            "contract": "relay_capture_v1",
            "relay_started_at_ms": started,
            "source_recorded_at_ms": started + recorded_offset,
            "accepted_at_ms": now,
            "cohort": expected,
        }
        # Fresh event age alone would classify all three as current, including backlog.
        assert (
            trading.publication_admission_status(
                case_id=row["case_id"], account_slot="demo-primary", now_ms=now, max_source_age_ms=600000
            )
            == publication_reason
        )
        with conn.transaction():
            assert (
                trading.accept_trigger(
                    kind="oi",
                    source_fact_key=event["source_fact_key"],
                    source_revision="v1",
                    payload_sha256="a" * 64,
                    payload=event["payload"],
                    selection=_selection(),
                    now_ms=now + 2000,
                    root_ttl_ms=600000,
                    relay_started_at_ms=now + 2000,
                    source_recorded_at_ms=now + 2000,
                )[2]
                == "duplicate"
            )
        assert trading.analysis_case(row["case_id"])["intake_context"] == context
    finally:
        runner._db_pool.shutdown(wait=True)
        conn.close()
