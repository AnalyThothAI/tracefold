"""Price Review capacity gates (#88 §14) under the native Serve statement timeout.

Marked slow: it seeds six figures of rows and belongs in the slow integration lane.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_review.pricing import (
    QUOTE_TARGET_MAX,
    REVIEW_MAX_HOURS,
    Quote,
    QuoteRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

NOW = 1_787_000_000_000
HOUR = 3_600_000
EVENTS = 50_000
ASSETS_PER_EVENT = 2
UPDATE_BATCH = 5_000


@pytest.fixture(scope="module")
def seeded(postgres_module_clone_dsn: str):
    conn = connect_postgres_test(read_only=False)
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
    """One window of Events, adopted updates and two Reactions each — 100k rows, set-based."""

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
        INSERT INTO news_events (event_id, leader_item_id, dedupe_family, event_kind,
                                 comparison_fingerprint, comparison_title,
                                 leader_title, focus_fact_id, focus_fact_text, focus_fact_context,
                                 focus_fact_method, focus_span_start, focus_span_end,
                                 opened_at_ms, last_member_at_ms, expires_at_ms, admission,
                                 storyline_key, ingest_mode, created_at_ms, updated_at_ms)
        SELECT 'e-' || g, 'i-' || g, 'general', 'news', 'f-' || g, 'c', 'leader ' || g, 'fact:' || g,
               'leader ' || g, '', 'whole_item', 0, length('leader ' || g),
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
    conn.commit()
    update_sql = """
        INSERT INTO news_analyses
          (event_id,content_revision,input_revision,adopted_at_ms,analysis_id,document,origin,work_id,
           input_sha256,program_identity,completed_at_ms,understanding,update_ref)
        SELECT 'e-' || g, repeat('a',64), 1, %s + g * %s::bigint, 'result:e-' || g,
               jsonb_build_object(
                 'schema_version','news_event_update_v2', 'event_id','e-' || g,
                 'content_revision',repeat('a',64), 'input_revision',1,
                 'previous_content_revision',NULL,
                 'claims',jsonb_build_array(jsonb_build_object(
                   'ref','cl:e-' || g,
                   'fields',jsonb_build_object('assets',jsonb_build_array(
                     jsonb_build_object('symbol','S' || (g %% 500),'market_type','crypto_perp','role','primary'),
                     jsonb_build_object('symbol','T' || (g %% 500),'market_type','crypto_perp','role','primary')
                   ))))),
               'semantic','work:e-'||g,repeat('a',64),'price-scale-fixture',
               %s + g * %s::bigint,'{}'::jsonb,
               'update:'||encode(sha256(convert_to(news_canonical_jsonb(
                 jsonb_build_array('e-'||g,repeat('a',64))),'UTF8')),'hex')
          FROM generate_series(%s::integer,%s::integer) AS g
    """
    for batch_start in range(1, EVENTS + 1, UPDATE_BATCH):
        batch_end = min(EVENTS, batch_start + UPDATE_BATCH - 1)
        conn.execute(update_sql, (window_start, step, window_start, step, batch_start, batch_end))
        conn.execute(
            "UPDATE news_events e SET current_analysis_id=a.analysis_id FROM news_analyses a "
            "WHERE a.event_id=e.event_id AND e.current_analysis_id IS NULL"
        )
        conn.commit()
    conn.execute("ANALYZE news_events")
    conn.execute("ANALYZE news_analyses")
    conn.commit()


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
    results = repos.price.quotes_for_symbols([QuoteRequest(symbol) for symbol in symbols], now_ms=NOW)

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


def _instrument(base: str) -> Any:
    from tracefold.news.market_review.instruments import Instrument

    return Instrument(
        venue="binance.perp",
        venue_symbol=f"{base}USDT",
        base_symbol=base,
        instrument_class="crypto",
        quote_asset="USDT",
    )
