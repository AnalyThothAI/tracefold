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
        """WITH
        source(intent_id,event_id,kind,state,card,receipt,error_code,attempted_at_ms,settled_at_ms,created_at_ms,
        history_context,content_revision,claim_refs,body,payload_sha256,plan_key) AS (VALUES (%s,%s,'update',%s,
        %s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,
                   news_text_digest(%s),%s))
INSERT INTO news_notifications(intent_id,event_id,kind,state,card,receipt,error_code,attempted_at_ms,
        settled_at_ms,created_at_ms,history_context,content_revision,claim_refs,plan_key,notification_id,origin,
        updated_at_ms)
SELECT s.intent_id::text,s.event_id::text,s.kind::text,s.state::text,s.card::jsonb||jsonb_build_object('body',
        s.body::text,'payload_sha256',s.payload_sha256::text),s.receipt::jsonb,s.error_code::text,
        s.attempted_at_ms::bigint,s.settled_at_ms::bigint,s.created_at_ms::bigint,s.history_context::jsonb,
        s.content_revision::text,s.claim_refs::jsonb,s.plan_key::boolean,s.intent_id::text,'legacy_delivery',
        s.created_at_ms::bigint FROM source s
ON CONFLICT(notification_id) DO UPDATE SET intent_id=EXCLUDED.intent_id,state=EXCLUDED.state,card=EXCLUDED.card,
        receipt=EXCLUDED.receipt,error_code=EXCLUDED.error_code,attempted_at_ms=EXCLUDED.attempted_at_ms,
        settled_at_ms=EXCLUDED.settled_at_ms,created_at_ms=EXCLUDED.created_at_ms,
        history_context=EXCLUDED.history_context,content_revision=EXCLUDED.content_revision,
        claim_refs=EXCLUDED.claim_refs,plan_key=EXCLUDED.plan_key,updated_at_ms=EXCLUDED.updated_at_ms""",
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
        """UPDATE news_notifications d SET sent_claims=(
             SELECT COALESCE(jsonb_agg(claim), '[]'::jsonb)
             FROM jsonb_array_elements(u.document->'claims') claim
             WHERE d.claim_refs ? (claim->>'ref'))
           FROM news_event_updates u WHERE d.intent_id=%s
             AND u.event_id=d.event_id AND u.content_revision=d.content_revision""",
        (intent_id,),
    )
    return intent_id
