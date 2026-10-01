"""The migration tree is one irreversible baseline plus ordered hard cuts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.postgres_test_utils import connect_postgres_test, prepare_test_migration_database
from tests.postgres_test_utils import postgres_migration_test_dsn as postgres_test_dsn
from tests.postgres_test_utils import test_postgres_dsn as admin_postgres_test_dsn
from tests.support.news_legacy import LEGACY_TRIAGE_POLICY_VERSION
from tests.support.news_legacy_storage import _persist_triage_verdict, legacy_intent_id, legacy_news
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.oi_signals import parse_oi_signal
from tracefold.news.smart_money import PARSER_VERSION
from tracefold.news.smart_money import source_key as smart_money_source_key
from tracefold.news.source_contracts import MARKET_CATEGORY_CONFLICT, classify_source_contracts, market_route
from tracefold.news.storage.wallet_snapshots import wallet_snapshot
from tracefold.news.wallet_contracts import NetBuySnapshot
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "tracefold" / "platform" / "postgres" / "alembic" / "versions"
BASELINE = "20260831_0340"
HEAD = "20261001_0421"
PRE_CUT = "20260928_0410"
# The revision before the smart-money reparse: what `20260905_0365` left behind, before `20260906_0370`
# ran the production parser over it.
BEFORE_REPARSE = "20260906_0369"
# The reparse itself. Named rather than reached through `HEAD`, because the one test that replays it
# asks for exactly that revision a second time -- and the revisions in front of it create tables, which
# a second run of them would not survive.
REPARSE = "20260906_0370"


def _config():
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    return config


def _empty_the_schema() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()
    prepare_test_migration_database(admin_postgres_test_dsn())


def _table_exists(name: str) -> bool:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute("SELECT to_regclass(%s) AS table_name", (f"public.{name}",)).fetchone()
        return row is not None and row["table_name"] is not None
    finally:
        conn.close()


def _stamped_revision() -> str | None:
    conn = connect_postgres_test(read_only=False)
    try:
        if conn.execute("SELECT to_regclass('alembic_version') AS table_name").fetchone()["table_name"] is None:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return None if row is None else str(row["version_num"])
    finally:
        conn.close()


def test_the_one_window_rewrite_keeps_the_surviving_window_and_marks_the_new_facts_unknown() -> None:
    """#649 PR-3: stored snapshots move to the one-window shape with the code that reads them.

    The snapshot model forbids unknown fields and every reader validates through it, so a row left in
    the `fast`/`slow` shape would raise on the next turn that touched its episode. What survives is
    the 30-minute window exactly as it was -- the rule that still exists -- and the two facts that
    were never recorded for it are `null` rather than a fabricated zero.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260915_0381")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        member = {
            "wallet": "0x" + "a" * 40,
            "handle": "buyer",
            "rank_quality": 1,
            "roster_version": 3,
            "roster_known_at_ms": at_ms - 3_600_000,
            "monitoring_from_ms": at_ms - 3_600_000,
            "source_closed_trades": 20,
            "source_profit_factor": "1.8",
            "buy_usd": "2000",
            "sell_usd": "0",
            "net_usd": "2000",
            "buy_token_raw": "2000",
            "sell_token_raw": "0",
            "net_token_raw": "2000",
            "unpriced_count": 0,
            "transfer_out_count": 0,
            "qualified": True,
            "reasons": [],
        }
        window = {
            "from_ms": at_ms - 1_800_000,
            "to_ms": at_ms,
            "required_n": 5,
            "qualified_n": 1,
            "buy_usd": "2000",
            "sell_usd": "0",
            "net_usd": "2000",
            "matched": True,
            "members": [member],
        }
        stored = {
            "chain_id": 4663,
            "token": "0x" + "b" * 40,
            "token_symbol": "XYZ",
            "token_decimals": 18,
            "cutoff_at_ms": at_ms,
            "cutoff_block": 1000,
            "cutoff_log": 7,
            "roster_version": 3,
            "min_net_buy_usd": "1000",
            "coverage_from_ms": at_ms - 3_600_000,
            "coverage_gap_at_ms": None,
            "fast": {**window, "window": "5m", "from_ms": at_ms - 300_000, "required_n": 3},
            "slow": {**window, "window": "30m"},
        }
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              'two-window-episode', 'news-robinhood-chain', 'two-window-episode', 'XYZ', '', '',
              'robinhood_chain', %(at)s, %(at)s, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s
            )
            """,
            {"at": at_ms},
        )
        conn.execute(
            """
            INSERT INTO news_market_wallet_events (
              item_id, chain_id, token, token_symbol, trigger_tx_hash, event_at_ms, received_at_ms,
              detected_at_ms, last_effective_buy_at_ms, initial_snapshot, latest_snapshot,
              send_snapshot, latest_matched, change_reason, updated_at_ms, trigger_max_age_s,
              notification_eligible
            ) VALUES (
              'two-window-episode', 4663, %(token)s, 'XYZ', %(tx)s, %(at)s, %(at)s, %(at)s, %(at)s,
              %(snapshot)s::jsonb, %(snapshot)s::jsonb, %(snapshot)s::jsonb, true, 'triggered',
              %(at)s, 60, true
            )
            """,
            {"token": stored["token"], "tx": "0x" + "c" * 64, "at": at_ms, "snapshot": json.dumps(stored)},
        )
        conn.commit()

        command.upgrade(config, "20261001_0419")

        row = conn.execute(
            "SELECT initial_snapshot, latest_snapshot, send_snapshot FROM news_market_wallet_events"
            " WHERE item_id = 'two-window-episode'"
        ).fetchone()
        for column in ("initial_snapshot", "latest_snapshot", "send_snapshot"):
            snapshot = NetBuySnapshot.model_validate(wallet_snapshot(row[column]))
            assert snapshot.window.from_ms == at_ms - 1_800_000 and snapshot.window.required_n == 5
            assert snapshot.window.matched and snapshot.window.qualified_n == 1
            assert snapshot.token_first_seen_at_ms is None and snapshot.token_age_ms is None
            assert [member.recent_episodes for member in snapshot.window.members] == [None]
            assert "fast" not in row[column] and "slow" not in row[column]
    finally:
        conn.close()


def test_migration_tree_is_one_root_and_head_in_the_flat_package() -> None:
    script = ScriptDirectory.from_config(_config())
    revisions = list(script.walk_revisions())

    assert Path(script.dir).resolve() == VERSIONS.parent.resolve()
    assert [revision.revision for revision in revisions] == [
        HEAD,
        "20261001_0420",
        "20261001_0419",
        "20260929_0418",
        "20260929_0417",
        "20260929_0416",
        "20260929_0415",
        "20260929_0413",
        "20260928_0412",
        "20260928_0411",
        PRE_CUT,
        "20260928_0409",
        "20260928_0408",
        "20260927_0407",
        "20260927_0406",
        "20260927_0405",
        "20260926_0404",
        "20260926_0403",
        "20260926_0402",
        "20260925_0401",
        "20260925_0400",
        "20260925_0399",
        "20260925_0398",
        "20260925_0397",
        "20260924_0396",
        "20260924_0395",
        "20260924_0394",
        "20260924_0393",
        "20260923_0392",
        "20260923_0391",
        "20260923_0390",
        "20260922_0389",
        "20260922_0388",
        "20260922_0387",
        "20260922_0386",
        "20260920_0385",
        "20260919_0384",
        "20260919_0383",
        "20260918_0382",
        "20260915_0381",
        "20260915_0380",
        "20260915_0379",
        "20260915_0378",
        "20260912_0377",
        "20260912_0376",
        "20260908_0375",
        "20260907_0374",
        "20260906_0373",
        "20260906_0372",
        "20260906_0371",
        "20260906_0370",
        "20260906_0369",
        "20260906_0368",
        "20260905_0367",
        "20260905_0366",
        "20260905_0365",
        "20260905_0364",
        "20260904_0363",
        "20260904_0362",
        "20260904_0361",
        "20260904_0360",
        "20260903_0359",
        "20260903_0358",
        "20260903_0357",
        "20260903_0356",
        "20260903_0355",
        "20260903_0354",
        "20260903_0353",
        "20260903_0352",
        "20260902_0351",
        "20260902_0350",
        "20260902_0349",
        "20260902_0348",
        "20260901_0347",
        "20260901_0346",
        "20260901_0345",
        "20260901_0344",
        "20260901_0343",
        "20260901_0342",
        "20260901_0341",
        BASELINE,
    ]
    # `walk_revisions` follows `down_revision` from head to root, so the ordered ids above are the
    # chain itself — a broken or forked link changes that list. Restating each link beside it cost
    # one edit per migration and only ever asserted the walk against itself.
    assert revisions[-1].down_revision is None

    # One file per revision, named by it. A dropped or extra file fails here the same way a broken
    # link fails above, and neither costs an edit when a migration is added.
    assert sorted("_".join(path.stem.split("_")[:2]) for path in VERSIONS.glob("*.py")) == sorted(
        revision.revision for revision in revisions
    )


def test_task_read_cut_preserves_history_without_promoting_source_only_completion() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260928_0408")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            _seed_pre_cut_oi_event(
                conn, event_id="ev-read-cut", leader_item="it-read-a", member_item="it-read-b", at_ms=100
            )
            conn.execute(
                "INSERT INTO news_semantic_work "
                "(event_id,wanted_revision,done_revision,processed_evidence_refs,lineage_id,"
                "next_attempt_at_ms,updated_at_ms) "
                "VALUES ('ev-read-cut',1,1,ARRAY['evidence:old'],'lineage-read',100,100)"
            )
            conn.execute(
                "INSERT INTO news_semantic_observations "
                "(result_id,work_id,event_id,input_revision,input_sha256,program_identity,"
                "completed_at_ms,understanding,evidence_refs) "
                "VALUES ('result-read','work-read','ev-read-cut',1,repeat('a',64),'original',100,"
                "'{}'::jsonb,ARRAY['evidence:old'])"
            )
        command.upgrade(config, PRE_CUT)
        work = conn.execute(
            "SELECT wanted_revision,done_revision,processed_read_refs FROM news_semantic_work "
            "WHERE event_id='ev-read-cut'"
        ).fetchone()
        observation = conn.execute(
            "SELECT evidence_refs,read_refs FROM news_semantic_observations WHERE result_id='result-read'"
        ).fetchone()
        assert work == {"wanted_revision": 1, "done_revision": 1, "processed_read_refs": []}
        assert observation == {"evidence_refs": ["evidence:old"], "read_refs": []}
        assert (
            conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name='news_semantic_work' "
                "AND column_name='processed_evidence_refs'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_notification_terminal_cut_fails_overdue_exhausted_work_and_drops_the_plan_column() -> None:
    """#742 0413: work the old code exhausted and never picked up again becomes visible `failed` work."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260928_0412")
    conn = connect_postgres_test(read_only=False)
    now_ms = int(time.time() * 1000)
    try:
        with conn.transaction():
            for index, (attempts, due_ms) in enumerate(((3, 100), (3, now_ms + 600_000), (1, 100))):
                event_id = f"ev-work-{index}"
                conn.execute(
                    "INSERT INTO news_items (item_id,source_id,source_item_key,title,raw_first_line,description,"
                    "canonical_url,reporting_origin,published_at_ms,observed_at_ms,provider_metadata,provenance,"
                    "first_ingest_mode,trace_id,created_at_ms,updated_at_ms,source_artifact_id,evidence_text,"
                    "evidence_text_sha256) VALUES (%s,'opennews',%s,'t','t','','https://x.test','R',100,100,"
                    "'{}'::jsonb,'[]'::jsonb,'live','trace',100,100,%s,'t',repeat('b',64))",
                    (f"it-{event_id}", f"it-{event_id}", f"it-{event_id}"),
                )
                conn.execute(
                    "INSERT INTO news_events (event_id,leader_item_id,dedupe_family,comparison_fingerprint,"
                    "comparison_title,leader_title,opened_at_ms,last_member_at_ms,expires_at_ms,admission,"
                    "ingest_mode,trace_id,created_at_ms,updated_at_ms,focus_fact_id,focus_fact_text,"
                    "focus_fact_context,focus_fact_method,focus_span_start,focus_span_end,event_kind) "
                    "VALUES (%s,%s,'general','fp','t','t',100,100,200,'candidate','live','trace',100,100,"
                    "'fact','t','','whole_item',0,1,'news')",
                    (event_id, f"it-{event_id}"),
                )
                conn.execute(
                    "INSERT INTO news_notification_work "
                    "(event_id,channel,content_revision,state,attempts,next_attempt_at_ms,updated_at_ms) "
                    "VALUES (%s,'news',repeat('a',64),'pending',%s,%s,100)",
                    (event_id, attempts, due_ms),
                )
        command.upgrade(config, HEAD)
        rows = conn.execute(
            "SELECT event_id,state,attempts,last_error_code FROM news_notification_work ORDER BY event_id"
        ).fetchall()
        assert [(row["state"], row["last_error_code"]) for row in rows] == [
            ("failed", "news_notification_exhausted_legacy"),
            # Exhausted but not yet due: the new code plans it once more; its next failure fails it.
            ("pending", None),
            ("pending", None),
        ]
        assert (
            conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name='news_notification_work' "
                "AND column_name='plan'"
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE news_notification_work SET state='failed', last_error_code=NULL")
    finally:
        conn.close()


def test_claim_links_are_backfilled_from_every_stored_update_and_reader_decisions_are_accepted() -> None:
    """#742 0416: every stored change that compares a claim with an earlier one becomes a claim link."""

    from tests.support.news_event_updates import first_update, persist_update, raised_update

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260929_0415")
    conn = connect_postgres_test(read_only=False)
    first = first_update("ev-links")
    raised = raised_update(first)
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO news_items (item_id,source_id,source_item_key,title,raw_first_line,description,"
                "canonical_url,reporting_origin,published_at_ms,observed_at_ms,provider_metadata,provenance,"
                "first_ingest_mode,trace_id,created_at_ms,updated_at_ms,source_artifact_id,evidence_text,"
                "evidence_text_sha256) VALUES ('it-links','opennews','it-links','t','t','','https://x.test','R',"
                "100,100,'{}'::jsonb,'[]'::jsonb,'live','trace',100,100,'it-links','t',repeat('b',64))"
            )
            conn.execute(
                "INSERT INTO news_events (event_id,leader_item_id,dedupe_family,comparison_fingerprint,"
                "comparison_title,leader_title,opened_at_ms,last_member_at_ms,expires_at_ms,admission,"
                "ingest_mode,trace_id,created_at_ms,updated_at_ms,focus_fact_id,focus_fact_text,"
                "focus_fact_context,focus_fact_method,focus_span_start,focus_span_end,event_kind) "
                "VALUES ('ev-links','it-links','general','fp','t','t',100,100,200,'candidate','live','trace',"
                "100,100,'fact','t','','whole_item',0,1,'news')"
            )
            persist_update(conn, first)
            persist_update(conn, raised)
        command.upgrade(config, HEAD)
        rows = conn.execute(
            "SELECT update_ref,current_ref,previous_ref,relation,current_event_id,previous_event_id,asserted_at_ms "
            "FROM news_claim_links ORDER BY current_ref"
        ).fetchall()
        change = raised.changes[0]
        assert [tuple(row.values()) for row in rows] == [
            (
                raised.ref,
                change.current_ref,
                change.previous_ref,
                "real_world_change",
                "ev-links",
                "ev-links",
                raised.adopted_at_ms,
            )
        ]
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conname='news_notification_decisions_origin_check'"
        ).fetchone()
        assert "reader_v2" in definition["definition"]
    finally:
        conn.close()


def test_migration_tree_resolves_outside_the_repository() -> None:
    origin = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="tracefold-alembic-cwd-") as elsewhere:
        os.chdir(elsewhere)
        try:
            resolved = Path(ScriptDirectory.from_config(alembic_config()).dir).resolve()
        finally:
            os.chdir(origin)

    assert resolved == VERSIONS.parent.resolve()


def test_fresh_database_upgrades_through_baseline_and_signal_cut() -> None:
    config = _config()
    _empty_the_schema()
    assert _stamped_revision() is None

    command.upgrade(config, "head")
    assert _stamped_revision() == HEAD
    command.upgrade(config, "head")
    assert _stamped_revision() == HEAD


def test_execution_hard_cut_retires_old_tables_and_is_forward_only() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260929_0415")
    command.upgrade(config, "20260929_0417")
    with pytest.raises(RuntimeError, match="trading_execution_hard_cut_forward_only_restore_verified_backup"):
        command.downgrade(config, "20260929_0416")
    command.upgrade(config, "20260929_0418")

    conn = connect_postgres_test(read_only=True)
    try:
        old = (
            "trading_trade_signals",
            "trading_signal_retirements",
            "trading_entry_validity_checks",
            "trading_trade_plans",
            "trading_execution_observations",
            "trading_execution_runtime_state",
            "trading_execution_runtime_control_state",
        )
        new = (
            "trading_signals",
            "trading_dispositions",
            "trading_plans",
            "trading_orders",
            "trading_fills",
            "trading_fill_attributions",
            "trading_executor_state",
            "trading_control_state",
        )
        assert all(
            conn.execute("SELECT to_regclass(%s) AS relation", (name,)).fetchone()["relation"] is None for name in old
        )
        assert all(
            conn.execute("SELECT to_regclass(%s) AS relation", (name,)).fetchone()["relation"] is not None
            for name in new
        )
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="Irreversible #746 Analysis hard cut"):
        command.downgrade(config, "20260929_0417")
    assert _stamped_revision() == "20260929_0418"
    command.upgrade(config, HEAD)
    assert _stamped_revision() == HEAD


def test_recall_retrieval_codes_are_generated_for_existing_assets_and_the_revision_reverses() -> None:
    """#771 `20261001_0419`: asset rows already stored get their retrieval codes from the rewrite; downgrade
    restores the 0418 shape with every row intact."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260929_0418")
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            "INSERT INTO news_items (item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,"
            " first_ingest_mode, created_at_ms, updated_at_ms)"
            " VALUES ('it-1', 'opennews', 'k-1', 't', 1, 1, 'live', 1, 1)"
        )
        conn.execute(
            "INSERT INTO news_events (event_id, leader_item_id, dedupe_family, comparison_fingerprint,"
            " comparison_title, leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,"
            " created_at_ms, updated_at_ms, focus_fact_id, focus_fact_method, event_kind)"
            " VALUES ('ev-1', 'it-1', 'news', 'fp', 't', 't', 1, 1, 2, 'candidate', 'live', 1, 1, 'f', 'whole_item',"
            " 'news')"
        )
        conn.execute(
            "INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)"
            " VALUES ('xyz-siusdt', 'ev-1', NULL, 1), (' $btc ', 'ev-1', 'crypto', 1), ('SIUSDT', 'ev-1', 'equity', 1)"
        )
    finally:
        conn.close()

    command.upgrade(config, "20261001_0419")
    conn = connect_postgres_test(read_only=True)
    try:
        rows = conn.execute(
            "SELECT symbol, retrieval_symbol, retrieval_pair_base FROM news_event_assets ORDER BY symbol"
        ).fetchall()
        assert [tuple(row.values()) for row in rows] == [
            (" $btc ", "BTC", None),
            ("SIUSDT", "SIUSDT", None),
            ("xyz-siusdt", "SIUSDT", "SI"),
        ]
        indexes = {
            "ix_news_event_assets_retrieval_symbol",
            "ix_news_event_assets_retrieval_pair_base",
            "ix_news_items_canonical_url",
            "ix_news_events_leader_item",
            "ix_news_event_members_fact_trgm",
        }
        assert all(conn.execute("SELECT to_regclass(%s) AS i", (name,)).fetchone()["i"] for name in indexes)
        assert conn.execute("SELECT to_regclass('ix_news_events_evidence_title') AS i").fetchone()["i"] is None
    finally:
        conn.close()

    command.downgrade(config, "20260929_0418")
    conn = connect_postgres_test(read_only=True)
    try:
        assert conn.execute("SELECT to_regclass('ix_news_events_evidence_title') AS i").fetchone()["i"]
        columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'news_event_assets'"
            ).fetchall()
        }
        assert columns == {"symbol", "event_id", "market_type", "opened_at_ms"}
        assert conn.execute("SELECT to_regproc('news_asset_retrieval_symbol') AS f").fetchone()["f"] is None
        assert conn.execute("SELECT count(*) AS n FROM news_event_assets").fetchone()["n"] == 3
    finally:
        conn.close()
    command.upgrade(config, HEAD)
    assert _stamped_revision() == HEAD


def test_review_task_source_recreation_changes_that_view_and_nothing_else() -> None:
    """#548 PR-B.2. `20260904_0363` recreates one view and touches no other catalog object.

    The old definition took the newest evidence snapshot and then demanded the newest model verdict have
    judged that exact version. A member join appends a snapshot without re-running triage, so a `v2`
    snapshot beside a `v1` verdict matched nothing and the Event left the view — the one the freeze
    projects — entirely. The new definition keys the snapshot lateral to `v.evidence_version`, which is
    the version the verdict actually judged, and `(event_id, evidence_version)` is that table's primary
    key so the lateral still yields at most one row.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0362")

    conn = connect_postgres_test(read_only=False)
    try:
        before_views = _view_definitions(conn)
        before_catalog = _catalog_inventory(conn)

        command.upgrade(config, "20260904_0363")

        after_views = _view_definitions(conn)
        assert set(after_views) == set(before_views)
        assert {name for name in before_views if before_views[name] != after_views[name]} == {
            "news_review_task_source_v1"
        }
        # Columns, constraints, indexes, triggers, functions and sequences are byte-identical, and the
        # view's own columns are in that inventory: `CREATE OR REPLACE VIEW` cannot change them.
        assert _catalog_inventory(conn) == before_catalog

        old, new = before_views["news_review_task_source_v1"], after_views["news_review_task_source_v1"]
        assert "ORDER BY x.evidence_version DESC" in old
        assert "ORDER BY x.evidence_version DESC" not in new
        assert "x.evidence_version = v.evidence_version" in new
        # The newest *verdict* is still how the verdict side is chosen.
        assert "ORDER BY x.created_at_ms DESC" in old and "ORDER BY x.created_at_ms DESC" in new
        assert _reloptions(conn, "news_review_task_source_v1") == ["security_barrier=true"]
    finally:
        conn.close()


def _view_definitions(conn) -> dict[str, str]:
    return {
        str(row["viewname"]): str(row["definition"])
        for row in conn.execute(
            "SELECT viewname, pg_get_viewdef(('public.' || quote_ident(viewname))::regclass, true) AS definition "
            "FROM pg_views WHERE schemaname = 'public'"
        ).fetchall()
    }


def _reloptions(conn, relation: str) -> list[str]:
    row = conn.execute(
        "SELECT coalesce(reloptions, '{}')::text[] AS options FROM pg_class WHERE oid = %s::regclass",
        (f"public.{relation}",),
    ).fetchone()
    return sorted(str(option) for option in (row["options"] if row is not None else ()))


def _catalog_inventory(conn) -> dict[str, list[str]]:
    """Everything in `public` except the view bodies themselves, as a comparable inventory."""

    queries = {
        "columns": (
            "SELECT table_name || '.' || column_name || ':' || data_type || ':' || ordinal_position || ':' "
            "|| is_nullable || ':' || coalesce(column_default, '-') AS entry "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        ),
        "constraints": (
            "SELECT conrelid::regclass::text || '.' || conname || ':' || pg_get_constraintdef(oid) AS entry "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
        ),
        "indexes": "SELECT indexname || ':' || indexdef AS entry FROM pg_indexes WHERE schemaname = 'public'",
        "triggers": ("SELECT tgrelid::regclass::text || '.' || tgname AS entry FROM pg_trigger WHERE NOT tgisinternal"),
        "functions": (
            "SELECT proname || ':' || pg_get_function_identity_arguments(oid) AS entry "
            "FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        ),
        "relations": (
            "SELECT relname || ':' || relkind::text AS entry FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'v', 'S', 'm')"
        ),
    }
    return {name: sorted(str(row["entry"]) for row in conn.execute(sql).fetchall()) for name, sql in queries.items()}


def _table_checks(conn, table: str) -> set[str]:
    return {
        str(row["conname"])
        for row in conn.execute(
            """
            SELECT con.conname FROM pg_constraint con
             WHERE con.contype = 'c' AND con.conrelid = %s::regclass
            """,
            (f"public.{table}",),
        ).fetchall()
    }


_OI_TITLE = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


def _seed_pre_cut_oi_event(
    conn,
    *,
    event_id: str,
    leader_item: str,
    member_item: str,
    at_ms: int,
    title: str = _OI_TITLE,
    venue: str = "binance",
) -> None:
    """One pre-#553 OI Event with two Items: the leader, and a frame the deduper merged into it."""

    for item_id in (leader_item, member_item):
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              %(item)s, 'opennews', %(item)s, %(title)s, %(title)s, '', 'opennews',
              %(at)s, %(at)s,
              jsonb_build_object(
                'source', %(venue)s::text,
                'strategies', jsonb_build_array(jsonb_build_object('id', '1019', 'name', 'OI Event Monitor'))
              ),
              '[]'::jsonb, 'recovery', 'trace', %(at)s, %(at)s
            )
            """,
            {"item": item_id, "title": title, "at": at_ms, "venue": venue},
        )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title,
          leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
          trace_id, created_at_ms, updated_at_ms, focus_fact_id, focus_fact_text,
          focus_fact_context, focus_fact_method, focus_span_start, focus_span_end, event_kind
        ) VALUES (
          %(event)s, %(leader)s, 'market_telemetry', 'fingerprint', %(title)s, %(title)s,
          %(at)s, %(at)s, %(at)s, 'telemetry_deterministic', 'recovery', 'trace', %(at)s, %(at)s,
          %(leader_fact)s, %(title)s, '', 'whole_item', 0, 10, 'oi'
        )
        """,
        {
            "event": event_id,
            "leader": leader_item,
            "title": title,
            "at": at_ms,
            "leader_fact": f"fact-{leader_item}",
        },
    )
    for item_id, match_kind in ((leader_item, "leader"), (member_item, "near")):
        conn.execute(
            """
            INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
            VALUES (%(event)s, %(item)s, %(at)s, %(kind)s, %(fact)s, %(title)s)
            """,
            {
                "event": event_id,
                "item": item_id,
                "at": at_ms,
                "kind": match_kind,
                "fact": f"fact-{item_id}",
                "title": title,
            },
        )


def test_the_market_cut_rebuilds_every_observation_an_event_had_swallowed() -> None:
    """#553 §3.3. Recovery frames and merged members were real measurements with no ledger row.

    A recovery frame skipped Triage entirely and a frame the title deduper joined to an existing
    Event was recorded as a member with no row of its own. Both are reconstructed here from the Items
    that survive, flagged `historical`, with the provider's own stamps intact and the rebuild moment
    as the first instant any consumer could read them.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        _seed_pre_cut_oi_event(
            conn,
            event_id="pre-cut-oi-event",
            leader_item="pre-cut-oi-leader",
            member_item="pre-cut-oi-member",
            at_ms=at_ms,
        )
        # One ledger row that already exists. A frozen Trading Case files its answer under this exact
        # `event_id`, so the rebuild must leave every column of it alone -- including the numbers,
        # which differ from what the template above would reconstruct.
        conn.execute(
            """
            INSERT INTO news_oi_signals (
              event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd,
              whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, created_at_ms,
              source_item_id, source_venue, available_at_ms, learning_epoch
            ) VALUES (
              'pre-cut-oi-event', 'oi_signal_v1', 'FROZEN', 'fall', -111, 222, 333, 444,
              %(at)s, %(at)s, 'pre-cut-oi-leader', 'hyperliquid', %(at)s, 'epoch-2026-08'
            )
            """,
            {"at": at_ms},
        )
        conn.commit()

        command.upgrade(config, PRE_CUT)

        rows = conn.execute(
            """
            SELECT source_item_id, symbol, raw_instrument, direction, oi_change_bps, oi_value_usd,
                   whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, received_at_ms,
                   available_at_ms, historical, source_venue, source_strategy_id, measurement_definition
              FROM news_oi_signals
             ORDER BY source_item_id
            """
        ).fetchall()
        assert [row["source_item_id"] for row in rows] == ["pre-cut-oi-leader", "pre-cut-oi-member"]
        frozen = next(row for row in rows if row["source_item_id"] == "pre-cut-oi-leader")
        assert (frozen["symbol"], frozen["direction"], frozen["oi_change_bps"]) == ("FROZEN", "fall", -111)
        assert frozen["historical"] is False, "an existing observation is not a reconstruction"
        assert frozen["source_venue"] == "hyperliquid"
        rebuilt = [row for row in rows if row["source_item_id"] == "pre-cut-oi-member"]
        assert len(rebuilt) == 1
        for row in rebuilt:
            assert row["historical"] is True
            assert (row["symbol"], row["raw_instrument"], row["direction"]) == ("TRUMP", "TRUMP", "rise")
            assert (row["oi_change_bps"], row["oi_value_usd"]) == (455, 32_170_000)
            assert (row["whale_long_profit_bps"], row["whale_oi_ratio_bps"]) == (8_021, 10_071)
            # The provider and host stamps are the originals; only availability is the rebuild's.
            assert row["observed_at_ms"] == at_ms
            assert row["received_at_ms"] == at_ms
            assert row["available_at_ms"] > at_ms
            assert row["source_venue"] == "binance"
            assert row["source_strategy_id"] == "1019"
            assert row["measurement_definition"] == "oi_signal_v1|opennews_oi_source_v1|300000"

        # The merged member is its own observation under its own published source identity, derived
        # from the Item and the fact it was admitted under rather than borrowed from the leader.
        assert len({row["source_item_id"] for row in rows}) == 2
        member_event_id = conn.execute(
            "SELECT event_id FROM news_oi_signals WHERE source_item_id = 'pre-cut-oi-member'"
        ).fetchone()["event_id"]
        assert member_event_id != "pre-cut-oi-event"
        assert re.fullmatch(r"[0-9a-f]{64}", member_event_id)
        items = conn.execute(
            "SELECT item_id, market_kind, market_parse_status, market_source_strategy_id, provider_params"
            " FROM news_items ORDER BY item_id"
        ).fetchall()
        assert [(row["market_kind"], row["market_parse_status"]) for row in items] == [
            ("oi", "parsed"),
            ("oi", "parsed"),
        ]
        assert {row["market_source_strategy_id"] for row in items} == {"1019"}
        # A backfilled Item has no stored business payload: the frame it came from is long gone, and
        # an empty object is the honest record of that rather than an invented one.
        assert [dict(row["provider_params"]) for row in items] == [{}, {}]
        # The Event is immutable historical evidence and is neither rewritten nor deleted.
        assert conn.execute("SELECT count(*) AS n FROM news_events").fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_market_item_whose_template_is_not_reconstructed_says_so_rather_than_claiming_a_parse() -> None:
    """A 2083 or 2026 Item predates any parser for it. `raw` with a reason is the honest state.

    Read at `BEFORE_REPARSE`, because that reason is exactly what `20260906_0370` spends: it is true
    only while no parser has been run against the frame, and the revision below runs one.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              'pre-cut-wallet', 'opennews', 'pre-cut-wallet',
              'js-2 Close Short SOL $482,113.55 , Price $137.01', '', '', 'opennews',
              %(at)s, %(at)s,
              '{"strategies": [{"id": "2026", "name": "聪明钱监控"}]}'::jsonb,
              '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s
            )
            """,
            {"at": at_ms},
        )
        conn.commit()

        command.upgrade(config, BEFORE_REPARSE)

        row = conn.execute(
            "SELECT market_kind, market_parse_status, market_parse_error, market_source_strategy_id"
            " FROM news_items WHERE item_id = 'pre-cut-wallet'"
        ).fetchone()
        assert row["market_kind"] == "smart_money"
        assert (row["market_parse_status"], row["market_parse_error"]) == ("raw", "market_backfill_not_reparsed")
        assert row["market_source_strategy_id"] == "2026"
    finally:
        conn.close()


# The four Strategy 2026 titles the reparse has to answer, in the shapes the retained production
# window actually holds: the abbreviated notional #560 taught the parser to read, a second report from
# the same account, another account's close with a PNL and the provider's `XYZ-` prefix, and the one
# `Withdraw` line that is not a position report at all.
_SMART_MONEY_TITLES = {
    "reparse-k-suffix": "js-2 Open Long BTC $798.18K , Price $79,817.87",
    "reparse-same-account": "js-2 Open Long BTC $1.20M , Price $79,900.00",
    "reparse-other-account": "whale 7 Close Short XYZ-NBIS $6.27M , Price $208.95 , PNL -$1.16K",
    "reparse-withdraw": "js-2 Withdraw USDC",
}


def _seed_pre_cut_smart_money(conn, *, item_id: str, title: str, at_ms: int, venue: str = "hyperliquid") -> None:
    """One Strategy 2026 Item as it was stored before any smart-money parser existed."""

    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, raw_first_line, description,
          reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
          first_ingest_mode, trace_id, created_at_ms, updated_at_ms
        ) VALUES (
          %(item)s, 'opennews', %(record)s, %(title)s, %(title)s, '', 'opennews',
          %(at)s, %(received)s,
          jsonb_build_object(
            'source', %(venue)s::text,
            'strategies', jsonb_build_array(jsonb_build_object('id', '2026', 'name', '聪明钱监控'))
          ),
          '[]'::jsonb, 'live', 'trace', %(received)s, %(received)s
        )
        """,
        {
            "item": item_id,
            "record": f"record-{item_id}",
            "title": title,
            "at": at_ms,
            # The host read the frame a second after the provider stamped it. The two clocks are
            # recorded and never compared (#544), and the reparse must carry both through unchanged.
            "received": at_ms + 1_000,
            "venue": venue,
        },
    )


def test_the_reparse_turns_every_backfilled_smart_money_record_into_the_fact_it_proves() -> None:
    """#562. `20260905_0365` marked these Items `raw / market_backfill_not_reparsed` because no parser
    had been run against them; #560 then taught `parse_smart_money` the provider's `K`/`M`/`B`. This
    revision runs that parser once, and it is parse evidence only: the provider stamps and the source
    identity are unchanged, the notification marker stays `historical`, and no track, delivery or
    trade appears.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        for offset, (item_id, title) in enumerate(_SMART_MONEY_TITLES.items()):
            _seed_pre_cut_smart_money(conn, item_id=item_id, title=title, at_ms=at_ms + offset * 60_000)
        conn.commit()

        command.upgrade(config, BEFORE_REPARSE)
        # Every backfilled Item's payload is empty in production -- `20260905_0365` classified rows
        # that were admitted before `provider_params` existed. The parser still reads the account
        # address from there for anything admitted after it, so one row states that read.
        conn.execute(
            """
            UPDATE news_items SET provider_params = jsonb_build_object('relatedAddress', %(address)s::text)
             WHERE item_id = 'reparse-other-account'
            """,
            {"address": "0x" + "7" * 40},
        )
        conn.commit()
        backlog = conn.execute(
            "SELECT market_parse_status, market_parse_error, market_notify_state FROM news_items"
            " WHERE market_kind = 'smart_money' ORDER BY item_id"
        ).fetchall()
        assert [(row["market_parse_status"], row["market_parse_error"]) for row in backlog] == [
            ("raw", "market_backfill_not_reparsed")
        ] * 4
        # The production population this revision meets: all 112 rows are `historical`, none pending.
        assert {row["market_notify_state"] for row in backlog} == {"historical"}

        command.upgrade(config, PRE_CUT)

        items = conn.execute(
            "SELECT item_id, title, published_at_ms, observed_at_ms, market_parse_status,"
            " market_parse_error, market_notify_state, market_notify_group_key,"
            " market_notify_delivery_key, first_ingest_mode, source_item_key"
            " FROM news_items ORDER BY item_id"
        ).fetchall()
        assert [(row["item_id"], row["market_parse_status"], row["market_parse_error"]) for row in items] == [
            ("reparse-k-suffix", "parsed", None),
            ("reparse-other-account", "parsed", None),
            ("reparse-same-account", "parsed", None),
            # The one refusal, under the reason the parser actually gives rather than "not reparsed".
            ("reparse-withdraw", "raw", "smart_money_template_unmatched"),
        ]
        # The provider record is untouched: same title, same stamps, same source identity.
        assert {row["title"] for row in items} == set(_SMART_MONEY_TITLES.values())
        assert {row["source_item_key"] for row in items} == {f"record-{item}" for item in _SMART_MONEY_TITLES}
        assert [row["published_at_ms"] for row in items] == [at_ms, at_ms + 120_000, at_ms + 60_000, at_ms + 180_000]
        # A reparse is not a new observation: it may not put a months-old report back on the reader's
        # to-do list, and it may not be adopted by the notification loop.
        assert {row["market_notify_state"] for row in items} == {"historical"}
        assert [row["market_notify_group_key"] for row in items] == [None] * 4
        assert [row["market_notify_delivery_key"] for row in items] == [None] * 4
        assert conn.execute("SELECT count(*) AS n FROM news_market_tracks").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_market_deliveries").fetchone()["n"] == 0

        facts = conn.execute(
            "SELECT * FROM news_market_smart_money ORDER BY item_id",
        ).fetchall()
        assert [row["item_id"] for row in facts] == [
            "reparse-k-suffix",
            "reparse-other-account",
            "reparse-same-account",
        ]
        by_item = {row["item_id"]: row for row in facts}

        abbreviated = by_item["reparse-k-suffix"]
        assert (abbreviated["trader_label"], abbreviated["action"], abbreviated["position_side"]) == (
            "js-2",
            "open",
            "long",
        )
        # The whole record, not just the price, was what the pre-#560 refusal cost.
        assert abbreviated["reported_notional_usd"] == Decimal("798180")
        assert abbreviated["price"] == Decimal("79817.87")
        assert abbreviated["pnl_usd"] is None
        assert (abbreviated["raw_instrument"], abbreviated["symbol"]) == ("BTC", "BTC")
        assert abbreviated["source_venue"] == "hyperliquid"
        assert abbreviated["account_address"] is None
        assert (abbreviated["provider"], abbreviated["source_strategy_id"]) == ("opennews", "2026")
        assert abbreviated["ingest_mode"] == "live"
        assert abbreviated["parser_version"] == PARSER_VERSION
        assert abbreviated["provider_record_identity"] == "record-reparse-k-suffix"

        other = by_item["reparse-other-account"]
        assert (other["trader_label"], other["action"], other["position_side"]) == ("whale 7", "close", "short")
        assert (other["raw_instrument"], other["symbol"]) == ("XYZ-NBIS", "NBIS")
        assert other["pnl_usd"] == Decimal("-1160")
        assert other["account_address"] == "0x" + "7" * 40

        # The provider's own stamps survive; only availability is the rebuild's, because that is the
        # first instant any consumer could read these facts (#553 §3.3).
        for item_id, row in by_item.items():
            offset = list(_SMART_MONEY_TITLES).index(item_id) * 60_000
            assert row["event_at_ms"] == at_ms + offset
            assert row["received_at_ms"] == at_ms + offset + 1_000
            assert row["available_at_ms"] > at_ms + offset + 1_000
            assert row["created_at_ms"] == row["available_at_ms"]
            # The identity a live replay of the same provider record would compute, so the replay
            # collides with this row instead of writing a second one.
            title = _SMART_MONEY_TITLES[item_id]
            unit = extract_fact_units(item_id=item_id, raw_text=title, fallback_title=title)[0]
            assert row["fact_id"] == unit.fact_id
            assert row["source_key"] == smart_money_source_key(item_id=item_id, fact_id=unit.fact_id)
    finally:
        conn.close()


def test_the_reparse_is_a_no_op_when_it_runs_again() -> None:
    """Re-running it writes nothing: every row it touched has left the set its predicate selects."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        for offset, (item_id, title) in enumerate(_SMART_MONEY_TITLES.items()):
            _seed_pre_cut_smart_money(conn, item_id=item_id, title=title, at_ms=at_ms + offset * 60_000)
        conn.commit()
        command.upgrade(config, PRE_CUT)

        before = conn.execute(
            "SELECT source_key, item_id, available_at_ms FROM news_market_smart_money ORDER BY source_key"
        ).fetchall()
        items_before = conn.execute(
            "SELECT item_id, market_parse_status, market_parse_error FROM news_items ORDER BY item_id"
        ).fetchall()

        # Alembic will not replay a revision it has stamped, so the second run is asked for directly.
        # The target is the reparse itself rather than the head: this is a claim about that one
        # revision, and later revisions on top of it are not asked to be replayable.
        conn.execute("UPDATE alembic_version SET version_num = %(previous)s", {"previous": BEFORE_REPARSE})
        conn.commit()
        command.upgrade(config, REPARSE)

        assert [
            dict(row)
            for row in conn.execute(
                "SELECT source_key, item_id, available_at_ms FROM news_market_smart_money ORDER BY source_key"
            ).fetchall()
        ] == [dict(row) for row in before]
        assert [
            dict(row)
            for row in conn.execute(
                "SELECT item_id, market_parse_status, market_parse_error FROM news_items ORDER BY item_id"
            ).fetchall()
        ] == [dict(row) for row in items_before]
    finally:
        conn.close()


def test_the_backfill_classifies_a_mixed_strategy_item_by_its_primary_strategy() -> None:
    """#553 SHOULD-FIX 4. The migration and the live classifier answer the same record the same way.

    An Item unions every Strategy tuple it was reported under. Both sides read the *primary* one, so a
    1019 record a news Strategy also matched is an OI observation whichever of the two classified it.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              'mixed-primary-oi', 'opennews', 'mixed-primary-oi', %(title)s, '', '', 'opennews',
              %(at)s, %(at)s,
              '{"source": "binance", "strategies": [
                 {"id": "1019", "name": "OI Event Monitor"},
                 {"id": "1018", "name": "News Score > 70"}]}'::jsonb,
              '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s
            )
            """,
            {"at": at_ms, "title": _OI_TITLE},
        )
        conn.commit()

        command.upgrade(config, PRE_CUT)

        row = conn.execute(
            "SELECT market_kind, market_source_strategy_id, market_parse_status FROM news_items"
            " WHERE item_id = 'mixed-primary-oi'"
        ).fetchone()
        assert row["market_kind"] == "oi"
        assert row["market_source_strategy_id"] == "1019"
        # No Event ever carried it, so there is nothing to reconstruct from and the Item stays raw --
        # what the classifier decides and what a parser could read are two separate answers.
        assert row["market_parse_status"] == "raw"
    finally:
        conn.close()


# Frames chosen for the three places the rebuild's SQL and the parser could disagree: half-up basis
# points including a negative, the six-digit truncation of the OI value under each unit, and a symbol
# carrying the provider prefix.
_REBUILD_ARITHMETIC_FRAMES = (
    "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
    "BTC OI Fall 0.5%, OI Value 3.8600005M, Whale Long Profit -3.5%, Whale/OI Ratio 1438.2%",
    "XYZ-UNITREE OI Drop 1438.25%, OI Value 999.9999999B, Whale Long Profit 0.005%, Whale/OI Ratio 0%",
    "S OI Rise 3.04%, OI Value 3.86K, Whale Long Profit 92.31%, Whale/OI Ratio 31.42%",
    "4 OI Rise 0.004%, OI Value 7, Whale Long Profit 0.5%, Whale/OI Ratio 0.5%",
)


def test_the_rebuild_reproduces_the_parsers_own_arithmetic() -> None:
    """#553. The migration re-implements the 1019 template deliberately; this is what holds it honest.

    A rebuild is a statement about what the provider sent, so it must not import a parser a later
    revision can change underneath it. The cost of that freedom is that the two can drift, and every
    place they could was wrong at least once: half-up basis points, the six-digit truncation of the OI
    value *before* the unit is applied (`3.8600005M` is 3_860_000, not 3_860_001), and the 32-character
    cap on the venue. So the same frames go through both and every field is compared.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        long_venue = "a-venue-name-far-longer-than-the-thirty-two-character-cap"
        for index, title in enumerate(_REBUILD_ARITHMETIC_FRAMES):
            _seed_pre_cut_oi_event(
                conn,
                event_id=f"arith-event-{index}",
                leader_item=f"arith-leader-{index}",
                member_item=f"arith-member-{index}",
                at_ms=at_ms,
                title=title,
                venue=long_venue,
            )
        conn.commit()

        command.upgrade(config, PRE_CUT)

        rebuilt = {
            str(row["source_item_id"]): row
            for row in conn.execute(
                "SELECT source_item_id, symbol, raw_instrument, direction, oi_change_bps, oi_value_usd,"
                " whale_long_profit_bps, whale_oi_ratio_bps, source_venue FROM news_oi_signals"
            ).fetchall()
        }
        assert len(rebuilt) == 2 * len(_REBUILD_ARITHMETIC_FRAMES)
        for index, title in enumerate(_REBUILD_ARITHMETIC_FRAMES):
            expected = parse_oi_signal(title)
            assert expected is not None, title
            for role in ("leader", "member"):
                row = rebuilt[f"arith-{role}-{index}"]
                assert row["symbol"] == expected.symbol, title
                assert row["raw_instrument"] == expected.raw_instrument, title
                assert row["direction"] == expected.direction, title
                assert row["oi_change_bps"] == expected.oi_change_bps, title
                assert row["oi_value_usd"] == expected.oi_value_usd, title
                assert row["whale_long_profit_bps"] == expected.whale_long_profit_bps, title
                assert row["whale_oi_ratio_bps"] == expected.whale_oi_ratio_bps, title
                # And the same 32-character cap `parse_liquidation` applies to a venue string.
                assert row["source_venue"] == long_venue[:32], title
    finally:
        conn.close()


# One corpus, both classifiers. Each entry is the `strategies` array an Item carries and nothing else,
# because the primary Strategy plus the set of market families present is all either side reads.
_CLASSIFIER_FIXTURES: tuple[tuple[str, list[str]], ...] = (
    ("oi-only", ["1019"]),
    ("oi-with-news", ["1019", "1018"]),
    ("news-primary-with-oi", ["1018", "1019"]),
    ("oi-and-liquidation", ["1019", "2083"]),
    ("both-liquidation-strategies", ["2000", "2083"]),
    ("smart-money-only", ["2026"]),
    ("news-only", ["1018"]),
    ("unbound-market", ["9999"]),
)


def test_the_backfill_and_the_live_classifier_agree_on_every_fixture() -> None:
    """#553. The migration mirrors `market_route`; nothing enforces that but this comparison.

    A migration may not import a parser a later revision can change underneath it, so the rule is
    written twice. The cost of that is drift, and the drift that matters is silent: one record
    classified `oi` by the live path and `unknown_market` by the backfill would exist or not exist as
    a typed fact depending only on which ran. So the same fixtures go through both and the answers
    are compared.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0363")

    names = {
        "1018": "News Score > 70",
        "1019": "OI Event Monitor",
        "2000": "实时清算",
        "2026": "聪明钱监控",
        "2083": "Large-scale liquidation",
        "9999": "An unbound market monitor",
    }
    source_types = {"1018": "news", "2026": "wallet"}

    def _metadata(strategy_ids: list[str]) -> dict[str, Any]:
        return {
            "strategies": [
                {
                    "id": strategy_id,
                    "name": names[strategy_id],
                    "source_type": source_types.get(strategy_id, "market"),
                    "engine_type": "news" if strategy_id == "1018" else "market",
                }
                for strategy_id in strategy_ids
            ]
        }

    conn = connect_postgres_test(read_only=False)
    try:
        at_ms = 1_787_542_200_000
        for item_id, strategy_ids in _CLASSIFIER_FIXTURES:
            conn.execute(
                """
                INSERT INTO news_items (
                  item_id, source_id, source_item_key, title, raw_first_line, description,
                  reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                  first_ingest_mode, trace_id, created_at_ms, updated_at_ms
                ) VALUES (
                  %(item)s, 'opennews', %(item)s, 'a frame with no template', '', '', 'opennews',
                  %(at)s, %(at)s, %(metadata)s::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s
                )
                """,
                {"item": item_id, "at": at_ms, "metadata": json.dumps(_metadata(strategy_ids))},
            )
        conn.commit()

        command.upgrade(config, PRE_CUT)

        stored = {
            str(row["item_id"]): (row["market_kind"], row["market_parse_error"])
            for row in conn.execute(
                "SELECT item_id, market_kind, market_parse_error FROM news_items WHERE item_id = ANY(%s)",
                ([item_id for item_id, _ in _CLASSIFIER_FIXTURES],),
            ).fetchall()
        }

        for item_id, strategy_ids in _CLASSIFIER_FIXTURES:
            live = market_route(classify_source_contracts(_metadata(strategy_ids)))
            migrated_kind, migrated_reason = stored[item_id]
            if live is None:
                assert migrated_kind is None, item_id
                continue
            expected_kind, expected_conflict = live
            assert migrated_kind == expected_kind, item_id
            # The reasons differ by design where they must: the live path records what its parser
            # read, and the backfill records that no parser was run. A conflict is the one reason both
            # can state, because it is decided before any parser is consulted.
            assert (migrated_reason == MARKET_CATEGORY_CONFLICT) is (expected_conflict is not None), item_id
    finally:
        conn.close()


def test_the_market_notification_marker_separates_the_pre_enable_backlog_from_live_records() -> None:
    """`20260905_0366` is enable-time: what was already here is history, what arrives next is a to-do.

    The revision cannot ask the loop which observations a reader has already seen, because before it
    ran no loop existed. What it can say is that every market record that predates it belongs to a
    window nobody was being alerted for, and alerting on a two-day-old OI frame at enable time
    interrupts a reader with something they cannot act on (#553 §4.1.5). This isolates the enable-time migration:
    the backlog seeded before the upgrade is `historical` and stays out of the take query. Current
    writer and replay behavior are verified by the market-observation integration tests.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260905_0365")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for item_id, kind in (("backlog-oi", "oi"), ("backlog-liq", "liquidation")):
                conn.execute(
                    """
                    INSERT INTO news_items (
                      item_id, source_id, source_item_key, title, raw_first_line, description,
                      reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                      first_ingest_mode, trace_id, created_at_ms, updated_at_ms,
                      market_kind, market_source_strategy_id, market_parse_status, market_parse_error
                    ) VALUES (
                      %s, 'opennews', %s, %s, %s, '', 'opennews', 1000, 1000, '{}'::jsonb, '[]'::jsonb,
                      'live', 'trace', 1000, 1000, %s, '1019', 'raw', 'market_backfill_not_reparsed'
                    )
                    """,
                    (item_id, item_id, item_id, item_id, kind),
                )
            # Ordinary news sits beside them and must come out of the upgrade with no marker at all:
            # its delivery is its Event's, and this column is not part of that decision.
            conn.execute(
                """
                INSERT INTO news_items (
                  item_id, source_id, source_item_key, title, raw_first_line, description,
                  reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                  first_ingest_mode, trace_id, created_at_ms, updated_at_ms
                ) VALUES (
                  'backlog-news', 'opennews', 'backlog-news', 'ordinary', 'ordinary', '', 'opennews',
                  1000, 1000, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', 1000, 1000
                )
                """
            )

        command.upgrade(config, "20260905_0366")

        marked = {
            str(row["item_id"]): row["market_notify_state"]
            for row in conn.execute("SELECT item_id, market_notify_state FROM news_items ORDER BY item_id").fetchall()
        }
        assert marked == {
            "backlog-liq": "historical",
            "backlog-news": None,
            "backlog-oi": "historical",
        }
        # The take query is the marker, so the backlog is not in it -- no card is ever prepared for
        # an observation that arrived before anyone was listening.
        backlog = conn.execute(
            "SELECT count(*) AS pending FROM news_items WHERE market_notify_state = 'pending'"
        ).fetchone()
        assert int(backlog["pending"]) == 0

    finally:
        conn.close()


def test_the_alert_round_backfill_starts_each_group_at_its_last_send_attempt() -> None:
    """`20260905_0367` on groups that already exist, which is every group in production.

    The round start bounds what the next card adopts, so the value the upgrade leaves behind decides
    which observations the first card after the deploy speaks for. The last send attempt is the
    newest moment a group is known to have interrupted a reader: what came before it was either on
    that card or held in a round that has ended. A group that has never sent keeps 0, so its first
    card still speaks for everything it holds (#562 PR-F).
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260905_0366")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for group_key, attempt_at_ms in (("oi|sent", 1_700_000_000_000), ("oi|never-sent", None)):
                conn.execute(
                    """
                    INSERT INTO news_market_tracks (
                      group_key, market_kind, family, last_observed_at_ms, last_observed_item_id,
                      anchor_attempt_at_ms, created_at_ms, updated_at_ms
                    ) VALUES (%s, 'oi', 'oi', 1, 'item', %s, 1, 1)
                    """,
                    (group_key, attempt_at_ms),
                )

        command.upgrade(config, "head")

        started = {
            str(row["group_key"]): int(row["round_started_at_ms"])
            for row in conn.execute("SELECT group_key, round_started_at_ms FROM news_market_tracks").fetchall()
        }
        assert started == {"oi|sent": 1_700_000_000_000, "oi|never-sent": 0}
    finally:
        conn.close()


def test_the_unstructured_cut_deletes_that_alerting_state_and_the_two_dead_columns() -> None:
    """`20260906_0371` on the tracks production actually holds (#582 §3.2).

    Three claims on one real database, because the revision makes three changes and each one can
    fail on its own: the `raw` groups -- notification state for a rule that no longer exists -- are
    gone, the tightened CHECK refuses the family that produced them, and the two columns whose only
    reader was the deleted side-change rule are dropped. The groups that still alert are untouched,
    including the anchor the remaining smart-money rule reads.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260906_0370")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for group_key, family, current_action in (
                ("raw|smart_money|deadbeef", "raw", None),
                ("raw|oi|c0ffee", "raw", None),
                ("smart_money|address|0x4d3a|hyperliquid|ETH", "smart_money", "close"),
                ("oi|opennews|binance|WIF|oi_signal_v1", "oi", None),
            ):
                conn.execute(
                    """
                    INSERT INTO news_market_tracks (
                      group_key, market_kind, family, last_observed_at_ms, last_observed_item_id,
                      current_action, current_position_side, anchor_state, anchor_delivery_key,
                      anchor_action, anchor_position_side, created_at_ms, updated_at_ms
                    ) VALUES (%s, 'smart_money', %s, 1, 'item', %s, 'long', 'sent', 'key-' || %s,
                              'open', 'long', 1, 1)
                    """,
                    (group_key, family, current_action, group_key),
                )

        command.upgrade(config, "head")

        remaining = {
            str(row["group_key"]): (str(row["family"]), str(row["anchor_action"]))
            for row in conn.execute("SELECT group_key, family, anchor_action FROM news_market_tracks").fetchall()
        }
        assert remaining == {
            "smart_money|address|0x4d3a|hyperliquid|ETH": ("smart_money", "open"),
            "oi|opennews|binance|WIF|oi_signal_v1": ("oi", "open"),
        }

        columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'news_market_tracks'"
            ).fetchall()
        }
        assert not {"current_action", "current_position_side"} & columns
        # The column the remaining rule does read is still here.
        assert "anchor_action" in columns

        # And the family that produced those rows cannot come back by accident.
        with pytest.raises(Exception, match="news_market_tracks_family_check"), conn.transaction():
            conn.execute(
                """
                INSERT INTO news_market_tracks (
                  group_key, market_kind, family, last_observed_at_ms, last_observed_item_id,
                  created_at_ms, updated_at_ms
                ) VALUES ('raw|smart_money|new', 'smart_money', 'raw', 1, 'item', 1, 1)
                """
            )
    finally:
        conn.close()


def test_the_catalogue_freshness_answer_survives_the_move_off_the_row() -> None:
    """`20260906_0368` must not make the console forget when the catalogue was last refreshed.

    Before it, `max(last_seen_ms)` over every row *was* the last snapshot time, because every refresh
    restamped every row. After it, an unchanged refresh writes no row, so the same question is answered
    from `news_market_instrument_snapshot_state` — and the number has to be the same one across the
    cutover rather than empty until the next six-hourly snapshot. This drives the real seed on a real
    pre-revision database: two venues refreshed at different moments, one of them holding a delisted
    row written by the refresh that delisted it.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260905_0367")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for venue, venue_symbol, status, seen in (
                ("binance.perp", "BTCUSDT", "trading", 1_787_000_000_000),
                ("binance.perp", "OLDUSDT", "delisted", 1_787_000_000_000),
                ("hl.perp", "ETH", "trading", 1_787_003_600_000),
                ("us.listed", "AAPL", "trading", 1_787_001_800_000),
            ):
                conn.execute(
                    "INSERT INTO news_market_instruments"
                    " (venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms)"
                    " VALUES (%s, %s, %s, 'crypto', NULL, %s, %s)",
                    (venue, venue_symbol, venue_symbol, status, seen),
                )
        before = conn.execute("SELECT max(last_seen_ms) AS stamp FROM news_market_instruments").fetchone()["stamp"]

        command.upgrade(config, "head")

        state = {
            str(row["venue"]): int(row["last_snapshot_ms"])
            for row in conn.execute(
                "SELECT key AS venue,value::bigint AS last_snapshot_ms FROM news_collectors c "
                "CROSS JOIN LATERAL jsonb_each_text(c.state->'venues') WHERE collector_id='instrument_catalog'"
            ).fetchall()
        }
        # One row per venue, each holding the last moment that venue answered — a delisting is written
        # by a refresh that answered, so it counts.
        assert state == {
            "binance.perp": 1_787_000_000_000,
            "hl.perp": 1_787_003_600_000,
            "us.listed": 1_787_001_800_000,
        }
        repos = repositories_for_connection(conn)
        assert repos.instruments.universe_summary()["last_snapshot_ms"] == int(before)
        # And the stamp that stays on the row keeps every value it had, under its honest name.
        rows = {
            str(row["venue_symbol"]): (str(row["status"]), int(row["observed_at_ms"]))
            for row in conn.execute(
                "SELECT venue_symbol, status, observed_at_ms FROM news_market_instruments"
            ).fetchall()
        }
        assert rows == {
            "BTCUSDT": ("trading", 1_787_000_000_000),
            "OLDUSDT": ("delisted", 1_787_000_000_000),
            "ETH": ("trading", 1_787_003_600_000),
            "AAPL": ("trading", 1_787_001_800_000),
        }
        # `RENAME COLUMN` does not rename the constraints that depend on the column, and PostgreSQL 18
        # catalogues NOT NULL as a named constraint — so the rename has to carry
        # `news_market_instruments_last_seen_ms_not_null` with it, or `\d news_market_instruments`
        # keeps showing the old name on a column that no longer has it.
        residue = [
            str(row["conname"])
            for row in conn.execute(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'public.news_market_instruments'::regclass AND conname LIKE %s",
                ("%last_seen_ms%",),
            ).fetchall()
        ]
        assert residue == []
        renamed = conn.execute(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'public.news_market_instruments'::regclass"
            "   AND conname = 'news_market_instruments_observed_at_ms_not_null'"
        ).fetchone()
        assert renamed is not None
    finally:
        conn.close()


def _seed_admitted_event(conn: Any, text: str, *, record: int = 664, at_ms: int = 1000) -> str:
    """One admitted Event with its leader Item and current evidence, written in SQL for an older schema.

    These revisions predate the #706 semantic-work tables, so a seed through today's admission -- which
    commits semantic work beside its evidence -- cannot run against them. The rows are the ones admission
    wrote at those revisions: an Item, an admitted Event, its leader membership and a v3 snapshot.
    """

    from tests.postgres_test_utils import seed_current_news_evidence

    item_id = f"item-{record}"
    event_id = f"event-{record}"
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
          reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
          first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id
        ) VALUES (%(item)s, 'opennews', %(record)s, %(text)s, %(text)s, '', %(url)s, 'Reuters', %(at)s, %(at)s,
                  '{}'::jsonb, '[]'::jsonb, 'live', 'migration-seed', %(at)s, %(at)s, %(item)s)
        """,
        {"item": item_id, "record": str(record), "text": text, "url": f"https://example.org/{record}", "at": at_ms},
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title, leader_title,
          opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode, trace_id, created_at_ms,
          updated_at_ms, focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method,
          focus_span_start, focus_span_end, event_kind, grounded_assets
        ) VALUES (%(event)s, %(item)s, 'general', %(fp)s, %(text)s, %(text)s, %(at)s, %(at)s, %(expires)s,
                  'candidate', 'live', 'migration-seed', %(at)s, %(at)s, %(fact)s, %(text)s, '', 'whole_item',
                  0, %(span)s, 'news', '[]'::jsonb)
        """,
        {
            "event": event_id,
            "item": item_id,
            "fp": hashlib.sha256(text.encode()).hexdigest(),
            "text": text,
            "at": at_ms,
            "expires": at_ms + 86_400_000,
            "fact": f"fact-{record}",
            "span": len(text),
        },
    )
    conn.execute(
        """
        INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
        VALUES (%s, %s, %s, 'leader', %s, %s)
        """,
        (event_id, item_id, at_ms, f"fact-{record}", text),
    )
    seed_current_news_evidence(conn)
    return event_id


def _persist_pre_v3_verdict(
    repos,
    *,
    event_id: str,
    policy_version: str,
    program_version: str,
    at_ms: int = 2000,
) -> None:
    """One `news_judgment_v2` verdict, written by hand because no builder emits that shape any more.

    A migration test's seed has to be what the ledger actually held before the cut, and after #675 §1
    that is a verdict carrying `magnitude` and `audience` inside a `news_editorial_v3` envelope carrying
    `relevance`. `tests.support.news_legacy` builds the v3 contract and nothing else, so the two
    canonical digests the CHECK recomputes are built here from the same `canonical_sha` the worker used.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.taxonomy import IPTC_CODEBOOK_SHA256

    evidence = repos.news.latest_evidence_snapshot(event_id)
    assert evidence is not None
    verdict = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "crypto", "role": "primary"}],
        "direction": "bearish",
        "scope": "single_name",
        "magnitude": 2,
        "confidence": 0.9,
        "audience": "crypto",
        "headline_zh": "阿里巴巴配售新股",
        "why_zh": "",
    }
    editorial_payload = {
        "editorial_contract_version": "news_editorial_v3",
        "editorial_origin": "model",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["earnings_cashflow"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
        "source_authority": "unknown",
        "taxonomy": {
            "subject_codes": [],
            "event_family": "other",
            "change_state": "unknown",
            "assertion_status": "unknown",
            "taxonomy_version": "news_taxonomy_v1",
            "codebook_sha256": IPTC_CODEBOOK_SHA256,
        },
        "taxonomy_status": "available",
        "taxonomy_error_code": None,
    }
    editorial = {**editorial_payload, "editorial_sha256": canonical_sha(editorial_payload)}
    verdict_sha256 = canonical_sha(verdict)
    judgment_sha256 = canonical_sha(
        {
            "judgment_contract_version": "news_judgment_v2",
            "verdict": verdict,
            "editorial": editorial,
            "verdict_sha256": verdict_sha256,
        }
    )
    runtime_manifest_sha = "b" * 64
    assert legacy_news(repos.news).insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=policy_version,
        judgment_contract_version="news_judgment_v2",
        judgment_origin="model",
        rule_baseline_decision="push",
        final_decision="push",
        override_rule="trade_relevance_realtime",
        throttled_by=None,
        verdict=verdict,
        model_editorial=editorial,
        judgment_sha256=judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model="test",
        program_version=program_version,
        program_sha256="a" * 64,
        degraded=False,
        error_code=None,
        trace={
            "judgment_contract_version": "news_judgment_v2",
            "judgment_origin": "model",
            "judgment_sha256": judgment_sha256,
            "verdict_sha256": verdict_sha256,
            "editorial_sha256": editorial["editorial_sha256"],
            "runtime_manifest_sha": runtime_manifest_sha,
            "program_version": program_version,
            "program_sha256": "a" * 64,
            "evidence_version": int(evidence["evidence_version"]),
            "evidence_sha256": str(evidence["evidence_sha256"]),
            "focus_fact_id": str(evidence["focus_fact_id"]),
            "told": [],
            "told_count": 0,
        },
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=at_ms - 1,
    )


def test_local_evidence_migration_preserves_v11_verdict_and_archive(monkeypatch):
    from contextlib import closing

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260919_0384")
    # The seed is a verdict written *before* this cut, so it carries the policy version, the Program
    # version and the whole judgment contract the 0384 CHECK admits. Leaving any of today's values here
    # would make the seed itself the thing the CHECK rejects, and the revision under test would never run.
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        event_id = _seed_admitted_event(conn, "BTC acquisition remains pending approval.")
        _persist_pre_v3_verdict(
            repos,
            event_id=event_id,
            policy_version="news_triage_policy_v14",
            program_version="news_semantic_program_v11",
        )
        conn.execute("""INSERT INTO news_evidence_documents
            (document_id, requested_url, final_url, normalized_url, response_sha256, extracted_text_sha256,
             extractor_version, extracted_text, observed_at_ms, available_at_ms, content_type, extraction_status)
            VALUES ('old', 'https://archive.invalid', 'https://archive.invalid', 'https://archive.invalid',
                    'response', 'text', 'old', 'Pending approval', 1, 1, 'text/plain', 'success')""")
        before = conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v WHERE stage='triage'").fetchall()
        assert before[0]["row"]["program_version"] == "news_semantic_program_v11"
    command.upgrade(config, PRE_CUT)
    command.upgrade(config, PRE_CUT)
    with closing(connect_postgres_test(read_only=False)) as conn:
        assert conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v WHERE stage='triage'").fetchall() == before
        assert (
            conn.execute("SELECT extracted_text FROM news_evidence_documents WHERE document_id='old'").fetchone()[
                "extracted_text"
            ]
            == "Pending approval"
        )
        assert conn.execute("SELECT to_regclass('ix_news_events_evidence_title') AS index").fetchone()["index"]


def test_judgment_v3_migration_keeps_the_v2_verdict_it_finds_and_admits_the_new_one():
    """`20260922_0387`, against the smallest history it can affect: one verdict on the old contract.

    The revision rewrites predicates and one CHECK and touches no row, and the property that matters is
    the one an operator cannot establish by reading it: the predicate it rewrote is the predicate
    PostgreSQL actually held, so the rows already under it stay valid and are not touched, while the
    shape the new Workers emit becomes writable in the same transaction. The refusal below is also why
    the image and the migration cannot be deployed in either order -- the old CHECK rejects exactly what
    the new image writes, on two counts at once: a verdict with `fact_kind` and no `magnitude`, under a
    contract version the v2-only gate does not name.
    """

    from contextlib import closing

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260922_0386")
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        old_event = _seed_admitted_event(conn, "BTC acquisition remains pending approval.", record=665)
        _persist_pre_v3_verdict(
            repos,
            event_id=old_event,
            policy_version="news_triage_policy_v15",
            program_version="news_semantic_program_v12",
        )
        conn.commit()
        before = conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v WHERE stage='triage'").fetchall()
        assert [row["row"]["judgment_contract_version"] for row in before] == ["news_judgment_v2"]
        assert before[0]["row"]["verdict"]["magnitude"] == 2

        blocked = _seed_admitted_event(conn, "ETH acquisition remains pending approval.", record=666)
        with pytest.raises(psycopg.errors.CheckViolation):
            _persist_triage_verdict(repos, event_id=blocked, at_ms=2100, symbol="ETH")
        conn.rollback()

    command.upgrade(config, PRE_CUT)
    # Head to head is a no-op: the revision refuses a predicate it has already rewritten.
    command.upgrade(config, PRE_CUT)

    with closing(connect_postgres_test(read_only=False)) as conn:
        assert conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v WHERE stage='triage'").fetchall() == before
        repos = repositories_for_connection(conn)
        new_event = _seed_admitted_event(conn, "SOL acquisition remains pending approval.", record=667)
        _persist_triage_verdict(repos, event_id=new_event, at_ms=2200, symbol="SOL")
        conn.commit()
        rows = conn.execute(
            "SELECT judgment_contract_version, policy_version, verdict FROM news_verdicts WHERE stage='triage'"
        ).fetchall()
        assert {str(row["judgment_contract_version"]) for row in rows} == {
            "news_judgment_v2",
            "news_judgment_v3",
        }
        # The current Workers' version, which is v17 since `20260923_0390` rather than the v16 this revision
        # itself admitted; the v15 row is the one that matters here.
        assert {str(row["policy_version"]) for row in rows} == {
            "news_triage_policy_v15",
            LEGACY_TRIAGE_POLICY_VERSION,
        }
        # And each shape stays bound to the contract that wrote it: a v3 row states a kind and no
        # magnitude, a v2 row the other way round, and `news_current_verdict_contract_shape_valid` is
        # what refuses the pair swapped.
        shapes = {
            str(row["judgment_contract_version"]): ("fact_kind" in row["verdict"], "magnitude" in row["verdict"])
            for row in rows
        }
        assert shapes == {"news_judgment_v2": (False, True), "news_judgment_v3": (True, False)}

        # The told ledger's predicate is rewritten the same way, and the keys it must keep are the ones
        # a later revision added to it rather than the ones `0350` first wrote: `20260919_0384` made
        # `assets` and `provenance_status` optional by rewriting the stored definition in place, so a
        # rewrite that copied `0350`'s text instead would reject every entry the current writer emits.
        told_entry = {
            "i": 0,
            "event_id": "e" * 64,
            "at_ms": 1_790_091_183_014,
            "ago_min": 3,
            "storyline_key": "asset:crypto:BTC",
            "comparison_title": "title",
            "comparison_fingerprint": "fp",
            "symbols": ["BTC"],
            "assets": [{"symbol": "BTC", "market_type": "crypto"}],
            "direction": "bullish",
            "headline_zh": "\u6807\u9898",
            "why_zh": "",
            "provenance_status": "delivery_bound",
            "tier": "recency",
            "similarity": 0.0,
            "history_scope": "recent",
            "retrieval_reason": "recent",
        }
        # And the shape predicate refuses a row with a missing argument instead of abstaining. A STRICT
        # function returns NULL for a NULL argument, `... AND NULL` is NULL, and a CHECK admits a row
        # whose predicate is NULL -- so a verdict with no `judgment_origin` would have walked straight
        # through the one clause written to bind it to its contract (#679 review 4).
        shape = "SELECT news_current_verdict_contract_shape_valid(%s, %s, %s::jsonb) AS ok"
        v3_verdict = json.dumps({"fact_kind": "state_change", "evidence_ref": "c1"})
        assert conn.execute(shape, ("news_judgment_v3", "model", v3_verdict)).fetchone()["ok"] is True
        for contract, origin, verdict in (
            (None, "model", v3_verdict),
            ("news_judgment_v3", None, v3_verdict),
            ("news_judgment_v3", "model", None),
        ):
            assert conn.execute(shape, (contract, origin, verdict)).fetchone()["ok"] is False

        told_valid = "SELECT news_current_told_trace_valid(%s::jsonb) AS ok"
        assert conn.execute(told_valid, (json.dumps([told_entry]),)).fetchone()["ok"]
        # And the entry a pre-v3 Worker wrote, magnitude and all, is still accepted unchanged.
        assert conn.execute(told_valid, (json.dumps([{**told_entry, "magnitude": 2}]),)).fetchone()["ok"]


def test_the_watchdog_alert_ledger_is_one_additive_table_and_reverses_cleanly() -> None:
    """`20260922_0388` (#680 PR-2): one new platform table, nothing existing touched, a real downgrade.

    The ledger holds only which conditions the operator was last told about, so dropping it is the
    exact reverse -- the next watchdog pass re-alerts whatever is still active -- and the revision
    below it keeps its own forward-only refusal.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260922_0387")
    assert _table_exists("platform_watchdog_alerts") is False

    command.upgrade(config, "20260922_0388")
    assert _stamped_revision() == "20260922_0388"
    assert _table_exists("platform_watchdog_alerts") is True
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO platform_watchdog_alerts
              (condition_key, active, opened_at_ms, notified_at_ms, clear_since_ms, detail, updated_at_ms)
            VALUES ('signal_lane_faulted', true, 1, NULL, NULL, 'x', 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.downgrade(config, "20260922_0387")
    assert _stamped_revision() == "20260922_0387"
    assert _table_exists("platform_watchdog_alerts") is False
    command.upgrade(config, "20260922_0388")
    assert _table_exists("platform_watchdog_alerts") is True


def test_retired_watchdog_ledger_is_dropped_at_0397_and_recreated_empty_on_downgrade() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260924_0396")
    assert _table_exists("platform_watchdog_alerts") is True
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("""
            INSERT INTO platform_watchdog_alerts
              (condition_key, active, opened_at_ms, detail, updated_at_ms)
            VALUES ('plan_overdue', true, 1, 'retired episode', 1)
        """)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception, match="watchdog_alerts_present"):
        command.upgrade(config, "20260925_0397")
    assert _stamped_revision() == "20260924_0396"
    conn = connect_postgres_test(read_only=False)
    try:
        assert conn.execute("SELECT count(*) AS n FROM platform_watchdog_alerts").fetchone()["n"] == 1
        conn.execute("DELETE FROM platform_watchdog_alerts")
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "20260925_0397")
    assert _stamped_revision() == "20260925_0397"
    assert _table_exists("platform_watchdog_alerts") is False
    command.downgrade(config, "20260924_0396")
    assert _table_exists("platform_watchdog_alerts") is True
    conn = connect_postgres_test(read_only=True)
    try:
        assert conn.execute("SELECT count(*) AS n FROM platform_watchdog_alerts").fetchone()["n"] == 0
    finally:
        conn.close()


def test_policy_v17_migration_keeps_the_budget_withholds_it_finds_and_admits_v17() -> None:
    """`20260923_0390`, against the smallest history it can affect: v16 verdicts, one a budget withhold.

    The revision only widens the three policy lists of the judgment CHECK, restated from the definition
    PostgreSQL actually holds. What an operator cannot establish by reading it: the v16 rows -- including a
    `storyline:<key>:budget` withhold the deleted rule wrote, which stays in the ledger as its history --
    keep validating and are not touched; a v17 verdict is refused before the revision and admitted after;
    the literal lands in the model, OI and degraded branches and nowhere else; and a second run is a no-op.
    """

    from contextlib import closing

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260922_0389")
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        pushed = _seed_admitted_event(conn, "Kraken opens BTC options trading to US customers.", record=69001)
        _persist_triage_verdict(
            repos, event_id=pushed, at_ms=2000, symbol="BTC", policy_version="news_triage_policy_v16"
        )
        withheld = _seed_admitted_event(conn, "Lido pauses ETH withdrawals after an oracle fault.", record=69002)
        _persist_triage_verdict(
            repos,
            event_id=withheld,
            at_ms=2100,
            symbol="ETH",
            policy_version="news_triage_policy_v16",
            final_decision="throttled",
            throttled_by="storyline:asset:crypto:ETH:budget",
        )
        conn.commit()
        before = conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v ORDER BY event_id").fetchall()
        assert {row["row"]["throttled_by"] for row in before} == {None, "storyline:asset:crypto:ETH:budget"}
        definition_before = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            "WHERE conrelid = 'news_verdicts'::regclass AND conname = 'news_verdicts_current_judgment_check'"
        ).fetchone()["d"]
        assert "news_triage_policy_v17" not in definition_before

        blocked = _seed_admitted_event(conn, "Solana validators vote to cut the SOL issuance rate.", record=69003)
        with pytest.raises(psycopg.errors.CheckViolation):
            _persist_triage_verdict(repos, event_id=blocked, at_ms=2200, symbol="SOL")
        conn.rollback()

    command.upgrade(config, PRE_CUT)
    # Head to head is a no-op: the revision refuses a predicate it has already rewritten.
    command.upgrade(config, PRE_CUT)

    with closing(connect_postgres_test(read_only=False)) as conn:
        assert conn.execute("SELECT to_jsonb(v) AS row FROM news_verdicts v ORDER BY event_id").fetchall() == before
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            "WHERE conrelid = 'news_verdicts'::regclass AND conname = 'news_verdicts_current_judgment_check'"
        ).fetchone()["d"]
        # The model, OI and degraded branches each gained the literal; nothing else in the predicate moved.
        assert definition.count("'news_triage_policy_v17'::text") == 3
        assert definition.replace(", 'news_triage_policy_v17'::text", "") == definition_before
        repos = repositories_for_connection(conn)
        current = _seed_admitted_event(conn, "Ripple wins an XRP custody licence in Singapore.", record=69004)
        _persist_triage_verdict(repos, event_id=current, at_ms=2300, symbol="XRP")
        conn.commit()
        rows = conn.execute("SELECT policy_version FROM news_verdicts WHERE stage = 'triage'").fetchall()
        assert sorted(str(row["policy_version"]) for row in rows) == [
            "news_triage_policy_v16",
            "news_triage_policy_v16",
            "news_triage_policy_v17",
        ]
        assert LEGACY_TRIAGE_POLICY_VERSION == "news_triage_policy_v17"


def test_event_update_cut_keys_every_delivery_by_its_legacy_intent_without_resending() -> None:
    """0404 backfills the Python legacy intent identity and keeps every row's state and payload."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260926_0403")
    at_ms = 1_790_000_000_000
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            _seed_pre_cut_oi_event(conn, event_id="ev-legacy", leader_item="it-a", member_item="it-b", at_ms=at_ms)
            conn.execute(
                """
                INSERT INTO news_deliveries (event_id, kind, state, card, receipt, attempted_at_ms,
                                             settled_at_ms, created_at_ms)
                VALUES ('ev-legacy', 'first', 'sent', '{"header": {"title": {"content": "旧卡"}}}'::jsonb,
                        '{"provider": "telegram", "message_id": 7}'::jsonb, %s, %s, %s),
                       ('ev-legacy', 'followup', 'terminal', '{}'::jsonb, NULL, %s, %s, %s)
                """,
                (at_ms, at_ms + 1, at_ms, at_ms, at_ms + 2, at_ms),
            )
            conn.execute(
                """
                INSERT INTO news_delivery_queue (event_id, kind, state, attempts, enqueued_at_ms,
                                                 next_attempt_at_ms, last_attempt_at_ms, updated_at_ms)
                VALUES ('ev-legacy', 'followup', 'pending', 1, %s, %s, %s, %s)
                """,
                (at_ms, at_ms, at_ms, at_ms),
            )
        before = {row["kind"]: row for row in conn.execute("SELECT * FROM news_deliveries ORDER BY kind").fetchall()}
        command.upgrade(config, PRE_CUT)
        after = {row["kind"]: row for row in conn.execute("SELECT * FROM news_deliveries").fetchall()}
        for kind, row in before.items():
            assert after[kind]["intent_id"] == legacy_intent_id("ev-legacy", kind)
            assert {key: after[kind][key] for key in row} == row
            assert after[kind]["body"] is after[kind]["payload_sha256"] is after[kind]["claim_refs"] is None
        queued = conn.execute("SELECT intent_id, state, attempts, error_code FROM news_delivery_queue").fetchone()
        assert queued["intent_id"] == legacy_intent_id("ev-legacy", "followup")
        assert (queued["state"], queued["attempts"], queued["error_code"]) == ("dead", 1, "legacy_intent_retired")

        # A legacy kind cannot be written under any other identity.
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """
                INSERT INTO news_delivery_queue (intent_id, event_id, kind, state, enqueued_at_ms,
                                                 next_attempt_at_ms, updated_at_ms)
                VALUES ('intent:' || repeat('0', 64), 'ev-legacy', 'first', 'pending', 1, 1, 1)
                """
            )
        # An update intent must retain the exact body whose digest it claims.
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """
                INSERT INTO news_deliveries (intent_id, event_id, kind, state, card, attempted_at_ms,
                                             created_at_ms, content_revision, claim_refs, body,
                                             payload_sha256, plan_key)
                VALUES ('intent:' || repeat('1', 64), 'ev-legacy', 'update', 'sending', '{}'::jsonb, 1, 1,
                        repeat('a', 64), '["cl:x"]'::jsonb, '正文', repeat('b', 64), false)
                """
            )
        kinds = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conname = 'news_trade_events_kind_check'"
        ).fetchone()
        assert "source_update" in kinds["definition"]
    finally:
        conn.close()


def test_source_revision_chain_migration_preserves_history_and_orders_existing_revisions() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260926_0404")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            _seed_pre_cut_oi_event(conn, event_id="ev-chain", leader_item="it-a", member_item="it-b", at_ms=100)
            for content, stamp in (("a" * 64, 300), ("b" * 64, 200)):
                conn.execute(
                    "INSERT INTO news_item_revisions "
                    "(item_id,revision_sha256,evidence_text,reporting_origin,source_artifact_id,"
                    "published_at_ms,received_at_ms) VALUES ('it-a',%s,%s,'wire','artifact',100,%s)",
                    (content, "Historical source " + content, stamp),
                )
            conn.execute(
                "INSERT INTO news_semantic_observations "
                "(result_id,work_id,event_id,input_revision,input_sha256,program_identity,completed_at_ms,understanding)"
                " VALUES ('result','work','ev-chain',1,repeat('c',64),'original',300,'{}'::jsonb)"
            )
            document = {
                "schema_version": "news_event_update_v1",
                "event_id": "ev-chain",
                "input_revision": 1,
                "content_revision": "d" * 64,
                "previous_content_revision": None,
                "historical_payload": {"body": "不可改写", "topics": ["original"]},
            }
            conn.execute(
                "INSERT INTO news_event_updates "
                "(event_id,content_revision,input_revision,adopted_at_ms,observation_result_id,document)"
                " VALUES ('ev-chain',repeat('d',64),1,300,'result',%s::jsonb)",
                (json.dumps(document),),
            )
        before = conn.execute("SELECT * FROM news_item_revisions ORDER BY received_at_ms").fetchall()
        command.upgrade(config, PRE_CUT)
        after = conn.execute("SELECT * FROM news_item_revisions ORDER BY revision_sequence").fetchall()
        for old, new in zip(before, after, strict=True):
            assert {key: new[key] for key in old} == old
            assert new["content_sha256"] == old["revision_sha256"]
        assert [row["revision_sequence"] for row in after] == [1, 2]
        assert [row["previous_revision_sha256"] for row in after] == [None, "b" * 64]
        assert (
            conn.execute("SELECT evidence_observed_at_ms FROM news_items WHERE item_id='it-a'").fetchone()[
                "evidence_observed_at_ms"
            ]
            == 300
        )
        assert conn.execute("SELECT document FROM news_event_updates").fetchone()["document"] == document
        indexes = {row["indexname"] for row in conn.execute("SELECT indexname FROM pg_indexes").fetchall()}
        assert {
            "news_updates_affected_claims_idx",
            "trading_amendments_affected_idx",
            "trading_amendments_retired_idx",
            "trading_catalyst_superseded_idx",
        } <= indexes
    finally:
        conn.close()


def test_retired_event_update_v1_is_removed_at_the_hard_cut() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, PRE_CUT)
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for name in ("old", "current"):
                conn.execute(
                    """INSERT INTO news_items
                         (item_id,source_id,source_item_key,title,raw_first_line,description,
                          reporting_origin,published_at_ms,observed_at_ms,provider_metadata,
                          provenance,first_ingest_mode,trace_id,created_at_ms,updated_at_ms)
                       VALUES (%s,'opennews',%s,%s,%s,'','opennews',100,100,'{}'::jsonb,
                               '[]'::jsonb,'live','trace',100,100)""",
                    (f"item-{name}", name, name, name),
                )
                conn.execute(
                    """INSERT INTO news_events
                         (event_id,leader_item_id,dedupe_family,comparison_fingerprint,
                          comparison_title,leader_title,opened_at_ms,last_member_at_ms,
                          expires_at_ms,admission,ingest_mode,trace_id,created_at_ms,updated_at_ms,
                          focus_fact_id,focus_fact_text,focus_fact_context,focus_fact_method,
                          focus_span_start,focus_span_end,event_kind)
                       VALUES (%s,%s,'general',%s,%s,%s,100,100,200,'candidate','live',
                               'trace',100,100,%s,%s,'','whole_item',0,1,'news')""",
                    (f"event-{name}", f"item-{name}", name, name, name, f"fact-{name}", name),
                )
            conn.execute(
                """INSERT INTO news_semantic_observations
                     (result_id,work_id,event_id,input_revision,input_sha256,program_identity,
                      completed_at_ms,understanding)
                   VALUES ('result-old','work-old','event-old',1,repeat('a',64),
                           'historical',100,'{}'::jsonb)"""
            )
            conn.execute(
                """INSERT INTO news_event_updates
                     (event_id,content_revision,input_revision,adopted_at_ms,
                      observation_result_id,document)
                   VALUES ('event-old',repeat('b',64),1,100,'result-old',
                     jsonb_build_object('schema_version','news_event_update_v1',
                                        'event_id','event-old','content_revision',repeat('b',64),
                                        'input_revision',1,'previous_content_revision',NULL))"""
            )
        command.upgrade(config, HEAD)
        assert [row["event_id"] for row in conn.execute("SELECT event_id FROM news_events")] == ["event-current"]
        assert {row["item_id"] for row in conn.execute("SELECT item_id FROM news_items")} == {
            "item-old",
            "item-current",
        }
        check = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid='news_event_updates'::regclass AND conname='news_event_updates_document_check'"
        ).fetchone()
        assert check is not None and "news_event_update_v2" in check["definition"]
        assert "news_event_update_v1" not in check["definition"]
    finally:
        conn.close()
