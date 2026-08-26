from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame
from tracefold.news.reader_history import build_reader_history

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]


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


def test_a_telemetry_card_never_becomes_a_targeted_asset_candidate(conn) -> None:
    """#267 gave the deterministic lanes Event assets. The targeted band must not notice.

    The 4 h to 48 h band asks "what *story* about this asset has the reader already been told", and a
    telemetry frame is a measurement rather than a story. Before #267 these Events had no
    `news_event_assets` row and could never be candidates; letting them in would have changed the model
    lane's `told` selection — and through it `decide()`'s novelty measurement — as a side effect of a
    price-plane fix, with nothing measured behind the change. The 4 h `recent` window is untouched.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        prior = _admit(
            repos,
            hit_id=175401,
            text="TRUMP OI Rise 4.55 percent, OI Value 32.17M",
            symbol="TRUMP",
            ts="2026-08-24T06:00:00+08:00",
        )
        current = _admit(
            repos,
            hit_id=175402,
            text="TRUMP token unlocks a large tranche to early backers",
            symbol="TRUMP",
            ts="2026-08-24T12:00:00+08:00",
        )
        current_opened = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_opened is not None
        _persist_sent_triage_card(
            repos,
            event_id=prior,
            at_ms=int(current_opened["opened_at_ms"]) - 6 * 3_600_000,
            symbol="TRUMP",
        )
        conn.execute(
            "UPDATE news_events SET admission = 'telemetry_deterministic' WHERE event_id = %s",
            (prior,),
        )

    history = repos.news.reader_history(event_id=current, now_ms=int(current_opened["opened_at_ms"]))
    assert history.targeted_told_rows == ()

    # And it is the admission that excludes it, not a missing asset row: the same delivered card on the
    # ordinary lane is exactly the candidate this band exists to find.
    with repos.transaction():
        conn.execute("UPDATE news_events SET admission = 'candidate' WHERE event_id = %s", (prior,))
    recalled = repos.news.reader_history(event_id=current, now_ms=int(current_opened["opened_at_ms"]))
    assert [(row.event_id, row.reason) for row in recalled.targeted_told_rows] == [(prior, "canonical_asset_overlap")]
    conn.commit()


def test_evaluator_does_not_recall_a_verdict_only_asset_that_production_cannot_join(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        prior = _admit(
            repos,
            hit_id=175003,
            text="An unrelated issuer files a routine notice",
            symbol="OTHER",
            ts="2026-08-24T06:00:00+08:00",
        )
        current = _admit(
            repos,
            hit_id=175004,
            text="Alibaba plans a new financing transaction",
            symbol="BABA",
            ts="2026-08-24T12:00:00+08:00",
        )
        current_opened = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_opened is not None
        now_ms = int(current_opened["opened_at_ms"])
        sent_at_ms = now_ms - 6 * 3_600_000
        _persist_sent_triage_card(repos, event_id=prior, at_ms=sent_at_ms, symbol="BABA")
        conn.execute("DELETE FROM news_event_assets WHERE event_id=%s", (prior,))
        conn.execute("UPDATE news_events SET grounded_assets='[]'::jsonb WHERE event_id=%s", (prior,))

    production = repos.news.reader_history(event_id=current, now_ms=now_ms)
    evaluator = build_reader_history(
        (
            {
                "event_id": prior,
                "at_ms": sent_at_ms,
                "storyline_key": "asset:OTHER",
                "comparison_title": "unrelated issuer routine notice",
                "comparison_fingerprint": "prior-fingerprint",
                "family": "general",
                "grounded_assets": [],
                "assets": ["BABA"],
                "canonical_assets": [],
                "event_type": "filing",
                "magnitude": 2,
                "direction": "bearish",
                "headline_zh": "无关发行人提交例行文件",
            },
        ),
        now_ms=now_ms,
        comparison_fingerprint="current-fingerprint",
        canonical_assets=("BABA",),
    )

    assert production.targeted_told_rows == evaluator.targeted_told_rows == ()
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

    history = repos.news.reader_history(event_id=current, now_ms=now_ms)

    assert len(history.recent_seen_rows) == 128
    assert prior not in {row.event_id for row in history.recent_seen_rows}
    assert [(row.event_id, row.reason) for row in history.targeted_told_rows] == [(prior, "canonical_asset_overlap")]
    conn.commit()
