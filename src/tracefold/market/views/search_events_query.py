from __future__ import annotations

import json
import re
from typing import Any

from tracefold.market.capture.evidence_repository import decode_event_row
from tracefold.market.identity.chain_identity import canonical_chain_address
from tracefold.market.identity.resolver_policy import TOKEN_RESOLVER_POLICY_VERSION
from tracefold.platform.validation import require_nonnegative_int

_SUBSTRING_QUERY_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_EVM_REGISTRY_CHAINS = ("eip155:1", "eip155:8453", "eip155:56")
_REGISTRY_CHAIN_ALIASES = {
    "eth": "eip155:1",
    "ethereum": "eip155:1",
    "base": "eip155:8453",
    "bsc": "eip155:56",
    "bnb": "eip155:56",
    "sol": "solana",
    "solana": "solana",
    "ton": "ton",
}


class SearchEventsQuery:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def resolve_targets(self, intent: Any) -> list[dict[str, Any]]:
        if intent.kind == "symbol" and intent.symbol:
            return self._resolve_symbol(intent.symbol)
        if intent.kind == "ca" and intent.ca:
            return self._resolve_ca(address=intent.ca, chain=intent.chain)
        return []

    def resolve_symbols(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = [str(symbol).strip().lstrip("$").upper() for symbol in symbols if str(symbol).strip()]
        if not normalized:
            return []
        rows = self.conn.execute(
            """
            WITH input_symbols AS (
              SELECT
                upper(trim(symbol_value)) AS symbol,
                ordinality
              FROM unnest(%s::text[]) WITH ORDINALITY AS input(symbol_value, ordinality)
              WHERE trim(symbol_value) <> ''
            ),
            distinct_symbols AS (
              SELECT symbol, MIN(ordinality) AS ordinality
              FROM input_symbols
              GROUP BY symbol
            ),
            cex_candidates AS (
              SELECT
                distinct_symbols.ordinality AS input_ordinal,
                'CexToken' AS target_type,
                cex_tokens.cex_token_id AS target_id,
                cex_tokens.base_symbol AS symbol,
                NULL::text AS chain_id,
                NULL::text AS address,
                'resolved' AS status,
                'cex_token' AS source,
                'CONFIRMED_CEX_TOKEN' AS reason,
                0 AS sort_group
              FROM distinct_symbols
              JOIN cex_tokens
                ON upper(cex_tokens.base_symbol) = distinct_symbols.symbol
              WHERE cex_tokens.status IN ('candidate', 'canonical')
            ),
            asset_candidates AS (
              SELECT
                distinct_symbols.ordinality AS input_ordinal,
                'Asset' AS target_type,
                registry_assets.asset_id AS target_id,
                asset_identity_current.canonical_symbol AS symbol,
                registry_assets.chain_id,
                registry_assets.address,
                CASE
                  WHEN COUNT(*) OVER (PARTITION BY distinct_symbols.symbol) = 1 THEN 'resolved'
                  ELSE 'ambiguous'
                END AS status,
                'asset_identity_current' AS source,
                'CANONICAL_SYMBOL_MATCH' AS reason,
                1 AS sort_group
              FROM distinct_symbols
              JOIN asset_identity_current
                ON upper(asset_identity_current.canonical_symbol) = distinct_symbols.symbol
              JOIN registry_assets
                ON registry_assets.asset_id = asset_identity_current.asset_id
              WHERE registry_assets.status IN ('candidate', 'canonical')
            )
            SELECT target_type, target_id, symbol, chain_id, address, status, source, reason
            FROM (
              SELECT * FROM cex_candidates
              UNION ALL
              SELECT * FROM asset_candidates
            ) candidates
            ORDER BY input_ordinal, sort_group, target_id
            """,
            (normalized,),
        ).fetchall()
        return [_candidate(row) for row in rows]

    def route_hits(
        self,
        *,
        intent: Any,
        target_candidates: list[dict[str, Any]],
        route_limit: int,
        since_ms: int,
    ) -> list[dict[str, Any]]:
        limit = require_nonnegative_int(route_limit, error_code="search_events_route_limit_required")
        if limit <= 0:
            return []
        hits: list[dict[str, Any]] = []
        resolved_targets = [
            candidate for candidate in target_candidates if str(candidate.get("status") or "") == "resolved"
        ]
        if resolved_targets:
            hits.extend(self._target_hits(resolved_targets, limit=limit, since_ms=since_ms))
        if intent.kind == "handle" and intent.handle:
            hits.extend(self._handle_hits(intent.handle, limit=limit, since_ms=since_ms))
        lexical_query = (intent.lexical_query or intent.normalized_text or "").strip()
        if intent.kind in {"symbol", "text", "ca"} and lexical_query:
            hits.extend(self._lexical_hits(lexical_query, limit=limit, since_ms=since_ms))
        substring_hits: list[dict[str, Any]] = []
        if len(hits) < limit and _safe_substring_query(lexical_query):
            substring_hits = self._substring_hits(lexical_query, limit=limit, since_ms=since_ms)
            hits.extend(substring_hits)
        return hits

    def target_hits_page(
        self,
        target_candidates: list[dict[str, Any]],
        *,
        limit: int,
        after: dict[str, Any] | None = None,
        since_ms: int,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_target_page_limit_required")
        if row_limit <= 0:
            return []
        resolved_targets = [
            candidate for candidate in target_candidates if str(candidate.get("status") or "") == "resolved"
        ]
        if not resolved_targets:
            return []
        return self._target_hits_page(resolved_targets, limit=row_limit, after=after, since_ms=since_ms)

    def _resolve_symbol(self, symbol: str) -> list[dict[str, Any]]:
        normalized = symbol.strip().lstrip("$").upper()
        rows = self.conn.execute(
            """
            WITH candidates AS (
              SELECT
                'CexToken' AS target_type,
                cex_token_id AS target_id,
                base_symbol AS symbol,
                NULL::text AS chain_id,
                NULL::text AS address,
                'resolved' AS status,
                'cex_token' AS source,
                'CONFIRMED_CEX_TOKEN' AS reason,
                0 AS sort_group
              FROM cex_tokens
              WHERE upper(base_symbol) = %s
                AND status IN ('candidate', 'canonical')
              UNION ALL
              SELECT
                'Asset' AS target_type,
                registry_assets.asset_id AS target_id,
                asset_identity_current.canonical_symbol AS symbol,
                registry_assets.chain_id,
                registry_assets.address,
                CASE
                  WHEN COUNT(*) OVER () = 1 THEN 'resolved'
                  ELSE 'ambiguous'
                END AS status,
                'asset_identity_current' AS source,
                'CANONICAL_SYMBOL_MATCH' AS reason,
                1 AS sort_group
              FROM registry_assets
              JOIN asset_identity_current
                ON asset_identity_current.asset_id = registry_assets.asset_id
              WHERE upper(asset_identity_current.canonical_symbol) = %s
                AND registry_assets.status IN ('candidate', 'canonical')
            )
            SELECT target_type, target_id, symbol, chain_id, address, status, source, reason
            FROM candidates
            ORDER BY sort_group, target_id
            """,
            (normalized, normalized),
        ).fetchall()
        return [_candidate(row) for row in rows]

    def _resolve_ca(self, *, address: str, chain: str | None) -> list[dict[str, Any]]:
        registry_chain = _registry_chain(chain)
        normalized_address = canonical_chain_address(registry_chain, address)
        clauses = ["registry_assets.address = %s", "registry_assets.status IN ('candidate', 'canonical')"]
        params: list[Any] = [normalized_address]
        if registry_chain:
            clauses.append("registry_assets.chain_id = %s")
            params.append(registry_chain)
        elif chain in {"evm", "evm_unknown"}:
            placeholders = ",".join("%s" for _ in _EVM_REGISTRY_CHAINS)
            clauses.append(f"registry_assets.chain_id IN ({placeholders})")
            params.extend(_EVM_REGISTRY_CHAINS)
        rows = self.conn.execute(
            f"""
            SELECT
              'Asset' AS target_type,
              registry_assets.asset_id AS target_id,
              asset_identity_current.canonical_symbol AS symbol,
              registry_assets.chain_id,
              registry_assets.address,
              'resolved' AS status,
              'registry_asset_address' AS source,
              'CHAIN_ADDRESS_EXACT' AS reason
            FROM registry_assets
            LEFT JOIN asset_identity_current
              ON asset_identity_current.asset_id = registry_assets.asset_id
            WHERE {" AND ".join(clauses)}
            ORDER BY registry_assets.updated_at_ms DESC, registry_assets.asset_id
            """,
            params,
        ).fetchall()
        return [_candidate(row) for row in rows]

    def _target_hits(
        self,
        target_candidates: list[dict[str, Any]],
        *,
        limit: int,
        since_ms: int,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_target_limit_required")
        if row_limit <= 0:
            return []
        values_sql = ",".join("(%s, %s, %s)" for _ in target_candidates)
        params: list[Any] = []
        for candidate in target_candidates:
            params.extend([candidate["target_type"], candidate["target_id"], candidate.get("symbol")])
        params.extend([TOKEN_RESOLVER_POLICY_VERSION, since_ms, row_limit])
        rows = self.conn.execute(
            f"""
            WITH target_candidates(target_type, target_id, target_symbol) AS (
              VALUES {values_sql}
            ),
            ranked AS (
              SELECT
                events.*,
                tir.target_type,
                tir.target_id,
                target_candidates.target_symbol,
                row_number() OVER (
                  ORDER BY
                    CASE
                      WHEN tir.resolution_status = 'EXACT' THEN 0
                      WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 1
                      WHEN tir.resolution_status = 'AMBIGUOUS' THEN 2
                      ELSE 3
                    END,
                    events.received_at_ms DESC,
                    events.event_id DESC
                ) AS route_rank,
                CASE
                  WHEN tir.resolution_status = 'EXACT' THEN 1.0
                  WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 0.9
                  WHEN tir.resolution_status = 'AMBIGUOUS' THEN 0.45
                  ELSE 0.1
                END AS route_score
              FROM target_candidates
              JOIN token_intent_resolutions tir
                ON tir.target_type = target_candidates.target_type
               AND tir.target_id = target_candidates.target_id
               AND tir.is_current = true
               AND tir.resolver_policy_version = %s
              JOIN events ON events.event_id = tir.event_id
              WHERE events.received_at_ms >= %s
            )
            SELECT *, 'target' AS route, jsonb_build_array('target:' || target_type) AS match_reasons_json
            FROM ranked
            ORDER BY route_rank
            LIMIT %s
            """,
            params,
        ).fetchall()
        return [_hit(row) for row in rows]

    def _target_hits_page(
        self,
        target_candidates: list[dict[str, Any]],
        *,
        limit: int,
        after: dict[str, Any] | None,
        since_ms: int,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_target_page_limit_required")
        if row_limit <= 0:
            return []
        values_sql = ",".join("(%s, %s, %s)" for _ in target_candidates)
        params: list[Any] = []
        for candidate in target_candidates:
            params.extend([candidate["target_type"], candidate["target_id"], candidate.get("symbol")])
        after_rank = int(after["status_rank"]) if after else None
        after_received = int(after["received_at_ms"]) if after else None
        after_event_id = str(after["event_id"]) if after else None
        params.extend(
            [
                TOKEN_RESOLVER_POLICY_VERSION,
                since_ms,
                after_rank,
                after_rank,
                after_rank,
                after_received,
                after_rank,
                after_received,
                after_event_id,
                row_limit,
            ]
        )
        rows = self.conn.execute(
            f"""
            WITH target_candidates(target_type, target_id, target_symbol) AS (
              VALUES {values_sql}
            ),
            ranked AS (
              SELECT
                events.*,
                tir.target_type,
                tir.target_id,
                target_candidates.target_symbol,
                CASE
                  WHEN tir.resolution_status = 'EXACT' THEN 0
                  WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 1
                  WHEN tir.resolution_status = 'AMBIGUOUS' THEN 2
                  ELSE 3
                END AS target_status_rank,
                CASE
                  WHEN tir.resolution_status = 'EXACT' THEN 1.0
                  WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 0.9
                  WHEN tir.resolution_status = 'AMBIGUOUS' THEN 0.45
                  ELSE 0.1
                END AS route_score
              FROM target_candidates
              JOIN token_intent_resolutions tir
                ON tir.target_type = target_candidates.target_type
               AND tir.target_id = target_candidates.target_id
               AND tir.is_current = true
               AND tir.resolver_policy_version = %s
              JOIN events ON events.event_id = tir.event_id
              WHERE events.received_at_ms >= %s
            ),
            deduped AS (
              SELECT *
              FROM (
                SELECT
                  ranked.*,
                  row_number() OVER (
                    PARTITION BY event_id
                    ORDER BY target_status_rank ASC, target_id ASC
                  ) AS target_event_rank
                FROM ranked
              ) unique_ranked
              WHERE target_event_rank = 1
            ),
            page AS (
              SELECT *
              FROM deduped
              WHERE (
                  %s::integer IS NULL
                  OR target_status_rank > %s::integer
                  OR (target_status_rank = %s::integer AND received_at_ms < %s::bigint)
                  OR (target_status_rank = %s::integer AND received_at_ms = %s::bigint AND event_id < %s::text)
                )
              ORDER BY target_status_rank ASC, received_at_ms DESC, event_id DESC
              LIMIT %s
            )
            SELECT
              *,
              row_number() OVER (ORDER BY target_status_rank ASC, received_at_ms DESC, event_id DESC) AS route_rank,
              'target' AS route,
              jsonb_build_array('target:' || target_type) AS match_reasons_json
            FROM page
            ORDER BY target_status_rank ASC, received_at_ms DESC, event_id DESC
            """,
            params,
        ).fetchall()
        return [_hit(row) for row in rows]

    def _handle_hits(self, handle: str, *, limit: int, since_ms: int) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_route_limit_required")
        if row_limit <= 0:
            return []
        rows = self.conn.execute(
            """
            SELECT
              events.*,
              NULL::text AS target_type,
              NULL::text AS target_id,
              NULL::text AS target_symbol,
              row_number() OVER (ORDER BY events.received_at_ms DESC, events.event_id DESC) AS route_rank,
              1.0 AS route_score,
              'handle' AS route,
              jsonb_build_array('author_handle') AS match_reasons_json
            FROM events
            WHERE events.author_handle = %s
              AND events.received_at_ms >= %s
            ORDER BY events.received_at_ms DESC, events.event_id DESC
            LIMIT %s
            """,
            (handle.strip().lstrip("@").lower(), since_ms, row_limit),
        ).fetchall()
        return [_hit(row) for row in rows]

    def _lexical_hits(self, query: str, *, limit: int, since_ms: int) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_route_limit_required")
        if row_limit <= 0:
            return []
        rows = self.conn.execute(
            """
            WITH query AS (
              SELECT
                websearch_to_tsquery('simple', %s) AS simple_q,
                websearch_to_tsquery('english', %s) AS english_q
            ),
            ranked AS (
              SELECT
                events.*,
                NULL::text AS target_type,
                NULL::text AS target_id,
                NULL::text AS target_symbol,
                (
                  ts_rank_cd(events.search_tsv, query.simple_q)
                  + ts_rank_cd(events.search_tsv, query.english_q)
                ) AS route_score
              FROM events, query
              WHERE (
                  events.search_tsv @@ query.simple_q
                  OR events.search_tsv @@ query.english_q
                )
                AND events.received_at_ms >= %s
            )
            SELECT
              *,
              row_number() OVER (
                ORDER BY received_at_ms DESC, event_id DESC
              ) AS route_rank,
              'lexical' AS route,
              jsonb_build_array('fts') AS match_reasons_json
            FROM ranked
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT %s
            """,
            (query, query, since_ms, row_limit),
        ).fetchall()
        return [_hit(row) for row in rows]

    def _substring_hits(self, query: str, *, limit: int, since_ms: int) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="search_events_route_limit_required")
        if row_limit <= 0:
            return []
        rows = self.conn.execute(
            """
            SELECT
              events.*,
              NULL::text AS target_type,
              NULL::text AS target_id,
              NULL::text AS target_symbol,
              0.45 AS route_score,
              row_number() OVER (ORDER BY events.received_at_ms DESC, events.event_id DESC) AS route_rank,
              'substring' AS route,
              jsonb_build_array('substring') AS match_reasons_json
            FROM events
            WHERE events.search_text ILIKE %s ESCAPE '\\'
              AND events.received_at_ms >= %s
            ORDER BY events.received_at_ms DESC, events.event_id DESC
            LIMIT %s
            """,
            (_substring_pattern(query), since_ms, row_limit),
        ).fetchall()
        return [_hit(row) for row in rows]


def _candidate(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "target_type": data.get("target_type"),
        "target_id": data.get("target_id"),
        "symbol": data.get("symbol"),
        "chain_id": data.get("chain_id"),
        "address": data.get("address"),
        "status": data.get("status"),
        "source": data.get("source"),
        "reason": data.get("reason"),
    }


def _hit(row: Any) -> dict[str, Any]:
    data = dict(row)
    target = None
    if data.get("target_type") and data.get("target_id"):
        target = {
            "target_type": data.get("target_type"),
            "target_id": data.get("target_id"),
            "symbol": data.get("target_symbol"),
            "status": "resolved",
            "source": "token_intent_resolutions",
            "reason": "TARGET_ROUTE",
        }
    return {
        "event_id": str(data.get("event_id")),
        "event": decode_event_row(data),
        "route": str(data.get("route")),
        "route_rank": int(data.get("route_rank") or 0),
        "route_score": float(data.get("route_score") or 0.0),
        "match_reasons": _json_array(data.get("match_reasons_json")),
        "target": target,
        "target_status_rank": int(data.get("target_status_rank") or 0),
        "received_at_ms": int(data.get("received_at_ms") or 0),
    }


def _json_array(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return [str(value)]


def _registry_chain(chain: str | None) -> str | None:
    if not chain:
        return None
    return _REGISTRY_CHAIN_ALIASES.get(chain.strip().lower())


def _safe_substring_query(query: str) -> bool:
    return bool(_SUBSTRING_QUERY_RE.fullmatch(query.strip()))


def _substring_pattern(query: str) -> str:
    escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
