from __future__ import annotations

import hashlib
import time
from typing import Any

from tracefold.market.capture.entity import EVM_QUERY_CHAINS, ExtractedEntity
from tracefold.market.capture.twitter_event import TwitterEvent
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import mutation_count
from tracefold.platform.validation import require_nonnegative_int


class EntityRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def insert_event_entities(
        self,
        event: TwitterEvent,
        entities: list[ExtractedEntity],
    ) -> int:
        require_transaction(self.conn, operation="insert_event_entities")
        inserted = 0
        now_ms = _now_ms()
        author = event.author.handle.lower() if event.author.handle else None
        for entity in entities:
            cursor = self.conn.execute(
                """
                INSERT INTO event_entities(
                  entity_id, event_id, entity_type, raw_value, normalized_value, chain,
                  token_resolution_status, confidence, source, received_at_ms, author_handle,
                  text_surface, span_start, span_end, sentence_id, local_group_key,
                  created_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    _entity_id(event.event_id, entity),
                    event.event_id,
                    entity.entity_type,
                    entity.raw_value,
                    entity.normalized_value,
                    entity.chain,
                    entity.token_resolution_status,
                    entity.confidence,
                    entity.source,
                    event.received_at_ms,
                    author,
                    entity.text_surface,
                    entity.span_start,
                    entity.span_end,
                    entity.sentence_id,
                    entity.local_group_key,
                    now_ms,
                ),
            )
            rowcount = mutation_count(cursor, error_code="entity_repository_rowcount_invalid")
            if rowcount not in (0, 1):
                raise TypeError("entity_repository_rowcount_invalid")
            inserted += rowcount
        return inserted

    def entities_for_event(self, event_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM event_entities WHERE event_id = %s ORDER BY entity_type, normalized_value",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def entities_for_events(self, event_ids: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        ids = _event_ids(event_ids)
        if not ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT *
            FROM event_entities
            WHERE event_id = ANY(%s)
            ORDER BY event_id, entity_type, normalized_value
            """,
            (ids,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(str(item["event_id"]), []).append(item)
        return grouped

    def find_by_symbol(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        return self._find(
            entity_type="symbol",
            normalized_value=symbol.strip().lstrip("$").upper(),
            chain=None,
            limit=limit,
        )

    def _find(
        self,
        *,
        entity_type: str,
        normalized_value: str,
        chain: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        parsed_limit = require_nonnegative_int(limit, error_code="entity_repository_find_limit_required")
        clauses = ["entity_type = %s", "normalized_value = %s"]
        params: list[Any] = [entity_type, normalized_value]
        if chain is None:
            clauses.append("chain IS NULL")
        elif chain == "evm_unknown" and entity_type == "ca":
            placeholders = ",".join("%s" for _ in EVM_QUERY_CHAINS)
            clauses.append(f"chain IN ({placeholders})")
            params.extend(sorted(EVM_QUERY_CHAINS))
        else:
            clauses.append("chain = %s")
            params.append(chain)
        rows = self.conn.execute(
            f"""
            SELECT * FROM event_entities
            WHERE {" AND ".join(clauses)}
            ORDER BY received_at_ms DESC
            LIMIT %s
            """,
            (*params, parsed_limit),
        ).fetchall()
        return [dict(row) for row in rows]


def _entity_id(event_id: str, entity: ExtractedEntity) -> str:
    payload = "|".join(
        [
            event_id,
            entity.entity_type,
            entity.normalized_value,
            entity.chain or "",
            entity.text_surface,
            str(entity.span_start),
            str(entity.span_end),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _event_ids(event_ids: tuple[str, ...]) -> list[str]:
    return [event_id for event_id in dict.fromkeys(str(item).strip() for item in event_ids) if event_id]
