"""Related-Event recall stays index-driven at production scale, with the window-wide scan's asset matching (#771).

#761's recall evaluated every Event of the 30-day window per call. The query audit only EXPLAINs route reads, so
nothing noticed until production overran the Workers' 3 s statement timeout. These tests run the real statement
under EXPLAIN ANALYZE on a seeded window of production size and hold the plan to bounded, index-driven reads.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tracefold.news.entities import ADDRESS_PATTERN, CRYPTO_QUOTE_SUFFIXES, RELATED_ASSET_ALIASES
from tracefold.news.evidence import BACKGROUND_WINDOW_MS, CANDIDATE_MAX, query_for
from tracefold.news.models import MarketAsset
from tracefold.news.storage.evidence import BACKGROUND_CANDIDATES_SQL, background_parameters

pytestmark = pytest.mark.integration

NOW = 1_790_000_000_000
EVENTS = 20_000  # production held 13k recall-eligible Events of 40k in the window (2026-10-01)
TAGGED_EVERY = 7  # one Event in seven carries the probed asset, like CL in production
RECALL_TABLES = ("news_events", "news_items", "news_event_members", "news_event_assets")


def _seed_window(conn: Any) -> None:
    # Headlines over a small shared vocabulary, so trigram posting lists are as long as real ones.
    conn.execute(
        """
        WITH words AS (SELECT ARRAY['oil','gold','tariff','iran','trump','fed','rate','bitcoin','etf','china',
                                    'exports','supply','talks','price','record','bank','shares','deal','strike',
                                    'opec','yields','inflation','sanctions','output','demand'] AS w),
        seeded AS (
          SELECT n, %(now)s::bigint - (n::bigint * %(span)s::bigint / %(events)s) AS at_ms,
                 initcap(w[1 + n %% 25]) || ' ' || w[1 + (n / 25) %% 25] || ' ' || w[1 + (n / 625) %% 25] || ' '
                   || substr(md5(n::text), 1, 6) || ' as ' || w[1 + (n * 7) %% 25] || ' ' || w[1 + (n * 11) %% 25]
                   AS headline
            FROM generate_series(1, %(events)s) n, words
        ), items AS (
          INSERT INTO news_items (item_id, source_id, source_item_key, title, canonical_url, published_at_ms,
                                  observed_at_ms, first_ingest_mode, created_at_ms, updated_at_ms,
                                  source_artifact_id, provider_params_available_at_ms)
          SELECT 'it-' || n, 'opennews', 'k-' || n, headline, 'https://example.org/' || n, at_ms, at_ms, 'live',
                 at_ms, at_ms, 'art-' || n, at_ms
            FROM seeded
        ), events AS (
          INSERT INTO news_events (event_id, leader_item_id, dedupe_family, comparison_fingerprint,
                                   comparison_title, leader_title, opened_at_ms, last_member_at_ms, expires_at_ms,
                                   admission, ingest_mode, created_at_ms, updated_at_ms, focus_fact_id,
                                   focus_fact_text, focus_fact_method, event_kind)
          SELECT 'ev-' || n, 'it-' || n, 'news', 'fp-' || n, lower(headline), headline, at_ms, at_ms,
                 at_ms + 86400000, 'candidate', 'live', at_ms, at_ms, 'fact-' || n, headline, 'whole_item', 'news'
            FROM seeded
        ), members AS (
          INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
          SELECT 'ev-' || n, 'it-' || n, at_ms, 'leader', 'fact-' || n, headline FROM seeded
          UNION ALL
          SELECT 'ev-' || n, 'it-' || n, at_ms, 'exact', 'fact-x-' || n, headline || ', sources say'
            FROM seeded WHERE n %% 2 = 0
        )
        INSERT INTO news_event_assets (symbol, event_id, opened_at_ms)
        SELECT CASE WHEN n %% %(tagged)s = 0 THEN 'CL' ELSE 'TOK' || (n %% 997) END, 'ev-' || n, at_ms
          FROM seeded
        """,
        {"now": NOW, "span": BACKGROUND_WINDOW_MS - 3_600_000, "events": EVENTS, "tagged": TAGGED_EVERY},
    )
    conn.execute("ANALYZE news_items, news_events, news_event_members, news_event_assets")


# A long task text (an article body); its trigram set is as large as a real article's.
_LONG_ARTICLE = " ".join(
    f"Paragraph {n} on supply route {hashlib.sha256(str(n).encode()).hexdigest()[:10]}." for n in range(80)
)


def _nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", ()):
        yield from _nodes(child)


def test_recall_reads_bounded_candidates_through_indexes_at_production_scale(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        _seed_window(conn)
        # A task text close to one stored headline (and to the dozens sharing its first words).
        headline = conn.execute("SELECT title FROM news_items WHERE item_id = 'it-42'").fetchone()["title"]
        query = query_for(
            event_id="ev-new",
            cutoff=NOW + 1,
            task_texts=(headline + " today", _LONG_ARTICLE),
            source_items=({"source_artifact_id": "art-42", "canonical_url": "https://example.org/42"},),
            assets=(MarketAsset("CL", "unknown"),),
        )
        params = background_parameters(query)
        conn.execute("SET jit = off")
        conn.execute("SET max_parallel_workers_per_gather = 0")
        plan = conn.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + BACKGROUND_CANDIDATES_SQL, params, prepare=False
        ).fetchone()["QUERY PLAN"][0]
        rows = conn.execute(BACKGROUND_CANDIDATES_SQL, params, prepare=False).fetchall()
        # The similarity probe's own matches; on this small vocabulary there are about a thousand.
        similar = conn.execute(
            "SELECT count(DISTINCT m.event_id) AS n FROM unnest(%s::text[]) p(text)"
            " JOIN news_event_members m ON m.fact_text %% p.text",
            (params["probe_texts"],),
        ).fetchone()["n"]

    reasons = {row["retrieval_reason"] for row in rows}
    assert reasons == {"explicit_origin", "entity_event_terms", "text_similarity"}
    nodes = list(_nodes(plan["Plan"]))
    # Every channel starts from an index; none walks a whole table.
    assert [node["Relation Name"] for node in nodes if node["Node Type"] == "Seq Scan"] == []
    # The window-wide scan read every window Event in each of its three channels. Reads of Events are now bounded
    # by what the indexes return -- the tagged Events (one in seven), the probe's matches and the explicit and
    # entity pools -- not by the window.
    tagged = EVENTS // TAGGED_EVERY
    event_reads = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in nodes
        if node.get("Relation Name") == "news_events"
    )
    assert event_reads <= tagged + similar + 3 * CANDIDATE_MAX < EVENTS // 2, (event_reads, similar)
    # Far inside the Workers' 3 s statement timeout; the production copy measured p95 ~270 ms, max ~450 ms.
    assert plan["Execution Time"] < 2_000, plan["Execution Time"]


# The #761 statement's per-row normalisation, kept as the oracle for the generated retrieval columns.
_WINDOW_SCAN_ASSET_MATCH = """
    SELECT a.event_id, a.symbol FROM news_event_assets a
      CROSS JOIN LATERAL (SELECT regexp_replace(a.symbol, '^[[:space:]$]+|[[:space:]]+$', '', 'g') AS text) tagged
      CROSS JOIN LATERAL (
        SELECT CASE WHEN tagged.text ~ %(address_pattern)s THEN tagged.text
          ELSE regexp_replace(regexp_replace(upper(tagged.text), '^XYZ-', ''), '^[^:]*:', '')
          END AS symbol
      ) normalized
     WHERE normalized.symbol = ANY(%(symbols)s)
        OR COALESCE(%(aliases)s::jsonb ->> normalized.symbol, normalized.symbol) = ANY(%(symbols)s)
        OR (COALESCE(a.market_type, 'unknown') IN ('crypto', 'unknown')
            AND tagged.text !~ %(address_pattern)s AND (
              SELECT left(normalized.symbol, length(normalized.symbol) - length(quote))
                FROM unnest(%(quotes)s::text[]) WITH ORDINALITY quotes(quote, rank)
               WHERE right(normalized.symbol, length(quote)) = quote
                 AND length(normalized.symbol) > length(quote) + 1
               ORDER BY rank LIMIT 1
            ) = ANY(%(symbols)s))
"""
_INDEXED_ASSET_MATCH = """
    SELECT a.event_id, a.symbol FROM news_event_assets a
     WHERE a.retrieval_symbol = ANY(%(stored_codes)s::text[]) OR a.retrieval_pair_base = ANY(%(symbols)s::text[])
"""
_STORED_TAGS = (
    ("BTC", None),
    (" $btc ", None),
    ("btcusdt", "crypto"),
    ("BTCUSDT", "equity"),
    ("binance:BTCUSDT", None),
    ("XYZ-BTC", None),
    ("xyz-cl", None),
    ("hl.xyz:CL", "unknown"),
    ("BTCBUSD", "crypto"),
    ("ABCFDUSD", "crypto"),
    ("USDT", None),
    ("XUSDT", None),
    ("AUSD", None),
    ("PYUSD", None),
    ("SIUSDT", None),
    ("SI", "commodity"),
    ("XAU", None),
    ("XAUT", "crypto"),
    ("GOLD", "commodity"),
    ("SKHX", "equity"),
    ("SKHYNIX", None),
    ("1810.hk", None),
    ("0xAbCdEf0123456789abcdef0123456789ABCDEF01", None),
    ("x:0xabcdef0123456789abcdef0123456789abcdef01", None),
    ("So11111111111111111111111111111111111111112", "crypto"),
    ("solana:So11111111111111111111111111111111111111112", None),
)
_QUERY_ASSETS = (
    MarketAsset("BTC", "crypto"),
    MarketAsset("BTC", "unknown"),
    MarketAsset("CL", "unknown"),
    MarketAsset("XYZ-CL", "unknown"),
    MarketAsset("GOLD", "commodity"),
    MarketAsset("XAU", "unknown"),
    MarketAsset("SI", "crypto"),
    MarketAsset("ABC", "crypto"),
    MarketAsset("SKHY", "equity"),
    MarketAsset("HK1810", "equity"),
    MarketAsset("USD", "fx"),
    MarketAsset("0xabcdef0123456789abcdef0123456789abcdef01", "crypto"),
    MarketAsset("So11111111111111111111111111111111111111112", "crypto"),
)


def test_generated_retrieval_codes_match_the_window_scan_asset_normalisation(postgres_clone_dsn):
    """Edge whitespace, `$`, case, `XYZ-`, venue prefixes, first quote suffix, addresses and catalogue aliases.

    The oracle reads `ADDRESS_PATTERN`, `CRYPTO_QUOTE_SUFFIXES` and `RELATED_ASSET_ALIASES` from the code, while the
    columns are generated by the database: changing any of them without a revision that redefines the columns
    fails here.
    """

    matched = 0
    with closing(connect_postgres_test(read_only=False)) as conn:
        _seed_window_for_tags(conn)
        for asset in _QUERY_ASSETS:
            params = background_parameters(query_for(event_id="q", cutoff=NOW, task_texts=("t",), assets=(asset,)))
            oracle = {
                (row["event_id"], row["symbol"])
                for row in conn.execute(
                    _WINDOW_SCAN_ASSET_MATCH,
                    {
                        **params,
                        "aliases": Jsonb(RELATED_ASSET_ALIASES),
                        "quotes": list(CRYPTO_QUOTE_SUFFIXES),
                        "address_pattern": ADDRESS_PATTERN,
                    },
                ).fetchall()
            }
            indexed = {
                (row["event_id"], row["symbol"]) for row in conn.execute(_INDEXED_ASSET_MATCH, params).fetchall()
            }
            assert indexed == oracle, asset
            matched += len(oracle)
    # Not vacuous: BTC alone matches six spellings (plain, `$`, `XYZ-`, venue prefix and two quote pairs).
    assert matched >= 20, matched


def _seed_window_for_tags(conn: Any) -> None:
    for index, (symbol, market_type) in enumerate(_STORED_TAGS):
        values = {"item": f"it-tag-{index}", "event": f"ev-tag-{index}", "now": NOW}
        conn.execute(
            """
            INSERT INTO news_items (item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
                                    first_ingest_mode, created_at_ms, updated_at_ms)
            VALUES (%(item)s, 'opennews', %(item)s, 't', %(now)s, %(now)s, 'live', %(now)s, %(now)s)
            """,
            values,
        )
        conn.execute(
            """
            INSERT INTO news_events (event_id, leader_item_id, dedupe_family, comparison_fingerprint,
                                     comparison_title, leader_title, opened_at_ms, last_member_at_ms, expires_at_ms,
                                     admission, ingest_mode, created_at_ms, updated_at_ms, focus_fact_id,
                                     focus_fact_method, event_kind)
            VALUES (%(event)s, %(item)s, 'news', %(event)s, 't', 't', %(now)s, %(now)s, %(now)s, 'candidate',
                    'live', %(now)s, %(now)s, 'f', 'whole_item', 'news')
            """,
            values,
        )
        conn.execute(
            "INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms) VALUES (%s, %s, %s, %s)",
            (symbol, values["event"], market_type, NOW),
        )
