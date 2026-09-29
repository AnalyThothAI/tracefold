"""Application-owned durable-fact seed and smoke checks for the PostgreSQL restore drill."""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.postgres.restore_drill import run_restore_drill as run_platform_restore_drill
from tracefold.trading.executor.core import SignalV4
from tracefold.trading.operator_control import prepare_operator_intent

_CURRENT_EVENT_ID = "restore-current-event"
_CASE_ID = "a" * 64
_SIGNAL_ID = "8" * 64
_COMMAND_ID = "9" * 64
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
    signal = SignalV4(
        seq=1,
        signal_id=_SIGNAL_ID,
        case_id=_CASE_ID,
        decision_id="b" * 64,
        account_slot=_ACCOUNT_SLOT,
        entry_scope_id="c" * 64,
        asset_id="crypto:RESTORE",
        native_symbol="RESTOREUSDT",
        mapping_semantics_digest="d" * 64,
        side="long",
        reference_price=Decimal("1"),
        max_drift_bps=200,
        stop_bps=100,
        tp_bps=200,
        max_hold_s=3600,
        policy_id="restore",
        policy_version="v1",
        geometry_version="v1",
        decided_at_ns=1_000,
        expires_at_ns=1_000 + 300_000_000_000,
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
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            repos.news.seed_restore_drill_facts(current_event_id=_CURRENT_EVENT_ID)
            repos.trading.seed_restore_drill_case(case_id=_CASE_ID)
            repos.trading.append_signal(signal)
            repos.trading.append_operator_intent(command)
            repos.trading.record_disposition(
                kind="signal",
                input_id=_SIGNAL_ID,
                account_slot=_ACCOUNT_SLOT,
                disposition="expired",
                reason="expired",
                now_ns=2_000,
            )
        return _summary(conn)


def _summary(conn: Any) -> dict[str, Any]:
    row = dict(
        conn.execute(
            """
            SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                   (SELECT count(*) FROM news_items WHERE left(item_id, 8) = 'restore-') AS news_items,
                   (SELECT count(*) FROM news_events WHERE event_id = %s) AS current_events,
                   (SELECT count(*) FROM news_event_evidence_snapshots WHERE event_id = %s) AS evidence_rows,
                   (SELECT max(evidence_sha256) FROM news_event_evidence_snapshots WHERE event_id = %s)
                     AS evidence_sha256,
                   (SELECT count(*) FROM news_deliveries WHERE event_id = %s AND state = 'terminal')
                     AS delivery_rows,
                   (SELECT count(*) FROM news_event_updates WHERE event_id = %s) AS update_rows,
                   (SELECT count(*) FROM news_notification_decisions WHERE event_id = %s)
                     AS decision_rows,
                   (SELECT count(*) FROM trading_cases
                     WHERE case_id = %s AND state = 'complete') AS case_rows,
                   (SELECT max(view_sha256) FROM trading_cases WHERE case_id = %s) AS case_view_sha256,
                   (SELECT count(*) FROM trading_signals
                     WHERE signal_id = %s AND case_id = %s AND payload ->> 'signal_id' = signal_id) AS signal_rows,
                   (SELECT count(*) FROM trading_operator_intents
                     WHERE command_id = %s AND payload ->> 'command_id' = command_id) AS command_rows,
                   (SELECT count(*) FROM trading_dispositions
                     WHERE input_kind='signal' AND input_id = %s AND disposition='expired') AS disposition_rows
            """,
            (
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CASE_ID,
                _CASE_ID,
                _SIGNAL_ID,
                _CASE_ID,
                _COMMAND_ID,
                _SIGNAL_ID,
            ),
        ).fetchone()
    )
    numeric = {
        "news_items",
        "current_events",
        "evidence_rows",
        "delivery_rows",
        "update_rows",
        "decision_rows",
        "case_rows",
        "signal_rows",
        "command_rows",
        "disposition_rows",
    }
    return {key: int(value) if key in numeric else str(value) for key, value in row.items()}


def _smoke(conn: Any) -> dict[str, bool]:
    summary = _summary(conn)
    repos = repositories_for_connection(conn)
    evidence = repos.news.latest_evidence_snapshot(_CURRENT_EVENT_ID)
    delivery = conn.execute(
        """SELECT d.state,d.kind,d.decision_ref,n.plan,u.document
             FROM news_deliveries d
             JOIN news_notification_decisions n ON n.decision_ref=d.decision_ref
             JOIN news_event_updates u ON u.event_id=n.event_id
              AND n.update_ref=public.news_identity('update',jsonb_build_array(u.event_id,u.content_revision))
            WHERE d.event_id=%s AND d.kind='update'""",
        (_CURRENT_EVENT_ID,),
    ).fetchone()
    case = repos.trading.restore_drill_case(case_id=_CASE_ID)
    signal = repos.trading.next_signal(account_slot=_ACCOUNT_SLOT, after_seq=0)
    command = repos.trading.next_intent(account_slot=_ACCOUNT_SLOT, after_seq=0)
    return {
        "migration_head": summary["migration_head"] == latest_migration_version(),
        "news_current_fact": repos.news.event_card(_CURRENT_EVENT_ID) is not None,
        "news_evidence_identity": evidence is not None and evidence["evidence_sha256"] == summary["evidence_sha256"],
        "news_delivery_terminal": delivery is not None
        and delivery["state"] == "terminal"
        and delivery["plan"]["action"] == "notify"
        and delivery["document"]["event_id"] == _CURRENT_EVENT_ID
        and summary["update_rows"] == summary["decision_rows"] == summary["delivery_rows"] == 1,
        "trading_case_fact": case is not None
        and case["state"] == "complete"
        and case["view_sha256"] == summary["case_view_sha256"],
        "trading_signal_fact": summary["signal_rows"] == 1,
        "trading_execution_facts": all(summary[key] == 1 for key in ("command_rows", "disposition_rows")),
        "trading_execution_read": signal is not None
        and signal.signal_id == _SIGNAL_ID
        and command is not None
        and command["command_id"] == _COMMAND_ID,
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
