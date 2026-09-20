"""Reproduce the isolated synthetic #664 PG receipt; run from the repository root.

PYTHONPATH=. TRACEFOLD_TEST_POSTGRES_DSN=<disposable test database> uv run python \
  docs/reports/issue-664-retrieval-reproduce.py

The existing clone factory validates the test DSN and creates/drops its own clone.
No production database, network source or model is used.
"""

import json
import os
import statistics
import time
from pathlib import Path

from psycopg import connect
from psycopg.rows import dict_row

from scripts.regen_db_schema import render_db_schema
from tests.integration.test_news_evidence_material import admit
from tests.postgres_test_utils import MigratedPostgresCloneFactory
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.evidence import EVIDENCE_INPUT_VERSION, EVIDENCE_SELECTION_SHA256, EvidenceQuery, text_sha
from tracefold.news.models import MarketAsset
from tracefold.news.storage.evidence import BACKGROUND_CANDIDATES_SQL, background_parameters

factory = MigratedPostgresCloneFactory(os.environ["TRACEFOLD_TEST_POSTGRES_DSN"])
try:
    with factory.clone() as dsn:
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = dsn
        with connect(dsn, row_factory=dict_row, autocommit=True) as conn:
            repos = repositories_for_connection(conn)
            stamp = int(time.time() * 1000)
            batch = admit(repos, "BTC acquisition agreement announced with approval pending.", stamp=stamp - 10000)
            sample = batch.results[0].event_id
            conn.execute(
                """INSERT INTO news_items
        SELECT (jsonb_populate_record(NULL::news_items, to_jsonb(template)|| jsonb_build_object(
            'item_id', md5('item'||n)::text||md5('item'||n)::text,
            'source_artifact_id', md5('artifact'||n)::text||md5('artifact'||n)::text,
            'source_item_key', 'bench-'||n, 'canonical_url','https://example.org/bench/'||n,
            'title', CASE WHEN n%%100=0 THEN 'BTC acquisition agreement approval pending '||n
                         ELSE 'Company '||n||' routine quarterly update industrial services' END,
            'provider_params_available_at_ms', %s-n::bigint*90000))).*
         FROM news_items template CROSS JOIN generate_series(1,25000) n WHERE template.item_id=%s""",
                (stamp, batch.item_id),
            )
            columns = [
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='news_events' AND is_generated='NEVER' ORDER BY ordinal_position"
                ).fetchall()
            ]
            from psycopg import sql

            event_columns = sql.SQL(",").join(map(sql.Identifier, columns))
            conn.execute(
                sql.SQL("INSERT INTO news_events (")
                + event_columns
                + sql.SQL(") SELECT ")
                + sql.SQL(",").join(sql.Identifier("expanded", c) for c in columns)
                + sql.SQL(""" FROM (SELECT (jsonb_populate_record(
              NULL::news_events, to_jsonb(template)|| jsonb_build_object(
            'event_id', md5('event'||n)::text||md5('event'||n)::text,
            'leader_item_id',md5('item'||n)::text||md5('item'||n)::text,
            'comparison_fingerprint',md5(n::text)::text||md5(n::text)::text,
            'comparison_title', CASE WHEN n%%100=0 THEN 'BTC acquisition agreement approval pending '||n
                                    ELSE 'Company '||n||' routine quarterly update industrial services' END,
            'created_at_ms',%s-n::bigint*90000))).*
          FROM news_events template CROSS JOIN generate_series(1,25000) n WHERE template.event_id=%s) expanded"""),
                (stamp, sample),
            )
            conn.execute(
                "INSERT INTO news_event_assets(event_id,symbol,opened_at_ms) "
                "SELECT event_id, 'BTC', opened_at_ms FROM news_events WHERE event_id<>%s",
                (sample,),
            )
            conn.execute("ANALYZE news_items")
            conn.execute("ANALYZE news_events")
            conn.execute("ANALYZE news_event_assets")
            query = EvidenceQuery(
                event_id=sample,
                focus_fact_id="benchmark",
                title="BTC acquisition agreement approved regulator execution pending",
                assets=(MarketAsset("BTC", "crypto"),),
                terms=("acquisition", "agreement", "approved", "regulator", "execution", "pending"),
                cutoff_at_ms=stamp,
            )
            params = background_parameters(query)
            plan = conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + BACKGROUND_CANDIDATES_SQL, params
            ).fetchone()["QUERY PLAN"]
            times = []
            for _ in range(25):
                begin = time.perf_counter()
                rows = conn.execute(BACKGROUND_CANDIDATES_SQL, params).fetchall()
                times.append((time.perf_counter() - begin) * 1000)
            result = dict(
                query_sha256=text_sha(BACKGROUND_CANDIDATES_SQL),
                selector_sha256=EVIDENCE_SELECTION_SHA256,
                input_version=EVIDENCE_INPUT_VERSION,
                workload="synthetic 25,001 editorial Items/Events, 90-second spacing over 26 days, "
                "1% matching titles; all share BTC as adversarial asset noise",
                postgres=conn.execute("SELECT version() AS version").fetchone()["version"],
                samples=len(times),
                candidate_count=len(rows),
                p50_ms=statistics.median(times),
                p95_ms=sorted(times)[23],
                plan=plan,
                timings_ms=times,
            )
            Path(os.environ.get("TRACEFOLD_RETRIEVAL_REPORT", "docs/reports/issue-664-retrieval-plan.json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
            print({k: v for k, v in result.items() if k not in ("plan", "timings_ms")})
            Path("docs/generated/db-schema.md").write_text(render_db_schema())
finally:
    factory.close()
