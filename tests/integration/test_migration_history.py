"""The migration tree is one irreversible baseline plus ordered hard cuts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.postgres_test_utils import connect_postgres_test, prepare_test_migration_database
from tests.postgres_test_utils import postgres_migration_test_dsn as postgres_test_dsn
from tests.postgres_test_utils import test_postgres_dsn as admin_postgres_test_dsn
from tracefold.integrations.nautilus.oi_runtime.audit_sink import ObservationFactory
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.trading.storage.execution_stream import (
    prepare_execution_observations,
    prepare_operator_intent,
)

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "tracefold" / "platform" / "postgres" / "alembic" / "versions"
BASELINE = "20260831_0340"
HEAD = "20260903_0356"


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


def _renamed_key(payload_json: str, old_key: str, new_key: str) -> str:
    """State a payload the way the revision under test stored it, before the #520 identity rename."""

    payload = json.loads(payload_json)
    payload[new_key] = payload.pop(old_key)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _stamped_revision() -> str | None:
    conn = connect_postgres_test(read_only=False)
    try:
        if conn.execute("SELECT to_regclass('alembic_version') AS table_name").fetchone()["table_name"] is None:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return None if row is None else str(row["version_num"])
    finally:
        conn.close()


def test_migration_tree_is_one_root_and_head_in_the_flat_package() -> None:
    script = ScriptDirectory.from_config(_config())
    revisions = list(script.walk_revisions())

    assert Path(script.dir).resolve() == VERSIONS.parent.resolve()
    assert [revision.revision for revision in revisions] == [
        HEAD,
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
    assert revisions[0].down_revision == "20260903_0355"
    assert revisions[1].down_revision == "20260903_0354"
    assert revisions[2].down_revision == "20260903_0353"
    assert revisions[3].down_revision == "20260903_0352"
    assert revisions[4].down_revision == "20260902_0351"
    assert revisions[5].down_revision == "20260902_0350"
    assert revisions[6].down_revision == "20260902_0349"
    assert revisions[7].down_revision == "20260902_0348"
    assert revisions[8].down_revision == "20260901_0347"
    assert revisions[9].down_revision == "20260901_0346"
    assert revisions[10].down_revision == "20260901_0345"
    assert revisions[11].down_revision == "20260901_0344"
    assert revisions[12].down_revision == "20260901_0343"
    assert revisions[13].down_revision == "20260901_0342"
    assert revisions[14].down_revision == "20260901_0341"
    assert revisions[15].down_revision == BASELINE
    assert revisions[16].down_revision is None
    assert sorted(path.name for path in VERSIONS.glob("*.py")) == [
        "20260831_0340_baseline.py",
        "20260901_0341_trading_signal_hard_cut.py",
        "20260901_0342_trading_notification_deliveries.py",
        "20260901_0343_trading_execution_runtime_state.py",
        "20260901_0344_news_oi_push_cut.py",
        "20260901_0345_trading_runtime_exposure_race.py",
        "20260901_0346_trading_notification_result.py",
        "20260901_0347_drop_retired_trading_tables.py",
        "20260902_0348_trading_runtime_control_state.py",
        "20260902_0349_trading_account_projection.py",
        "20260902_0350_news_reader_history_title_similarity.py",
        "20260902_0351_news_program_v9_judgment_check.py",
        "20260903_0352_news_policy_v12_judgment_check.py",
        "20260903_0353_trading_execution_reference_collation.py",
        "20260903_0354_trading_execution_runtime_routes.py",
        "20260903_0355_trading_case_dead_columns.py",
        "20260903_0356_trading_account_slot_identity.py",
    ]


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


def test_current_head_downgrade_is_irreversible() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "head")

    # `20260903_0356` deletes the activation ledger, so it is the first refusal the walk to base
    # meets; `20260903_0352` is still the next one behind it.
    with pytest.raises(RuntimeError, match="20260903_0356 deletes the execution profile activation ledger"):
        command.downgrade(config, "base")
    assert _stamped_revision() == HEAD

    # And 0352 is still the refusal behind it, proven on a database that stops there.
    _empty_the_schema()
    command.upgrade(config, "20260903_0352")
    with pytest.raises(RuntimeError, match="news_policy_v12_judgment_check_forward_only"):
        command.downgrade(config, "base")

    assert _stamped_revision() == "20260903_0352"


def test_runtime_control_hard_cut_backfills_current_control_and_forces_runtime_restart() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260901_0347")
    conn = connect_postgres_test(read_only=False)
    profile_id = "migration-demo-v1"
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO trading_execution_profile_activations (
                  runtime_profile_id, account_slot, activated_after_signal_seq,
                  activated_after_command_seq, mode, runtime_release, config_sha256, created_at_ns
                ) VALUES (%s, 'binance_usdm_primary', 0, 0, 'paper', 'runtime-test', %s, 1000)
                """,
                (profile_id, "a" * 64),
            )
            factory = ObservationFactory(profile_id, "runtime-test", "oi_nautilus_v1")
            for index, action in enumerate(("pause_entries", "resume_entries", "emergency_halt"), start=1):
                prepared = prepare_operator_intent(
                    command_id=f"{index:064x}",
                    account_slot=profile_id,
                    action=action,
                    scope="account" if action == "emergency_halt" else "entries",
                    reason="migration test",
                    operator_identity="operator:test",
                    authentication_identity="test:authenticated",
                    requested_at_ns=1_000 + index,
                    expires_at_ns=10_000 + index,
                    confirmation_identity="b" * 64 if action != "pause_entries" else None,
                    market_key=None,
                    direction=None,
                )
                value = prepared.value
                command_row = conn.execute(
                    """
                    INSERT INTO trading_operator_intents (
                      command_id, target_profile_id, action, scope, reason, operator_identity,
                      authentication_identity, requested_at_ns, expires_at_ns,
                      confirmation_identity, market_key, direction, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s::jsonb)
                    RETURNING seq
                    """,
                    (
                        value.command_id,
                        value.account_slot,
                        value.action,
                        value.scope,
                        value.reason,
                        value.operator_identity,
                        value.authentication_identity,
                        value.requested_at_ns,
                        value.expires_at_ns,
                        value.confirmation_identity,
                        _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
                    ),
                ).fetchone()
                assert command_row is not None
                observation = factory.create(
                    normalized_kind="control_disposition",
                    command_id=value.command_id,
                    occurred_at_ns=2_000 + index,
                    observed_at_ns=2_000 + index,
                    summary={"action": action, "disposition": "accepted", "reason": "test"},
                    payload={"action": action, "disposition": "accepted"},
                    event_identity="accepted",
                )
                payload = json.loads(prepare_execution_observations((observation,)).payload_json)[0]
                payload["runtime_profile_id"] = payload.pop("account_slot")
                conn.execute(
                    """
                    INSERT INTO trading_execution_observations (
                      event_id, runtime_profile_id, runtime_release, execution_strategy,
                      signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                      native_identity_references, summary, payload_digest, payload
                    ) VALUES (
                      %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb
                    )
                    """,
                    (
                        observation.event_id,
                        observation.account_slot,
                        observation.runtime_release,
                        observation.execution_strategy,
                        observation.command_id,
                        observation.normalized_kind,
                        observation.occurred_at_ns,
                        observation.observed_at_ns,
                        json.dumps(payload["native_identity_references"]),
                        json.dumps(payload["summary"]),
                        observation.payload_digest,
                        json.dumps(payload),
                    ),
                )
            conn.execute(
                """
                INSERT INTO trading_execution_runtime_state (
                  account_slot, runtime_profile_id, mode, runtime_release, config_sha256,
                  runtime_id, runtime_revision, image_digest, credential_fingerprint,
                  lifecycle_state, ready, singleton_ready, credential_ready, activation_ready,
                  startup_reconciled, portfolio_ready, audit_ready, unexpected_exposure,
                  account_flat, reconciliation_observed_at_ns, heartbeat_at_ns,
                  unavailable_reason, started_at_ns, updated_at_ns
                ) VALUES (
                  'binance_usdm_primary', %s, 'paper', 'runtime-test', %s,
                  '11111111-1111-4111-8111-111111111111', %s, 'unversioned', %s,
                  'running', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, FALSE, TRUE,
                  3000, 3000, NULL, 1000, 3000
                )
                """,
                (profile_id, "a" * 64, "c" * 40, "d" * 64),
            )
    finally:
        conn.close()

    command.upgrade(config, "head")
    conn = connect_postgres_test(read_only=False)
    try:
        control = conn.execute(
            "SELECT * FROM trading_execution_runtime_control_state WHERE account_slot = %s",
            ("binance_usdm_primary",),
        ).fetchone()
        assert control == {
            "account_slot": "binance_usdm_primary",
            "entries_paused": True,
            "emergency_halted": True,
            "last_command_seq": 3,
            "last_command_id": f"{3:064x}",
            "updated_at_ns": 2_003,
        }
        runtime = conn.execute(
            """
            SELECT alive, execution_safe, entries_armed, entry_block_reason,
                   control_plane_ready, day_start_ready, positions_count,
                   open_orders_count, protection_status
              FROM trading_execution_runtime_state
            """
        ).fetchone()
        assert runtime == {
            "alive": False,
            "execution_safe": False,
            "entries_armed": False,
            "entry_block_reason": "migration_restart_required",
            "control_plane_ready": False,
            "day_start_ready": False,
            "positions_count": 0,
            "open_orders_count": 0,
            "protection_status": "unknown",
        }
    finally:
        conn.close()


def _seed_pre_0356_profile(
    conn,
    *,
    profile_id: str,
    account_slot: str,
    created_at_ns: int,
    command_index: int,
    action: str,
    entries_paused: bool,
    emergency_halted: bool,
) -> str:
    """Write one whole pre-#520 profile: activation, Command, disposition and current control."""

    conn.execute(
        """
        INSERT INTO trading_execution_profile_activations (
          runtime_profile_id, account_slot, activated_after_signal_seq,
          activated_after_command_seq, mode, runtime_release, config_sha256, created_at_ns
        ) VALUES (%s, %s, 0, 0, 'paper', 'runtime-test', %s, %s)
        """,
        (profile_id, account_slot, f"{command_index:064x}", created_at_ns),
    )
    prepared = prepare_operator_intent(
        command_id=f"{command_index:064x}",
        account_slot=profile_id,
        action=action,
        scope="account" if action in {"emergency_halt", "flatten"} else "entries",
        reason="migration test",
        operator_identity="operator:test",
        authentication_identity="test:authenticated",
        requested_at_ns=created_at_ns,
        expires_at_ns=created_at_ns + 1_000,
        confirmation_identity="b" * 64 if action != "pause_entries" else None,
        market_key=None,
        direction=None,
    )
    value = prepared.value
    row = conn.execute(
        """
        INSERT INTO trading_operator_intents (
          command_id, target_profile_id, action, scope, reason, operator_identity,
          authentication_identity, requested_at_ns, expires_at_ns,
          confirmation_identity, market_key, direction, payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s::jsonb)
        RETURNING seq
        """,
        (
            value.command_id,
            profile_id,
            value.action,
            value.scope,
            value.reason,
            value.operator_identity,
            value.authentication_identity,
            value.requested_at_ns,
            value.expires_at_ns,
            value.confirmation_identity,
            _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
        ),
    ).fetchone()
    assert row is not None
    factory = ObservationFactory(profile_id, "runtime-test", "oi_nautilus_v1")
    observation = factory.create(
        normalized_kind="control_disposition",
        command_id=value.command_id,
        occurred_at_ns=created_at_ns + 10,
        observed_at_ns=created_at_ns + 10,
        summary={"action": action, "disposition": "accepted", "reason": "test"},
        payload={"action": action, "disposition": "accepted"},
        event_identity="accepted",
    )
    payload = json.loads(prepare_execution_observations((observation,)).payload_json)[0]
    payload["runtime_profile_id"] = payload.pop("account_slot")
    conn.execute(
        """
        INSERT INTO trading_execution_observations (
          event_id, runtime_profile_id, runtime_release, execution_strategy,
          signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
          native_identity_references, summary, payload_digest, payload
        ) VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
        """,
        (
            observation.event_id,
            profile_id,
            observation.runtime_release,
            observation.execution_strategy,
            observation.command_id,
            observation.normalized_kind,
            observation.occurred_at_ns,
            observation.observed_at_ns,
            json.dumps(payload["native_identity_references"]),
            json.dumps(payload["summary"]),
            observation.payload_digest,
            json.dumps(payload),
        ),
    )
    conn.execute(
        """
        INSERT INTO trading_execution_runtime_control_state (
          runtime_profile_id, entries_paused, emergency_halted,
          last_command_seq, last_command_id, updated_at_ns
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            profile_id,
            entries_paused,
            emergency_halted,
            int(row["seq"]),
            value.command_id,
            created_at_ns + 10,
        ),
    )
    return value.command_id


def test_account_slot_identity_cut_renames_backfills_and_folds_control_state() -> None:
    """#520 PR-A. Two profiles on one account slot become one identity, and control survives the fold."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260903_0355")
    slot = "binance_usdm_primary"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            _seed_pre_0356_profile(
                conn,
                profile_id="binance_usdm_demo_win_a",
                account_slot=slot,
                created_at_ns=1_000,
                command_index=1,
                action="emergency_halt",
                entries_paused=True,
                emergency_halted=True,
            )
            newest_command_id = _seed_pre_0356_profile(
                conn,
                profile_id="binance_usdm_demo_win_b",
                account_slot=slot,
                created_at_ns=2_000,
                command_index=2,
                action="pause_entries",
                entries_paused=False,
                emergency_halted=False,
            )
            conn.execute(
                """
                INSERT INTO trading_execution_runtime_state (
                  account_slot, runtime_profile_id, mode, runtime_release, config_sha256,
                  runtime_id, runtime_revision, image_digest, credential_fingerprint,
                  lifecycle_state, alive, execution_safe, entries_armed, control_plane_ready,
                  singleton_ready, credential_ready, activation_ready, startup_reconciled,
                  portfolio_ready, audit_ready, day_start_ready, unexpected_exposure, account_flat,
                  positions_count, open_orders_count, protection_status,
                  reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
                  started_at_ns, updated_at_ns
                ) VALUES (
                  %s, 'binance_usdm_demo_win_b', 'paper', 'runtime-test', %s,
                  '22222222-2222-4222-8222-222222222222', %s, 'unversioned', %s,
                  'running', TRUE, FALSE, FALSE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE,
                  FALSE, TRUE, 0, 0, 'not_applicable', 3000, 3000, 'entries_paused', 1000, 3000
                )
                """,
                (slot, "a" * 64, "c" * 40, "d" * 64),
            )
    finally:
        conn.close()

    command.upgrade(config, "head")

    conn = connect_postgres_test(read_only=False)
    try:
        observations = conn.execute(
            """
            SELECT account_slot,
                   payload ->> 'account_slot' AS payload_slot,
                   payload ? 'runtime_profile_id' AS keeps_profile_key,
                   jsonb_array_length(to_jsonb(array(SELECT jsonb_object_keys(payload)))) AS key_count
              FROM trading_execution_observations
             ORDER BY seq
            """
        ).fetchall()
        assert [dict(row) for row in observations] == [
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False, "key_count": 13},
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False, "key_count": 13},
        ]

        commands = conn.execute(
            """
            SELECT account_slot,
                   payload ->> 'account_slot' AS payload_slot,
                   payload ? 'target_profile_id' AS keeps_profile_key
              FROM trading_operator_intents
             ORDER BY seq
            """
        ).fetchall()
        assert [dict(row) for row in commands] == [
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False},
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False},
        ]

        # One row per slot: the newest Command wins the pause flag and the halt stays sticky.
        control = conn.execute("SELECT * FROM trading_execution_runtime_control_state").fetchall()
        assert [dict(row) for row in control] == [
            {
                "account_slot": slot,
                "entries_paused": True,
                "emergency_halted": True,
                "last_command_seq": 2,
                "last_command_id": newest_command_id,
                "updated_at_ns": 2_010,
            }
        ]

        columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'trading_execution_runtime_state'
                """
            ).fetchall()
        }
        assert columns.isdisjoint({"runtime_profile_id", "credential_ready", "activation_ready"})
        assert {"account_slot", "runtime_release", "config_sha256", "image_digest", "credential_fingerprint"} <= columns

        retired = conn.execute(
            """
            SELECT to_regclass('trading_execution_profile_activations') AS activations,
                   to_regclass('trading_decision_runtime') AS decision_runtime
            """
        ).fetchone()
        assert retired == {"activations": None, "decision_runtime": None}
    finally:
        conn.close()


def _seed_pre_0356_signal_disposition(conn, *, profile_id: str, signal_id: str, case_id: str) -> None:
    """One Signal and one profile's disposition of it, in the pre-#520 column and payload shape."""

    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, primary_source_key,
          supplemental_source_keys, manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms, strategy_id, strategy_version, strategy_config_digest
        ) VALUES (
          %s, %s, 'oi', %s, '[]'::jsonb, '{"test":"0356"}'::jsonb,
          %s, 'SIGNAL_EMITTED', 'long', 'migration_test', 1, 1, 1, 1,
          'migration_test', 'v1', %s
        )
        ON CONFLICT DO NOTHING
        """,
        (case_id, f"slot:{case_id}", f"source:{case_id}", "e" * 64, "f" * 64),
    )
    conn.execute(
        """
        INSERT INTO trading_trade_signals (
          signal_id, case_id, alpha_contract_sha256, market_key, direction,
          observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata, payload
        ) VALUES (%s, %s, %s, 'crypto:perp:BTC:USDT', 'long', 1000, 10000, %s, '{}'::jsonb, %s::jsonb)
        ON CONFLICT DO NOTHING
        """,
        (
            signal_id,
            case_id,
            "b" * 64,
            "c" * 64,
            json.dumps(
                {
                    "signal_version": "trade_signal_v1",
                    "signal_id": signal_id,
                    "case_id": case_id,
                    "alpha_contract_sha256": "b" * 64,
                    "market_key": "crypto:perp:BTC:USDT",
                    "direction": "long",
                    "observed_at_ns": 1000,
                    "expires_at_ns": 10000,
                    "evidence_sha256": "c" * 64,
                    "alpha_metadata": {},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    factory = ObservationFactory(profile_id, "runtime-test", "oi_nautilus_v1")
    observation = factory.create(
        normalized_kind="signal_disposition",
        signal_id=signal_id,
        occurred_at_ns=2_000,
        observed_at_ns=2_000,
        summary={"disposition": "accepted"},
        payload={"disposition": "accepted"},
        event_identity=f"disposition:{profile_id}",
    )
    payload = json.loads(prepare_execution_observations((observation,)).payload_json)[0]
    payload["runtime_profile_id"] = payload.pop("account_slot")
    conn.execute(
        """
        INSERT INTO trading_execution_observations (
          event_id, runtime_profile_id, runtime_release, execution_strategy,
          signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
          native_identity_references, summary, payload_digest, payload
        ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
        """,
        (
            observation.event_id,
            profile_id,
            observation.runtime_release,
            observation.execution_strategy,
            signal_id,
            observation.normalized_kind,
            observation.occurred_at_ns,
            observation.observed_at_ns,
            json.dumps(payload["native_identity_references"]),
            json.dumps(payload["summary"]),
            observation.payload_digest,
            json.dumps(payload),
        ),
    )


def test_account_slot_identity_cut_refuses_a_fold_that_would_lose_a_disposition() -> None:
    """#520 PR-A. Two profiles on one slot that both disposed of the same Signal are one key after the fold."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260903_0355")
    signal_id = "9" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            for index, profile_id in enumerate(("fold-collide-a", "fold-collide-b"), start=1):
                conn.execute(
                    """
                    INSERT INTO trading_execution_profile_activations (
                      runtime_profile_id, account_slot, activated_after_signal_seq,
                      activated_after_command_seq, mode, runtime_release, config_sha256, created_at_ns
                    ) VALUES (%s, 'binance_usdm_primary', 0, 0, 'paper', 'runtime-test', %s, %s)
                    """,
                    (profile_id, f"{index:064x}", 1_000 * index),
                )
                _seed_pre_0356_signal_disposition(
                    conn,
                    profile_id=profile_id,
                    signal_id=signal_id,
                    case_id="fold-collide-case",
                )
    finally:
        conn.close()

    with pytest.raises(Exception, match="trading_folded_disposition_collisions: signals=1, commands=0"):
        command.upgrade(config, "head")
    assert _stamped_revision() == "20260903_0355"


def test_account_slot_identity_cut_refuses_an_identity_it_cannot_map_to_a_slot() -> None:
    """A Command naming a profile with no activation row has no account slot to become."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260903_0355")
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            prepared = prepare_operator_intent(
                command_id="a" * 64,
                account_slot="orphan-profile",
                action="pause_entries",
                scope="entries",
                reason="migration test",
                operator_identity="operator:test",
                authentication_identity="test:authenticated",
                requested_at_ns=1_000,
                expires_at_ns=2_000,
                confirmation_identity=None,
                market_key=None,
                direction=None,
            )
            value = prepared.value
            conn.execute(
                """
                INSERT INTO trading_operator_intents (
                  command_id, target_profile_id, action, scope, reason, operator_identity,
                  authentication_identity, requested_at_ns, expires_at_ns,
                  confirmation_identity, market_key, direction, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s::jsonb)
                """,
                (
                    value.command_id,
                    "orphan-profile",
                    value.action,
                    value.scope,
                    value.reason,
                    value.operator_identity,
                    value.authentication_identity,
                    value.requested_at_ns,
                    value.expires_at_ns,
                    _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
                ),
            )
    finally:
        conn.close()

    with pytest.raises(Exception, match="trading_execution_identity_unmapped"):
        command.upgrade(config, "head")
    assert _stamped_revision() == "20260903_0355"
