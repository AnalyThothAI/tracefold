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
from tracefold.trading import DailyRiskPolicyV1, ExecutionObservationV1, SettlementRiskLimitV1
from tracefold.trading.storage.execution_stream import (
    ExecutionProfileActivation,
    materialize_operator_intents,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)

_CURRENT_EVENT_ID = "restore-current-event"
_CASE_ID = "restore-trading-case"
_LEGACY_INTENT_ID = "7" * 64
_EXECUTION_SIGNAL_ID = "8" * 64
_EXECUTION_COMMAND_ID = "9" * 64
_EXECUTION_EVENT_ID = "a" * 64
_EXECUTION_PROFILE_ID = "restore-disabled"


def run_restore_drill(admin_dsn: str, migration_dsn: str) -> dict[str, Any]:
    """Compose the generic isolated restore mechanism with News and Trading evidence."""

    return run_platform_restore_drill(
        admin_dsn,
        migration_dsn,
        seed_and_summarize=_seed_and_summarize,
        summarize=_summary,
        smoke=_smoke,
    )


def _seed_and_summarize(dsn: str) -> dict[str, Any]:
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
    signal = prepare_trade_signal(
        signal_id=_EXECUTION_SIGNAL_ID,
        case_id="restore-execution-case",
        alpha_contract_sha256="b" * 64,
        market_key="crypto:perp:BTC:USDT",
        direction="long",
        observed_at_ns=1_000,
        expires_at_ns=10_000,
        evidence_sha256="c" * 64,
        alpha_metadata={"restore": True},
    )
    command = prepare_operator_intent(
        command_id=_EXECUTION_COMMAND_ID,
        target_profile_id=_EXECUTION_PROFILE_ID,
        action="pause_entries",
        scope="account",
        reason="restore drill",
        operator_identity="restore-drill",
        authentication_identity="restore-drill",
        requested_at_ns=1_000,
        expires_at_ns=10_000,
        confirmation_identity=None,
        market_key=None,
        direction=None,
    )
    observation = prepare_execution_observations(
        (
            ExecutionObservationV1(
                event_id=_EXECUTION_EVENT_ID,
                runtime_profile_id=_EXECUTION_PROFILE_ID,
                runtime_release="restore-release",
                execution_strategy="oi-nautilus-v1",
                signal_id=_EXECUTION_SIGNAL_ID,
                normalized_kind="signal_disposition",
                occurred_at_ns=2_000,
                observed_at_ns=2_100,
                summary={"disposition": "expired"},
                payload_digest="d" * 64,
            ),
        )
    )
    activation = ExecutionProfileActivation(
        runtime_profile_id=_EXECUTION_PROFILE_ID,
        account_slot="restore-account",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="disabled",
        runtime_release="restore-release",
        config_sha256="e" * 64,
        created_at_ns=1_500,
    )

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            repos.news.seed_restore_drill_facts(current_event_id=_CURRENT_EVENT_ID)
            repos.trading.blacklist_upsert(
                base_symbol="RESTORE",
                reason="restore_drill",
                expires_at_ms=None,
                now_ms=10,
            )
            repos.trading.seed_restore_drill_case(case_id=_CASE_ID)
            repos.trading.seed_restore_drill_archive_intent(
                intent_id=_LEGACY_INTENT_ID,
                case_id=_CASE_ID,
            )
            if not repos.trading.append_daily_risk_policy(policy, created_at_ms=10):
                raise RuntimeError("postgres_restore_drill_risk_policy_seed_conflict")
            repos.trading.append_trade_signal(signal)
            repos.trading.append_operator_intent(command)
            repos.trading.append_execution_profile_activation(activation)
            repos.trading.append_execution_observations(observation)
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
                   (SELECT count(*) FROM trading_cases WHERE case_id = %s AND state = 'NO_TRADE') AS case_rows,
                   (SELECT max(manifest_sha256) FROM trading_cases WHERE case_id = %s) AS case_manifest_sha256,
                   (SELECT count(*) FROM trading_intents
                     WHERE intent_id = %s AND intent_version = 'trade_intent_v1') AS legacy_intent_rows,
                   (SELECT count(*) FROM trading_symbol_blacklist WHERE base_symbol = 'RESTORE') AS blacklist_rows,
                   (SELECT count(*) FROM trading_daily_risk_policies
                     WHERE approved_release = 'restore-release') AS risk_policy_rows,
                   (SELECT max(risk_policy_sha256) FROM trading_daily_risk_policies
                     WHERE approved_release = 'restore-release') AS risk_policy_sha256,
                   (SELECT count(*) FROM trading_trade_signals
                     WHERE signal_id = %s AND payload ->> 'signal_id' = signal_id) AS execution_signal_rows,
                   (SELECT count(*) FROM trading_operator_intents
                     WHERE command_id = %s AND payload ->> 'command_id' = command_id) AS execution_command_rows,
                   (SELECT count(*) FROM trading_execution_observations
                     WHERE event_id = %s AND payload ->> 'event_id' = event_id) AS execution_observation_rows,
                   (SELECT count(*) FROM trading_execution_profile_activations
                     WHERE runtime_profile_id = %s AND config_sha256 = %s) AS execution_activation_rows
            """,
            (
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CURRENT_EVENT_ID,
                _CASE_ID,
                _CASE_ID,
                _LEGACY_INTENT_ID,
                _EXECUTION_SIGNAL_ID,
                _EXECUTION_COMMAND_ID,
                _EXECUTION_EVENT_ID,
                _EXECUTION_PROFILE_ID,
                "e" * 64,
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
        "legacy_intent_rows",
        "blacklist_rows",
        "risk_policy_rows",
        "execution_signal_rows",
        "execution_command_rows",
        "execution_observation_rows",
        "execution_activation_rows",
    }
    return {key: int(value) if key in numeric else str(value) for key, value in row.items()}


def _smoke(conn: Any) -> dict[str, bool]:
    summary = _summary(conn)
    repos = repositories_for_connection(conn)
    evidence = repos.news.latest_evidence_snapshot(_CURRENT_EVENT_ID)
    delivery = repos.news.delivery(event_id=_CURRENT_EVENT_ID, kind="first")
    case = repos.trading.case(case_id=_CASE_ID)
    policy = repos.trading.daily_risk_policy(summary["risk_policy_sha256"])
    command_rows = repos.trading.unresolved_operator_intents(
        runtime_profile_id=_EXECUTION_PROFILE_ID,
        execution_strategy="oi-nautilus-v1",
        limit=10,
    )
    commands = materialize_operator_intents(command_rows)
    return {
        "migration_head": summary["migration_head"] == latest_migration_version(),
        "news_current_fact": repos.news.event_card(_CURRENT_EVENT_ID) is not None,
        "news_evidence_identity": evidence is not None and evidence["evidence_sha256"] == summary["evidence_sha256"],
        "news_delivery_terminal": delivery is not None and delivery["state"] == "terminal",
        "pre_genesis_compatibility_absent": summary["retired_compatibility_objects"] == 0,
        "trading_case_fact": case is not None
        and case["state"] == "NO_TRADE"
        and case["manifest_sha256"] == summary["case_manifest_sha256"],
        "trading_legacy_archive_preserved": summary["legacy_intent_rows"] == 1,
        "trading_legacy_archive_excluded": repos.trading.intent(_LEGACY_INTENT_ID) is None,
        "trading_blacklist_fact": summary["blacklist_rows"] == 1,
        "trading_risk_policy_fact": policy is not None and policy.risk_policy_sha256 == summary["risk_policy_sha256"],
        "trading_execution_stream_facts": all(
            summary[key] == 1
            for key in (
                "execution_signal_rows",
                "execution_command_rows",
                "execution_observation_rows",
                "execution_activation_rows",
            )
        ),
        "trading_execution_stream_read": len(commands) == 1 and commands[0].command_id == _EXECUTION_COMMAND_ID,
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
