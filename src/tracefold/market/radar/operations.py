from __future__ import annotations

from typing import Any

TOKEN_RADAR_CURRENT_MIGRATION = "20260810_0250"


class TokenRadarStatusUnavailable(RuntimeError):
    """The compact Radar singleton has not been installed yet."""

    code = "token_radar_current_schema_required"
    required_migration = TOKEN_RADAR_CURRENT_MIGRATION


def token_radar_status(conn: Any) -> dict[str, Any]:
    """Return singleton operational state without exposing the serving packet."""

    schema = conn.execute("SELECT to_regclass('token_radar_current') AS token_radar_current").fetchone()
    if schema is None or schema["token_radar_current"] is None:
        raise TokenRadarStatusUnavailable

    row = conn.execute(
        """
        SELECT schema_version, ruleset_version, ruleset_fingerprint,
               input_fingerprint, state_fingerprint,
               evidence_as_of_ms, evaluation_at_ms, input_rows, input_bytes,
               latest_attempt_status, latest_error_code, failure_count,
               updated_at_ms,
               COALESCE((served_payload ->> 'eligible_total')::bigint, 0)
                 AS eligible_total,
               jsonb_array_length(served_payload -> 'items') AS public_items
          FROM token_radar_current
         WHERE singleton_key = true
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("token_radar_current_singleton_missing")
    return dict(row)


__all__ = ["TokenRadarStatusUnavailable", "token_radar_status"]
