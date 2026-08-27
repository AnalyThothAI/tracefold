"""Persist exact source-contract Event kind and record the Program route hard cut (#288).

The backfill replays normalized provider tuples from immutable evidence. IDs alone and title text are
deliberately insufficient: an account-scoped Strategy handle can be rebound, while a title is wire
content rather than source identity. For an already judged pre-cut Event, its verdict Program is the
historical route authority so a generic verdict cannot be replayed as a deterministic one. Unsupported
contracts and tuple drift still fail closed before that compatibility rule.

The classifier changes which frames reach a strict deterministic parser, the semantic Program, or a
named unsupported terminal.  That is a code-owned model-route change, so factory v6 candidates are
tripped and one append-only migration receipt records the v7 cut.  The Program artifact itself ships
with the image and is not stored here.

Revision ID: 20260827_0315
Revises: 20260827_0314
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260827_0315"
down_revision = "20260827_0314"
branch_labels = None
depends_on = None

MIGRATION_RECEIPT = {
    "kind": "news_source_contract_event_kind_hard_cut",
    "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/288",
    "epoch_id": "program_v7",
    "from_program_factory_id": "tracefold.news.program.factory_v6",
    "to_program_factory_id": "tracefold.news.program.factory_v7",
    "program_version": "news_semantic_program_v5",
    "event_identity_version": "news_event_identity_v5",
    "prior_evidence_disposition": "prior_factory_evidence_audit_only",
    "activation_disposition": "open_activations_tripped",
}
TRIP_REASON = "news_source_contract_event_kind_hard_cut"

_ROUTING_SOURCE_CTE = """
routing_source AS (
  SELECT e.event_id,
         CASE
           WHEN jsonb_typeof(COALESCE(judged.snapshot, latest.snapshot) -> 'card' -> 'provider_metadata') = 'object'
             THEN COALESCE(judged.snapshot, latest.snapshot) -> 'card' -> 'provider_metadata'
           ELSE i.provider_metadata
         END AS provider_metadata,
         COALESCE(
           NULLIF(COALESCE(judged.snapshot, latest.snapshot) -> 'card' ->> 'leader_item_id', ''),
           e.leader_item_id
         ) AS focus_item_id,
         COALESCE(NULLIF(COALESCE(judged.focus_fact_id, latest.focus_fact_id), ''), e.focus_fact_id) AS focus_fact_id,
         verdict.program_version AS verdict_program_version
    FROM news_events e
    JOIN news_items i ON i.item_id = e.leader_item_id
    LEFT JOIN LATERAL (
      SELECT evidence_version, evidence_sha256, program_version
        FROM news_verdicts
       WHERE event_id = e.event_id AND stage = 'triage' AND evidence_version IS NOT NULL
       ORDER BY created_at_ms DESC, policy_version DESC LIMIT 1
    ) verdict ON true
    LEFT JOIN news_event_evidence_snapshots judged
      ON judged.event_id = e.event_id
     AND judged.evidence_version = verdict.evidence_version
     AND judged.evidence_sha256 = verdict.evidence_sha256
    LEFT JOIN LATERAL (
      SELECT snapshot, focus_fact_id
        FROM news_event_evidence_snapshots
       WHERE event_id = e.event_id
       ORDER BY evidence_version DESC LIMIT 1
    ) latest ON true
)
"""
_ENTRY = "r.provider_metadata -> 'strategies' -> 0"
_COMPLETE_ENTRY = (
    "("
    + " AND ".join(
        f"COALESCE({_ENTRY} ->> '{field}', '') <> ''" for field in ("id", "name", "source_type", "engine_type")
    )
    + ")"
)


def _identity(strategy_id: str, name: str, source_type: str, engine_type: str) -> str:
    return (
        f"{_ENTRY} ->> 'id' = '{strategy_id}' "
        f"AND {_ENTRY} ->> 'name' = '{name}' "
        f"AND {_ENTRY} ->> 'source_type' = '{source_type}' "
        f"AND {_ENTRY} ->> 'engine_type' = '{engine_type}'"
    )


_OI = _identity("1019", "OI Event Monitor", "market", "market")
_LISTING = _identity("1353", "Listing and Delisting Announcements", "news", "listing")
_LIQUIDATION = _identity("2000", "实时清算", "market", "market")
_UNSUPPORTED_WALLET = _identity("2026", "聪明钱监控", "wallet", "market")
_UNSUPPORTED_LIQUIDATION = _identity("2083", "Large-scale liquidation", "market", "market")
_OI_PROGRAM = "news_oi_signal_v1"
_LIQUIDATION_PROGRAM = "news_liquidation_fact_v1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migration_receipt_sha() -> str:
    return hashlib.sha256(_canonical({"kind": "epoch_reset", "payload": MIGRATION_RECEIPT}).encode()).hexdigest()


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("SET LOCAL transaction_timeout = '600s'")
    op.execute("ALTER TABLE news_events ADD COLUMN event_kind TEXT, ADD COLUMN source_contract_reason TEXT")
    op.execute(
        f"""
        WITH {_ROUTING_SOURCE_CTE}
        UPDATE news_events e
           SET event_kind = CASE
             WHEN ({_UNSUPPORTED_WALLET}) OR ({_UNSUPPORTED_LIQUIDATION}) THEN 'unsupported_market'
             -- A known account-scoped handle whose tuple changed is contract drift, never the old lane.
             WHEN {_ENTRY} ->> 'id' IN ('1019', '1353', '2000', '2026', '2083')
              AND NOT (({_OI}) OR ({_LISTING}) OR ({_LIQUIDATION})
                       OR ({_UNSUPPORTED_WALLET}) OR ({_UNSUPPORTED_LIQUIDATION}))
               THEN 'unsupported_market'
             -- An unknown scoreless market/wallet tuple is unsupported, except for the two exact deterministic
             -- contracts below. This safety terminal wins even over a pre-cut generic verdict.
             WHEN NOT (({_OI}) OR ({_LIQUIDATION}))
              AND ({_ENTRY} ->> 'source_type' IN ('market', 'wallet')
                   OR {_ENTRY} ->> 'engine_type' = 'market')
              AND r.provider_metadata ->> 'score' IS NULL THEN 'unsupported_market'
             -- A queued verdict is only reusable under the Program that actually produced it. Before this cut,
             -- a cross-kind focused member could still be judged by the Event's generic admission; preserving
             -- that Program route prevents the same row being replayed as a deterministic verdict.
             WHEN r.verdict_program_version = '{_OI_PROGRAM}' THEN 'oi'
             WHEN r.verdict_program_version = '{_LIQUIDATION_PROGRAM}' THEN 'liquidation'
             WHEN r.verdict_program_version IS NOT NULL THEN
               CASE WHEN ({_LISTING}) OR {_ENTRY} ->> 'engine_type' = 'listing' THEN 'listing' ELSE 'news' END
             WHEN {_OI} THEN 'oi'
             WHEN {_LISTING} THEN 'listing'
             WHEN {_LIQUIDATION} THEN 'liquidation'
             -- Only rows without a complete first tuple may fall back to the old durable route. A live typed
             -- liquidation beside a generic Event is a secondary material fact, not authority to reclassify
             -- that Event; recovery is the sole typed-fact fallback because its provider history can omit tuple
             -- fields. Neither fallback can override a rebound known id above.
             WHEN NOT {_COMPLETE_ENTRY} AND (
                    e.admission = 'liquidation_deterministic'
                    OR (
                      e.ingest_mode = 'recovery'
                      AND EXISTS (
                        SELECT 1 FROM news_market_liquidations l
                         WHERE l.item_id = r.focus_item_id AND l.fact_id = r.focus_fact_id
                      )
                    )
                  ) THEN 'liquidation'
             WHEN NOT {_COMPLETE_ENTRY} AND e.admission = 'telemetry_deterministic' THEN 'oi'
             WHEN NOT {_COMPLETE_ENTRY} AND e.admission = 'listing_deterministic' THEN 'listing'
             WHEN NOT {_COMPLETE_ENTRY} AND e.admission = 'unsupported_market_contract' THEN 'unsupported_market'
             WHEN {_ENTRY} ->> 'engine_type' = 'listing' THEN 'listing'
             ELSE 'news'
           END
          FROM routing_source r
         WHERE r.event_id = e.event_id
        """
    )
    # A foreign or incomplete legacy Item still has an honest generic Event rather than a nullable type.
    op.execute("UPDATE news_events SET event_kind = 'news' WHERE event_kind IS NULL")
    # Deterministic parse failures are source-contract drift even when the historical Event had a valid
    # provider tuple.  Keep this first so the generic unsupported backfill cannot hide stronger evidence.
    op.execute(
        """
        UPDATE news_events e
           SET source_contract_reason = 'source_contract_drift'
         WHERE e.event_kind IN ('oi', 'liquidation')
           AND EXISTS (
             SELECT 1 FROM news_verdicts v
              WHERE v.event_id = e.event_id AND v.stage = 'triage'
                AND v.error_code IN ('oi_parse_failed', 'liquidation_parse_failed')
           )
        """
    )
    # A known account-scoped handle under the wrong full tuple is drift. Exact unsupported contracts and
    # unknown scoreless market/wallet contracts are merely unsupported by this release.
    op.execute(
        f"""
        WITH {_ROUTING_SOURCE_CTE}
        UPDATE news_events e
           SET source_contract_reason = CASE
             WHEN {_ENTRY} ->> 'id' IN ('1019', '1353', '2000', '2026', '2083')
              AND NOT (({_OI}) OR ({_LISTING}) OR ({_LIQUIDATION})
                       OR ({_UNSUPPORTED_WALLET}) OR ({_UNSUPPORTED_LIQUIDATION}))
               THEN 'source_contract_drift'
             ELSE 'unsupported_market_contract'
           END
          FROM routing_source r
         WHERE r.event_id = e.event_id
           AND e.event_kind = 'unsupported_market'
           AND e.source_contract_reason IS NULL
        """
    )
    op.execute(
        "UPDATE news_events SET source_contract_reason = 'unsupported_market_contract' "
        "WHERE event_kind = 'unsupported_market' AND source_contract_reason IS NULL"
    )
    # Pre-cut Admission did not durably record a successful strict parse. Absence of a failure verdict is
    # not success: only existing typed success evidence proves it. The OI row is a derived read-model row,
    # not a second material truth; it is used here only as conservative migration evidence.
    op.execute(
        """
        UPDATE news_events e
           SET source_contract_reason = 'source_contract_unverified'
         WHERE e.event_kind = 'oi'
           AND e.source_contract_reason IS NULL
           AND NOT EXISTS (SELECT 1 FROM news_oi_signals s WHERE s.event_id = e.event_id)
        """
    )
    op.execute(
        f"""
        WITH {_ROUTING_SOURCE_CTE}
        UPDATE news_events e
           SET source_contract_reason = 'source_contract_unverified'
          FROM routing_source r
         WHERE e.event_kind = 'liquidation'
           AND r.event_id = e.event_id
           AND e.source_contract_reason IS NULL
           AND NOT EXISTS (
             SELECT 1 FROM news_market_liquidations l
              WHERE l.item_id = r.focus_item_id AND l.fact_id = r.focus_fact_id
           )
        """
    )
    # Every unsupported row adopts the current named hold. The public outcome gives any historical delivery
    # ledger state priority over current routing admission, while a queued push becomes held instead of
    # staying permanently pending.
    op.execute(
        """
        UPDATE news_events e
           SET admission = 'unsupported_market_contract'
         WHERE e.event_kind = 'unsupported_market'
        """
    )
    op.execute(
        """
        ALTER TABLE news_events
          ALTER COLUMN event_kind SET NOT NULL,
          ADD CONSTRAINT news_events_event_kind_check CHECK (
            event_kind IN ('news', 'listing', 'oi', 'liquidation', 'unsupported_market')
          ),
          ADD CONSTRAINT news_events_source_contract_reason_check CHECK (
            source_contract_reason IS NULL OR source_contract_reason IN (
              'source_contract_drift', 'source_contract_unverified', 'unsupported_market_contract'
            )
          ),
          ADD CONSTRAINT news_events_source_contract_consistency_check CHECK (
            (event_kind IN ('news', 'listing') AND source_contract_reason IS NULL)
            OR (
              event_kind IN ('oi', 'liquidation')
              AND (
                source_contract_reason IS NULL
                OR source_contract_reason IN ('source_contract_drift', 'source_contract_unverified')
              )
            )
            OR (
              event_kind = 'unsupported_market'
              AND source_contract_reason IS NOT NULL
              AND source_contract_reason IN ('source_contract_drift', 'unsupported_market_contract')
            )
          )
        """
    )
    op.execute("CREATE INDEX ix_news_events_kind_opened ON news_events (event_kind, opened_at_ms DESC, event_id DESC)")
    # `liquidation_deterministic` joined the admitted set in this cut. Keep the
    # Janitor rescue query covered by the matching partial index after a
    # commit-before-publish crash.
    op.execute("DROP INDEX ix_news_events_unpublished")
    op.execute(
        """
        CREATE INDEX ix_news_events_unpublished ON news_events (opened_at_ms)
         WHERE published_at_ms IS NULL
           AND admission IN (
             'candidate', 'listing_deterministic',
             'telemetry_deterministic', 'liquidation_deterministic'
           )
        """
    )

    op.execute(
        sa.text(
            """
            UPDATE news_canary_activations
               SET state = 'tripped', revision = revision + 1, trip_reason = :trip_reason,
                   tripped_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
             WHERE state IN ('armed', 'active')
            """
        ).bindparams(trip_reason=TRIP_REASON)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO news_learning_artifacts (
              artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
            )
            SELECT :artifact_sha, 'epoch_reset', NULL, CAST(:payload AS jsonb), :created_by,
                   floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
            ON CONFLICT (artifact_sha) DO NOTHING
            """
        ).bindparams(
            artifact_sha=migration_receipt_sha(),
            payload=_canonical(MIGRATION_RECEIPT),
            created_by="migration_20260827_0315",
        )
    )


def downgrade() -> None:
    raise RuntimeError("20260827_0315 is an irreversible Event-kind and Program-route hard cut")
