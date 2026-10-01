"""The event's single notification-planning marker, shared with semantic adoption."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class NotificationJobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content_revision: str
    reader_revision: str | None = None
    decision_ref: str | None = None


class NotificationJobs:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def open_or_retarget(self, event_id: str, content_revision: str, *, reopen: bool, now_ms: int) -> None:
        detail = NotificationJobDetail(content_revision=content_revision).model_dump_json()
        if not reopen:
            self.conn.execute(
                """UPDATE news_jobs SET detail=%s::jsonb,next_attempt_at_ms=LEAST(next_attempt_at_ms,%s)
                   WHERE job_kind='notify' AND subject_id=%s AND state IN ('pending','failed')""",
                (detail, now_ms, event_id),
            )
            return
        self.conn.execute(
            """INSERT INTO news_jobs(
                 job_kind,subject_id,state,attempts,next_attempt_at_ms,detail,created_at_ms,updated_at_ms)
               VALUES ('notify',%s,'pending',0,%s,%s::jsonb,%s,%s)
               ON CONFLICT(job_kind,subject_id) DO UPDATE SET state='pending',attempts=0,last_error_code=NULL,
                 next_attempt_at_ms=EXCLUDED.next_attempt_at_ms,detail=EXCLUDED.detail,
                 updated_at_ms=EXCLUDED.updated_at_ms""",
            (event_id, now_ms, detail, now_ms, now_ms),
        )


class MarketNotificationJobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    market_kind: str
    family: str
    provider: str | None = None
    source_venue: str | None = None
    venue_known: bool = False
    raw_instrument: str | None = None
    symbol: str | None = None
    measurement_definition: str | None = None
    liquidated_position_side: str | None = None
    account_key: str | None = None
    account_verified: bool = False
    trader_label: str | None = None
    last_observed_at_ms: int = 0
    last_observed_item_id: str = ""
    anchor_state: str = ""
    anchor_delivery_key: str | None = None
    anchor_attempt_at_ms: int | None = None
    anchor_oi_change_bps: int | None = None
    anchor_direction: str | None = None
    anchor_action: str | None = None
    anchor_position_side: str | None = None
    open_delivery_key: str | None = None
    pending_reason: str = ""
    round_started_at_ms: int = 0
