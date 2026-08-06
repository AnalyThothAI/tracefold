from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.json_safety import postgres_safe_json, postgres_safe_text
from tracefold.platform.postgres.write_contract import returning_mutation_count


def _optional_returning_row(cursor: Any, row: Any) -> dict[str, Any] | None:
    returning_mutation_count(cursor, row, error_code="cex_token_profile_repository_rowcount_invalid")
    return dict(row) if row is not None else None


class CexTokenProfileRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def upsert_ready_profile_if_token_exists(
        self,
        *,
        base_symbol: str,
        provider: str,
        symbol: str | None,
        name: str | None,
        logo_url: str,
        source_ref: str | None,
        raw_payload: Mapping[str, Any] | None,
        observed_at_ms: int,
    ) -> dict[str, Any] | None:
        updated_at_ms = int(observed_at_ms) if observed_at_ms is not None else _now_ms()

        cursor = self.conn.execute(
            """
            INSERT INTO cex_token_profiles(
              cex_token_id, provider, status, symbol, name, logo_url, source_ref,
              raw_payload_json, observed_at_ms, last_error, created_at_ms, updated_at_ms
            )
            SELECT
              cex_tokens.cex_token_id, %s, 'ready', %s, %s, %s, %s, %s, %s, NULL, %s, %s
            FROM cex_tokens
            WHERE cex_tokens.base_symbol = %s
              AND cex_tokens.status IN ('candidate', 'canonical')
            ON CONFLICT(cex_token_id, provider) DO UPDATE SET
              status = 'ready',
              symbol = excluded.symbol,
              name = excluded.name,
              logo_url = excluded.logo_url,
              source_ref = excluded.source_ref,
              raw_payload_json = excluded.raw_payload_json,
              observed_at_ms = excluded.observed_at_ms,
              last_error = NULL,
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (
                _required_text(provider),
                _optional_text(symbol),
                _optional_text(name),
                _required_url(logo_url),
                _optional_text(source_ref),
                Jsonb(_required_raw_payload(raw_payload)),
                int(observed_at_ms),
                updated_at_ms,
                updated_at_ms,
                _symbol(base_symbol),
            ),
        )
        row = cursor.fetchone()
        return _optional_returning_row(cursor, row)


def _symbol(value: Any) -> str:
    return str(value or "").strip().lstrip("$").upper()


def _optional_text(value: Any) -> str | None:
    text = _clean_text(value).strip()
    return text or None


def _required_text(value: Any) -> str:
    return _clean_text(value).strip()


def _required_url(value: Any) -> str:
    text = _clean_text(value).strip()
    if not text.startswith(("http://", "https://")):
        raise ValueError("cex token profile logo_url must be an absolute URL")
    return text


def _clean_text(value: Any) -> str:
    return postgres_safe_text(value)


def _sanitize_json(value: Any) -> Any:
    return postgres_safe_json(value)


def _required_raw_payload(value: Any) -> Any:
    if value is None:
        raise TypeError("cex_token_profile_repository_raw_payload_required")
    if not isinstance(value, Mapping):
        raise TypeError("cex_token_profile_repository_raw_payload_invalid")
    return _sanitize_json(dict(value))


def _now_ms() -> int:
    return int(time.time() * 1000)
