from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tracefold.platform.postgres.json_safety import postgres_safe_text
from tracefold.platform.postgres.payload_hash import stable_dirty_target_payload_hash
from tracefold.platform.postgres.queue_terminal import terminalize_source_row
from tracefold.platform.postgres.write_contract import expect_mutation_count, mutation_count
from tracefold.platform.validation import require_nonnegative_int, require_positive_int


class AssetProfileRefreshTargetRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def enqueue_targets(
        self,
        targets: Iterable[Mapping[str, Any]],
        *,
        reason: str,
        now_ms: int,
        due_at_ms: int | None = None,
    ) -> dict[str, int]:
        records = _target_records(targets, reason=reason, now_ms=int(now_ms), due_at_ms=due_at_ms)
        if not records:
            return {"targets": 0}

        cursor = self.conn.execute(
            """
            WITH incoming AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(target_types)s::text[],
                %(target_ids)s::text[],
                %(chain_ids)s::text[],
                %(addresses)s::text[],
                %(symbols)s::text[],
                %(payload_hashes)s::text[],
                %(source_watermark_ms_values)s::bigint[],
                %(heat_tiers)s::text[],
                %(priorities)s::integer[],
                %(due_at_ms_values)s::bigint[]
              ) AS incoming(
                provider,
                target_type,
                target_id,
                chain_id,
                address,
                symbol,
                payload_hash,
                source_watermark_ms,
                heat_tier,
                priority,
                due_at_ms
              )
            )
            INSERT INTO asset_profile_refresh_targets(
              provider,
              target_type,
              target_id,
              chain_id,
              address,
              symbol,
              dirty_reason,
              payload_hash,
              source_watermark_ms,
              heat_tier,
              priority,
              due_at_ms,
              leased_until_ms,
              lease_owner,
              attempt_count,
              last_error,
              first_dirty_at_ms,
              updated_at_ms
            )
            SELECT
              provider,
              target_type,
              target_id,
              chain_id,
              address,
              symbol,
              %(dirty_reason)s,
              payload_hash,
              source_watermark_ms,
              heat_tier,
              priority,
              due_at_ms,
              NULL,
              NULL,
              0,
              NULL,
              %(now_ms)s,
              %(now_ms)s
            FROM incoming
            ON CONFLICT(provider, target_type, target_id) DO UPDATE SET
              chain_id = EXCLUDED.chain_id,
              address = EXCLUDED.address,
              symbol = EXCLUDED.symbol,
              dirty_reason = EXCLUDED.dirty_reason,
              payload_hash = EXCLUDED.payload_hash,
              source_watermark_ms = GREATEST(
                asset_profile_refresh_targets.source_watermark_ms,
                EXCLUDED.source_watermark_ms
              ),
              priority = LEAST(asset_profile_refresh_targets.priority, EXCLUDED.priority),
              heat_tier = CASE
                WHEN asset_profile_refresh_targets.heat_tier = 'hot'
                  OR EXCLUDED.heat_tier = 'hot' THEN 'hot'
                WHEN asset_profile_refresh_targets.heat_tier = 'warm'
                  OR EXCLUDED.heat_tier = 'warm' THEN 'warm'
                ELSE 'cold'
              END,
              due_at_ms = CASE
                WHEN asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                  OR EXCLUDED.priority < asset_profile_refresh_targets.priority
                  THEN LEAST(asset_profile_refresh_targets.due_at_ms, EXCLUDED.due_at_ms)
                ELSE asset_profile_refresh_targets.due_at_ms
              END,
              leased_until_ms = CASE
                WHEN asset_profile_refresh_targets.leased_until_ms IS NOT NULL
                  AND asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                  THEN NULL
                ELSE asset_profile_refresh_targets.leased_until_ms
              END,
              lease_owner = CASE
                WHEN asset_profile_refresh_targets.leased_until_ms IS NOT NULL
                  AND asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                  THEN NULL
                ELSE asset_profile_refresh_targets.lease_owner
              END,
              attempt_count = CASE
                WHEN asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN 0
                ELSE asset_profile_refresh_targets.attempt_count
              END,
              last_error = CASE
                WHEN asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN NULL
                ELSE asset_profile_refresh_targets.last_error
              END,
              terminal_reason = CASE
                WHEN asset_profile_refresh_targets.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN NULL
                ELSE asset_profile_refresh_targets.terminal_reason
              END,
              first_dirty_at_ms = asset_profile_refresh_targets.first_dirty_at_ms,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            {**_target_params(records), "dirty_reason": str(reason), "now_ms": int(now_ms)},
        )
        changed = mutation_count(cursor, error_code="asset_profile_refresh_target_rowcount_invalid")
        self.conn.execute(
            """
            WITH incoming AS (
              SELECT *
              FROM unnest(
                %(terminal_target_keys)s::text[],
                %(payload_hashes)s::text[]
              ) AS incoming(target_key, payload_hash)
            )
            UPDATE queue_terminal_events terminal
            SET operator_action = 'retry',
                operator_reason = 'reactivated_by_new_evidence',
                operator_action_at_ms = %(now_ms)s
            FROM incoming
            WHERE terminal.owner_key = 'asset_profile_refresh'
              AND terminal.source_table = 'asset_profile_refresh_targets'
              AND terminal.target_key = incoming.target_key
              AND terminal.operator_action IS NULL
              AND terminal.payload_hash IS DISTINCT FROM incoming.payload_hash
            """,
            {**_target_params(records), "now_ms": int(now_ms)},
        )
        return {"targets": changed}

    def enqueue_missing_token_radar_current_targets_for_ops(
        self,
        *,
        provider: str,
        now_ms: int,
        limit: int,
    ) -> dict[str, int]:
        parsed_provider = _required_text(provider, field_name="provider")
        bounded_limit = require_nonnegative_int(
            limit,
            error_code="asset_profile_refresh_target_limit_required",
        )
        if not parsed_provider or bounded_limit <= 0:
            return {"targets": 0, "source_rows_scanned": 0}

        rows = self.conn.execute(
            """
            WITH eligible AS (
              SELECT DISTINCT ON (current_rows.identity_id)
                'Asset' AS target_type,
                current_rows.identity_id AS target_id,
                current_rows.factor_snapshot_json #>> '{subject,chain}' AS chain_id,
                current_rows.factor_snapshot_json #>> '{subject,address}' AS address,
                current_rows.factor_snapshot_json #>> '{subject,symbol}' AS symbol,
                current_rows.source_max_received_at_ms AS source_watermark_ms
              FROM token_radar_current_rows current_rows
              WHERE current_rows.target_type_key = 'Asset'
                AND current_rows.venue = 'all'
                AND current_rows.identity_id IS NOT NULL
                AND btrim(current_rows.identity_id) <> ''
                AND current_rows.source_max_received_at_ms > 0
                AND COALESCE(current_rows.factor_snapshot_json #>> '{subject,chain}', '') <> ''
                AND COALESCE(current_rows.factor_snapshot_json #>> '{subject,address}', '') <> ''
                AND NOT EXISTS (
                  SELECT 1
                  FROM asset_profiles source_cache
                  WHERE source_cache.provider = %(provider)s
                    AND source_cache.asset_id = current_rows.identity_id
                )
                AND NOT EXISTS (
                  SELECT 1
                  FROM asset_profile_refresh_targets queue
                  WHERE queue.provider = %(provider)s
                    AND queue.target_type = 'Asset'
                    AND queue.target_id = current_rows.identity_id
                )
              ORDER BY current_rows.identity_id,
                       current_rows.source_max_received_at_ms DESC,
                       current_rows.computed_at_ms DESC
            )
            SELECT *
            FROM eligible
            ORDER BY source_watermark_ms DESC, target_id ASC
            LIMIT %(limit)s
            """,
            {"provider": parsed_provider, "limit": bounded_limit},
        ).fetchall()
        targets = [
            {
                "provider": parsed_provider,
                "target_type": str(row["target_type"]),
                "target_id": str(row["target_id"]),
                "chain_id": str(row["chain_id"]),
                "address": str(row["address"]),
                "symbol": row.get("symbol"),
                "source_watermark_ms": int(row["source_watermark_ms"]),
                "heat_tier": "hot",
                "priority": 20,
                "due_at_ms": int(now_ms),
            }
            for row in rows
        ]
        enqueue_result = self.enqueue_targets(
            targets,
            reason="token_radar_current_backfill",
            now_ms=now_ms,
        )
        return {
            "targets": int(enqueue_result.get("targets") or 0),
            "source_rows_scanned": len(rows),
        }

    def release_provider_failure(
        self,
        claim: Mapping[str, Any],
        *,
        due_at_ms: int,
        now_ms: int,
    ) -> int:
        """Release one claim without consuming its target attempt."""

        cursor = self.conn.execute(
            """
            UPDATE asset_profile_refresh_targets
            SET due_at_ms = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                attempt_count = GREATEST(0, attempt_count - 1),
                last_error = NULL,
                updated_at_ms = %s
            WHERE provider = %s
              AND target_type = %s
              AND target_id = %s
              AND payload_hash = %s
              AND lease_owner = %s
              AND attempt_count = %s
            """,
            (
                int(due_at_ms),
                int(now_ms),
                str(claim["provider"]),
                str(claim["target_type"]),
                str(claim["target_id"]),
                str(claim["payload_hash"]),
                str(claim["lease_owner"]),
                int(claim["attempt_count"]),
            ),
        )
        return mutation_count(
            cursor,
            error_code="asset_profile_provider_failure_release_count_invalid",
        )

    def claim_due(
        self,
        *,
        provider: str,
        now_ms: int,
        limit: int,
        lease_owner: str,
        lease_ms: int,
    ) -> list[dict[str, Any]]:
        parsed_limit = require_nonnegative_int(
            limit,
            error_code="asset_profile_refresh_target_claim_limit_required",
        )
        parsed_lease_ms = require_positive_int(
            lease_ms,
            error_code="asset_profile_refresh_target_claim_lease_ms_required",
        )

        cursor = self.conn.execute(
            """
            WITH due AS (
              SELECT provider, target_type, target_id,
                     last_error AS previous_last_error
              FROM asset_profile_refresh_targets
              WHERE provider = %(provider)s
                AND terminal_reason IS NULL
                AND due_at_ms <= %(now_ms)s
                AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ORDER BY priority ASC,
                       due_at_ms ASC,
                       updated_at_ms ASC,
                       target_type ASC,
                       target_id ASC
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE asset_profile_refresh_targets
            SET leased_until_ms = %(leased_until_ms)s,
                lease_owner = %(lease_owner)s,
                attempt_count = asset_profile_refresh_targets.attempt_count + 1,
                last_error = NULL,
                updated_at_ms = %(now_ms)s
            FROM due
            WHERE asset_profile_refresh_targets.provider = due.provider
              AND asset_profile_refresh_targets.target_type = due.target_type
              AND asset_profile_refresh_targets.target_id = due.target_id
            RETURNING asset_profile_refresh_targets.*, due.previous_last_error
            """,
            {
                "provider": str(provider),
                "now_ms": int(now_ms),
                "leased_until_ms": int(now_ms) + parsed_lease_ms,
                "lease_owner": str(lease_owner),
                "limit": parsed_limit,
            },
        )
        rows = cursor.fetchall()
        expect_mutation_count(cursor, expected=len(rows), error_code="asset_profile_refresh_target_rowcount_invalid")
        return [dict(row) for row in rows]

    def release_prework(self, claim: Mapping[str, Any]) -> bool:
        row = self.conn.execute(
            """
            UPDATE asset_profile_refresh_targets
               SET leased_until_ms = NULL,
                   lease_owner = NULL,
                   attempt_count = attempt_count - 1,
                   last_error = %s
             WHERE provider = %s
               AND target_type = %s
               AND target_id = %s
               AND payload_hash = %s
               AND lease_owner = %s
               AND attempt_count = %s
               AND attempt_count > 0
            RETURNING target_id
            """,
            (
                claim.get("previous_last_error"),
                str(claim["provider"]),
                str(claim["target_type"]),
                str(claim["target_id"]),
                str(claim["payload_hash"]),
                str(claim["lease_owner"]),
                int(claim["attempt_count"]),
            ),
        ).fetchone()
        return row is not None

    def reschedule(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        due_at_ms: int,
        now_ms: int,
        reason: str | None = None,
        reset_attempts: bool = False,
    ) -> int:
        records = _claim_records(claims)
        if not records:
            return 0
        params = {
            **_claim_params(records),
            "due_at_ms": int(due_at_ms),
            "now_ms": int(now_ms),
            "reason": reason,
            "reset_attempts": bool(reset_attempts),
        }

        cursor = self.conn.execute(
            """
            WITH rescheduled AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(target_types)s::text[],
                %(target_ids)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS rescheduled(provider, target_type, target_id, payload_hash, lease_owner, attempt_count)
            )
            UPDATE asset_profile_refresh_targets queue
            SET due_at_ms = %(due_at_ms)s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                attempt_count = CASE
                  WHEN %(reset_attempts)s THEN 0
                  ELSE queue.attempt_count
                END,
                dirty_reason = COALESCE(%(reason)s, queue.dirty_reason),
                updated_at_ms = %(now_ms)s
            FROM rescheduled
            WHERE queue.provider = rescheduled.provider
              AND queue.target_type = rescheduled.target_type
              AND queue.target_id = rescheduled.target_id
              AND queue.payload_hash = rescheduled.payload_hash
              AND queue.lease_owner = rescheduled.lease_owner
              AND queue.attempt_count = rescheduled.attempt_count
            """,
            params,
        )
        return mutation_count(cursor, error_code="asset_profile_refresh_target_rowcount_invalid")

    def mark_terminal(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        reason: str,
        now_ms: int,
    ) -> int:
        records = _claim_records(claims)
        if not records:
            return 0
        terminal_reason = _required_text(reason, field_name="terminal_reason")
        params = {
            **_claim_params(records),
            "terminal_reason": terminal_reason,
            "now_ms": int(now_ms),
        }
        cursor = self.conn.execute(
            """
            WITH terminal AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(target_types)s::text[],
                %(target_ids)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS terminal(
                provider, target_type, target_id, payload_hash,
                lease_owner, attempt_count
              )
            )
            UPDATE asset_profile_refresh_targets queue
            SET leased_until_ms = NULL,
                lease_owner = NULL,
                terminal_reason = %(terminal_reason)s,
                last_error = %(terminal_reason)s,
                updated_at_ms = %(now_ms)s
            FROM terminal
            WHERE queue.provider = terminal.provider
              AND queue.target_type = terminal.target_type
              AND queue.target_id = terminal.target_id
              AND queue.payload_hash = terminal.payload_hash
              AND queue.lease_owner = terminal.lease_owner
              AND queue.attempt_count = terminal.attempt_count
            RETURNING queue.*
            """,
            params,
        )
        rows = [dict(row) for row in cursor.fetchall()]
        expect_mutation_count(
            cursor,
            expected=len(rows),
            error_code="asset_profile_refresh_target_rowcount_invalid",
        )
        for row in rows:
            terminalize_source_row(
                self.conn,
                owner_key="asset_profile_refresh",
                source_table="asset_profile_refresh_targets",
                target_key=_terminal_target_key(row),
                source_row=row,
                final_status="terminal",
                final_reason=terminal_reason,
                now_ms=now_ms,
                attempt_count=int(row["attempt_count"]),
                payload_hash=str(row["payload_hash"]),
                first_seen_at_ms=int(row["first_dirty_at_ms"]),
                last_attempted_at_ms=now_ms,
            )
        return len(rows)

    def mark_error(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        error: str,
        now_ms: int,
        retry_ms: int,
    ) -> int:
        records = _claim_records(claims)
        if not records:
            return 0
        parsed_retry_ms = require_positive_int(
            retry_ms,
            error_code="asset_profile_refresh_target_retry_ms_required",
        )
        params = {
            **_claim_params(records),
            "due_at_ms": int(now_ms) + parsed_retry_ms,
            "now_ms": int(now_ms),
            "last_error": str(error)[:2048],
        }

        cursor = self.conn.execute(
            """
            WITH failed AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(target_types)s::text[],
                %(target_ids)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS failed(provider, target_type, target_id, payload_hash, lease_owner, attempt_count)
            )
            UPDATE asset_profile_refresh_targets queue
            SET due_at_ms = %(due_at_ms)s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error = %(last_error)s,
                updated_at_ms = %(now_ms)s
            FROM failed
            WHERE queue.provider = failed.provider
              AND queue.target_type = failed.target_type
              AND queue.target_id = failed.target_id
              AND queue.payload_hash = failed.payload_hash
              AND queue.lease_owner = failed.lease_owner
              AND queue.attempt_count = failed.attempt_count
            """,
            params,
        )
        return mutation_count(cursor, error_code="asset_profile_refresh_target_rowcount_invalid")

    def queue_depth(self, *, provider: str, now_ms: int) -> int:
        row = self.conn.execute(
            """
            SELECT count(*) AS count
            FROM asset_profile_refresh_targets
            WHERE provider = %(provider)s
              AND terminal_reason IS NULL
              AND due_at_ms <= %(now_ms)s
              AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
            """,
            {"provider": str(provider), "now_ms": int(now_ms)},
        ).fetchone()
        return int(row["count"] if row else 0)

    def queue_health(self, *, provider: str, now_ms: int) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE terminal_reason IS NULL) AS active,
              count(*) FILTER (
                WHERE terminal_reason IS NULL
                  AND due_at_ms <= %(now_ms)s
                  AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ) AS due,
              count(*) FILTER (
                WHERE terminal_reason IS NULL AND heat_tier = 'hot'
              ) AS hot,
              count(*) FILTER (
                WHERE terminal_reason IS NULL AND heat_tier = 'warm'
              ) AS warm,
              count(*) FILTER (
                WHERE terminal_reason IS NULL AND heat_tier = 'cold'
              ) AS cold,
              count(*) FILTER (WHERE terminal_reason IS NOT NULL) AS terminal,
              COALESCE(
                %(now_ms)s - min(due_at_ms) FILTER (
                  WHERE terminal_reason IS NULL
                    AND due_at_ms <= %(now_ms)s
                    AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
                ),
                0
              ) AS oldest_due_age_ms
            FROM asset_profile_refresh_targets
            WHERE provider = %(provider)s
            """,
            {"provider": str(provider), "now_ms": int(now_ms)},
        ).fetchone()
        return {
            key: int(row[key] if row and row[key] is not None else 0)
            for key in ("active", "due", "hot", "warm", "cold", "terminal", "oldest_due_age_ms")
        }


def _target_records(
    targets: Iterable[Mapping[str, Any]],
    *,
    reason: str,
    now_ms: int,
    due_at_ms: int | None,
) -> list[dict[str, Any]]:
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    for target in targets:
        provider = _required_text(target.get("provider"), field_name="provider")
        target_type = _required_text(target.get("target_type"), field_name="target_type")
        target_id = _required_text(target.get("target_id"), field_name="target_id")
        chain_id = _required_text(target.get("chain_id"), field_name="chain_id")
        address = _required_text(target.get("address"), field_name="address")
        record = {
            "provider": provider,
            "target_type": target_type,
            "target_id": target_id,
            "chain_id": chain_id,
            "address": address,
            "symbol": _optional_text(target.get("symbol")),
            "source_watermark_ms": _source_watermark_ms(target),
            "heat_tier": _heat_tier(target.get("heat_tier")),
            "due_at_ms": int(target.get("due_at_ms") or due_at_ms or now_ms),
        }
        record["priority"] = int(target.get("priority") or _heat_tier_priority(str(record["heat_tier"])))
        record["payload_hash"] = str(
            target.get("payload_hash")
            or _payload_hash(
                {
                    "provider": provider,
                    "target_type": target_type,
                    "target_id": target_id,
                    "chain_id": chain_id,
                    "address": address,
                    "symbol": record["symbol"],
                    "source_watermark_ms": record["source_watermark_ms"],
                    "profile_contract_version": "asset_profile_refresh_v1",
                }
            )
        )
        records[(provider, target_type, target_id)] = record
    return list(records.values())


def _target_params(records: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {
        "providers": [str(record["provider"]) for record in records],
        "target_types": [str(record["target_type"]) for record in records],
        "target_ids": [str(record["target_id"]) for record in records],
        "chain_ids": [str(record["chain_id"]) for record in records],
        "addresses": [str(record["address"]) for record in records],
        "symbols": [record["symbol"] for record in records],
        "payload_hashes": [str(record["payload_hash"]) for record in records],
        "terminal_target_keys": [_terminal_target_key(record) for record in records],
        "source_watermark_ms_values": [int(record["source_watermark_ms"]) for record in records],
        "heat_tiers": [str(record["heat_tier"]) for record in records],
        "priorities": [int(record["priority"]) for record in records],
        "due_at_ms_values": [int(record["due_at_ms"]) for record in records],
    }


def _claim_records(claims: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for claim in claims:
        provider = str(claim.get("provider") or "").strip()
        target_type = str(claim.get("target_type") or "").strip()
        target_id = str(claim.get("target_id") or "").strip()
        if not provider or not target_type or not target_id:
            raise ValueError("asset profile refresh target completion requires full target key from claim_due")
        payload_hash = _completion_payload_hash(claim)
        lease_owner = _completion_lease_owner(claim)
        attempt_count = _completion_attempt_count(claim)
        if not payload_hash:
            raise ValueError("asset profile refresh target completion requires payload_hash from claim_due")
        if not lease_owner:
            raise ValueError("asset profile refresh target completion requires lease_owner from claim_due")
        if attempt_count <= 0:
            raise ValueError("asset profile refresh target completion requires attempt_count from claim_due")
        records.append(
            {
                "provider": provider,
                "target_type": target_type,
                "target_id": target_id,
                "payload_hash": payload_hash,
                "lease_owner": lease_owner,
                "attempt_count": attempt_count,
            }
        )
    return records


def _completion_attempt_count(claim: Mapping[str, Any]) -> int:
    try:
        value = claim["attempt_count"]
    except KeyError as exc:
        raise ValueError("asset profile refresh target completion requires attempt_count from claim_due") from exc
    return require_positive_int(
        value,
        error_code="asset profile refresh target completion requires attempt_count from claim_due",
    )


def _completion_lease_owner(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["lease_owner"]
    except KeyError as exc:
        raise ValueError("asset profile refresh target completion requires lease_owner from claim_due") from exc
    if value is None:
        raise ValueError("asset profile refresh target completion requires lease_owner from claim_due")
    lease_owner = str(value).strip()
    if not lease_owner:
        raise ValueError("asset profile refresh target completion requires lease_owner from claim_due")
    return lease_owner


def _completion_payload_hash(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["payload_hash"]
    except KeyError as exc:
        raise ValueError("asset profile refresh target completion requires payload_hash from claim_due") from exc
    if value is None:
        raise ValueError("asset profile refresh target completion requires payload_hash from claim_due")
    payload_hash = str(value).strip()
    if not payload_hash:
        raise ValueError("asset profile refresh target completion requires payload_hash from claim_due")
    return payload_hash


def _claim_params(records: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {
        "providers": [str(record["provider"]) for record in records],
        "target_types": [str(record["target_type"]) for record in records],
        "target_ids": [str(record["target_id"]) for record in records],
        "payload_hashes": [str(record["payload_hash"]) for record in records],
        "lease_owners": [str(record["lease_owner"]) for record in records],
        "attempt_counts": [int(record["attempt_count"]) for record in records],
    }


def _required_text(value: Any, *, field_name: str) -> str:
    text = postgres_safe_text(value).strip()
    if not text:
        raise ValueError(f"asset profile refresh target {field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = postgres_safe_text(value).strip()
    return text or None


def _source_watermark_ms(target: Mapping[str, Any]) -> int:
    try:
        value = target["source_watermark_ms"]
    except KeyError as exc:
        raise ValueError("asset_profile_refresh_target_source_watermark_required") from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("asset_profile_refresh_target_source_watermark_required")
    if value <= 0:
        raise ValueError("asset_profile_refresh_target_source_watermark_required")
    return int(value)


def _heat_tier(value: Any) -> str:
    tier = str(value or "cold").strip().lower()
    if tier not in {"hot", "warm", "cold"}:
        raise ValueError("asset_profile_refresh_target_heat_tier_invalid")
    return tier


def _heat_tier_priority(heat_tier: str) -> int:
    return {"hot": 20, "warm": 60, "cold": 100}[heat_tier]


def _terminal_target_key(target: Mapping[str, Any]) -> str:
    return ":".join(
        (
            str(target["provider"]),
            str(target["target_type"]),
            str(target["target_id"]),
        )
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return stable_dirty_target_payload_hash(payload)
