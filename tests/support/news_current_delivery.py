"""Seed a current frozen News notification receipt for read-side integration tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def seed_delivery(
    conn: Any,
    *,
    event_id: str,
    at_ms: int,
    history_context: dict[str, Any],
    card: dict[str, Any] | None = None,
    state: str = "sent",
) -> str:
    intent_id = "intent:" + hashlib.sha256(f"{event_id}:{at_ms}".encode()).hexdigest()
    body = str((card or {}).get("header", {}).get("title", {}).get("content") or "Test notification")
    conn.execute(
        """INSERT INTO news_deliveries
             (intent_id,event_id,kind,state,card,receipt,error_code,attempted_at_ms,settled_at_ms,
              created_at_ms,history_context,content_revision,claim_refs,body,payload_sha256,plan_key)
           VALUES (%s,%s,'update',%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,
                   news_text_digest(%s),%s)""",
        (
            intent_id,
            event_id,
            state,
            json.dumps(card or {}),
            json.dumps({"ok": True}) if state == "sent" else None,
            None if state == "sent" else "gave_up",
            at_ms - 1,
            at_ms,
            at_ms - 1,
            json.dumps(history_context),
            hashlib.sha256(event_id.encode()).hexdigest(),
            json.dumps(["cl:fixture"]),
            body,
            body,
            False,
        ),
    )
    conn.execute(
        """UPDATE news_deliveries d SET sent_claims=(
             SELECT COALESCE(jsonb_agg(claim), '[]'::jsonb)
             FROM jsonb_array_elements(u.document->'claims') claim
             WHERE d.claim_refs ? (claim->>'ref'))
           FROM news_event_updates u WHERE d.intent_id=%s
             AND u.event_id=d.event_id AND u.content_revision=d.content_revision""",
        (intent_id,),
    )
    return intent_id
