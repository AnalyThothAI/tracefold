"""Price Review capacity gates (#88 §14) under the native Serve statement timeout.

Marked slow: it seeds six figures of rows and belongs in the slow integration lane.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_review.pricing import (
    QUOTE_TARGET_MAX,
    REACTION_METRIC_VERSION,
    REVIEW_MAX_HOURS,
    Quote,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_787_000_000_000
HOUR = 3_600_000
EVENTS = 50_000
ASSETS_PER_EVENT = 2


@pytest.fixture(scope="module")
def seeded():
    conn = connect_postgres_test(read_only=False)
    migrate(conn)
    _seed(conn)
    _serve_session(conn)
    yield conn
    conn.close()


def _serve_session(conn: Any) -> None:
    """Measure under the settings Serve actually runs with (`_SERVE_SESSION_CONFIG`), not psql defaults.

    Serve also caps every statement at one second, so the review budget is a hard timeout, not a preference.
    """

    for setting, value in (
        ("jit", "off"),
        ("max_parallel_workers_per_gather", "0"),
        ("work_mem", "8MB"),
        ("statement_timeout", "1s"),
    ):
        conn.execute(f"SET {setting} = '{value}'")
    conn.commit()


def _seed(conn: Any) -> None:
    """One window of Events, verdicts, deliveries and two Reactions each — 100k rows, set-based."""

    window_start = NOW - REVIEW_MAX_HOURS * HOUR
    # Spread the corpus across the whole window, the way a live month arrives — bunching it at the start made
    # the default 168 h window measure an empty set.
    step = (REVIEW_MAX_HOURS * HOUR) // (EVENTS + 1)
    conn.execute(
        """
        INSERT INTO news_items (item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
                                provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms)
        SELECT 'i-' || g, 'opennews', 'k-' || g, 'headline ' || g, %s + g * %s::bigint, %s + g * %s::bigint,
               '{}'::jsonb, 'live', %s, %s
          FROM generate_series(1, %s) AS g
        """,
        (window_start, step, window_start, step, NOW, NOW, EVENTS),
    )
    conn.execute(
        """
        INSERT INTO news_events (event_id, leader_item_id, family, comparison_fingerprint, comparison_title,
                                 leader_title, focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission,
                                 storyline_key, ingest_mode, created_at_ms, updated_at_ms)
        SELECT 'e-' || g, 'i-' || g, 'general', 'f-' || g, 'c', 'leader ' || g, 'fact:' || g,
               %s + g * %s::bigint, %s + g * %s::bigint, %s + g * %s::bigint + 3600000, 'candidate',
               'asset:S' || (g %% 500), 'live', %s, %s
          FROM generate_series(1, %s) AS g
        """,
        (window_start, step, window_start, step, window_start, step, NOW, NOW, EVENTS),
    )
    conn.execute(
        """
        INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
        SELECT 'S' || (g %% 500), 'e-' || g, NULL, %s + g * %s::bigint FROM generate_series(1, %s) AS g
        """,
        (window_start, step, EVENTS),
    )
    conn.execute(
        """
        INSERT INTO news_verdicts (event_id, stage, policy_version, rule_baseline_decision, final_decision,
                                   verdict, degraded, created_at_ms)
        SELECT 'e-' || g, 'triage', 'v6', 'push',
               (ARRAY['push', 'drop', 'throttled', 'escalate'])[1 + g %% 4],
               jsonb_build_object(
                 'direction', (ARRAY['bullish', 'bearish', 'neutral'])[1 + g %% 3],
                 'magnitude', g %% 4,
                 'event_type', (ARRAY['listing', 'hack', 'regulation', 'product'])[1 + g %% 4],
                 'assets', jsonb_build_array(
                   jsonb_build_object('symbol', 'S' || (g %% 500), 'role', 'primary'),
                   jsonb_build_object('symbol', 'T' || (g %% 500), 'role', 'primary')
                 )
               ),
               false, %s + g * %s::bigint
          FROM generate_series(1, %s) AS g
        """,
        (window_start, step, EVENTS),
    )
    conn.execute(
        """
        INSERT INTO news_deliveries (event_id, kind, state, card, attempted_at_ms, settled_at_ms, created_at_ms)
        SELECT 'e-' || g, 'first', 'sent', '{}'::jsonb, %s + g * %s::bigint, %s + g * %s::bigint, %s + g * %s::bigint
          FROM generate_series(1, %s) AS g WHERE g %% 4 = 0
        """,
        (window_start, step, window_start, step, window_start, step, EVENTS),
    )
    conn.execute(
        """
        INSERT INTO news_event_reactions (event_id, symbol, metric_version, venue, venue_symbol,
                                          instrument_class, anchor_at_ms, p0, p0_at_ms, p1, p1_at_ms,
                                          p4, p4_at_ms, return_1h_bps, return_4h_bps, is_primary, state,
                                          created_at_ms, updated_at_ms)
        SELECT 'e-' || g, prefix || (g %% 500), %s, 'binance.perp', prefix || (g %% 500) || 'USDT', 'crypto',
               %s + g * %s::bigint, 100, %s + g * %s::bigint, 101,
               %s + g * %s::bigint + 3600000, 104, %s + g * %s::bigint + 14400000,
               (g %% 400) - 200, (g %% 800) - 400, true, 'complete', %s, %s
          FROM generate_series(1, %s) AS g, unnest(ARRAY['S', 'T']) AS prefix
        """,
        (
            REACTION_METRIC_VERSION,
            window_start,
            step,
            window_start,
            step,
            window_start,
            step,
            window_start,
            step,
            NOW,
            NOW,
            EVENTS,
        ),
    )
    conn.execute("ANALYZE news_event_reactions")
    conn.execute("ANALYZE news_events")
    conn.execute("ANALYZE news_verdicts")
    conn.commit()


def test_the_corpus_is_the_size_the_budget_was_written_for(seeded) -> None:
    rows = seeded.execute("SELECT count(*) AS n FROM news_event_reactions").fetchone()["n"]
    assert rows >= 100_000


def test_review_completes_under_the_serve_statement_timeout(seeded) -> None:
    """Both public windows complete against 100k rows before PostgreSQL's native timeout."""

    repos = repositories_for_connection(seeded)
    for hours in (24, 168):
        review = repos.price.review(hours=hours, now_ms=NOW)
        assert review["coverage"][0]["eligible_n"] > 0


def test_feed_attachment_completes_under_the_serve_statement_timeout(seeded) -> None:
    repos = repositories_for_connection(seeded)
    event_ids = [f"e-{index}" for index in range(1, 101)]
    aggregates = repos.price.event_reaction_aggregates(event_ids, now_ms=NOW)

    assert len(aggregates) == 100


def test_the_quote_read_stays_bounded_with_a_full_snapshot(seeded) -> None:
    repos = repositories_for_connection(seeded)
    quotes = [
        Quote(
            venue="binance.perp",
            venue_symbol=f"S{index}USDT",
            base_symbol=f"S{index}",
            price=1,  # type: ignore[arg-type]
            price_kind="last",
        )
        for index in range(QUOTE_TARGET_MAX)
    ]
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_instrument(f"S{index}") for index in range(QUOTE_TARGET_MAX)],
            now_ms=NOW,
        )
        repos.price.replace_source_snapshot(
            source_key="binance.perp",
            quotes=quotes,
            target_count=len(quotes),
            source_at_ms=NOW,
            received_at_ms=NOW,
            now_ms=NOW,
        )
    seeded.commit()
    symbols = [f"S{index}" for index in range(100)]
    results = repos.price.quotes_for_symbols(symbols, now_ms=NOW)

    assert len(results) == 100


def test_source_batch_persistence_is_the_reason_the_naive_design_was_rejected(seeded) -> None:
    """One successful source is one row replacement however many Events reference its quotes (#88 §14)."""

    repos = repositories_for_connection(seeded)
    before = seeded.execute("SELECT count(*) AS n FROM news_quote_snapshots").fetchone()["n"]
    with repos.transaction():
        for turn in range(10):
            repos.price.replace_source_snapshot(
                source_key="binance.perp",
                quotes=[
                    Quote(
                        venue="binance.perp",
                        venue_symbol=f"S{index}USDT",
                        base_symbol=f"S{index}",
                        price=1,  # type: ignore[arg-type]
                        price_kind="last",
                    )
                    for index in range(QUOTE_TARGET_MAX)
                ],
                target_count=QUOTE_TARGET_MAX,
                source_at_ms=NOW + turn,
                received_at_ms=NOW + turn,
                now_ms=NOW + turn,
            )
    seeded.commit()
    after = seeded.execute("SELECT count(*) AS n FROM news_quote_snapshots").fetchone()["n"]

    # Ten turns over 256 instruments: 2,560 rows under the rejected per-instrument design, 1 row here.
    assert after == before
    assert after <= 12  # the source-group ceiling, not the target count


def test_the_due_scan_and_review_stay_bounded_against_a_year_of_finished_rows(seeded) -> None:
    """The two reads that could grow silently: the due scan and the review window.

    The due scan walks Event-assets oldest-first and probes the Reaction key; a corpus where every row is
    already finished is its worst case, because nothing stops it early. That case must still be fast enough
    for a 60 s loop, and the review must ride its partial index rather than the whole table.
    """

    repos = repositories_for_connection(seeded)
    due = repos.price.due_reactions(now_ms=NOW, limit=100)
    assert isinstance(due, list)

    review_plan = "\n".join(
        row["QUERY PLAN"]
        for row in seeded.execute(
            """
            EXPLAIN SELECT event_id, count(*) FROM news_event_reactions
             WHERE metric_version = %s AND is_primary AND anchor_at_ms >= %s AND anchor_at_ms < %s
             GROUP BY event_id
            """,
            (REACTION_METRIC_VERSION, NOW - 168 * HOUR, NOW),
        ).fetchall()
    )
    assert "ix_news_reactions_review" in review_plan, review_plan


def _instrument(base: str) -> Any:
    from tracefold.news.market_review.instruments import Instrument

    return Instrument(
        venue="binance.perp",
        venue_symbol=f"{base}USDT",
        base_symbol=base,
        instrument_class="crypto",
        quote_asset="USDT",
    )
