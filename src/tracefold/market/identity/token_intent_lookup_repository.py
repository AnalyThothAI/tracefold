from __future__ import annotations

from typing import Any

from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import expect_mutation_count, mutation_count
from tracefold.platform.validation import require_positive_int


class TokenIntentLookupRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def replace_lookup_keys(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: list[str],
        source_evidence_id: str | None,
        created_at_ms: int,
    ) -> None:
        require_transaction(self.conn, operation="replace_token_intent_lookup_keys")
        delete_cursor = self.conn.execute("DELETE FROM token_intent_lookup_keys WHERE intent_id = %s", (intent_id,))
        mutation_count(delete_cursor, error_code="token_intent_lookup_repository_rowcount_invalid")
        for key in sorted(set(keys)):
            cursor = self.conn.execute(
                """
                INSERT INTO token_intent_lookup_keys(
                  lookup_key, intent_id, event_id, source_evidence_id, created_at_ms
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(lookup_key, intent_id) DO UPDATE SET
                  source_evidence_id = excluded.source_evidence_id,
                  created_at_ms = excluded.created_at_ms
                """,
                (key, intent_id, event_id, source_evidence_id, int(created_at_ms)),
            )
            expect_mutation_count(cursor, expected=1, error_code="token_intent_lookup_repository_rowcount_invalid")

    def keys_for_intent(self, intent_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT lookup_key
            FROM token_intent_lookup_keys
            WHERE intent_id = %s
            ORDER BY lookup_key
            """,
            (intent_id,),
        ).fetchall()
        return [str(row["lookup_key"]) for row in rows]

    def intents_for_lookup_keys(self, keys: list[str], *, limit: int) -> list[dict[str, Any]]:
        if not keys:
            return []
        placeholders = ",".join("%s" for _ in keys)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT intent_id, event_id
            FROM token_intent_lookup_keys
            WHERE lookup_key IN ({placeholders})
            ORDER BY intent_id
            LIMIT %s
            """,
            (*keys, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_intents_for_lookup_keys(
        self,
        keys: list[str],
        *,
        since_ms: int,
        limit: int,
        after_intent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parsed_limit = require_positive_int(limit, error_code="token_intent_lookup_limit_required")
        if not keys:
            return []
        placeholders = ",".join("%s" for _ in keys)
        rows = self.conn.execute(
            f"""
            WITH picked AS (
              SELECT DISTINCT token_intents.intent_id
              FROM token_intent_lookup_keys
              JOIN token_intents ON token_intents.intent_id = token_intent_lookup_keys.intent_id
              JOIN events ON events.event_id = token_intents.event_id
              WHERE token_intent_lookup_keys.lookup_key IN ({placeholders})
                AND events.received_at_ms >= %s
                AND (%s::text IS NULL OR token_intents.intent_id > %s::text)
              ORDER BY token_intents.intent_id
              LIMIT %s
            )
            SELECT token_intents.*
            FROM picked
            JOIN token_intents ON token_intents.intent_id = picked.intent_id
            ORDER BY token_intents.created_at_ms DESC, token_intents.intent_id
            """,
            (*keys, int(since_ms), after_intent_id, after_intent_id, parsed_limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_unresolved_intents_for_lookup_keys(
        self,
        keys: list[str],
        *,
        since_ms: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        parsed_limit = require_positive_int(limit, error_code="token_intent_lookup_limit_required")
        if not keys:
            return []
        placeholders = ",".join("%s" for _ in keys)
        rows = self.conn.execute(
            f"""
            WITH picked AS (
              SELECT DISTINCT token_intents.intent_id
              FROM token_intent_lookup_keys
              JOIN token_intents ON token_intents.intent_id = token_intent_lookup_keys.intent_id
              JOIN events ON events.event_id = token_intents.event_id
              LEFT JOIN token_intent_resolutions current_resolution
                ON current_resolution.intent_id = token_intents.intent_id
               AND current_resolution.is_current = true
              WHERE token_intent_lookup_keys.lookup_key IN ({placeholders})
                AND events.received_at_ms >= %s
                AND (
                  current_resolution.resolution_id IS NULL
                  OR current_resolution.resolution_status IN ('NIL', 'AMBIGUOUS')
                )
              ORDER BY token_intents.intent_id
              LIMIT %s
            )
            SELECT token_intents.*
            FROM picked
            JOIN token_intents ON token_intents.intent_id = picked.intent_id
            ORDER BY token_intents.created_at_ms DESC, token_intents.intent_id
            """,
            (*keys, int(since_ms), parsed_limit),
        ).fetchall()
        return [dict(row) for row in rows]
