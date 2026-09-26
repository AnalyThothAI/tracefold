"""PostgreSQL proof of the News -> App -> Trading public update relay (#706 §3.3).

A source update is a Trading amendment, dispatched before target selection: it creates no
trigger or Case and moves no freshness clock. A catalyst delta enters the existing trigger
path with freshness from its first availability. Redelivery after a crash before the News
acknowledgement is idempotent.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from tests.integration.test_trading_analysis_runner import _Analyst, _Market
from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn, reset_postgres_schema
from tests.trading.news_public_updates import first_report, next_update, outbox_row
from tracefold.app import trading_analysis
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.config.models import PostgresConfig, Settings
from tracefold.trading.engine.target import SourceAsset
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]

_TTL_MS = 600_000


def _runner(tmp_path, analyst: Any = None) -> AnalysisRunner:
    settings = Settings()
    settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(), password_file=None)
    assert settings.trading.analysis.root_ttl_seconds * 1000 == _TTL_MS
    return AnalysisRunner(settings=settings, market_data=_Market(), analyst=analyst, files_root=tmp_path / "archive")


def _run(runner: AnalysisRunner, *steps: str) -> list[Any]:
    async def run() -> list[Any]:
        try:
            return [await (runner.relay_once() if step == "relay" else runner.analyze_one()) for step in steps]
        finally:
            runner._db_executor.shutdown(wait=True)

    return asyncio.run(run())


def _record_selections(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[SourceAsset, ...]]]:
    calls: list[tuple[str, tuple[SourceAsset, ...]]] = []
    select = trading_analysis.select_target

    def recording(*, kind: Any, assets: tuple[SourceAsset, ...], **values: Any) -> Any:
        calls.append((kind, assets))
        return select(kind=kind, assets=assets, **values)

    monkeypatch.setattr(trading_analysis, "select_target", recording)
    return calls


def _counts(conn: Any) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in ("trading_triggers", "trading_cases", "trading_source_amendments", "trading_trigger_conflicts")
    }


def _correction(head, now_ms: int):
    return next_update(
        head,
        "Correction: the SOL swap fee was set to 20 bps, not 25 bps.",
        previous_ref=head.claims[0].ref,
        relation="corrects",
        change_kind="correction",
        quantity="20",
        revision=2,
        first_available_at_ms=now_ms - 20_000,
        completed_at_ms=now_ms - 10_000,
    )


def test_source_update_is_an_amendment_dispatched_before_target_selection(tmp_path, monkeypatch) -> None:
    conn = connect_postgres_test(tmp_path / "public-relay-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        news = NewsRepository(conn)
        trading = TradingRepository(conn)
        now_ms = int(time.time() * 1000)
        head, catalyst = first_report(
            event_id="event-sol", first_available_at_ms=now_ms - 60_000, completed_at_ms=now_ms - 30_000
        )
        _, correction = _correction(head, now_ms)
        assert correction.kind == "source_update" and correction.retired_claim_refs == catalyst.claim_refs
        with conn.transaction():
            assert news.enqueue_trade_event(**outbox_row(catalyst))
            assert news.enqueue_trade_event(**outbox_row(correction))
        selections = _record_selections(monkeypatch)

        assert _run(_runner(tmp_path), "relay") == [2]
        assert selections == [("catalyst", (SourceAsset("SOL", "crypto", "primary"),))]
        assert _counts(conn) == {
            "trading_triggers": 1,
            "trading_cases": 1,
            "trading_source_amendments": 1,
            "trading_trigger_conflicts": 0,
        }
        trigger = conn.execute(
            "SELECT trigger_id,kind,source_fact_key,source_revision,source_observed_at_ms,root_expires_at_ms "
            "FROM trading_triggers"
        ).fetchone()
        assert trigger["kind"] == "catalyst" and trigger["source_fact_key"] == "event-sol"
        assert trigger["source_revision"] == catalyst.content_revision
        # Freshness is first availability, not semantic completion or relay time.
        assert trigger["source_observed_at_ms"] == now_ms - 60_000
        assert trigger["root_expires_at_ms"] == now_ms - 60_000 + _TTL_MS
        case = conn.execute(
            "SELECT case_id,state,root_expires_at_ms,trigger_persisted_at_ms FROM trading_cases"
        ).fetchone()
        assert case["state"] == "PENDING" and case["root_expires_at_ms"] == now_ms - 60_000 + _TTL_MS
        assert case["trigger_persisted_at_ms"] == now_ms - 30_000
        amendment = conn.execute(
            "SELECT update_id,source_fact_key,content_revision,affected_claim_refs,retired_claim_refs,"
            "payload,payload_sha256 FROM trading_source_amendments"
        ).fetchone()
        outbox = conn.execute(
            "SELECT kind,payload_sha256,acknowledged_at_ms,rejected_reason FROM news_trade_events ORDER BY event_id"
        ).fetchall()
        assert amendment == {
            "update_id": correction.update_id,
            "source_fact_key": "event-sol",
            "content_revision": correction.content_revision,
            "affected_claim_refs": list(catalyst.claim_refs),
            "retired_claim_refs": list(catalyst.claim_refs),
            "payload": correction.model_dump(mode="json"),
            "payload_sha256": outbox[1]["payload_sha256"],
        }
        assert [(row["kind"], row["rejected_reason"]) for row in outbox] == [
            ("catalyst", None),
            ("source_update", None),
        ]
        assert all(row["acknowledged_at_ms"] is not None for row in outbox)
        visible = trading.source_amendments(trigger_id=trigger["trigger_id"], known_at_ms=int(time.time() * 1000))
        assert [item["update_id"] for item in visible] == [correction.update_id]
        assert trading.source_amendments(trigger_id=trigger["trigger_id"], known_at_ms=now_ms - 1) == []

        # Trading committed both receipts, then the process died before News recorded the acks.
        with conn.transaction():
            conn.execute("UPDATE news_trade_events SET acknowledged_at_ms=NULL")
        before = conn.execute("SELECT * FROM trading_cases").fetchall()
        assert _run(_runner(tmp_path), "relay") == [2]
        assert _counts(conn) == {
            "trading_triggers": 1,
            "trading_cases": 1,
            "trading_source_amendments": 1,
            "trading_trigger_conflicts": 0,
        }
        assert conn.execute("SELECT * FROM trading_cases").fetchall() == before
        assert [kind for kind, _ in selections] == ["catalyst", "catalyst"]
        assert all(
            row["acknowledged_at_ms"] is not None
            for row in conn.execute("SELECT acknowledged_at_ms FROM news_trade_events").fetchall()
        )
    finally:
        conn.close()


def test_retired_headline_why_catalyst_is_rejected_by_name(tmp_path, monkeypatch) -> None:
    conn = connect_postgres_test(tmp_path / "legacy-catalyst-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        news = NewsRepository(conn)
        now_ms = int(time.time() * 1000)
        with conn.transaction():
            news.enqueue_trade_event(
                kind="catalyst",
                source_fact_key="event-old",
                source_revision="1:" + "e" * 64,
                payload={
                    "kind": "catalyst",
                    "headline": "Old headline",
                    "why": "Old why",
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                    "source_recorded_at_ms": now_ms,
                },
                source_recorded_at_ms=now_ms,
            )
            news.enqueue_trade_event(
                kind="oi",
                source_fact_key="after-legacy",
                source_revision="v1",
                payload={
                    "kind": "oi",
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                    "source_recorded_at_ms": now_ms,
                    "provider_event_at_ms": now_ms,
                },
                source_recorded_at_ms=now_ms,
            )
        selections = _record_selections(monkeypatch)
        assert _run(_runner(tmp_path), "relay") == [2]
        rows = conn.execute(
            "SELECT kind,acknowledged_at_ms,rejected_reason FROM news_trade_events ORDER BY event_id"
        ).fetchall()
        assert rows[0]["rejected_reason"] == "legacy_catalyst_payload" and rows[0]["acknowledged_at_ms"] is None
        assert rows[1]["rejected_reason"] is None and rows[1]["acknowledged_at_ms"] is not None
        assert [kind for kind, _ in selections] == ["oi"]
        assert conn.execute("SELECT kind FROM trading_triggers").fetchall() == [{"kind": "oi"}]
    finally:
        conn.close()


def test_catalyst_freshness_is_first_availability_not_semantic_completion(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "catalyst-ttl-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        trading = TradingRepository(conn)
        now_ms = 10_000_000
        # A model rerun or a new member completed the semantics just now; the information is old.
        _, stale = first_report(
            event_id="event-stale", first_available_at_ms=now_ms - _TTL_MS - 1, completed_at_ms=now_ms - 1_000
        )
        _, fresh = first_report(event_id="event-fresh", first_available_at_ms=now_ms - 5_000, completed_at_ms=now_ms)
        with conn.transaction():
            accepted = [
                trading.accept_trigger(
                    kind="catalyst",
                    source_fact_key=update.event_id,
                    source_revision=update.content_revision,
                    payload_sha256=f"{index + 1:064x}",
                    payload=update.model_dump(mode="json"),
                    selection=_selection(),
                    now_ms=now_ms,
                    root_ttl_ms=_TTL_MS,
                )
                for index, update in enumerate((stale, fresh))
            ]
        assert [disposition for _, _, disposition in accepted] == ["accepted", "accepted"]
        rows = {
            row["case_id"]: row
            for row in conn.execute(
                "SELECT case_id,state,analysis_status,policy_reason,root_expires_at_ms,"
                "source_observed_at_ms,trigger_persisted_at_ms FROM trading_cases"
            ).fetchall()
        }
        stale_case, fresh_case = rows[accepted[0][1]], rows[accepted[1][1]]
        assert (stale_case["state"], stale_case["analysis_status"], stale_case["policy_reason"]) == (
            "EXCLUDED",
            "expired",
            "source_expired",
        )
        assert stale_case["root_expires_at_ms"] == now_ms - 1
        assert fresh_case["state"] == "PENDING" and fresh_case["root_expires_at_ms"] == now_ms - 5_000 + _TTL_MS
        assert fresh_case["source_observed_at_ms"] == now_ms - 5_000
        assert fresh_case["trigger_persisted_at_ms"] == now_ms
        with pytest.raises(KeyError), conn.transaction():
            trading.accept_trigger(
                kind="catalyst",
                source_fact_key="event-shapeless",
                source_revision="r1",
                payload_sha256="f" * 64,
                payload={"kind": "catalyst", "headline": "Old headline"},
                selection=_selection(),
                now_ms=now_ms,
                root_ttl_ms=_TTL_MS,
            )
    finally:
        conn.close()


def test_source_update_receipt_is_idempotent_and_keeps_the_first_payload(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "amendment-receipt-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        trading = TradingRepository(conn)
        head, _ = first_report(event_id="event-sol", first_available_at_ms=1_000, completed_at_ms=1_050)
        _, correction = _correction(head, 100_000)

        def receive(payload_sha256: str, payload: dict[str, Any], now_ms: int) -> str:
            with conn.transaction():
                return trading.receive_source_update(
                    update_id=correction.update_id,
                    source_fact_key=correction.event_id,
                    content_revision=correction.content_revision,
                    affected_claim_refs=correction.affected_claim_refs,
                    retired_claim_refs=correction.retired_claim_refs,
                    payload=payload,
                    payload_sha256=payload_sha256,
                    now_ms=now_ms,
                )

        payload = correction.model_dump(mode="json")
        assert receive("a" * 64, payload, 2_000) == "accepted"
        assert receive("a" * 64, payload, 3_000) == "duplicate"
        assert receive("b" * 64, {**payload, "text": "rewritten"}, 4_000) == "source_conflict"
        stored = conn.execute("SELECT payload,payload_sha256,received_at_ms FROM trading_source_amendments").fetchall()
        assert stored == [{"payload": payload, "payload_sha256": "a" * 64, "received_at_ms": 2_000}]
        assert conn.execute(
            "SELECT kind,source_fact_key,source_revision,attempted_sha256,original_sha256 "
            "FROM trading_trigger_conflicts"
        ).fetchall() == [
            {
                "kind": "source_update",
                "source_fact_key": "event-sol",
                "source_revision": correction.content_revision,
                "attempted_sha256": "b" * 64,
                "original_sha256": "a" * 64,
            }
        ]
        assert _counts(conn)["trading_triggers"] == 0 and _counts(conn)["trading_cases"] == 0
    finally:
        conn.close()


def test_analysis_sees_the_correction_recorded_for_its_claims(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "amended-analysis-db", read_only=False)
    try:
        reset_postgres_schema(conn)
        news = NewsRepository(conn)
        now_ms = int(time.time() * 1000)
        head, catalyst = first_report(
            event_id="event-sol", first_available_at_ms=now_ms - 60_000, completed_at_ms=now_ms - 30_000
        )
        _, correction = _correction(head, now_ms)
        with conn.transaction():
            news.enqueue_trade_event(**outbox_row(catalyst))
            news.enqueue_trade_event(**outbox_row(correction))
        assert _run(_runner(tmp_path, _Analyst()), "relay", "analyze") == [2, True]
        row = conn.execute(
            "SELECT c.state,a.evidence_ref,a.brief_ref FROM trading_cases c "
            "JOIN trading_case_attempts a USING (case_id)"
        ).fetchone()
        assert row["state"] == "DONE"
        files = AnalysisFiles(tmp_path / "archive")
        snapshot = files.read(row["evidence_ref"])
        brief = json.loads(files.read(row["brief_ref"])["brief_json"])
        for recorded in (snapshot["source_amendments"], brief["source_amendments"]):
            assert [item["update_id"] for item in recorded] == [correction.update_id]
            assert recorded[0]["retired_claim_refs"] == list(catalyst.claim_refs)
        assert brief["evidence"]["source"]["values"] == {"text": catalyst.text}
        assert brief["plan_menu"]
    finally:
        conn.close()
