"""Latest Case read and isolated restore drill seed for the LIVE Analysis ledger."""

from __future__ import annotations

from typing import Any

LATEST_CASE_CREATED_AT_SQL = "SELECT max(created_at_ms) AS latest FROM trading_cases"
_RESTORE_VIEW_SHA = "3b2e02e881ed247244c6d9fc9d499e43b9ea2258f46cf5b05681ec1d83a879c9"


class HistoricalCaseStorage:
    conn: Any

    def restore_drill_case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT case_id,state,view_sha256 FROM trading_cases WHERE case_id=%s",
            (case_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def seed_restore_drill_case(self, *, case_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_inputs (
              input_id,kind,source_fact_key,source_revision,payload_sha256,payload,
              first_visible_at_ms,source_observed_at_ms,selected_asset_id,target_selection,received_at_ms
            ) VALUES (
              %s,'oi','restore-source','v1',%s,'{"kind":"oi"}'::jsonb,
              9,9,'crypto:RESTORE','{"reason":"selected"}'::jsonb,10
            )
            """,
            ("7" * 64, "b" * 64),
        )
        self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id,trigger_id,trigger_kind,asset_id,native_symbol,mapping_digest,
              created_at_ms,root_expires_at_ms,state,view,view_sha256,updated_at_ms
            ) VALUES (
              %s,%s,'oi','crypto:RESTORE','RESTOREUSDT',%s,
              10,600010,'complete','{"restore":"case"}'::jsonb,%s,10
            )
            """,
            (case_id, "7" * 64, "d" * 64, _RESTORE_VIEW_SHA),
        )

    def latest_case_created_at_ms(self) -> int | None:
        row = self.conn.execute(LATEST_CASE_CREATED_AT_SQL).fetchone()
        latest = None if row is None else row["latest"]
        return None if latest is None else int(latest)
