"""The migration tree is one irreversible baseline plus ordered hard cuts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.postgres_test_utils import connect_postgres_test, prepare_test_migration_database
from tests.postgres_test_utils import postgres_migration_test_dsn as postgres_test_dsn
from tests.postgres_test_utils import test_postgres_dsn as admin_postgres_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import (
    ObservationFactory,
    day_start_baseline_from_observation,
)
from tracefold.news.oi_signals import parse_oi_signal
from tracefold.news.source_contracts import MARKET_CATEGORY_CONFLICT, classify_source_contracts, market_route
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.trading.storage.execution_stream import (
    materialize_execution_observation,
    materialize_operator_intents,
    materialize_trade_signals,
    prepare_execution_observations,
    prepare_operator_intent,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "tracefold" / "platform" / "postgres" / "alembic" / "versions"
BASELINE = "20260831_0340"
HEAD = "20260906_0368"


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


# The release literal every observation carried in a column and again in its payload until
# `20260904_0361` deleted both. A seed for an older schema has to state it, because the contract no
# longer can.
PRE_0361_RUNTIME_RELEASE = "nautilus-1.231.0+oi-v1"


def _pre_0361_payload(observation) -> dict:
    payload = json.loads(prepare_execution_observations((observation,)).payload_json)[0]
    payload["runtime_release"] = PRE_0361_RUNTIME_RELEASE
    return payload


def _pre_0357_observation(observation, *, profile_key: str | None = None) -> tuple[str, dict]:
    """State one observation the way every revision before 20260903_0357 stored it.

    `ExecutionObservationV1` no longer carries `payload_digest` or `runtime_release`, so a seed for an
    older schema has to put those keys and their columns back: the pre-0357 CHECK counted 13 payload
    keys and compared the digest against its own column.
    """

    payload = _pre_0361_payload(observation)
    if profile_key is not None:
        payload[profile_key] = payload.pop("account_slot")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload["payload_digest"] = digest
    return digest, payload


def _pre_0357_command_payload(payload_json: str, *, confirmation_identity: str | None) -> str:
    """The column and its payload key are pre-0357 history; #520 PR-B removed both from the contract."""

    payload = json.loads(payload_json)
    payload["confirmation_identity"] = confirmation_identity
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
    assert revisions[0].down_revision == "20260905_0367"
    assert revisions[1].down_revision == "20260905_0366"
    assert revisions[2].down_revision == "20260905_0365"
    assert revisions[3].down_revision == "20260905_0364"
    assert revisions[4].down_revision == "20260904_0363"
    assert revisions[5].down_revision == "20260904_0362"
    assert revisions[6].down_revision == "20260904_0361"
    assert revisions[7].down_revision == "20260904_0360"
    assert revisions[8].down_revision == "20260903_0359"
    assert revisions[9].down_revision == "20260903_0358"
    assert revisions[10].down_revision == "20260903_0357"
    assert revisions[11].down_revision == "20260903_0356"
    assert revisions[12].down_revision == "20260903_0355"
    assert revisions[13].down_revision == "20260903_0354"
    assert revisions[14].down_revision == "20260903_0353"
    assert revisions[15].down_revision == "20260903_0352"
    assert revisions[16].down_revision == "20260902_0351"
    assert revisions[17].down_revision == "20260902_0350"
    assert revisions[18].down_revision == "20260902_0349"
    assert revisions[19].down_revision == "20260902_0348"
    assert revisions[20].down_revision == "20260901_0347"
    assert revisions[21].down_revision == "20260901_0346"
    assert revisions[22].down_revision == "20260901_0345"
    assert revisions[23].down_revision == "20260901_0344"
    assert revisions[24].down_revision == "20260901_0343"
    assert revisions[25].down_revision == "20260901_0342"
    assert revisions[26].down_revision == "20260901_0341"
    assert revisions[27].down_revision == BASELINE
    assert revisions[28].down_revision is None
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
        "20260903_0357_trading_pydantic_only_validation.py",
        "20260903_0358_news_policy_v13_judgment_check.py",
        "20260903_0359_drop_trading_notification_deliveries.py",
        "20260904_0360_trading_lane_gate_cut.py",
        "20260904_0361_trading_runtime_identity_cut.py",
        "20260904_0362_news_oi_clock_check_cut.py",
        "20260904_0363_news_review_task_source_judged_evidence.py",
        "20260905_0364_workers_runtime_capabilities.py",
        "20260905_0365_news_market_facts_at_admission.py",
        "20260905_0366_news_market_notification_tracks.py",
        "20260905_0367_news_market_alert_round_start.py",
        "20260906_0368_news_instrument_snapshot_state.py",
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

    # `20260906_0368` is now the first refusal the walk to base meets, and it is the head, so the walk
    # stops before reversing anything: renaming `observed_at_ms` back to `last_seen_ms` would restore a
    # name whose meaning it deliberately narrows -- after it the row stamp moves only when a contract's
    # identity moves -- so a previous-revision reader computing `max(last_seen_ms)` would report the
    # last catalogue change as the last catalogue refresh, and dropping the state table would lose the
    # only record of which venue last answered. `20260905_0367` is the refusal immediately behind it:
    # its `round_started_at_ms` is the only record of which alert round each notification group is
    # currently in, and dropping it returns every group to an unbounded adoption that sweeps
    # observations from ended rounds into the next card. Then `20260905_0366`'s notification to-do list
    # and delivery receipts, and `20260905_0365`'s market facts; `20260905_0364`'s dropped capability
    # column, `20260904_0363`'s restored view and `20260904_0362`'s re-added CHECKs are all reversible
    # and all behind those, as are the two refusals that were in front before — `20260904_0361` and
    # `20260903_0357`.
    with pytest.raises(RuntimeError, match="news_instrument_snapshot_state_downgrade_unsupported"):
        command.downgrade(config, "base")
    assert _stamped_revision() == HEAD

    # And 0357 is still the refusal behind it, proven on a database that stops there.
    _empty_the_schema()
    command.upgrade(config, "20260903_0357")
    with pytest.raises(RuntimeError, match="20260903_0357 deletes the unread execution digests"):
        command.downgrade(config, "base")

    assert _stamped_revision() == "20260903_0357"


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
            factory = ObservationFactory(profile_id, "oi_nautilus_v1")
            for index, action in enumerate(("pause_entries", "resume_entries", "emergency_halt"), start=1):
                confirmation_identity = "b" * 64 if action != "pause_entries" else None
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
                        confirmation_identity,
                        _pre_0357_command_payload(
                            _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
                            confirmation_identity=confirmation_identity,
                        ),
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
                payload_digest, payload = _pre_0357_observation(observation, profile_key="runtime_profile_id")
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
                        PRE_0361_RUNTIME_RELEASE,
                        observation.execution_strategy,
                        observation.command_id,
                        observation.normalized_kind,
                        observation.occurred_at_ns,
                        observation.observed_at_ns,
                        json.dumps(payload["native_identity_references"]),
                        json.dumps(payload["summary"]),
                        payload_digest,
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
                   positions_count, open_orders_count, protection_status
              FROM trading_execution_runtime_state
            """
        ).fetchone()
        assert runtime == {
            "alive": False,
            "execution_safe": False,
            "entries_armed": False,
            "entry_block_reason": "migration_restart_required",
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
    confirmation_identity = "b" * 64 if action != "pause_entries" else None
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
            confirmation_identity,
            _pre_0357_command_payload(
                _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
                confirmation_identity=confirmation_identity,
            ),
        ),
    ).fetchone()
    assert row is not None
    factory = ObservationFactory(profile_id, "oi_nautilus_v1")
    observation = factory.create(
        normalized_kind="control_disposition",
        command_id=value.command_id,
        occurred_at_ns=created_at_ns + 10,
        observed_at_ns=created_at_ns + 10,
        summary={"action": action, "disposition": "accepted", "reason": "test"},
        payload={"action": action, "disposition": "accepted"},
        event_identity="accepted",
    )
    payload_digest, payload = _pre_0357_observation(observation, profile_key="runtime_profile_id")
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
            PRE_0361_RUNTIME_RELEASE,
            observation.execution_strategy,
            observation.command_id,
            observation.normalized_kind,
            observation.occurred_at_ns,
            observation.observed_at_ns,
            json.dumps(payload["native_identity_references"]),
            json.dumps(payload["summary"]),
            payload_digest,
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
        # 11, not 13: `20260903_0357` took `payload_digest` out of the stored payload and
        # `20260904_0361` took `runtime_release` out of it too.
        assert [dict(row) for row in observations] == [
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False, "key_count": 11},
            {"account_slot": slot, "payload_slot": slot, "keeps_profile_key": False, "key_count": 11},
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
        # #520 PR-A made `account_slot` the identity; `20260904_0361` then deleted the five values
        # that only ever described what was running, so the walk to head keeps the slot and nothing
        # else from that group.
        assert "account_slot" in columns
        assert columns.isdisjoint(
            {"runtime_release", "config_sha256", "runtime_revision", "image_digest", "credential_fingerprint"}
        )

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
    factory = ObservationFactory(profile_id, "oi_nautilus_v1")
    observation = factory.create(
        normalized_kind="signal_disposition",
        signal_id=signal_id,
        occurred_at_ns=2_000,
        observed_at_ns=2_000,
        summary={"disposition": "accepted"},
        payload={"disposition": "accepted"},
        event_identity=f"disposition:{profile_id}",
    )
    payload_digest, payload = _pre_0357_observation(observation, profile_key="runtime_profile_id")
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
            PRE_0361_RUNTIME_RELEASE,
            observation.execution_strategy,
            signal_id,
            observation.normalized_kind,
            observation.occurred_at_ns,
            observation.observed_at_ns,
            json.dumps(payload["native_identity_references"]),
            json.dumps(payload["summary"]),
            payload_digest,
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
                    _pre_0357_command_payload(
                        _renamed_key(prepared.payload_json, "account_slot", "target_profile_id"),
                        confirmation_identity=None,
                    ),
                ),
            )
    finally:
        conn.close()

    with pytest.raises(Exception, match="trading_execution_identity_unmapped"):
        command.upgrade(config, "head")
    assert _stamped_revision() == "20260903_0355"


def _seed_pre_0357_facts(conn, *, signal_id: str, case_id: str, command_id: str, slot: str) -> str:
    """Write one Signal, one Command and one Observation in the shape 20260903_0356 left behind."""

    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, primary_source_key,
          supplemental_source_keys, manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms, strategy_id, strategy_version, strategy_config_digest
        ) VALUES (
          %s, %s, 'oi', %s, '[]'::jsonb, '{"test":"0357"}'::jsonb,
          %s, 'SIGNAL_EMITTED', 'long', 'migration_test', 1, 1, 1, 1,
          'migration_test', 'v1', %s
        )
        """,
        (case_id, f"slot:{case_id}", f"source:{case_id}", "e" * 64, "f" * 64),
    )
    conn.execute(
        """
        INSERT INTO trading_trade_signals (
          signal_id, case_id, alpha_contract_sha256, market_key, direction,
          observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata, payload
        ) VALUES (%s, %s, %s, 'crypto:perp:BTC:USDT', 'long', 1000, 10000, %s, '{}'::jsonb, %s::jsonb)
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
    prepared = prepare_operator_intent(
        command_id=command_id,
        account_slot=slot,
        action="flatten",
        scope="account",
        reason="migration test",
        operator_identity="operator:test",
        authentication_identity="test:authenticated",
        requested_at_ns=1_000,
        expires_at_ns=2_000,
        market_key=None,
        direction=None,
    )
    value = prepared.value
    conn.execute(
        """
        INSERT INTO trading_operator_intents (
          command_id, account_slot, action, scope, reason, operator_identity,
          authentication_identity, requested_at_ns, expires_at_ns,
          confirmation_identity, market_key, direction, payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s::jsonb)
        """,
        (
            value.command_id,
            slot,
            value.action,
            value.scope,
            value.reason,
            value.operator_identity,
            value.authentication_identity,
            value.requested_at_ns,
            value.expires_at_ns,
            "a" * 64,
            _pre_0357_command_payload(prepared.payload_json, confirmation_identity="a" * 64),
        ),
    )
    observation = ObservationFactory(slot, "oi_nautilus_v1").create(
        normalized_kind="signal_disposition",
        signal_id=signal_id,
        occurred_at_ns=2_000,
        observed_at_ns=2_000,
        summary={"disposition": "accepted"},
        payload={"disposition": "accepted"},
        event_identity="disposition:0357",
    )
    payload_digest, payload = _pre_0357_observation(observation)
    conn.execute(
        """
        INSERT INTO trading_execution_observations (
          event_id, account_slot, runtime_release, execution_strategy,
          signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
          native_identity_references, summary, payload_digest, payload
        ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
        """,
        (
            observation.event_id,
            slot,
            PRE_0361_RUNTIME_RELEASE,
            observation.execution_strategy,
            signal_id,
            observation.normalized_kind,
            observation.occurred_at_ns,
            observation.observed_at_ns,
            json.dumps(payload["native_identity_references"]),
            json.dumps(payload["summary"]),
            payload_digest,
            json.dumps(payload),
        ),
    )
    conn.execute(
        """
        INSERT INTO trading_execution_runtime_state (
          account_slot, mode, runtime_release, config_sha256,
          runtime_id, runtime_revision, image_digest, credential_fingerprint,
          lifecycle_state, alive, execution_safe, entries_armed, control_plane_ready,
          singleton_ready, startup_reconciled, portfolio_ready, audit_ready, day_start_ready,
          unexpected_exposure, account_flat, positions_count, open_orders_count, protection_status,
          reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
          started_at_ns, updated_at_ns, routes
        ) VALUES (
          %s, 'paper', 'runtime-test', %s,
          '33333333-3333-4333-8333-333333333333', %s, 'unversioned', %s,
          'running', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE,
          FALSE, TRUE, 0, 0, 'not_applicable', 3000, 3000, NULL, 1000, 3000,
          '["crypto:perp:BTC:USDT"]'::jsonb
        )
        """,
        (slot, "a" * 64, "c" * 40, "d" * 64),
    )
    return observation.event_id


def test_pydantic_only_cut_drops_the_shape_checks_the_digests_and_the_readiness_columns() -> None:
    """#520 PR-C. Rows written under the old double validation upgrade, and the second opinion is gone."""

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260903_0356")
    slot = "binance_usdm_primary"
    signal_id = "9" * 64
    command_id = "8" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            event_id = _seed_pre_0357_facts(
                conn, signal_id=signal_id, case_id="pydantic-only-case", command_id=command_id, slot=slot
            )
    finally:
        conn.close()

    command.upgrade(config, "head")

    conn = connect_postgres_test(read_only=False)
    try:
        surviving = conn.execute(
            """
            SELECT count(*) AS n FROM pg_proc
             WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'trading\\_%'
            """
        ).fetchone()
        assert surviving is not None
        assert surviving["n"] == 0

        columns = {
            (str(row["table_name"]), str(row["column_name"]))
            for row in conn.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name LIKE 'trading\\_%'
                """
            ).fetchall()
        }
        assert columns.isdisjoint(
            {
                ("trading_execution_observations", "payload_digest"),
                ("trading_trade_signals", "alpha_contract_sha256"),
                ("trading_trade_signals", "evidence_sha256"),
                ("trading_operator_intents", "confirmation_identity"),
                ("trading_execution_runtime_state", "singleton_ready"),
                ("trading_execution_runtime_state", "portfolio_ready"),
                ("trading_execution_runtime_state", "control_plane_ready"),
                ("trading_execution_runtime_state", "audit_ready"),
                ("trading_execution_runtime_state", "day_start_ready"),
            }
        )
        assert ("trading_cases", "manifest_sha256") in columns

        checks = {
            str(row["conname"])
            for row in conn.execute(
                """
                SELECT con.conname FROM pg_constraint con
                  JOIN pg_class rel ON rel.oid = con.conrelid
                 WHERE con.contype = 'c' AND rel.relname LIKE 'trading\\_%'
                """
            ).fetchall()
        }
        assert checks.isdisjoint(
            {
                "trading_execution_observation_payload_check",
                "trading_execution_observation_native_refs_check",
                "trading_execution_observation_summary_check",
                "trading_execution_observation_digest_check",
                "trading_trade_signal_payload_check",
                "trading_trade_signal_metadata_check",
                "trading_trade_signal_alpha_sha_check",
                "trading_trade_signal_evidence_sha_check",
                "trading_operator_intent_payload_check",
                "trading_operator_intent_confirmation_check",
                "trading_execution_runtime_account_snapshot_check",
                "trading_execution_runtime_routes_check",
            }
        )
        # Kept: enumerated values, identity regexes, clock inequalities and the two safety gates.
        assert {
            "trading_execution_observation_kind_check",
            "trading_execution_observation_id_check",
            "trading_execution_observation_clock_check",
            "trading_operator_intent_action_check",
            "trading_trade_signal_direction_check",
            "trading_execution_runtime_safe_check",
            "trading_execution_runtime_armed_check",
        } <= checks

        # The stored payloads lost exactly the keys whose columns went, and nothing else.
        stored = conn.execute(
            """
            SELECT (SELECT payload FROM trading_execution_observations WHERE event_id = %s) AS observation,
                   (SELECT payload FROM trading_trade_signals WHERE signal_id = %s) AS signal,
                   (SELECT payload FROM trading_operator_intents WHERE command_id = %s) AS command
            """,
            (event_id, signal_id, command_id),
        ).fetchone()
        assert stored is not None
        assert "payload_digest" not in stored["observation"]
        assert stored["observation"]["event_id"] == event_id
        assert {"alpha_contract_sha256", "evidence_sha256"}.isdisjoint(stored["signal"])
        assert stored["signal"]["case_id"] == "pydantic-only-case"
        assert "confirmation_identity" not in stored["command"]
        assert stored["command"]["action"] == "flatten"

        # And every one of them still materializes through the contract that is now the only validator.
        assert materialize_execution_observation((1, dict(stored["observation"]))).event_id == event_id
        assert materialize_trade_signals(((1, dict(stored["signal"])),))[0].signal_id == signal_id
        assert materialize_operator_intents(((1, dict(stored["command"])),))[0].command_id == command_id

        assert (
            conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_trading_trade_signals_unresolved'")
            .fetchone()["indexdef"]
            .endswith("USING btree (seq) INCLUDE (signal_id, expires_at_ns, payload)")
        )
    finally:
        conn.close()


def test_runtime_identity_cut_drops_the_columns_and_rewrites_the_observation_payload() -> None:
    """#537 PR-4. Rows written with the identity ceremony upgrade, and the projection still reads.

    Six columns on the Runtime projection and one on the observation ledger described what build was
    running. `ExecutionObservationV1` forbids extra keys, so an observation whose stored payload still
    carried `runtime_release` would stop materialising at all -- including the day-start equity fact
    the Runtime reads back before it will size an entry.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0360")
    slot = "binance_usdm_primary"
    event_id = "7" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        observation = ObservationFactory(slot, "oi_nautilus_v1").create(
            normalized_kind="risk",
            occurred_at_ns=2_000,
            observed_at_ns=2_000,
            summary={"risk_fact": "day_start_equity", "utc_day": "2030-03-17", "equity_usd_decimal": "1000.50"},
            payload={"risk_fact": "day_start_equity"},
            fixed_event_id=event_id,
        )
        payload = _pre_0361_payload(observation)
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                ) VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                """,
                (
                    event_id,
                    slot,
                    PRE_0361_RUNTIME_RELEASE,
                    observation.execution_strategy,
                    observation.normalized_kind,
                    observation.occurred_at_ns,
                    observation.observed_at_ns,
                    json.dumps(payload["native_identity_references"]),
                    json.dumps(payload["summary"]),
                    json.dumps(payload),
                ),
            )
            conn.execute(
                """
                INSERT INTO trading_execution_runtime_state (
                  account_slot, mode, runtime_release, config_sha256, runtime_id, runtime_revision,
                  image_digest, credential_fingerprint, lifecycle_state, alive, execution_safe,
                  entries_armed, startup_reconciled, unexpected_exposure, account_flat,
                  positions_count, open_orders_count, protection_status,
                  reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
                  started_at_ns, updated_at_ns, routes_count
                ) VALUES (
                  %s, 'paper', %s, %s, '33333333-3333-4333-8333-333333333333', %s,
                  'unversioned', %s, 'running', TRUE, TRUE, FALSE, TRUE, FALSE, TRUE,
                  0, 0, 'not_applicable', 3000, 3000, 'entries_paused', 1000, 3000, 5
                )
                """,
                (slot, PRE_0361_RUNTIME_RELEASE, "a" * 64, "c" * 40, "d" * 64),
            )
    finally:
        conn.close()

    command.upgrade(config, "head")

    conn = connect_postgres_test(read_only=False)
    try:
        columns = {
            (str(row["table_name"]), str(row["column_name"]))
            for row in conn.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name LIKE 'trading\\_%'
                """
            ).fetchall()
        }
        assert columns.isdisjoint(
            {
                ("trading_execution_runtime_state", "runtime_release"),
                ("trading_execution_runtime_state", "config_sha256"),
                ("trading_execution_runtime_state", "runtime_revision"),
                ("trading_execution_runtime_state", "image_digest"),
                ("trading_execution_runtime_state", "credential_fingerprint"),
                ("trading_execution_runtime_state", "lifecycle_state"),
                ("trading_execution_observations", "runtime_release"),
            }
        )
        # What the projection is for survives untouched: the generation fence, the readiness answer
        # and the account facts an operator acts on.
        assert {
            ("trading_execution_runtime_state", "runtime_id"),
            ("trading_execution_runtime_state", "alive"),
            ("trading_execution_runtime_state", "entries_armed"),
            ("trading_execution_runtime_state", "entry_block_reason"),
            ("trading_execution_runtime_state", "routes_count"),
            ("trading_execution_observations", "account_slot"),
            ("trading_execution_observations", "execution_strategy"),
        } <= columns

        checks = {
            str(row["conname"])
            for row in conn.execute(
                """
                SELECT con.conname FROM pg_constraint con
                  JOIN pg_class rel ON rel.oid = con.conrelid
                 WHERE con.contype = 'c' AND rel.relname LIKE 'trading\\_%'
                """
            ).fetchall()
        }
        assert checks.isdisjoint(
            {
                "trading_execution_runtime_release_check",
                "trading_execution_runtime_config_check",
                "trading_execution_runtime_revision_check",
                "trading_execution_runtime_image_check",
                "trading_execution_runtime_credential_check",
                "trading_execution_runtime_lifecycle_check",
                "trading_execution_runtime_alive_check",
                "trading_execution_observation_release_check",
            }
        )
        assert {"trading_execution_runtime_safe_check", "trading_execution_runtime_armed_check"} <= checks

        # The ledger is still append-only, and the rewritten payload still materialises.
        trigger = conn.execute(
            """
            SELECT tgname FROM pg_trigger
             WHERE tgrelid = 'public.trading_execution_observations'::regclass AND NOT tgisinternal
            """
        ).fetchall()
        assert {str(row["tgname"]) for row in trigger} == {"trg_trading_execution_observations_append_only"}

        stored = conn.execute(
            "SELECT payload FROM trading_execution_observations WHERE event_id = %s", (event_id,)
        ).fetchone()
        assert stored is not None
        assert "runtime_release" not in stored["payload"]
        materialized = materialize_execution_observation((1, dict(stored["payload"])))
        assert materialized.event_id == event_id
        assert day_start_baseline_from_observation(materialized).equity_usd == Decimal("1000.50")

        # And the projection round-trips through the repository without the deleted columns.
        repo = TradingRepository(conn)
        state = repo.execution_runtime_state(slot)
        assert state is not None
        assert (state.mode, state.entries_armed, state.routes_count) == ("paper", False, 5)
        rewritten = replace(state, heartbeat_at_ns=4_000, updated_at_ns=4_000, entry_block_reason="runtime_stopped")
        with conn.transaction():
            assert repo.update_execution_runtime_state(rewritten) is True
        assert repo.execution_runtime_state(slot) == rewritten
    finally:
        conn.close()


def test_cross_clock_check_cut_removes_exactly_two_rules_and_leaves_the_rest() -> None:
    """#544. `20260904_0362` deletes the two CHECKs that ordered a venue clock against this host's.

    `news_oi_signals_available_clock_check` compared the provider's `observed_at_ms` with this host's
    `available_at_ms`; PostgreSQL enforced it by refusing the insert, which the Triage handler does not
    classify and the Workers process therefore died on. `news_market_liquidations_time_order` compared
    the venue's `event_at_ms` with this host's `received_at_ms` over the same pair of clocks, and never
    fired only because `parse_liquidation` dropped such a frame first. Nothing else on either ledger
    moves: the rules that state what a fact *is* are not clock assumptions.
    """

    config = _config()
    _empty_the_schema()
    command.upgrade(config, "20260904_0361")

    conn = connect_postgres_test(read_only=False)
    try:
        before = _table_checks(conn, "news_oi_signals") | _table_checks(conn, "news_market_liquidations")
        assert {"news_oi_signals_available_clock_check", "news_market_liquidations_time_order"} <= before

        command.upgrade(config, "20260904_0362")
        after = _table_checks(conn, "news_oi_signals") | _table_checks(conn, "news_market_liquidations")

        assert before - after == {
            "news_oi_signals_available_clock_check",
            "news_market_liquidations_time_order",
        }
        assert after - before == set()
        assert {
            "news_oi_signals_direction_check",
            "news_oi_signals_learning_epoch_nonempty",
            "news_oi_signals_source_contract_check",
        } <= after
    finally:
        conn.close()


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

        command.upgrade(config, HEAD)

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
    """A 2083 or 2026 Item predates any parser for it. `raw` with a reason is the honest state."""

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

        command.upgrade(config, HEAD)

        row = conn.execute(
            "SELECT market_kind, market_parse_status, market_parse_error, market_source_strategy_id"
            " FROM news_items WHERE item_id = 'pre-cut-wallet'"
        ).fetchone()
        assert row["market_kind"] == "smart_money"
        assert (row["market_parse_status"], row["market_parse_error"]) == ("raw", "market_backfill_not_reparsed")
        assert row["market_source_strategy_id"] == "2026"
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

        command.upgrade(config, HEAD)

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

        command.upgrade(config, HEAD)

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

        command.upgrade(config, HEAD)

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
    interrupts a reader with something they cannot act on (#553 §4.1.5). This proves both halves on
    one database: the backlog seeded *before* the upgrade is `historical` and stays out of the take
    query, and a record admitted *after* it is `pending` and is the first thing the loop reads.
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

        command.upgrade(config, "head")

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

        # And the writer that runs after the revision produces the other half.
        repos = repositories_for_connection(conn)
        with repos.transaction():
            for item_id, mode in (("live-oi", "live"), ("recovered-oi", "recovery")):
                repos.news.upsert_item(
                    item_id=item_id,
                    source_id="opennews",
                    source_item_key=item_id,
                    title=item_id,
                    raw_first_line=item_id,
                    description="",
                    canonical_url=None,
                    reporting_origin="opennews",
                    published_at_ms=2000,
                    observed_at_ms=2000,
                    provider_metadata_json="{}",
                    strategy_ids_json="[]",
                    ingest_mode=mode,
                    trace_id="trace",
                    now_ms=2000,
                    market_kind="oi",
                    market_source_strategy_id="1019",
                    market_parse_status="parsed",
                    market_parse_error=None,
                )
        admitted = {
            str(row["item_id"]): row["market_notify_state"]
            for row in conn.execute(
                "SELECT item_id, market_notify_state FROM news_items WHERE item_id IN ('live-oi', 'recovered-oi')"
            ).fetchall()
        }
        assert admitted == {"live-oi": "pending", "recovered-oi": "historical"}

        # A replay of a record the backlog already marked does not put it back on the to-do list.
        with repos.transaction():
            repos.news.upsert_item(
                item_id="backlog-oi",
                source_id="opennews",
                source_item_key="backlog-oi",
                title="backlog-oi",
                raw_first_line="backlog-oi",
                description="",
                canonical_url=None,
                reporting_origin="opennews",
                published_at_ms=1000,
                observed_at_ms=1000,
                provider_metadata_json="{}",
                strategy_ids_json="[]",
                ingest_mode="live",
                trace_id="trace",
                now_ms=3000,
                market_kind="oi",
                market_source_strategy_id="1019",
                market_parse_status="parsed",
                market_parse_error=None,
            )
        replayed = conn.execute("SELECT market_notify_state FROM news_items WHERE item_id = 'backlog-oi'").fetchone()
        assert replayed["market_notify_state"] == "historical"
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
                "SELECT venue, last_snapshot_ms FROM news_market_instrument_snapshot_state"
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
