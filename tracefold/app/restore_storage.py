"""Application-owned durable-fact seed and smoke checks for the PostgreSQL restore drill."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.postgres.restore_drill import run_restore_drill as run_platform_restore_drill
from tracefold.trading import EXECUTION_STRATEGY_ID, ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    materialize_operator_intents,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)

_CURRENT_EVENT_ID = "restore-current-event"
_CASE_ID = "restore-trading-case"
_SIGNAL_ID = "8" * 64
_COMMAND_ID = "9" * 64
_OBSERVATION_ID = "a" * 64
_ACCOUNT_SLOT = "restore-account"
# The Command read is bounded by its own TTL now, so the drill's Command has to be live when the
# restored database is smoke-tested rather than frozen at a fixed nanosecond (#520 PR-A).
_COMMAND_TTL_NS = 3_600_000_000_000


def run_restore_drill(admin_dsn: str, migration_dsn: str) -> dict[str, Any]:
    return run_platform_restore_drill(
        admin_dsn,
        migration_dsn,
        seed_and_summarize=_seed_and_summarize,
        summarize=_summary,
        smoke=_smoke,
    )


def _seed_and_summarize(dsn: str) -> dict[str, Any]:
    signal = prepare_trade_signal(
        signal_id=_SIGNAL_ID,
        case_id=_CASE_ID,
        market_key="crypto:perp:RESTORE:USDT",
        direction="long",
        observed_at_ns=1_000,
        expires_at_ns=10_000,
        alpha_metadata={"restore": True},
    )
    requested_at_ns = time.time_ns()
    command = prepare_operator_intent(
        command_id=_COMMAND_ID,
        account_slot=_ACCOUNT_SLOT,
        action="pause_entries",
        scope="account",
        reason="restore drill",
        operator_identity="restore-drill",
        authentication_identity="restore-drill",
        requested_at_ns=requested_at_ns,
        expires_at_ns=requested_at_ns + _COMMAND_TTL_NS,
        market_key=None,
        direction=None,
    )
    observations = prepare_execution_observations(
        (
            ExecutionObservationV1(
                event_id=_OBSERVATION_ID,
                account_slot=_ACCOUNT_SLOT,
                runtime_release="restore-release",
                execution_strategy=EXECUTION_STRATEGY_ID,
                signal_id=_SIGNAL_ID,
                normalized_kind="signal_disposition",
                occurred_at_ns=2_000,
                observed_at_ns=2_100,
                summary={"disposition": "expired"},
            ),
        )
    )
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            repos.news.seed_restore_drill_facts(current_event_id=_CURRENT_EVENT_ID)
            repos.trading.seed_restore_drill_case(case_id=_CASE_ID)
            repos.trading.append_trade_signal(signal)
            repos.trading.append_operator_intent(command)
            repos.trading.append_execution_observations(observations)
        return _summary(conn)


def _summary(conn: Any) -> dict[str, Any]:
    row = dict(
        conn.execute(
            """
            SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                   (SELECT count(*) FROM news_items WHERE left(item_id, 8) = 'restore-') AS news_items,
                   (SELECT count(*) FROM news_events WHERE event_id = %s) AS current_events,
                   (SELECT count(*) FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name IN ('news_events', 'news_reviews')
                       AND column_name = 'current_contract_archive_only')
                     + (SELECT count(*) FROM pg_class relation
                          JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                         WHERE namespace.nspname = 'public'
                           AND relation.relname = 'news_current_events_v1') AS retired_compatibility_objects,
                   (SELECT count(*) FROM news_event_evidence_snapshots WHERE event_id = %s) AS evidence_rows,
                   (SELECT max(evidence_sha256) FROM news_event_evidence_snapshots WHERE event_id = %s)
                     AS evidence_sha256,
                   (SELECT count(*) FROM news_deliveries WHERE event_id = %s AND state = 'terminal')
                     AS delivery_rows,
                   (SELECT count(*) FROM trading_cases
                     WHERE case_id = %s AND state = 'SIGNAL_EMITTED') AS case_rows,
                   (SELECT max(manifest_sha256) FROM trading_cases WHERE case_id = %s) AS case_manifest_sha256,
                   (SELECT count(*) FROM trading_trade_signals
                     WHERE signal_id = %s AND case_id = %s AND payload ->> 'signal_id' = signal_id) AS signal_rows,
                   (SELECT count(*) FROM trading_operator_intents
                     WHERE command_id = %s AND payload ->> 'command_id' = command_id) AS command_rows,
                   (SELECT count(*) FROM trading_execution_observations
                     WHERE event_id = %s AND payload ->> 'event_id' = event_id) AS observation_rows
            """,
            (
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CASE_ID,
                _CASE_ID,
                _SIGNAL_ID,
                _CASE_ID,
                _COMMAND_ID,
                _OBSERVATION_ID,
            ),
        ).fetchone()
    )
    numeric = {
        "news_items",
        "current_events",
        "retired_compatibility_objects",
        "evidence_rows",
        "delivery_rows",
        "case_rows",
        "signal_rows",
        "command_rows",
        "observation_rows",
    }
    return {key: int(value) if key in numeric else str(value) for key, value in row.items()}


def _smoke(conn: Any) -> dict[str, bool]:
    summary = _summary(conn)
    repos = repositories_for_connection(conn)
    evidence = repos.news.latest_evidence_snapshot(_CURRENT_EVENT_ID)
    delivery = repos.news.delivery(event_id=_CURRENT_EVENT_ID, kind="first")
    case = repos.trading.case(case_id=_CASE_ID)
    commands = materialize_operator_intents(
        repos.trading.unresolved_operator_intents(
            account_slot=_ACCOUNT_SLOT,
            execution_strategy=EXECUTION_STRATEGY_ID,
            now_ns=time.time_ns(),
            limit=10,
        )
    )
    return {
        "migration_head": summary["migration_head"] == latest_migration_version(),
        "news_current_fact": repos.news.event_card(_CURRENT_EVENT_ID) is not None,
        "news_evidence_identity": evidence is not None and evidence["evidence_sha256"] == summary["evidence_sha256"],
        "news_delivery_terminal": delivery is not None and delivery["state"] == "terminal",
        "pre_genesis_compatibility_absent": summary["retired_compatibility_objects"] == 0,
        "trading_case_fact": case is not None
        and case["state"] == "SIGNAL_EMITTED"
        and case["manifest_sha256"] == summary["case_manifest_sha256"],
        "trading_signal_fact": summary["signal_rows"] == 1,
        "trading_execution_stream_facts": all(summary[key] == 1 for key in ("command_rows", "observation_rows")),
        "trading_execution_stream_read": len(commands) == 1 and commands[0].command_id == _COMMAND_ID,
    }


def main() -> None:
    admin_dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN")
    migration_dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_MIGRATION_DSN")
    if not admin_dsn:
        raise SystemExit("TRACEFOLD_TEST_POSTGRES_DSN is required")
    if not migration_dsn:
        raise SystemExit("TRACEFOLD_TEST_POSTGRES_MIGRATION_DSN is required")
    print(json.dumps(run_restore_drill(admin_dsn, migration_dsn), sort_keys=True))


if __name__ == "__main__":
    main()
