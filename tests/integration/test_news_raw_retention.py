from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.program.runtime import PROGRAM_VERSION

pytestmark = pytest.mark.integration

DAY_MS = 86_400_000
NOW_MS = 2_000_000_000_000


def _seed_current_event(repos, *, event_id: str, at_ms: int, judged: bool) -> str:
    item_id = f"item-{event_id}"
    fact_id = f"fact-{event_id}"
    repos.news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=f"key-{event_id}",
        title=f"title {event_id}",
        raw_first_line=f"title {event_id}",
        description="",
        canonical_url=None,
        reporting_origin="OpenNews",
        published_at_ms=at_ms,
        observed_at_ms=at_ms,
        provider_metadata_json="{}",
        strategy_ids_json="[]",
        ingest_mode="live",
        trace_id=f"trace-{event_id}",
        now_ms=at_ms,
    )
    repos.news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        dedupe_family="general",
        event_kind="news",
        comparison_fingerprint=f"fingerprint-{event_id}",
        comparison_title=f"title {event_id}",
        leader_title=f"title {event_id}",
        focus_fact_id=fact_id,
        focus_fact_text=f"title {event_id}",
        focus_fact_context="",
        focus_fact_method="whole_item",
        focus_span_start=0,
        focus_span_end=len(f"title {event_id}"),
        opened_at_ms=at_ms,
        expires_at_ms=at_ms + DAY_MS,
        admission="candidate",
        queue_priority="normal",
        provider_score=None,
        engine_type="news",
        asset_class="none",
        grounded_assets=[],
        grounded_assets_json="[]",
        watchlist_hits=[],
        watchlist_hits_json="[]",
        macro_lexicon=False,
        storyline_key=f"story-{event_id}",
        context_line="",
        ingest_mode="live",
        trace_id=f"trace-{event_id}",
        band_keys=[],
        now_ms=at_ms,
    )
    evidence = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=at_ms)
    if judged:
        verdict = TriageVerdict(
            novelty="new_fact",
            assets=[],
            direction="neutral",
            scope="macro",
            magnitude=1,
            confidence=0.7,
            headline_zh="保留证据",
            why_zh="",
        )
        judgment = scored_judgment(verdict)
        runtime_manifest_sha = "b" * 64
        program_sha = "a" * 64
        verdict_payload = verdict.model_dump(mode="json")
        trace = {
            "judgment_contract_version": judgment.judgment_contract_version,
            "judgment_origin": "model",
            "judgment_sha256": judgment.scored_judgment_sha256,
            "verdict_sha256": canonical_sha(verdict_payload),
            "editorial_sha256": judgment.editorial.editorial_sha256,
            "runtime_manifest_sha": runtime_manifest_sha,
            "program_version": PROGRAM_VERSION,
            "program_sha256": program_sha,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        }
        repos.news.insert_verdict(
            event_id=event_id,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=judgment.judgment_contract_version,
            judgment_origin="model",
            rule_baseline_decision="drop",
            final_decision="drop",
            override_rule=None,
            throttled_by=None,
            verdict=verdict_payload,
            model_editorial=judgment.editorial.model_dump(mode="json"),
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model="retention-fixture",
            program_version=PROGRAM_VERSION,
            program_sha256=program_sha,
            degraded=False,
            error_code=None,
            trace=trace,
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=at_ms,
        )
    return str(evidence["evidence_sha256"])


def test_raw_retention_deletes_stable_bounded_batches_and_reports_backlog(
    tmp_path,
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    repos = repositories_for_connection(conn)
    try:
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'raw-' || value, 'opennews', 'key-' || value, 'raw ' || value,
                   value, value, '{}'::jsonb, 'live', value, value
              FROM generate_series(0, 4) AS value
            """,
        )
        conn.commit()

        batches: list[dict[str, object]] = []
        for _ in range(3):
            with repos.transaction():
                batches.append(repos.news.purge_before(cutoff_ms=10, batch_size=2))

        assert [batch["deleted_items"] for batch in batches] == [2, 2, 1]
        assert [batch["backlog_items"] for batch in batches] == [3, 1, 0]
        assert [batch["backlog_capped"] for batch in batches] == [True, False, False]
        assert [batch["oldest_observed_at_ms"] for batch in batches] == [2, 4, None]
        assert conn.execute("SELECT item_id FROM news_items ORDER BY observed_at_ms, item_id").fetchall() == []
    finally:
        conn.close()


def test_raw_retention_keeps_a_2001_item_backlog_bounded_to_500_rows(
    tmp_path,
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    repos = repositories_for_connection(conn)
    try:
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            )
            SELECT 'scale-' || value, 'opennews', 'scale-key-' || value, 'scale ' || value,
                   value, value, '{}'::jsonb, 'live', value, value
              FROM generate_series(0, 2000) AS value
            """
        )
        conn.commit()

        batches: list[dict[str, object]] = []
        for _ in range(4):
            with repos.transaction():
                conn.execute("SET LOCAL statement_timeout = '1s'")
                batches.append(repos.news.purge_before(cutoff_ms=10_000, batch_size=500))

        assert [batch["deleted_items"] for batch in batches] == [500, 500, 500, 500]
        assert [batch["backlog_items"] for batch in batches] == [501, 501, 501, 1]
        assert [batch["backlog_capped"] for batch in batches] == [True, True, True, False]
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone() == {"n": 1}
    finally:
        conn.close()


def test_raw_retention_keeps_30_day_judged_corpus_and_expires_it_after_365_days(
    tmp_path,
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    repos = repositories_for_connection(conn)
    try:
        with repos.transaction():
            _seed_current_event(repos, event_id="raw-31d", at_ms=NOW_MS - 31 * DAY_MS, judged=False)
            retained_sha = _seed_current_event(
                repos,
                event_id="judged-31d",
                at_ms=NOW_MS - 31 * DAY_MS,
                judged=True,
            )
            _seed_current_event(repos, event_id="judged-366d", at_ms=NOW_MS - 366 * DAY_MS, judged=True)

        with repos.transaction():
            result = repos.news.purge_before(
                cutoff_ms=NOW_MS - 30 * DAY_MS,
                judged_cutoff_ms=NOW_MS - 365 * DAY_MS,
                batch_size=10,
            )

        assert result["deleted_items"] == 2
        assert result["backlog_items"] == 0
        assert conn.execute("SELECT item_id FROM news_items ORDER BY item_id").fetchall() == [
            {"item_id": "item-judged-31d"}
        ]
        retained = conn.execute(
            "SELECT evidence_sha256 FROM news_event_evidence_snapshots WHERE event_id = 'judged-31d'"
        ).fetchone()
        assert retained == {"evidence_sha256": retained_sha}
        assert conn.execute("SELECT count(*) AS n FROM news_verdicts WHERE event_id = 'judged-31d'").fetchone() == {
            "n": 1
        }
        assert conn.execute(
            "SELECT count(*) AS n FROM news_events WHERE event_id IN ('raw-31d', 'judged-366d')"
        ).fetchone() == {"n": 0}
    finally:
        conn.close()


def _seed_market_item(repos, *, item_id: str, at_ms: int, parsed: bool) -> None:
    """One market Item with its typed OI fact, or one raw card with neither."""

    repos.news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=f"key-{item_id}",
        title="TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
        raw_first_line="",
        description="",
        canonical_url=None,
        reporting_origin="OpenNews",
        published_at_ms=at_ms,
        observed_at_ms=at_ms,
        provider_metadata_json="{}",
        strategy_ids_json="[]",
        ingest_mode="live",
        trace_id=f"trace-{item_id}",
        now_ms=at_ms,
        market_kind="oi" if parsed else "unknown_market",
        market_source_strategy_id="1019" if parsed else "9999",
        market_parse_status="parsed" if parsed else "raw",
        market_parse_error=None if parsed else "unknown_market_source",
    )
    if not parsed:
        return
    repos.news.insert_oi_signal(
        event_id=f"event-{item_id}",
        metric_version="oi_signal_v1",
        symbol="TRUMP",
        raw_instrument="TRUMP",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8_021,
        whale_oi_ratio_bps=10_071,
        observed_at_ms=at_ms,
        received_at_ms=at_ms,
        now_ms=at_ms,
        provider="opennews",
        source_strategy_id="1019",
        source_contract_version="opennews_oi_source_v1",
        measurement_window_ms=300_000,
        measurement_definition="oi_signal_v1|opennews_oi_source_v1|300000",
        source_item_id=item_id,
        source_venue="binance",
    )


def test_market_items_live_on_the_judged_tier_whatever_their_parse_status(postgres_clone_dsn: str) -> None:
    """#553 §3.4. A market Item has no verdict, so `raw_days` alone would expire all of them.

    Under the preservation predicate a market Item can never be evidence: it leads no Event, so no
    verdict, review or learning case can reach it. Thirty days later every OI frame, liquidation
    report and account report would be gone while the ordinary news beside it kept a year. Which
    retention an observation gets is a decision about the observation, not a reward for being judged
    -- and a raw card is retained exactly as long as a parsed one.
    """

    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            _seed_market_item(repos, item_id="market-parsed-recent", at_ms=NOW_MS - 60 * DAY_MS, parsed=True)
            _seed_market_item(repos, item_id="market-raw-recent", at_ms=NOW_MS - 60 * DAY_MS, parsed=False)
            _seed_market_item(repos, item_id="market-parsed-ancient", at_ms=NOW_MS - 400 * DAY_MS, parsed=True)
            _seed_current_event(repos, event_id="news-unjudged", at_ms=NOW_MS - 60 * DAY_MS, judged=False)

        with repos.transaction():
            result = repos.news.purge_before(
                cutoff_ms=NOW_MS - 30 * DAY_MS,
                judged_cutoff_ms=NOW_MS - 365 * DAY_MS,
                batch_size=500,
            )

        surviving = {
            row["item_id"]
            for row in conn.execute(
                "SELECT item_id FROM news_items WHERE item_id LIKE 'market-%' OR item_id = 'item-news-unjudged'"
            ).fetchall()
        }
        assert surviving == {"market-parsed-recent", "market-raw-recent"}
        assert result["deleted_items"] >= 2
        # The typed fact left with the Item it was parsed from, and no orphan stayed behind.
        orphans = conn.execute(
            """
            SELECT count(*) AS n
              FROM news_oi_signals s
             WHERE NOT EXISTS (SELECT 1 FROM news_items i WHERE i.item_id = s.source_item_id)
            """
        ).fetchone()
        assert orphans["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM news_oi_signals WHERE source_item_id = 'market-parsed-recent'"
            ).fetchone()["n"]
            == 1
        )
        conn.commit()
    finally:
        conn.close()
