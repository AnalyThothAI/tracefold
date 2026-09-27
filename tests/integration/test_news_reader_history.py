from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support import news_novelty_sequences as sequences
from tests.support.news_legacy import LEGACY_PROGRAM_VERSION, LEGACY_TRIAGE_POLICY_VERSION, legacy_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.market_review.instrument_storage import InstrumentsRepository
from tracefold.news.models import TriageVerdict, base_symbol
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame
from tracefold.news.similarity import trigram_similarity

pytestmark = pytest.mark.integration

CALIBRATION = Path(__file__).resolve().parents[1] / "fixtures" / "news_dedup_calibration_v1.json.gz"


@pytest.fixture
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _admit(repos, *, hit_id: int, text: str, symbol: str, ts: str, engine_type: str = "news") -> str:
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": hit_id,
                "text": text,
                "link": f"https://example.test/{hit_id}",
                "source": f"wire-{hit_id}",
                "newsType": engine_type,
                "engineType": engine_type,
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
    direction: str = "bearish",
    headline_zh: str = "阿里巴巴配售新股",
    policy_version: str = LEGACY_TRIAGE_POLICY_VERSION,
    final_decision: str = "push",
    throttled_by: str | None = None,
) -> None:
    evidence = repos.news.latest_evidence_snapshot(event_id)
    assert evidence is not None
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[{"symbol": symbol, "role": "primary"}],
        direction=direction,
        scope="single_name",
        fact_kind="state_change",
        evidence_ref="c1",
        confidence=0.9,
        headline_zh=headline_zh,
        why_zh="",
    )
    judgment = legacy_judgment(verdict)
    runtime_manifest_sha = "b" * 64
    trace = {
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": "model",
        "judgment_sha256": judgment.scored_judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "editorial_sha256": judgment.editorial.editorial_sha256,
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": LEGACY_PROGRAM_VERSION,
        "program_sha256": "a" * 64,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
    }
    assert repos.news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=policy_version,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin="model",
        rule_baseline_decision="push",
        final_decision=final_decision,
        override_rule="fact_kind_state_change",
        throttled_by=throttled_by,
        verdict=verdict.model_dump(mode="json"),
        model_editorial=judgment.editorial.document,
        judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model="test",
        program_version=LEGACY_PROGRAM_VERSION,
        program_sha256="a" * 64,
        degraded=False,
        error_code=None,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=at_ms - 1,
    )


def _canonical_assets(repos, symbols: list[str]) -> list[str]:
    """The Event-asset ledger's base symbols: an alias resolves to its base, anything else is itself."""

    aliases = InstrumentsRepository(repos.news.conn).alias_map()
    return sorted({aliases.get(symbol, aliases.get(base_symbol(symbol), base_symbol(symbol))) for symbol in symbols})


def _persist_sent_triage_card(
    repos,
    *,
    event_id: str,
    at_ms: int,
    symbol: str,
    direction: str = "bearish",
    headline_zh: str = "阿里巴巴配售新股",
    state: str = "sent",
) -> None:
    _persist_triage_verdict(
        repos,
        event_id=event_id,
        at_ms=at_ms,
        symbol=symbol,
        direction=direction,
        headline_zh=headline_zh,
    )
    event = repos.news.event_card(event_id)
    verdict = repos.news.latest_verdict(event_id=event_id, stage="triage")
    bound = {
        key: event.get(key)
        for key in ("storyline_key", "comparison_title", "comparison_fingerprint", "dedupe_family", "grounded_assets")
    }
    bound.update({key: verdict["verdict"].get(key) for key in ("direction", "headline_zh", "why_zh", "assets")})
    bound["canonical_assets"] = _canonical_assets(repos, [symbol])
    assert (
        repos.news.begin_delivery(
            event_id=event_id, kind="first", card={}, now_ms=at_ms - 1, history_context_json=json.dumps(bound)
        )
        == "new"
    )
    assert repos.news.settle_delivery(
        event_id=event_id,
        kind="first",
        state=state,
        receipt={"ok": True} if state == "sent" else None,
        error_code=None if state == "sent" else "provider_rejected",
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


def test_sent_asset_binding_survives_later_grounding_removal(conn) -> None:
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

    # The sent card keeps the asset binding it was delivered with, though its Event lost the tag since.
    assert [r.event_id for r in production.targeted_told_rows] == [prior]
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
            conn.execute("UPDATE news_events SET comparison_fingerprint=%s WHERE event_id=%s", ("f" * 64, event_id))
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
        # Bind the fixture's shared fingerprint at send time as well as on the current candidate.
        conn.execute(
            "UPDATE news_deliveries SET history_context=jsonb_set(history_context, "
            "'{comparison_fingerprint}', to_jsonb(%s::text)) WHERE event_id=ANY(%s)",
            ("f" * 64, list(states.values())),
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


def test_title_similarity_band_recalls_a_same_story_card_the_recent_cap_and_targeted_bands_cannot(conn) -> None:
    """#491: a different wire, a different instrument tag, no fingerprint match, 12 h old, under a bucket that
    received more than 128 cards since. Only the title band can bring it back, and it comes back ranked by the
    same pg_trgm number the pure builder recomputes."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        current = _admit(
            repos,
            hit_id=491000,
            text="This deal secures stable low cost oil for Americans and will drive Venezuela's economic recovery",
            symbol="CL",
            ts="2026-09-01T20:00:00+08:00",
        )
        current_row = conn.execute("SELECT opened_at_ms FROM news_events WHERE event_id=%s", (current,)).fetchone()
        assert current_row is not None
        now_ms = int(current_row["opened_at_ms"])
        prior = _admit(
            repos,
            hit_id=491001,
            text="Fact sheet: President Donald J. Trump announces historic oil agreement to secure American energy",
            symbol="XOM",
            ts="2026-09-01T07:00:00+08:00",
        )
        _persist_sent_triage_card(repos, event_id=prior, at_ms=now_ms - 12 * 3_600_000, symbol="XOM")
        unrelated_old = _admit(
            repos,
            hit_id=491002,
            text="Bank of Japan keeps its policy rate unchanged at the September meeting",
            symbol="JPY",
            ts="2026-09-01T07:30:00+08:00",
        )
        _persist_sent_triage_card(repos, event_id=unrelated_old, at_ms=now_ms - 11 * 3_600_000, symbol="JPY")
        for index in range(129):
            symbol = f"RH{index:03d}"
            event_id = _admit(
                repos,
                hit_id=491100 + index,
                text=f"Issuer {symbol} announces distinct operational milestone {symbol}",
                symbol=symbol,
                ts="2026-09-01T19:00:00+08:00",
            )
            _persist_sent_triage_card(repos, event_id=event_id, at_ms=now_ms - index * 1_000, symbol=symbol)

    history = repos.news.reader_history(event_id=current, now_ms=now_ms)

    assert len(history.recent_seen_rows) == 128
    assert prior not in {row.event_id for row in history.recent_seen_rows}
    assert history.targeted_told_rows == ()
    similar = [(row.event_id, row.scope, row.reason) for row in history.similar_told_rows]
    assert similar[0] == (prior, "targeted", "title_similarity")
    # The band admits any shared trigram (English function words share a few), so the unrelated card may be in
    # it; what matters is that it ranks below the same-story card.
    ranked = [row.event_id for row in history.similar_told_rows]
    assert unrelated_old not in ranked or ranked.index(unrelated_old) > ranked.index(prior)
    # The band never spends a slot on a row the recent ledger already carries.
    assert not {row.event_id for row in history.similar_told_rows} & {row.event_id for row in history.recent_seen_rows}
    told = history.told_source_rows
    assert told[0].event_id == prior and len(told) == len(history.recent_seen_rows) + len(similar)

    # The pure twin agrees with PostgreSQL on the number it ranked by.
    titles = conn.execute(
        "SELECT event_id, comparison_title FROM news_events WHERE event_id IN (%s, %s)", (current, prior)
    ).fetchall()
    by_id = {str(row["event_id"]): str(row["comparison_title"]) for row in titles}
    pg_score = conn.execute("SELECT similarity(%s, %s) AS s", (by_id[current], by_id[prior])).fetchone()
    assert pg_score is not None
    assert abs(float(pg_score["s"]) - trigram_similarity(by_id[current], by_id[prior])) < 1e-6
    assert float(pg_score["s"]) > 0.1
    conn.commit()


def test_trigram_similarity_is_pg_trgm_similarity_on_the_calibration_titles(conn) -> None:
    """`assemble_reader_history` re-ranks the SQL band in Python. Equality of the two numbers, on every title pair
    of the 2026-09-01 calibration set that shares a trigram, is what keeps the Python ranking the SQL one."""

    with gzip.open(CALIBRATION, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)
    cards = {card["event_id"]: card["comparison_title"] for card in doc["cards"]}
    pairs = [(cards[pair["earlier"]], cards[pair["later"]]) for pair in doc["duplicate_pairs"]]
    titles = sorted(cards.values())
    pairs.extend((titles[index], titles[(index * 7 + 3) % len(titles)]) for index in range(0, len(titles), 3))

    rows = conn.execute(
        "SELECT a, b, similarity(a, b) AS s FROM unnest(%s::text[], %s::text[]) AS pair(a, b)",
        ([pair[0] for pair in pairs], [pair[1] for pair in pairs]),
    ).fetchall()
    assert len(rows) == len(pairs) >= 400
    mismatched = [
        (row["a"], row["b"], float(row["s"]), trigram_similarity(row["a"], row["b"]))
        for row in rows
        if abs(float(row["s"]) - trigram_similarity(row["a"], row["b"])) > 1e-6
    ]
    assert mismatched == []
    assert sum(1 for row in rows if float(row["s"]) > 0) >= 143


def _sequence_events(repos, sequence_id: str) -> list[dict[str, object]]:
    """Admit the frozen sequence's Events, in its own clock order.

    Titles, symbols and clocks come from the read-only production export, so the ledger this builds is the
    one the sequence's last card was judged against rather than a made-up chain.
    """

    admitted: list[dict[str, object]] = []
    for offset, step in enumerate(sequences.sequence(sequence_id)["steps"]):
        case_key = str(step["case"])
        event = sequences.event(case_key)
        verdict = sequences.verdict_row(case_key)["verdict"]
        symbol = next(asset["symbol"] for asset in verdict["assets"] if asset["role"] == "primary")
        event_id = _admit(
            repos,
            hit_id=651_000 + offset + (0 if sequence_id.startswith("visa") else 100),
            text=str(event["leader_title"]),
            symbol=str(symbol),
            ts=datetime.fromtimestamp(int(event["opened_at_ms"]) / 1000, tz=UTC).isoformat(),
            # The Upbit notice and the wire repeat of it were two Events in production because the
            # provider tagged them with two engine types; collapsing that here would replay a chain the
            # reader never had.
            engine_type=str(event["engine_type"]),
        )
        admitted.append(
            {
                "case": case_key,
                "event_id": event_id,
                "symbol": symbol,
                "settled_at_ms": step["settled_at_ms"],
                "triage_stamp": sequences.triage_stamp(case_key),
                "direction": str(verdict["direction"]),
                "headline_zh": str(verdict["headline_zh"]),
            }
        )
    return admitted


@pytest.mark.parametrize("sequence_id", ["visa_onchain_credit", "cp_listing_three_venues"])
def test_sql_history_holds_exactly_the_receipts_of_a_frozen_sequence(conn, sequence_id: str) -> None:
    """The reader ledger PostgreSQL returns for one frozen production chain (#651 §6.3, §12).

    The clocks are the frozen ones: each card's receipt settles when it actually settled, and the ledger is
    read at the last card's own stamp. A queued delivery, a failed one and one that settles after the read
    clock also exist in the database and must not appear: a receipt is proof the reader had the card at
    that stamp, and none of those is.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        admitted = _sequence_events(repos, sequence_id)
        for row in admitted[:-1]:
            if row["settled_at_ms"] is None:
                continue
            _persist_sent_triage_card(
                repos,
                event_id=str(row["event_id"]),
                at_ms=int(row["settled_at_ms"]),
                symbol=str(row["symbol"]),
                direction=str(row["direction"]),
                headline_zh=str(row["headline_zh"]),
            )
        current = admitted[-1]
        now_ms = int(current["triage_stamp"])
        excluded: dict[str, str] = {}
        fillers: list[str] = []
        for label, state, at_ms in (
            ("queued", "sending", now_ms - 600_000),
            ("failed", "terminal", now_ms - 900_000),
            ("future", "sent", now_ms + 600_000),
        ):
            symbol = f"EX{len(excluded)}"
            event_id = _admit(
                repos,
                hit_id=651_900 + len(excluded) + (0 if sequence_id.startswith("visa") else 50),
                text=f"An unrelated issuer files a {label} operational notice",
                symbol=symbol,
                ts=datetime.fromtimestamp((now_ms - 1_800_000) / 1000, tz=UTC).isoformat(),
            )
            _persist_sent_triage_card(repos, event_id=event_id, at_ms=at_ms, symbol=symbol, state=state)
            excluded[label] = event_id
        # A `sending` row carries no settle stamp at all, which is what "not proven delivered" means.
        conn.execute("UPDATE news_deliveries SET settled_at_ms = NULL WHERE event_id = %s", (excluded["queued"],))
        # Ordinary cards the reader also received while the sequence ran. The bands order by settle stamp
        # and then by event id, so more than one row is what makes "the same indices" mean anything.
        for index in range(3):
            symbol = f"FL{index}"
            filler = _admit(
                repos,
                hit_id=651_940 + index + (0 if sequence_id.startswith("visa") else 20),
                text=f"Issuer {symbol} completes a distinct unrelated operational milestone {symbol}",
                symbol=symbol,
                ts=datetime.fromtimestamp((now_ms - 2_400_000) / 1000, tz=UTC).isoformat(),
            )
            _persist_sent_triage_card(
                repos,
                event_id=filler,
                at_ms=now_ms - (index + 1) * 120_000,
                symbol=symbol,
            )
            fillers.append(filler)

    delivered = [row for row in admitted[:-1] if row["settled_at_ms"] is not None]
    delivered_ids = [str(row["event_id"]) for row in delivered]

    production = repos.news.reader_history(event_id=str(current["event_id"]), now_ms=now_ms)
    sql_ids = [row.event_id for row in production.told_source_rows]
    # A card the policy dropped is never a receipt, so the CP chain's middle step contributes no row.
    assert [str(row["case"]) for row in admitted[:-1] if row["settled_at_ms"] is None] == (
        ["cp_upbit_cross_channel"] if sequence_id == "cp_listing_three_venues" else []
    )
    assert delivered_ids
    for label in ("queued", "failed", "future"):
        assert excluded[label] not in sql_ids, label
    assert set(delivered_ids) | set(fillers) == set(sql_ids)
    conn.commit()
