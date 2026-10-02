"""Replay real ONNX backfill only in an owned, disposable PostgreSQL test clone.

Requires the locked dev environment, an already prepared local model cache, and
TRACEFOLD_TEST_POSTGRES_DSN naming the dedicated tracefold_test resource. No
operator config or production database is read. The clone is removed on exit.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import numpy as np
import yaml
from psycopg import sql

from tests.integration import test_news_claim_recall_latency as fixture
from tests.postgres_test_utils import (
    MigratedPostgresCloneFactory,
    connect_postgres_test,
    postgres_migration_test_dsn,
)
from tests.support.news_update_pg import Clock, adopted_head, store
from tracefold.cli import main as run_cli
from tracefold.news.claim_recall import CALIBRATION, vector_bytes
from tracefold.news.storage.claim_index import ClaimIndexStorage
from tracefold.news.updates.identity import digest


def _facts() -> dict[str, Any]:
    with closing(connect_postgres_test()) as conn:
        return {
            table: dict(
                conn.execute(
                    sql.SQL(
                        "SELECT count(*) AS rows,md5(string_agg(md5(to_jsonb(t)::text),'' "
                        "ORDER BY {},to_jsonb(t)::text)) AS digest "
                        "FROM {} t"
                    ).format(sql.Identifier(key), sql.Identifier(table))
                ).fetchone()
            )
            for table, key in (
                ("news_events", "event_id"),
                ("news_analyses", "analysis_id"),
                ("news_notifications", "intent_id"),
                ("news_jobs", "subject_id"),
            )
        }


def _verify_vectors(expected_count: int, started_at: datetime) -> dict[str, Any]:
    with closing(connect_postgres_test()) as conn:
        frozen = conn.execute(
            """SELECT c->>'ref' AS ref,c->>'statement' AS statement FROM news_analyses a
                 CROSS JOIN LATERAL jsonb_array_elements(a.document->'claims') c
                WHERE a.document IS NOT NULL
                UNION SELECT c->>'ref',c->>'statement' FROM news_notifications n
                 CROSS JOIN LATERAL jsonb_array_elements(n.sent_claims) c WHERE n.state='sent'"""
        ).fetchall()
        expected = {(row["ref"], digest(row["statement"])) for row in frozen}
        indexed = conn.execute("SELECT claim_ref,text_sha256,embedder,vector FROM news_claim_index").fetchall()
        actual = {(row["claim_ref"], row["text_sha256"]) for row in indexed}
        if len(expected) != expected_count or actual != expected:
            raise RuntimeError("benchmark_exact_versions_mismatch")
        if any(
            row["vector"] is None
            or row["embedder"] != CALIBRATION.embedder.key
            or len(row["vector"]) != CALIBRATION.embedder.dimensions * 2
            for row in indexed
        ):
            raise RuntimeError("benchmark_missing_or_incompatible_vector")
        matrix = np.stack([np.frombuffer(row["vector"], dtype="<f2").astype(np.float32) for row in indexed])
        norms = np.linalg.norm(matrix, axis=1)
        if not np.isfinite(matrix).all() or np.max(np.abs(norms - 1.0)) > 0.001:
            raise RuntimeError("benchmark_invalid_vector")
        analyzed_at = conn.execute(
            "SELECT last_analyze FROM pg_stat_user_tables WHERE relname='news_claim_index'"
        ).fetchone()["last_analyze"]
        if analyzed_at is None or analyzed_at < started_at:
            raise RuntimeError("benchmark_analyze_missing")
        return {
            "exact_versions": len(expected),
            "compatible_vectors": len(indexed),
            "max_norm_error": float(np.max(np.abs(norms - 1.0))),
            "last_analyze": analyzed_at.isoformat(),
            "analyze_completed": True,
            "schema_head": conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],
            "postgres_version": conn.execute("SHOW server_version").fetchone()["server_version"],
        }


class _CaptureQuery:
    """Capture the actual adapter statement without duplicating its paging SQL."""

    statement: str
    parameters: Any

    def execute(self, statement: str, parameters: Any, **_kwargs: Any) -> _CaptureQuery:
        self.statement, self.parameters = statement, parameters
        return self

    def fetchall(self) -> list[Any]:
        return []


def _plan_summary(plan: dict[str, Any], batch_size: int) -> dict[str, Any]:
    def nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield plan
        for child in plan.get("Plans", ()):
            yield from nodes(child)

    flattened = list(nodes(plan["Plan"]))
    documents = [node for node in flattened if node.get("Subplan Name") == "CTE documents"]
    if len(documents) != 1 or documents[0]["Actual Rows"] > batch_size:
        raise RuntimeError("benchmark_source_page_unbounded")
    scans = [node for node in flattened if node.get("Relation Name") in {"news_analyses", "news_notifications"}]
    return {
        "documents_actual_rows": documents[0]["Actual Rows"],
        "source_scan_rows_returned": sum(node["Actual Rows"] * node["Actual Loops"] for node in scans),
        "source_scan_rows_removed_by_filter": sum(
            node.get("Rows Removed by Filter", 0) * node["Actual Loops"] for node in scans
        ),
        "eligibility_json_calls": sum(node["Actual Loops"] for node in flattened if node.get("Alias") == "eligible"),
        "index_names": sorted({node["Index Name"] for node in flattened if "Index Name" in node}),
        "plan": plan,
    }


def _plans(as_of_ms: int, batch_size: int) -> dict[str, Any]:
    plans = {}
    with closing(connect_postgres_test(read_only=True)) as conn:
        for phase in ("sent", "adopted"):
            capture = _CaptureQuery()
            ClaimIndexStorage(capture).historical_batch(
                phase=phase, after=[0, "", 0], limit=batch_size, now_ms=as_of_ms
            )
            plan = conn.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + capture.statement, capture.parameters
            ).fetchone()["QUERY PLAN"][0]
            plans[phase] = _plan_summary(plan, batch_size)
    return plans


def _benchmark(args: argparse.Namespace) -> dict[str, Any]:
    server_dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN")
    if not server_dsn:
        raise ValueError("TRACEFOLD_TEST_POSTGRES_DSN must name the dedicated tracefold_test resource")
    factory = MigratedPostgresCloneFactory(server_dsn)
    try:
        with factory.clone() as clone_dsn, TemporaryDirectory(prefix="tracefold-embedding-benchmark-") as temporary:
            operator_home = Path(temporary)
            with patch.dict(os.environ, {"TRACEFOLD_TEST_POSTGRES_DSN": clone_dsn, "TRACEFOLD_HOME": temporary}):
                pg, _, clock = store(Clock(int(time.time() * 1000)))
                head = adopted_head(pg.semantic, clock)
                vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
                seeded_at = int(time.time() * 1000)
                with (
                    patch.object(fixture, "CURRENT_CLAIMS", args.current_claims),
                    patch.object(fixture, "SENT_CLAIMS", args.sent_claims),
                ):
                    fixture._seed_windows(head, seeded_at, vector)
                before = _facts()
                with closing(connect_postgres_test()) as conn, conn.transaction():
                    conn.execute("DELETE FROM news_claim_index")
                config = {
                    "storage": {"postgres": {"dsn": postgres_migration_test_dsn(clone_dsn), "password_file": None}},
                    "llm": {
                        "news_embedding": {
                            "model": CALIBRATION.embedder.model,
                            "cache_dir": str(args.cache_dir.resolve()),
                            "max_batch_size": 32,
                        }
                    },
                }
                (operator_home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
                checkpoint = operator_home / "backfill.json"
                output = io.StringIO()
                started_at = datetime.now(UTC)
                started = time.perf_counter()
                code = run_cli(
                    [
                        "news",
                        "embedding",
                        "backfill",
                        "--batch-size",
                        str(args.batch_size),
                        "--checkpoint",
                        str(checkpoint),
                    ],
                    stdout=output,
                )
                elapsed = time.perf_counter() - started
                result = json.loads(output.getvalue())
                if code != 0 or not result["ok"]:
                    raise RuntimeError(f"benchmark_backfill_failed:{result.get('error')}")
                expected = args.current_claims + args.sent_claims + len(head.claims)
                verified = _verify_vectors(expected, started_at)
                if _facts() != before:
                    raise RuntimeError("benchmark_backfill_changed_persisted_facts")
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                if saved["resume"]["phase"] != "done" or saved["resume"]["embedded"] != expected:
                    raise RuntimeError("benchmark_checkpoint_incomplete")
                data = {key: value for key, value in result["data"].items() if key != "checkpoint"}
                plans = _plans(saved["resume"]["as_of_ms"], args.batch_size)
                if min(args.current_claims, args.sent_claims) >= args.batch_size:
                    for phase, index_name in (
                        ("adopted", "news_analyses_adopted"),
                        ("sent", "news_notifications_sent"),
                    ):
                        page = plans[phase]
                        if (
                            index_name not in page["index_names"]
                            or page["source_scan_rows_returned"] > args.batch_size
                            or page["eligibility_json_calls"] > args.batch_size
                        ):
                            raise RuntimeError("benchmark_source_group_scan_unbounded")
                return {
                    "started_at_utc": started_at.isoformat(),
                    "environment": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "onnxruntime": version("onnxruntime"),
                        "tokenizers": version("tokenizers"),
                        "numpy": version("numpy"),
                    },
                    "scope": {
                        "current_fixture_claims": args.current_claims,
                        "frozen_sent_fixture_claims": args.sent_claims,
                        "bootstrap_claims": len(head.claims),
                        "statement_shape": "deterministic English tariff statements; one claim per source document",
                        "cache_prepared_before_run": True,
                        "index_deleted_before_run": True,
                        "database_page_size": args.batch_size,
                        "inference_batch_size": 32,
                        "inference_threads": 2,
                        "production_24h_observation": "not measured",
                        "resource_limits": "host run; container limits measured separately",
                    },
                    "wall_seconds_including_cli_model_load_and_close": elapsed,
                    "backfill": data,
                    "checkpoint": saved["resume"],
                    "source_page_plans": plans,
                    "verification": {**verified, "persisted_facts_unchanged": True, "scratch_database_removed": True},
                }
    finally:
        factory.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True, help="Already prepared offline model cache")
    parser.add_argument("--current-claims", type=int, default=17_500)
    parser.add_argument("--sent-claims", type=int, default=2_000)
    parser.add_argument("--batch-size", type=int, default=128, help="Database page size, 32 through 512")
    parser.add_argument("--output", type=Path, default=Path("artifacts/news-799-backfill.json"))
    args = parser.parse_args()
    if args.current_claims < 1 or args.sent_claims < 1:
        parser.error("Both fixture sizes must be positive")
    if not 32 <= args.batch_size <= 512:
        parser.error("Batch size must be 32 through 512")
    report = _benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "source_page_plans"}, indent=2, sort_keys=True
        )
    )
    print(f"Full source-page EXPLAIN plans saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
