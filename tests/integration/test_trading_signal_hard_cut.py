"""Real PostgreSQL proof for the 0341 Case/Signal hard cut."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
    news_genesis_test_evidence,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
    temporary_unmigrated_postgres_database,
)
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import alembic_config, latest_migration_version
from tracefold.trading.storage.execution_stream import PreparedTradeSignal, prepare_trade_signal
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.migration]


def _insert_case(conn: Any, *, case_id: str, state: str) -> None:
    """Insert one Case against the current schema, which has no capital columns (`20260903_0355`)."""

    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, primary_source_key,
          supplemental_source_keys, manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms, strategy_id, strategy_version, strategy_config_digest
        ) VALUES (
          %s, %s, 'news', %s, '[]'::jsonb, '{"test":"signal-hard-cut"}'::jsonb,
          %s, %s, %s, 'test_fixture', 1, 1, 1, 1,
          'signal_hard_cut_fixture', 'v1', %s
        )
        """,
        (
            case_id,
            f"hard-cut:{case_id}",
            f"hard-cut-source:{case_id}",
            "a" * 64,
            state,
            "long" if state == "SIGNAL_EMITTED" else "no_trade",
            "b" * 64,
        ),
    )


def _insert_case_at_0340(conn: Any, *, case_id: str, state: str) -> None:
    """The same Case at `20260831_0340`, where `capital_disposition` is still NOT NULL."""

    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, primary_source_key,
          supplemental_source_keys, manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms, strategy_id, strategy_version, strategy_config_digest,
          capital_disposition, capital_reason
        ) VALUES (
          %s, %s, 'news', %s, '[]'::jsonb, '{"test":"signal-hard-cut"}'::jsonb,
          %s, %s, %s, 'test_fixture', 1, 1, 1, 1,
          'signal_hard_cut_fixture', 'v1', %s, 'not_applicable', NULL
        )
        """,
        (
            case_id,
            f"hard-cut:{case_id}",
            f"hard-cut-source:{case_id}",
            "a" * 64,
            state,
            "long" if state in {"INTENT_EMITTED", "SIGNAL_EMITTED", "ORDER_PREPARED"} else "no_trade",
            "b" * 64,
        ),
    )


def _signal(
    *,
    case_id: str,
    suffix: str = "c",
    expires_at_ns: int = 10_000,
) -> PreparedTradeSignal:
    return prepare_trade_signal(
        signal_id=suffix * 64,
        case_id=case_id,
        alpha_contract_sha256="d" * 64,
        market_key="crypto:perp:SOL:USDT",
        direction="long",
        observed_at_ns=1_000,
        expires_at_ns=expires_at_ns,
        evidence_sha256="e" * 64,
        alpha_metadata={"policy_rule": "test"},
    )


def _seed_not_paused(conn: Any) -> None:
    assert conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1").rowcount == 1


def _seed_pending_case(conn: Any) -> None:
    _insert_case_at_0340(conn, case_id="preflight-pending-case", state="PENDING")


def _seed_pending_intent(conn: Any) -> None:
    case_id = "preflight-pending-intent"
    _insert_case_at_0340(conn, case_id=case_id, state="INTENT_EMITTED")
    # 0340 already forbids creating a new v1 row. Disabling only that insert guard recreates an
    # historical row that could still exist at cutover; all table constraints and FKs remain active.
    conn.execute("ALTER TABLE trading_intents DISABLE TRIGGER trg_trading_intents_v3_only")
    conn.execute(
        """
        INSERT INTO trading_intents (
          intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
          execution_environment, instrument_id, side, created_at_ms, valid_until_ms,
          reference_price, target_notional_usd, stop_loss_bps, max_holding_ms,
          max_entry_drift_bps, max_spread_bps, execution_state
        ) VALUES (
          %s, 'trade_intent_v1', %s, %s, %s, 'BINANCE_USDM_DEMO', 'SOLUSDT',
          'long', 1, 60001, 100, 10, 200, 180000, 25, 30, 'PENDING'
        )
        """,
        ("f" * 64, case_id, "a" * 64, "1" * 64),
    )
    conn.execute("ALTER TABLE trading_intents ENABLE TRIGGER trg_trading_intents_v3_only")


def _seed_nonterminal_order(conn: Any) -> None:
    case_id = "preflight-open-order"
    _insert_case_at_0340(conn, case_id=case_id, state="ORDER_PREPARED")
    conn.execute(
        """
        INSERT INTO trading_orders (
          order_id, case_id, underlying_key, exchange_id, provider_symbol, account_ref,
          mode, side, notional_usd, quantity, entry_reference, stop_price,
          payload, payload_sha256, state, created_at_ms, updated_at_ms
        ) VALUES (
          'preflight-order', %s, 'crypto:SOL', 'paper', 'SOLUSDT', 'test-account',
          'paper', 'buy', 10, 0.1, 100, 90, '{}'::jsonb, %s, 'PREPARED', 1, 1
        )
        """,
        (case_id, "2" * 64),
    )


def _seed_exposure(conn: Any) -> None:
    assert (
        conn.execute(
            "UPDATE trading_binding_runtime SET account_state = 'exposure_present' WHERE binding = 'BINANCE_USDM'"
        ).rowcount
        == 1
    )


def _seed_unreconciled_previously_executable_account(conn: Any) -> None:
    assert (
        conn.execute(
            """
            UPDATE trading_binding_runtime
               SET credential_state = 'unconfigured', credential_fingerprint = NULL,
                   account_generation = 2, account_state = 'unknown',
                   reason = 'account_reconciliation_unproven'
             WHERE binding = 'BINANCE_USDM'
            """
        ).rowcount
        == 1
    )


def _seed_preexisting_signal(conn: Any) -> None:
    with conn.transaction():
        TradingRepository(conn).append_trade_signal(_signal(case_id="preflight-no-case"))


@contextmanager
def _at_0340(postgres_server_dsn: str) -> Iterator[tuple[str, Any]]:
    with temporary_unmigrated_postgres_database(postgres_server_dsn) as dsn:
        prepare_test_migration_database(dsn)
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)
        with news_genesis_test_evidence():
            command.upgrade(config, "20260831_0340")
        with connect_postgres(dsn) as conn:
            yield dsn, conn


@contextmanager
def _at_0354(postgres_server_dsn: str) -> Iterator[tuple[str, Any]]:
    """One database at the revision before the dead-column drop."""

    with temporary_unmigrated_postgres_database(postgres_server_dsn) as dsn:
        prepare_test_migration_database(dsn)
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)
        with news_genesis_test_evidence():
            command.upgrade(config, "20260903_0354")
        with connect_postgres(dsn) as conn:
            yield dsn, conn


def _seed_retired_case_state(conn: Any) -> None:
    # 0341's trigger already refuses to *write* a retired state. Disabling only that guard recreates a
    # row an older writer left behind, which is exactly what 0355's count has to find. Its admission
    # row is `CASE_CREATED` — not itself retired — so it is the foreign key the operator's archive
    # step has to clear first, and `docs/MIGRATIONS.md` deletes the ledger before the Case for it.
    conn.execute("ALTER TABLE trading_cases DISABLE TRIGGER reject_retired_case_state")
    _insert_case_at_0340(conn, case_id="retired-state-case", state="POLICY_REJECTED")
    conn.execute("ALTER TABLE trading_cases ENABLE TRIGGER reject_retired_case_state")
    conn.execute(
        """
        INSERT INTO trading_candidate_gate_decisions (
          source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
          source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
          first_evaluated_at_ms, last_evaluated_at_ms, attempt_count, release_revision
        ) VALUES (
          'oi:retired-state-case:v1', 'trading_admission_v3', %s, 'oi', 'crypto:RETIRED',
          1, 'CASE_CREATED', 'freeze', 'case_created', false, '{}'::jsonb, 'retired-state-case',
          1, 1, 1, 'test'
        )
        """,
        ("d" * 64,),
    )


def _seed_retired_gate_decision(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO trading_candidate_gate_decisions (
          source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
          source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
          first_evaluated_at_ms, last_evaluated_at_ms, attempt_count, release_revision
        ) VALUES (
          'oi:retired-stage:v1', 'trading_admission_v3', %s, 'oi', 'crypto:RETIRED',
          1, 'RESEARCH_ONLY', 'routing', 'unsupported_venue', false, '{}'::jsonb, NULL,
          1, 1, 1, 'test'
        )
        """,
        ("c" * 64,),
    )


@pytest.mark.parametrize(
    ("seed", "purge"),
    [
        (
            _seed_retired_case_state,
            # Exactly the two statements `docs/MIGRATIONS.md` gives the operator, in that order.
            "DELETE FROM trading_candidate_gate_decisions"
            " WHERE status = 'RESEARCH_ONLY'"
            "    OR stage IN ('capability', 'catalog', 'routing')"
            "    OR case_id IN (SELECT case_id FROM trading_cases"
            "                    WHERE state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED'));"
            " DELETE FROM trading_cases"
            "  WHERE state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED')",
        ),
        (
            _seed_retired_gate_decision,
            "DELETE FROM trading_candidate_gate_decisions"
            " WHERE status = 'RESEARCH_ONLY' OR stage IN ('capability', 'catalog', 'routing')",
        ),
    ],
    ids=("case-state", "gate-decision"),
)
def test_0355_refuses_a_retired_value_it_cannot_archive_and_passes_once_the_row_is_gone(
    postgres_server_dsn: str,
    seed: Callable[[Any], None],
    purge: str,
) -> None:
    """The narrowed CHECKs are unreachable while a stored row still uses the vocabulary.

    Deleting a historical row is the operator's decision, not a migration's, so the revision counts
    them first and refuses by name. `docs/MIGRATIONS.md` carries the archive step the refusal points at.
    """

    with _at_0354(postgres_server_dsn) as (dsn, conn):
        seed(conn)
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)

        with pytest.raises(Exception, match="trading_retired_values_present"):
            command.upgrade(config, "head")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260903_0354"

        conn.execute(purge)
        conn.commit()
        command.upgrade(config, "head")
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
            == latest_migration_version()
        )
        assert (
            conn.execute(
                """
                SELECT count(*) AS n FROM information_schema.columns
                 WHERE table_name = 'trading_cases'
                   AND column_name IN ('regime', 'program_version', 'program_sha256',
                                       'program_output', 'capital_disposition', 'capital_reason')
                """
            ).fetchone()["n"]
            == 0
        )


def test_0355_leaves_one_owner_for_each_closed_trading_vocabulary(postgres_clone_dsn: str) -> None:
    """At head: no dead Case column, no retired-value trigger, and a CHECK that refuses each name."""

    del postgres_clone_dsn
    conn = connect_postgres_test(read_only=False)
    try:
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'trading_cases'"
            ).fetchall()
        }
        triggers = {
            str(row["tgname"])
            for row in conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        }
        assert columns.isdisjoint(
            {
                "regime",
                "program_version",
                "program_sha256",
                "program_output",
                "capital_disposition",
                "capital_reason",
            }
        )
        assert triggers.isdisjoint({"reject_retired_case_state", "trg_trading_candidate_gate_stage_hard_cut"})

        with (
            pytest.raises(psycopg.errors.CheckViolation, match="trading_candidate_gate_status_check"),
            conn.transaction(),
        ):
            _insert_gate_decision(conn, source_key="oi:retired-status:v1", status="RESEARCH_ONLY", stage="source")
        for retired_stage in ("capability", "catalog", "routing"):
            with (
                pytest.raises(psycopg.errors.CheckViolation, match="trading_candidate_gate_stage_check"),
                conn.transaction(),
            ):
                _insert_gate_decision(
                    conn, source_key=f"oi:retired-{retired_stage}:v1", status="REJECTED", stage=retired_stage
                )
        for retired_state in ("POLICY_REJECTED", "INTENT_EMITTED", "ORDER_PREPARED"):
            with (
                pytest.raises(psycopg.errors.CheckViolation, match="trading_cases_state_check"),
                conn.transaction(),
            ):
                _insert_case(conn, case_id=f"retired-{retired_state}", state=retired_state)
    finally:
        conn.close()


def _insert_gate_decision(conn: Any, *, source_key: str, status: str, stage: str) -> None:
    conn.execute(
        """
        INSERT INTO trading_candidate_gate_decisions (
          source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
          source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
          first_evaluated_at_ms, last_evaluated_at_ms, attempt_count, release_revision
        ) VALUES (
          %s, 'trading_admission_v8', %s, 'oi', 'crypto:RETIRED',
          1, %s, %s, 'test_fixture', false, '{}'::jsonb, NULL, 1, 1, 1, 'test'
        )
        """,
        (source_key, "c" * 64, status, stage),
    )


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        (_seed_not_paused, "trading_signal_cutover_requires_paused"),
        (_seed_pending_case, "trading_signal_cutover_case_nonterminal"),
        (_seed_pending_intent, "trading_signal_cutover_intent_nonterminal"),
        (_seed_nonterminal_order, "trading_signal_cutover_order_nonterminal"),
        (_seed_exposure, "trading_signal_cutover_exposure_present"),
        (
            _seed_unreconciled_previously_executable_account,
            "trading_signal_cutover_account_not_reconciled_flat",
        ),
        (_seed_preexisting_signal, "trading_signal_cutover_preexisting_signal"),
    ],
    ids=("paused", "case", "intent", "order", "exposure", "unknown-account", "signal"),
)
def test_0341_preflight_rolls_back_each_unsafe_cutover_state(
    postgres_server_dsn: str,
    seed: Callable[[Any], None],
    reason: str,
) -> None:
    with _at_0340(postgres_server_dsn) as (dsn, conn):
        seed(conn)
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)
        with pytest.raises(Exception, match=reason):
            command.upgrade(config, "20260901_0341")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260831_0340"


@pytest.mark.parametrize("account_state", ("never_configured", "reconciled_flat"))
def test_0341_allows_only_safe_account_cutover_states(
    postgres_server_dsn: str,
    account_state: str,
) -> None:
    with _at_0340(postgres_server_dsn) as (dsn, conn):
        if account_state == "reconciled_flat":
            assert (
                conn.execute(
                    """
                    UPDATE trading_binding_runtime
                       SET account_generation = 1, account_state = 'reconciled_flat',
                           reason = 'account_reconciled_flat'
                     WHERE binding = 'BINANCE_USDM'
                    """
                ).rowcount
                == 1
            )
            conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)
        command.upgrade(config, "20260901_0341")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260901_0341"


def test_0341_makes_retired_execution_tables_read_only(postgres_server_dsn: str) -> None:
    """The guard 0347 later replaces with the tables' absence, proven where it still exists.

    Pinned at 0341 rather than at head: `20260901_0347` drops all 22 of these tables, so at head there
    is nothing left to refuse a write. An operator upgrading a database that stops at 0341 still gets
    this trigger, and this is the only place that says so.
    """

    with _at_0340(postgres_server_dsn) as (dsn, conn):
        config = alembic_config()
        config.attributes["database_url"] = postgres_migration_test_dsn(dsn)
        command.upgrade(config, "20260901_0341")
        with (
            pytest.raises(psycopg.errors.RaiseException, match="retired_trading_execution_table_read_only"),
            conn.transaction(),
        ):
            conn.execute("UPDATE trading_runtime_state SET orders_today = orders_today WHERE id = 1")


def test_0341_enforces_the_atomic_case_signal_pair(
    postgres_clone_dsn: str,
) -> None:
    del postgres_clone_dsn
    conn = connect_postgres_test(read_only=False)
    try:
        with (
            pytest.raises(psycopg.errors.RaiseException, match="trading_case_signal_link_invalid"),
            conn.transaction(),
        ):
            _insert_case(conn, case_id="orphan-signal-case", state="SIGNAL_EMITTED")

        with conn.transaction():
            _insert_case(conn, case_id="paired-signal-case", state="PENDING")

        with (
            pytest.raises(psycopg.errors.RaiseException, match="trading_case_signal_state_invalid"),
            conn.transaction(),
        ):
            TradingRepository(conn).append_trade_signal(_signal(case_id="paired-signal-case"))

        with conn.transaction():
            conn.execute(
                "UPDATE trading_cases SET state = 'SIGNAL_EMITTED', policy_decision = 'long' WHERE case_id = %s",
                ("paired-signal-case",),
            )
            TradingRepository(conn).append_trade_signal(_signal(case_id="paired-signal-case", expires_at_ns=3_000_000))

        row = conn.execute(
            """
            SELECT trading_cases.state, count(trading_trade_signals.signal_id) AS signals
              FROM trading_cases
              JOIN trading_trade_signals USING (case_id)
             WHERE trading_cases.case_id = %s
             GROUP BY trading_cases.state
            """,
            ("paired-signal-case",),
        ).fetchone()
        assert row == {"state": "SIGNAL_EMITTED", "signals": 1}

        with conn.transaction():
            _insert_case(conn, case_id="old-open-case", state="PENDING")
        assert TradingRepository(conn).runtime_summary(since_ms=2, now_ms=2) == {
            "cases_24h": 0,
            "signals_24h": 0,
            "no_trade_24h": 0,
            "blocked_24h": 0,
            "cases_open": 1,
            "signals_unexpired": 1,
        }

        for state in ("PENDING", "RUNNING", "NO_TRADE", "BLOCKED"):
            with (
                pytest.raises(psycopg.errors.RaiseException, match="trading_case_signal_state_invalid"),
                conn.transaction(),
            ):
                conn.execute(
                    "UPDATE trading_cases SET state = %s WHERE case_id = %s",
                    (state, "paired-signal-case"),
                )

        # `20260903_0355` dropped `reject_retired_case_state`: the narrowed CHECK is the one owner of
        # the state vocabulary, and it refuses the retired names on insert and update alike.
        for retired_state in ("POLICY_REJECTED", "INTENT_EMITTED", "ORDER_PREPARED"):
            with (
                pytest.raises(psycopg.errors.CheckViolation, match="trading_cases_state_check"),
                conn.transaction(),
            ):
                conn.execute(
                    "UPDATE trading_cases SET state = %s WHERE case_id = %s",
                    (retired_state, "paired-signal-case"),
                )
    finally:
        conn.close()


# The exact set `20260901_0347` drops, written out rather than imported from the revision module: this
# is the assertion that the tables are gone, and a list that moved with the migration could not fail.
_DROPPED_TABLES = (
    "trading_binding_runtime",
    "trading_capital_authorization_receipts",
    "trading_capital_risk_events",
    "trading_capital_risk_reservation_state",
    "trading_capital_risk_reservations",
    "trading_daily_risk_policies",
    "trading_evidence_clock_receipts",
    "trading_evidence_future_capture_batches",
    "trading_execution_bindings",
    "trading_execution_capability_snapshots",
    "trading_intents",
    "trading_nautilus_runtime_starts",
    "trading_operator_arm_receipts",
    "trading_order_observations",
    "trading_orders",
    "trading_production_promotion_grants",
    "trading_production_release_registrations",
    "trading_promotion_grant_revocations",
    "trading_replay_runs",
    "trading_runtime_state",
    "trading_symbol_blacklist",
    "trading_venue_catalog_snapshots",
)

_DROPPED_FUNCTIONS = (
    "reject_retired_trading_execution_mutation",
    "reject_trading_append_only_mutation",
    "validate_trading_evidence_parent",
    "validate_trading_future_capture_batch",
    "validate_trading_promotion_future_evidence",
    "reject_new_execution_capability_v1",
    "reject_new_legacy_trade_intent",
    "reject_trading_terminal_intent_revival",
    "stamp_trading_release_registration",
    "materialize_trading_blacklist_expiry",
    "store_trading_venue_catalog_snapshot",
    "trading_evidence_now_ms",
    "trading_canonical_jsonb",
)

# The guards 0347 must not take with it: two still fire on `trading_cases` / `trading_trade_signals` /
# the execution stream, and three are called from live CHECKs on the Signal and observation payloads.
# `reject_retired_trading_case_state` and `reject_retired_candidate_gate_stage` are not here because
# `20260903_0355` dropped both — a narrowed CHECK says the same thing once.
_KEPT_FUNCTIONS = (
    "enforce_trading_case_signal_link",
    "reject_trading_execution_stream_mutation",
    "trading_jsonb_object_size",
    "trading_execution_metadata_valid",
    "trading_execution_string_array_valid",
)


def test_0347_drops_every_retired_execution_table_and_only_its_own_functions(
    postgres_clone_dsn: str,
) -> None:
    """At head: the 22 tables and their 13 functions are gone, and nothing live went with them.

    The second half is the part worth having. `DROP FUNCTION` without `CASCADE` already refuses to
    remove a function a surviving trigger calls, so the migration cannot silently disarm a live guard
    — but nothing stops a later edit from adding a name to the drop list *and* removing the trigger
    that used it, which is how a table quietly loses its append-only guarantee.
    """

    del postgres_clone_dsn
    conn = connect_postgres_test(read_only=False)
    try:
        tables = {
            str(row["table_name"])
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        functions = {
            str(row["proname"])
            for row in conn.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert tables.isdisjoint(_DROPPED_TABLES)
    assert functions.isdisjoint(_DROPPED_FUNCTIONS)
    assert set(_KEPT_FUNCTIONS) <= functions
