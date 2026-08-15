from __future__ import annotations

from typing import Any


class TitlePresentationStore:
    """Focused PostgreSQL implementation behind the presentation module."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def reconcile(self, *, now_ms: int, policy_version: str) -> int:
        outcome = self.conn.execute(
            """
            UPDATE news_item_title_presentations
               SET state = 'resolved',
                   display_title = original_title,
                   outcome = 'fallback',
                   provider = NULL,
                   policy_version = %(policy_version)s,
                   fallback_code = 'news_title_presentation_interrupted_unknown',
                   resolved_at_ms = %(now_ms)s,
                   duration_ms = greatest(0, %(now_ms)s - attempted_at_ms),
                   updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
             WHERE state = 'resolving'
            """,
            {"now_ms": int(now_ms), "policy_version": str(policy_version)},
        )
        return int(outcome.rowcount)

    def peek_pending(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT presentation.*
              FROM news_item_title_presentations presentation
              LEFT JOIN LATERAL (
                SELECT delivery.live_observed_at_ms
                  FROM news_push_deliveries delivery
                 WHERE delivery.item_id = presentation.item_id
                   AND delivery.source_title_fingerprint =
                       presentation.source_title_fingerprint
                   AND delivery.status = 'pending'
                   AND delivery.source_title_fingerprint IS NOT NULL
                 LIMIT 1
              ) push_wait ON true
             WHERE presentation.state = 'pending'
             ORDER BY (push_wait.live_observed_at_ms IS NOT NULL) DESC,
                      coalesce(
                        push_wait.live_observed_at_ms,
                        presentation.created_at_ms
                      ),
                      presentation.created_at_ms,
                      presentation.item_id,
                      presentation.source_title_fingerprint
             LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def fence(
        self,
        *,
        item_id: str,
        source_title_fingerprint: str,
        attempted_at_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE news_item_title_presentations
               SET state = 'resolving',
                   attempted_at_ms = %(attempted_at_ms)s,
                   updated_at_ms = greatest(updated_at_ms, %(attempted_at_ms)s)
             WHERE item_id = %(item_id)s
               AND source_title_fingerprint = %(source_title_fingerprint)s
               AND state = 'pending'
            RETURNING item_id
            """,
            {
                "item_id": str(item_id),
                "source_title_fingerprint": str(source_title_fingerprint),
                "attempted_at_ms": int(attempted_at_ms),
            },
        ).fetchone()
        return row is not None

    def resolve(
        self,
        *,
        item_id: str,
        source_title_fingerprint: str,
        expected_state: str,
        display_title: str,
        outcome: str,
        provider: str | None,
        policy_version: str,
        fallback_code: str | None,
        resolved_at_ms: int,
        duration_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE news_item_title_presentations
               SET state = 'resolved',
                   display_title = %(display_title)s,
                   outcome = %(outcome)s,
                   provider = %(provider)s,
                   policy_version = %(policy_version)s,
                   fallback_code = %(fallback_code)s,
                   resolved_at_ms = %(resolved_at_ms)s,
                   duration_ms = %(duration_ms)s,
                   updated_at_ms = greatest(updated_at_ms, %(resolved_at_ms)s)
             WHERE item_id = %(item_id)s
               AND source_title_fingerprint = %(source_title_fingerprint)s
               AND state = %(expected_state)s
            RETURNING item_id
            """,
            {
                "item_id": str(item_id),
                "source_title_fingerprint": str(source_title_fingerprint),
                "expected_state": str(expected_state),
                "display_title": str(display_title),
                "outcome": str(outcome),
                "provider": provider,
                "policy_version": str(policy_version),
                "fallback_code": fallback_code,
                "resolved_at_ms": int(resolved_at_ms),
                "duration_ms": int(duration_ms),
            },
        ).fetchone()
        return row is not None


__all__ = ["TitlePresentationStore"]
