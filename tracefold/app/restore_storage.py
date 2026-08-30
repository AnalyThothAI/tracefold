"""Application-owned durable-fact seed and smoke checks for the PostgreSQL restore drill."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.postgres.restore_drill import run_restore_drill as run_platform_restore_drill
from tracefold.trading import DailyRiskPolicyV1, SettlementRiskLimitV1

_CURRENT_EVENT_ID = "restore-current-event"
_ARCHIVE_EVENT_ID = "restore-archive-event"
_CASE_ID = "restore-trading-case"


def run_restore_drill(admin_dsn: str) -> dict[str, Any]:
    """Compose the generic isolated restore mechanism with News and Trading evidence."""

    return run_platform_restore_drill(
        admin_dsn,
        seed_and_summarize=_seed_and_summarize,
        summarize=_summary,
        smoke=_smoke,
    )


def _seed_and_summarize(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        repos = repositories_for_connection(conn)
        repos.news.seed_restore_drill_facts(
            current_event_id=_CURRENT_EVENT_ID,
            archive_event_id=_ARCHIVE_EVENT_ID,
        )
        repos.trading.blacklist_upsert(
            base_symbol="RESTORE",
            reason="restore_drill",
            expires_at_ms=None,
            now_ms=10,
        )
        repos.trading.seed_restore_drill_case(case_id=_CASE_ID)
        policy = DailyRiskPolicyV1(
            approved_release="restore-release",
            cost_model_sha256="c" * 64,
            max_committed_entry_attempts=1,
            max_target_notional=Decimal("10"),
            settlement_limits=(
                SettlementRiskLimitV1(
                    settlement_asset="USDT",
                    max_planned_risk_amount=Decimal("1"),
                    max_realized_loss_amount=Decimal("1"),
                    fee_slippage_reserve_bps=10,
                ),
            ),
            issuer="restore-drill",
            issued_at_ms=10,
            effective_from_ms=10,
            expires_at_ms=100,
        )
        if not repos.trading.append_daily_risk_policy(policy, created_at_ms=10):
            raise RuntimeError("postgres_restore_drill_risk_policy_seed_conflict")
        conn.commit()
        return _summary(conn)


def _summary(conn: Any) -> dict[str, Any]:
    row = dict(
        conn.execute(
            """
            SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                   (SELECT count(*) FROM news_items WHERE left(item_id, 8) = 'restore-') AS news_items,
                   (SELECT count(*) FROM news_current_events_v1 WHERE event_id = %s) AS current_events,
                   (SELECT count(*) FROM news_events WHERE event_id = %s) AS archive_events,
                   (SELECT count(*) FROM news_current_events_v1 WHERE event_id = %s) AS archive_in_current,
                   (SELECT count(*) FROM news_event_evidence_snapshots WHERE event_id = %s) AS evidence_rows,
                   (SELECT max(evidence_sha256) FROM news_event_evidence_snapshots WHERE event_id = %s)
                     AS evidence_sha256,
                   (SELECT count(*) FROM news_deliveries WHERE event_id = %s AND state = 'terminal')
                     AS delivery_rows,
                   (SELECT count(*) FROM trading_cases WHERE case_id = %s AND state = 'NO_TRADE') AS case_rows,
                   (SELECT max(manifest_sha256) FROM trading_cases WHERE case_id = %s) AS case_manifest_sha256,
                   (SELECT count(*) FROM trading_symbol_blacklist WHERE base_symbol = 'RESTORE') AS blacklist_rows,
                   (SELECT count(*) FROM trading_daily_risk_policies
                     WHERE approved_release = 'restore-release') AS risk_policy_rows,
                   (SELECT max(risk_policy_sha256) FROM trading_daily_risk_policies
                     WHERE approved_release = 'restore-release') AS risk_policy_sha256
            """,
            (
                _CURRENT_EVENT_ID,
                _ARCHIVE_EVENT_ID,
                _ARCHIVE_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CASE_ID,
                _CASE_ID,
            ),
        ).fetchone()
    )
    numeric = {
        "news_items",
        "current_events",
        "archive_events",
        "archive_in_current",
        "evidence_rows",
        "delivery_rows",
        "case_rows",
        "blacklist_rows",
        "risk_policy_rows",
    }
    return {key: int(value) if key in numeric else str(value) for key, value in row.items()}


def _smoke(conn: Any) -> dict[str, bool]:
    summary = _summary(conn)
    repos = repositories_for_connection(conn)
    evidence = repos.news.latest_evidence_snapshot(_CURRENT_EVENT_ID)
    delivery = repos.news.delivery(event_id=_CURRENT_EVENT_ID, kind="first")
    case = repos.trading.case(case_id=_CASE_ID)
    policy = repos.trading.daily_risk_policy(summary["risk_policy_sha256"])
    return {
        "migration_head": summary["migration_head"] == latest_migration_version(),
        "news_current_fact": repos.news.event_card(_CURRENT_EVENT_ID) is not None,
        "news_evidence_identity": evidence is not None and evidence["evidence_sha256"] == summary["evidence_sha256"],
        "news_delivery_terminal": delivery is not None and delivery["state"] == "terminal",
        "archive_excluded_from_current": summary["archive_events"] == 1 and summary["archive_in_current"] == 0,
        "trading_case_fact": case is not None
        and case["state"] == "NO_TRADE"
        and case["manifest_sha256"] == summary["case_manifest_sha256"],
        "trading_blacklist_fact": summary["blacklist_rows"] == 1,
        "trading_risk_policy_fact": policy is not None and policy.risk_policy_sha256 == summary["risk_policy_sha256"],
    }


def main() -> None:
    dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("TRACEFOLD_TEST_POSTGRES_DSN is required")
    print(json.dumps(run_restore_drill(dsn), sort_keys=True))


if __name__ == "__main__":
    main()
