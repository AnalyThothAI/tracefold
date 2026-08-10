from __future__ import annotations

import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.market.identity.chain_identity import canonical_chain_address, canonical_chain_id
from tracefold.platform.postgres.write_contract import expect_mutation_count, returning_mutation_count
from tracefold.platform.validation import require_nonnegative_int


class RegistryRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def upsert_cex_token(
        self,
        *,
        base_symbol: str,
        source: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        symbol = _symbol(base_symbol)
        cex_token_id = f"cex_token:{symbol}"
        cursor = self.conn.execute(
            """
            INSERT INTO cex_tokens(
              cex_token_id, base_symbol, status, evidence_level,
              first_seen_at_ms, updated_at_ms
            )
            VALUES (%s, %s, 'canonical', %s, %s, %s)
            ON CONFLICT(cex_token_id) DO UPDATE SET
              base_symbol = excluded.base_symbol,
              status = 'canonical',
              evidence_level = excluded.evidence_level,
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (cex_token_id, symbol, source, int(observed_at_ms), int(observed_at_ms)),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def upsert_chain_asset(
        self,
        *,
        chain_id: str,
        address: str,
        observed_at_ms: int,
        token_standard: str | None = None,
        status: str = "candidate",
    ) -> dict[str, Any]:
        normalized_chain = canonical_chain_id(chain_id)
        normalized_address = canonical_chain_address(normalized_chain, address)
        standard = token_standard or ("erc20" if normalized_chain.startswith("eip155:") else "token")
        asset_id = f"asset:{normalized_chain}:{standard}:{normalized_address}"
        cursor = self.conn.execute(
            """
            WITH existing AS (
              SELECT registry_assets.asset_id
                FROM registry_assets
               WHERE registry_assets.asset_id = %s
                  OR (
                    registry_assets.chain_id = %s
                    AND registry_assets.address = %s
                  )
               ORDER BY
                 CASE WHEN registry_assets.asset_id = %s THEN 0 ELSE 1 END,
                 registry_assets.first_seen_at_ms ASC,
                 registry_assets.asset_id ASC
               LIMIT 1
            ),
            updated AS (
              UPDATE registry_assets
                 SET chain_id = %s,
                     token_standard = %s,
                     address = %s,
                     status = CASE
                       WHEN registry_assets.status = 'demoted_search' THEN 'candidate'
                       ELSE registry_assets.status
                     END,
                     updated_at_ms = %s
               WHERE registry_assets.asset_id = (SELECT asset_id FROM existing)
               RETURNING *
            ),
            inserted AS (
              INSERT INTO registry_assets(
                asset_id, chain_id, token_standard, address, status, first_seen_at_ms, updated_at_ms
              )
              SELECT %s, %s, %s, %s, %s, %s, %s
               WHERE NOT EXISTS (SELECT 1 FROM updated)
              ON CONFLICT(chain_id, address) DO UPDATE SET
                status = CASE
                  WHEN registry_assets.status = 'demoted_search' THEN 'candidate'
                  ELSE registry_assets.status
                END,
                updated_at_ms = excluded.updated_at_ms
              RETURNING *
            )
            SELECT *
              FROM updated
            UNION ALL
            SELECT *
              FROM inserted
            LIMIT 1
            """,
            (
                asset_id,
                normalized_chain,
                normalized_address,
                asset_id,
                normalized_chain,
                standard,
                normalized_address,
                int(observed_at_ms),
                asset_id,
                normalized_chain,
                standard,
                normalized_address,
                status,
                int(observed_at_ms),
                int(observed_at_ms),
            ),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def upsert_pricefeed(
        self,
        *,
        feed_type: str,
        provider: str,
        subject_type: str,
        subject_id: str,
        observed_at_ms: int,
        native_market_id: str | None = None,
        chain_id: str | None = None,
        address: str | None = None,
        base_asset_id: str | None = None,
        base_cex_token_id: str | None = None,
        base_symbol: str | None = None,
        quote_symbol: str | None = None,
        multiplier: Any = None,
    ) -> dict[str, Any]:
        normalized_feed_type = feed_type.strip().lower()
        normalized_provider = provider.strip().lower()
        normalized_market = native_market_id.strip().upper() if native_market_id else None
        normalized_chain = canonical_chain_id(chain_id) if chain_id else None
        normalized_address = canonical_chain_address(normalized_chain, address) if address else None
        pricefeed_id = _pricefeed_id(
            feed_type=normalized_feed_type,
            provider=normalized_provider,
            native_market_id=normalized_market,
            chain_id=normalized_chain,
            address=normalized_address,
        )
        cursor = self.conn.execute(
            """
            INSERT INTO price_feeds(
              pricefeed_id, feed_type, provider, subject_type, subject_id, chain_id, address,
              native_market_id, base_asset_id, base_cex_token_id, base_symbol,
              quote_symbol, multiplier, status, evidence_level, first_seen_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              'canonical', 'price_observation', %s, %s
            )
            ON CONFLICT(pricefeed_id) DO UPDATE SET
              subject_type = excluded.subject_type,
              subject_id = excluded.subject_id,
              base_asset_id = COALESCE(excluded.base_asset_id, price_feeds.base_asset_id),
              base_cex_token_id = COALESCE(excluded.base_cex_token_id, price_feeds.base_cex_token_id),
              base_symbol = COALESCE(excluded.base_symbol, price_feeds.base_symbol),
              quote_symbol = COALESCE(excluded.quote_symbol, price_feeds.quote_symbol),
              multiplier = COALESCE(excluded.multiplier, price_feeds.multiplier),
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (
                pricefeed_id,
                normalized_feed_type,
                normalized_provider,
                subject_type,
                subject_id,
                normalized_chain,
                normalized_address,
                normalized_market,
                base_asset_id,
                base_cex_token_id,
                _symbol(base_symbol) if base_symbol else None,
                quote_symbol.upper() if quote_symbol else None,
                multiplier,
                int(observed_at_ms),
                int(observed_at_ms),
            ),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def upsert_us_equity_symbol(
        self,
        *,
        symbol: str,
        exchange: str | None,
        security_name: str | None,
        instrument_type: str,
        source: str,
        source_updated_at_ms: int,
        raw_payload: dict[str, Any] | None,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        normalized_symbol = _symbol(symbol)
        market_instrument_id = f"market_instrument:us_equity:{normalized_symbol}"
        cursor = self.conn.execute(
            """
            INSERT INTO us_equity_symbols(
              symbol, market_instrument_id, exchange, security_name, instrument_type, status, source,
              source_updated_at_ms, raw_payload_json, created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s)
            ON CONFLICT(symbol) DO UPDATE SET
              market_instrument_id = excluded.market_instrument_id,
              exchange = excluded.exchange,
              security_name = excluded.security_name,
              instrument_type = excluded.instrument_type,
              status = 'active',
              source = excluded.source,
              source_updated_at_ms = excluded.source_updated_at_ms,
              raw_payload_json = excluded.raw_payload_json,
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (
                normalized_symbol,
                market_instrument_id,
                _optional_text(exchange),
                _optional_text(security_name),
                str(instrument_type or "equity").strip().lower() or "equity",
                source,
                int(source_updated_at_ms),
                Jsonb(raw_payload or {}),
                int(observed_at_ms),
                int(observed_at_ms),
            ),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def find_us_equity_symbol(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM us_equity_symbols
            WHERE symbol = %s AND status = 'active'
            """,
            (_symbol(symbol),),
        ).fetchone()
        return dict(row) if row else None

    def chain_token_market_target(self, asset_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT chain_id, address
            FROM registry_assets
            WHERE asset_id = %s
            """,
            (asset_id,),
        ).fetchone()
        if not row or not row.get("chain_id") or not row.get("address"):
            return None
        chain_id = str(row["chain_id"])
        address = str(row["address"])
        return {
            "target_type": "chain_token",
            "target_id": f"{chain_id}:{address}",
        }

    def cex_pricefeed_for_token(
        self,
        *,
        cex_token_id: str,
        pricefeed_id: str | None = None,
    ) -> dict[str, Any] | None:
        if pricefeed_id:
            row = self.conn.execute(
                """
                SELECT *
                FROM price_feeds
                WHERE pricefeed_id = %s
                  AND subject_type = 'CexToken'
                  AND subject_id = %s
                  AND provider = 'binance'
                  AND feed_type = 'cex_swap'
                  AND quote_symbol = 'USDT'
                  AND status = 'canonical'
                """,
                (pricefeed_id, cex_token_id),
            ).fetchone()
            if row:
                return dict(row)
        row = self.conn.execute(
            """
            SELECT *
            FROM price_feeds
            WHERE subject_type = 'CexToken'
              AND subject_id = %s
              AND provider = 'binance'
              AND feed_type = 'cex_swap'
              AND quote_symbol = 'USDT'
              AND status = 'canonical'
            ORDER BY
              updated_at_ms DESC,
              native_market_id ASC
            LIMIT 1
            """,
            (cex_token_id,),
        ).fetchone()
        return dict(row) if row else None

    def deactivate_missing_us_equity_symbols(
        self,
        *,
        source: str,
        active_symbols: set[str],
        observed_at_ms: int,
    ) -> int:
        normalized_symbols = sorted({_symbol(symbol) for symbol in active_symbols if _symbol(symbol)})
        if normalized_symbols:
            cursor = self.conn.execute(
                """
                UPDATE us_equity_symbols
                SET status = 'inactive', updated_at_ms = %s
                WHERE source = %s
                  AND status = 'active'
                  AND NOT (symbol = ANY(%s))
                RETURNING symbol
                """,
                (int(observed_at_ms), source, normalized_symbols),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE us_equity_symbols
                SET status = 'inactive', updated_at_ms = %s
                WHERE source = %s
                  AND status = 'active'
                RETURNING symbol
                """,
                (int(observed_at_ms), source),
            )
        rows = cursor.fetchall()
        return expect_mutation_count(cursor, expected=len(rows), error_code="registry_repository_rowcount_invalid")

    def find_cex_token(self, base_symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM cex_tokens
            WHERE base_symbol = %s AND status IN ('candidate', 'canonical')
            """,
            (_symbol(base_symbol),),
        ).fetchone()
        return dict(row) if row else None

    def find_assets_by_address(self, *, chain_id: str | None, address: str) -> list[dict[str, Any]]:
        normalized_chain = canonical_chain_id(chain_id) if chain_id else None
        normalized_address = canonical_chain_address(normalized_chain, address)
        clauses = ["address = %s", "status IN ('candidate', 'canonical')"]
        params: list[Any] = [normalized_address]
        if chain_id:
            clauses.append("chain_id = %s")
            params.append(normalized_chain)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM registry_assets
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at_ms DESC, asset_id
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def find_assets_by_symbol_with_identity_metadata(self, symbol: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              registry_assets.*,
              asset_identity_current.canonical_symbol AS symbol,
              asset_identity_current.canonical_name AS name,
              asset_identity_current.decimals,
              asset_identity_current.identity_confidence,
              identity_metadata.raw_payload_json AS identity_raw_payload_json,
              identity_metadata.observed_at_ms AS observed_at_ms,
              identity_metadata.observed_at_ms AS identity_metadata_observed_at_ms,
              identity_metadata.provider AS identity_metadata_provider
            FROM registry_assets
            LEFT JOIN LATERAL (
              SELECT raw_payload_json, observed_at_ms, provider
              FROM asset_identity_evidence
              WHERE asset_identity_evidence.asset_id = registry_assets.asset_id
                AND asset_identity_evidence.provider = 'okx'
                AND asset_identity_evidence.lookup_mode IN ('symbol_search', 'exact_address')
              ORDER BY observed_at_ms DESC, evidence_id DESC
              LIMIT 1
            ) identity_metadata ON true
            JOIN asset_identity_current
              ON asset_identity_current.asset_id = registry_assets.asset_id
            WHERE asset_identity_current.canonical_symbol = %s
              AND registry_assets.status IN ('candidate', 'canonical')
            ORDER BY registry_assets.asset_id
            """,
            (_symbol(symbol),),
        ).fetchall()
        assets = [_with_identity_metadata(dict(row)) for row in rows]
        return sorted(assets, key=_identity_metadata_sort_key)

    def ranked_market_targets(
        self,
        *,
        since_ms: int,
        target_types: tuple[str, ...],
        limit: int,
        exclude_keys: tuple[tuple[str, str], ...] = (),
    ) -> list[dict[str, Any]]:
        parsed_limit = require_nonnegative_int(limit, error_code="registry_ranked_market_targets_limit_required")
        parsed_target_types = tuple(dict.fromkeys(str(value) for value in target_types))
        if not parsed_target_types or set(parsed_target_types) - {"chain_token", "cex_symbol"}:
            raise ValueError("registry_ranked_market_targets_target_types_required")
        excluded_target_types = [str(target_type) for target_type, _ in exclude_keys]
        excluded_target_ids = [str(target_id) for _, target_id in exclude_keys]
        rows = self.conn.execute(
            """
            WITH active_targets AS MATERIALIZED (
              SELECT DISTINCT ON (resolution.target_type, resolution.target_id)
                resolution.target_type,
                resolution.target_id,
                event.received_at_ms AS computed_at_ms,
                event.received_at_ms::double precision AS score
              FROM token_intent_resolutions resolution
              JOIN token_intents intent ON intent.intent_id = resolution.intent_id
              JOIN events event ON event.event_id = intent.event_id
              WHERE resolution.is_current = true
                AND resolution.resolution_status IN ('EXACT', 'UNIQUE_BY_CONTEXT')
                AND resolution.target_type IN ('Asset', 'CexToken')
                AND resolution.target_id IS NOT NULL
                AND event.received_at_ms >= %s
              ORDER BY
                resolution.target_type,
                resolution.target_id,
                event.received_at_ms DESC,
                event.event_id ASC
            ),
            live_targets AS (
              SELECT
                'chain_token' AS target_type,
                registry_assets.chain_id || ':' || registry_assets.address AS target_id,
                registry_assets.chain_id,
                registry_assets.address,
                NULL::text AS native_market_id,
                NULL::text AS quote_symbol,
                'okx' AS provider,
                NULL::text AS pricefeed_id,
                active_targets.computed_at_ms,
                active_targets.score
              FROM active_targets
              JOIN registry_assets ON registry_assets.asset_id = active_targets.target_id
              WHERE active_targets.target_type = 'Asset'
                AND registry_assets.status IN ('candidate', 'canonical')
                AND registry_assets.chain_id IS NOT NULL
                AND registry_assets.address IS NOT NULL
              UNION ALL
              SELECT
                'cex_symbol' AS target_type,
                preferred_pricefeed.provider || ':' || preferred_pricefeed.native_market_id
                  AS target_id,
                NULL::text AS chain_id,
                NULL::text AS address,
                preferred_pricefeed.native_market_id AS native_market_id,
                preferred_pricefeed.quote_symbol AS quote_symbol,
                preferred_pricefeed.provider AS provider,
                preferred_pricefeed.pricefeed_id AS pricefeed_id,
                active_targets.computed_at_ms,
                active_targets.score
              FROM active_targets
              JOIN cex_tokens ON cex_tokens.cex_token_id = active_targets.target_id
              LEFT JOIN LATERAL (
                SELECT pricefeed_id, provider, native_market_id, quote_symbol
                FROM price_feeds
                WHERE price_feeds.subject_type = 'CexToken'
                  AND price_feeds.subject_id = active_targets.target_id
                  AND price_feeds.provider = 'binance'
                  AND price_feeds.feed_type = 'cex_swap'
                  AND price_feeds.quote_symbol = 'USDT'
                  AND price_feeds.status = 'canonical'
                ORDER BY
                  price_feeds.updated_at_ms DESC,
                  price_feeds.native_market_id ASC
                LIMIT 1
              ) preferred_pricefeed ON true
              WHERE active_targets.target_type = 'CexToken'
                AND cex_tokens.status IN ('candidate', 'canonical')
            )
            SELECT
              target_type,
              target_id,
              chain_id,
              address,
              native_market_id,
              quote_symbol,
              provider,
              pricefeed_id
            FROM live_targets
            WHERE target_type = ANY(%s::text[])
              AND (target_type = 'chain_token' OR native_market_id IS NOT NULL)
              AND NOT EXISTS (
                SELECT 1
                FROM unnest(%s::text[], %s::text[]) AS excluded(target_type, target_id)
                WHERE excluded.target_type = live_targets.target_type
                  AND excluded.target_id = live_targets.target_id
              )
            ORDER BY score DESC NULLS LAST, computed_at_ms DESC, target_type, target_id
            LIMIT %s
            """,
            (
                int(since_ms),
                list(parsed_target_types),
                excluded_target_types,
                excluded_target_ids,
                parsed_limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def product_targets_for_market_targets(
        self,
        targets: list[tuple[str, str]],
    ) -> dict[tuple[str, str], tuple[str, str]]:
        requested = list(dict.fromkeys((str(target_type), str(target_id)) for target_type, target_id in targets))
        if not requested:
            return {}
        if any(
            target_type not in {"chain_token", "cex_symbol"} or not target_id for target_type, target_id in requested
        ):
            raise ValueError("registry_market_target_identity_required")
        rows = self.conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY
                AS requested(market_target_type, market_target_id, ordinality)
            ),
            resolved AS (
              SELECT
                requested.market_target_type,
                requested.market_target_id,
                'Asset'::text AS product_target_type,
                registry_assets.asset_id AS product_target_id,
                requested.ordinality,
                registry_assets.updated_at_ms
              FROM requested
              JOIN registry_assets
                ON requested.market_target_type = 'chain_token'
               AND requested.market_target_id = registry_assets.chain_id || ':' || registry_assets.address
               AND registry_assets.status IN ('candidate', 'canonical')
              UNION ALL
              SELECT
                requested.market_target_type,
                requested.market_target_id,
                'CexToken'::text AS product_target_type,
                price_feeds.subject_id AS product_target_id,
                requested.ordinality,
                price_feeds.updated_at_ms
              FROM requested
              JOIN price_feeds
                ON requested.market_target_type = 'cex_symbol'
               AND requested.market_target_id = price_feeds.provider || ':' || price_feeds.native_market_id
               AND price_feeds.subject_type = 'CexToken'
               AND price_feeds.provider = 'binance'
               AND price_feeds.feed_type = 'cex_swap'
               AND price_feeds.quote_symbol = 'USDT'
               AND price_feeds.status = 'canonical'
            )
            SELECT DISTINCT ON (market_target_type, market_target_id)
              market_target_type,
              market_target_id,
              product_target_type,
              product_target_id
            FROM resolved
            ORDER BY
              market_target_type,
              market_target_id,
              updated_at_ms DESC,
              product_target_id
            """,
            (
                [target_type for target_type, _target_id in requested],
                [target_id for _target_type, target_id in requested],
            ),
        ).fetchall()
        return {
            (str(row["market_target_type"]), str(row["market_target_id"])): (
                str(row["product_target_type"]),
                str(row["product_target_id"]),
            )
            for row in rows
        }

    def find_cex_pricefeed(self, *, exchange: str, native_market_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM price_feeds
            WHERE provider = 'binance'
              AND %s = 'binance'
              AND native_market_id = %s
              AND feed_type = 'cex_swap'
              AND quote_symbol = 'USDT'
              AND status = 'canonical'
            ORDER BY updated_at_ms DESC
            LIMIT 1
            """,
            (exchange.strip().lower(), native_market_id.strip().upper()),
        ).fetchone()
        return dict(row) if row else None

    def binance_usdt_perp_sync_plan_counts(
        self,
        *,
        base_symbols: list[str],
        native_market_ids: list[str],
    ) -> dict[str, int]:
        normalized_symbols = sorted({_symbol(symbol) for symbol in base_symbols if _symbol(symbol)})
        normalized_token_ids = [f"cex_token:{symbol}" for symbol in normalized_symbols]
        normalized_market_ids = sorted({str(market_id or "").strip().upper() for market_id in native_market_ids})
        row = self.conn.execute(
            """
            SELECT
              (
                SELECT COUNT(*)::bigint
                FROM unnest(%s::text[]) AS route_token(cex_token_id)
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM cex_tokens
                  WHERE cex_tokens.cex_token_id = route_token.cex_token_id
                )
              ) AS cex_tokens_to_insert,
              (
                SELECT COUNT(*)::bigint
                FROM cex_tokens
                WHERE NOT (cex_tokens.cex_token_id = ANY(%s::text[]))
              ) AS cex_tokens_to_delete,
              (
                SELECT COUNT(*)::bigint
                FROM unnest(%s::text[]) AS route_market(native_market_id)
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM price_feeds
                  WHERE price_feeds.provider = 'binance'
                    AND price_feeds.feed_type = 'cex_swap'
                    AND price_feeds.quote_symbol = 'USDT'
                    AND price_feeds.status = 'canonical'
                    AND price_feeds.native_market_id = route_market.native_market_id
                )
              ) AS pricefeeds_to_insert,
              (
                (
                  SELECT COUNT(*)::bigint
                  FROM price_feeds
                  WHERE provider = 'okx'
                    AND left(feed_type, 4) = 'cex_'
                )
                + (
                  SELECT COUNT(*)::bigint
                  FROM market_ticks
                  WHERE target_type = 'cex_symbol'
                    AND (target_id LIKE 'okx:%%' OR source_provider = 'okx_cex_rest')
                )
              ) AS old_okx_cex_rows_to_delete
            """,
            (normalized_token_ids, normalized_token_ids, normalized_market_ids),
        ).fetchone()
        return {
            "cex_tokens_to_insert": int(row["cex_tokens_to_insert"] or 0) if row else 0,
            "cex_tokens_to_delete": int(row["cex_tokens_to_delete"] or 0) if row else 0,
            "pricefeeds_to_insert": int(row["pricefeeds_to_insert"] or 0) if row else 0,
            "old_okx_cex_rows_to_delete": int(row["old_okx_cex_rows_to_delete"] or 0) if row else 0,
        }

    def find_preferred_cex_pricefeed(self, base_symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM price_feeds
            WHERE subject_type = 'CexToken'
              AND base_symbol = %s
              AND provider = 'binance'
              AND feed_type = 'cex_swap'
              AND quote_symbol = 'USDT'
              AND status = 'canonical'
            ORDER BY
              updated_at_ms DESC,
              native_market_id ASC
            LIMIT 1
            """,
            (_symbol(base_symbol),),
        ).fetchone()
        return dict(row) if row else None


def _required_returning_row(cursor: Any, row: Any) -> dict[str, Any]:
    returning_mutation_count(cursor, row, error_code="registry_repository_rowcount_invalid")
    if row is None:
        raise TypeError("registry_repository_rowcount_invalid")
    return dict(row)


def _pricefeed_id(
    *,
    feed_type: str,
    provider: str,
    native_market_id: str | None,
    chain_id: str | None,
    address: str | None,
) -> str:
    if native_market_id:
        market_type = feed_type.removeprefix("cex_")
        return f"pricefeed:cex:{provider}:{market_type}:{native_market_id}"
    if chain_id and address:
        return f"pricefeed:dex-token:{provider}:{chain_id}:{address}"
    return (
        "pricefeed:"
        + hashlib.sha256(
            "|".join([feed_type, provider, native_market_id or "", chain_id or "", address or ""]).encode("utf-8")
        ).hexdigest()
    )


def _symbol(value: str | None) -> str:
    return str(value or "").strip().lstrip("$").upper()


def _optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _with_identity_metadata(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.pop("identity_raw_payload_json", None) or {}
    observed_at_ms = row.get("identity_metadata_observed_at_ms")
    provider = row.get("identity_metadata_provider")
    market_cap_usd = _metadata_number(
        payload,
        "market_cap_usd",
        "marketCapUsd",
        "marketCapUSD",
        "marketCap",
        "market_cap",
        "mcap",
    )
    liquidity_usd = _metadata_number(
        payload,
        "liquidity_usd",
        "liquidityUsd",
        "liquidityUSD",
        "liquidity",
    )
    holders = _metadata_int(payload, "holders", "holderCount", "holder_count")
    provider_rank = _metadata_int(payload, "provider_rank")
    return {
        **row,
        "price_usd": _metadata_number(payload, "price_usd", "priceUsd", "priceUSD", "price"),
        "price_observed_at_ms": observed_at_ms,
        "price_provider": provider,
        "market_cap_usd": market_cap_usd,
        "market_cap_observed_at_ms": observed_at_ms if market_cap_usd is not None else None,
        "market_cap_provider": provider if market_cap_usd is not None else None,
        "liquidity_usd": liquidity_usd,
        "liquidity_observed_at_ms": observed_at_ms if liquidity_usd is not None else None,
        "liquidity_provider": provider if liquidity_usd is not None else None,
        "volume_24h_usd": _metadata_number(payload, "volume_24h_usd", "volume24hUsd", "volume24hUSD", "volume24h"),
        "holders": holders,
        "holders_observed_at_ms": observed_at_ms if holders is not None else None,
        "holders_provider": provider if holders is not None else None,
        "provider_rank": provider_rank,
        "provider_rank_observed_at_ms": observed_at_ms if provider_rank is not None else None,
        "market_cap_status": _identity_metadata_status(market_cap_usd, observed_at_ms),
        "liquidity_status": _identity_metadata_status(liquidity_usd, observed_at_ms),
        "holders_status": _identity_metadata_status(holders, observed_at_ms),
    }


def _identity_metadata_status(value: Any, observed_at_ms: Any) -> str:
    return "fresh" if value is not None and observed_at_ms is not None else "missing"


def _identity_metadata_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    return (-_metadata_float(row.get("market_cap_usd")), str(row.get("asset_id") or ""))


def _metadata_number(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _metadata_int(payload: dict[str, Any], *keys: str) -> int | None:
    value = _metadata_number(payload, *keys)
    return int(value) if value is not None else None


def _metadata_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
