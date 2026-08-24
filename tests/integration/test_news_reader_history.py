from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame
from tracefold.news.reader_history import RECENT_HISTORY_MAX, RECENT_HISTORY_WINDOW_MS
from tracefold.news.storage.decisions import _READER_HISTORY_PROJECTION

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _admit(repos, *, hit_id: int, text: str, symbol: str, ts: str) -> str:
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": hit_id,
                "text": text,
                "link": f"https://example.test/{hit_id}",
                "source": f"wire-{hit_id}",
                "newsType": "news",
                "engineType": "news",
                "ts": ts,
                "aiRating": {"score": 90, "signal": "short", "status": "done"},
                "coins": [{"expired": False, "grade": "A", "market_type": "cex", "score": 90, "symbol": symbol}],
                "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
            },
        }
    )
    assert event is not None
    stamp = int(event.entry.published_at_ms or 0)
    batch = admit_frame(
        repos,
        event=event,
        ingest_mode="live",
        observed_at_ms=stamp,
        trace_id=f"trace-{hit_id}",
        watchlist_symbols=frozenset(),
        now_ms=stamp,
    )
    assert len(batch.results) == 1 and batch.results[0].event_created
    return batch.results[0].event_id


def _persist_triage_verdict(
    repos,
    *,
    event_id: str,
    at_ms: int,
    symbol: str,
    policy_version: str = "news_triage_policy_test",
) -> None:
    evidence = repos.news.latest_evidence_snapshot(event_id)
    assert evidence is not None
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="filing",
        assets=[{"symbol": symbol, "role": "primary"}],
        direction="bearish",
        scope="single_name",
        magnitude=2,
        actionable=True,
        confidence=0.9,
        decision="push",
        headline_zh="阿里巴巴配售新股",
        why_zh="",
    )
    judgment = scored_judgment(verdict)
    assert repos.news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=policy_version,
        model_decision="push",
        rule_baseline_decision="push",
        final_decision="push",
        override_rule="trade_relevance_realtime",
        throttled_by=None,
        verdict=verdict.model_dump(mode="json"),
        editorial=judgment.editorial.model_dump(mode="json"),
        scored_judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha="b" * 64,
        model="test",
        program_version="news_semantic_program_test",
        program_sha256="a" * 64,
        degraded=False,
        error_code=None,
        trace={},
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=at_ms - 1,
    )


def _persist_sent_triage_card(repos, *, event_id: str, at_ms: int, symbol: str) -> None:
    _persist_triage_verdict(repos, event_id=event_id, at_ms=at_ms, symbol=symbol)
    assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=at_ms - 1) == "new"
    assert repos.news.settle_delivery(
        event_id=event_id,
        kind="first",
        state="sent",
        receipt={"ok": True},
        error_code=None,
        now_ms=at_ms,
    )


def test_reader_history_recalls_a_sent_cross_source_alias_after_four_hours(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        conn.execute(
            "INSERT INTO news_symbol_aliases(alias, base_symbol, source, updated_at_ms) VALUES ('9988','BABA','seed',1)"
        )
        prior = _admit(
            repos,
            hit_id=175001,
            text="Alibaba prices a Hong Kong share placement",
            symbol="9988",
            ts="2026-08-24T06:00:00+08:00",
        )
        current = _admit(
            repos,
            hit_id=175002,
            text="Alibaba plans a large AI-funded equity sale",
            symbol="BABA",
            ts="2026-08-24T12:00:00+08:00",
        )
        current_opened = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_opened is not None
        _persist_sent_triage_card(
            repos,
            event_id=prior,
            at_ms=int(current_opened["opened_at_ms"]) - 6 * 3_600_000,
            symbol="9988",
        )
        conn.execute(
            "UPDATE news_events SET grounded_assets='[]'::jsonb WHERE event_id = ANY(%s)",
            ([prior, current],),
        )

    history = repos.news.reader_history(event_id=current, now_ms=int(current_opened["opened_at_ms"]))

    assert history.recent_seen_rows == ()
    assert [(row.event_id, row.reason, row.canonical_assets) for row in history.targeted_told_rows] == [
        (prior, "canonical_asset_overlap", ("BABA",))
    ]
    conn.commit()


def test_reader_history_exact_target_requires_a_settled_sent_receipt(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        current = _admit(
            repos,
            hit_id=175010,
            text="Issuer repeats one normalized announcement",
            symbol="NVDA",
            ts="2026-08-26T12:00:00+08:00",
        )
        current_opened = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_opened is not None
        now_ms = int(current_opened["opened_at_ms"])
        states = {}
        variants = {
            "sent": "Nvidia opens a new chip assembly plant",
            "sending": "Regulator fines Nvidia over an export filing",
            "terminal": "Nvidia chief financial officer announces retirement",
            "ambiguous": "Nvidia discloses an unresolved customs assessment",
            "decision-only": "Nvidia board approves a larger quarterly dividend",
        }
        for index, (label, text) in enumerate(variants.items(), start=1):
            event_id = _admit(
                repos,
                hit_id=175010 + index,
                text=text,
                symbol="NVDA",
                ts=f"2026-08-25T0{index}:00:00+08:00",
            )
            _persist_sent_triage_card(
                repos,
                event_id=event_id,
                at_ms=now_ms - (5 + index) * 3_600_000,
                symbol="NVDA",
            )
            states[label] = event_id
        conn.execute(
            "UPDATE news_events SET comparison_fingerprint=%s WHERE event_id = ANY(%s)",
            ("f" * 64, [current, *states.values()]),
        )
        conn.execute(
            "UPDATE news_deliveries SET state='sending', settled_at_ms=NULL WHERE event_id=%s",
            (states["sending"],),
        )
        conn.execute(
            "UPDATE news_deliveries SET state='terminal' WHERE event_id=%s",
            (states["terminal"],),
        )
        conn.execute(
            "UPDATE news_deliveries SET state='terminal', error_code='ambiguous_after_crash' WHERE event_id=%s",
            (states["ambiguous"],),
        )
        conn.execute("DELETE FROM news_deliveries WHERE event_id=%s", (states["decision-only"],))
        _persist_triage_verdict(
            repos,
            event_id=states["sent"],
            at_ms=now_ms - 1,
            symbol="NVDA",
            policy_version="news_triage_policy_test_second_route",
        )

    history = repos.news.reader_history(event_id=current, now_ms=now_ms)

    assert history.recent_seen_rows == ()
    assert [(row.event_id, row.reason) for row in history.targeted_told_rows] == [(states["sent"], "exact_fingerprint")]
    conn.commit()


def test_targeted_history_is_not_displaced_by_more_than_128_recent_cards(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        current = _admit(
            repos,
            hit_id=175200,
            text="Alibaba financing transaction appears on a second international wire",
            symbol="BABA",
            ts="2026-08-28T12:00:00+08:00",
        )
        current_row = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_row is not None
        now_ms = int(current_row["opened_at_ms"])
        prior = _admit(
            repos,
            hit_id=175201,
            text="Alibaba completes an overseas financing transaction",
            symbol="BABA",
            ts="2026-08-28T05:00:00+08:00",
        )
        _persist_sent_triage_card(repos, event_id=prior, at_ms=now_ms - 6 * 3_600_000, symbol="BABA")
        for index in range(129):
            symbol = f"RH{index:03d}"
            event_id = _admit(
                repos,
                hit_id=175300 + index,
                text=f"Issuer {symbol} announces distinct operational milestone {symbol}",
                symbol=symbol,
                ts="2026-08-28T11:00:00+08:00",
            )
            _persist_sent_triage_card(repos, event_id=event_id, at_ms=now_ms - index * 1_000, symbol=symbol)
        noise_event = _admit(
            repos,
            hit_id=175500,
            text="Index plan noise owner remains outside the sent reader ledger",
            symbol="NOISEBASE",
            ts="2026-08-28T11:30:00+08:00",
        )
        noise_opened = conn.execute(
            "SELECT opened_at_ms FROM news_events WHERE event_id=%s",
            (noise_event,),
        ).fetchone()
        assert noise_opened is not None
        conn.execute(
            """
            INSERT INTO news_event_assets(symbol, event_id, market_type, opened_at_ms)
            SELECT 'NOISE' || lpad(value::text, 5, '0'), %s, NULL, %s
              FROM generate_series(1, 7000) value
            """,
            (noise_event, noise_opened["opened_at_ms"]),
        )
        conn.execute("ANALYZE news_event_assets")
        plan_rows = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS) "
            + _READER_HISTORY_PROJECTION
            + """
             WHERE e.event_id <> %s AND d.settled_at_ms >= %s
             ORDER BY d.settled_at_ms DESC, v.event_id LIMIT %s
            """,
            (current, now_ms - RECENT_HISTORY_WINDOW_MS, RECENT_HISTORY_MAX),
        ).fetchall()
        plan = "\n".join(str(row["QUERY PLAN"]) for row in plan_rows)

    history = repos.news.reader_history(event_id=current, now_ms=now_ms)

    assert "ix_news_event_assets_event" in plan
    assert "Seq Scan on news_event_assets" not in plan
    assert len(history.recent_seen_rows) == 128
    assert prior not in {row.event_id for row in history.recent_seen_rows}
    assert [(row.event_id, row.reason) for row in history.targeted_told_rows] == [(prior, "canonical_asset_overlap")]
    conn.commit()
