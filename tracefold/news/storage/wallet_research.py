"""Wallet research reads: stable persisted segments, full-scope totals and priced evidence."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from ..wallet_contracts import VERIFIED_WALLET_PRICE_SOURCE, WALLET_OUTCOME_HORIZONS

# Collapse before paging. Cumulative snapshots are never summed within a segment.
_SCOPE: Final = """
WITH scoped AS MATERIALIZED (
 SELECT e.item_id, e.chain_id, e.kind, e.handle, e.wallet, e.token, e.token_symbol,
        e.segment_key, e.tone, e.ratio_bps, e.basis, e.closed, e.peer_wallets, e.premium_bps,
        e.event_at_ms, e.window_from_ms, e.window_to_ms, e.usd, e.position_usd, e.entry_price,
        e.mark_price, e.evidence, i.market_notify_delivery_key AS delivery_key,
        CASE WHEN e.kind = 'buy' AND nullif(e.segment_key, '') IS NOT NULL
             THEN jsonb_build_array(e.chain_id, e.wallet, e.token, e.segment_key)::text
             ELSE e.item_id END AS research_id
 FROM news_market_wallet_events e
 LEFT JOIN news_items i ON i.item_id = e.item_id
 WHERE e.event_at_ms >= %(from_ms)s AND e.event_at_ms < %(to_ms)s
   AND (%(kind)s::text IS NULL OR e.kind = %(kind)s)
   AND (%(chain_id)s::bigint IS NULL OR e.chain_id = %(chain_id)s)
   AND (%(wallet_address)s::text IS NULL OR e.wallet = %(wallet_address)s)
   AND (%(token_address)s::text IS NULL OR e.token = %(token_address)s)
   AND (%(segment_key)s::text IS NULL OR e.segment_key = %(segment_key)s)
), ranked AS (
 SELECT scoped.*,
        row_number() OVER (PARTITION BY research_id ORDER BY event_at_ms DESC, item_id DESC) AS rank,
        count(*) OVER (PARTITION BY research_id) AS observation_count,
        min(event_at_ms) OVER (PARTITION BY research_id) AS first_event_at_ms
 FROM scoped
), selected AS (
 SELECT * FROM ranked WHERE %(view)s = 'observations' OR rank = 1
)
"""
WALLET_RESEARCH_SQL: Final = (
    _SCOPE  # noqa: S608 -- fixed code-owned CTE; all user input is bound
    + """
SELECT c.*, d.state AS delivery_state, d.settled_at_ms,
       (SELECT jsonb_agg(jsonb_build_object(
          'horizon', o.horizon, 'price', o.price::text, 'source', o.source,
          'sampled_at_ms', o.at_ms, 'reference_price', o.reference_price::text,
          'reference_at_ms', o.reference_at_ms, 'target_at_ms', o.target_at_ms))
          FROM news_market_wallet_outcomes o WHERE o.item_id = c.item_id) AS receipts
FROM selected c LEFT JOIN news_market_deliveries d ON d.delivery_key = c.delivery_key
WHERE (%(cursor_at_ms)s::bigint IS NULL OR (c.event_at_ms, c.item_id) < (%(cursor_at_ms)s, %(cursor_id)s))
ORDER BY c.event_at_ms DESC, c.item_id DESC LIMIT %(limit)s
"""
)
WALLET_RESEARCH_TOTALS_SQL: Final = (
    _SCOPE
    + """
SELECT count(*) AS segments,
       coalesce(sum(observation_count), 0)::bigint AS observations,
       count(DISTINCT (chain_id, wallet)) FILTER (WHERE wallet != '') AS wallets,
       count(DISTINCT (chain_id, token)) FILTER (WHERE token != '') AS tokens,
       coalesce(sum(usd) FILTER (WHERE kind = 'buy'), 0)::text AS priced_buy_usd
FROM ranked WHERE rank = 1
"""
)


def research_params(**values: Any) -> dict[str, Any]:
    return {
        "kind": None,
        "chain_id": None,
        "wallet_address": None,
        "token_address": None,
        "segment_key": None,
        "view": "segments",
        "cursor_at_ms": None,
        "cursor_id": "",
        **values,
    }


def wallet_research_card(row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    evidence = row["evidence"] or {}
    direct = (
        "item_id",
        "chain_id",
        "kind",
        "handle",
        "wallet",
        "token",
        "token_symbol",
        "segment_key",
        "research_id",
        "observation_count",
        "first_event_at_ms",
        "tone",
        "ratio_bps",
        "basis",
        "closed",
        "peer_wallets",
        "premium_bps",
        "event_at_ms",
        "window_from_ms",
        "window_to_ms",
        "delivery_key",
        "delivery_state",
        "settled_at_ms",
    )
    result = {key: row[key] for key in direct}
    for key in ("usd", "position_usd", "entry_price", "mark_price"):
        result[key] = None if row[key] is None else str(row[key])
    for key in (
        "stage",
        "selection_reason",
        "buy_count",
        "unpriced_buys",
        "observed_at_ms",
        "history_from_ms",
        "price_reference",
    ):
        result[key] = evidence.get(key)
    result["mark_source"] = evidence.get("mark_source")
    result["price_status"] = (
        "missing" if row["mark_price"] is None else "verified" if _verified_reference(row) else "identity_unverified"
    )
    result["price_quote"] = "USD"
    result["price_unit"] = "token"
    result["price_source_at_ms"] = evidence.get("price_source_at_ms")
    receipts = {receipt["horizon"]: receipt for receipt in (row["receipts"] or [])}
    result["outcomes"] = (
        [_outcome(row, receipts.get(horizon), horizon, delay, now_ms) for horizon, delay in WALLET_OUTCOME_HORIZONS]
        if row["kind"] != "digest"
        else []
    )
    result["digest_lines"] = (
        [str(line["text"]) for line in evidence.get("lines", [])] if row["kind"] == "digest" else None
    )
    result["digest_model_used"] = bool(evidence.get("model_used")) if row["kind"] == "digest" else None
    return result


def _verified_reference(row: dict[str, Any]) -> bool:
    e = row["evidence"] or {}
    return (
        e.get("mark_source") == VERIFIED_WALLET_PRICE_SOURCE
        and e.get("price_chain_id") == row["chain_id"]
        and e.get("price_token") == row["token"]
        and e.get("price_quote") == "USD"
        and e.get("price_unit") == "token"
    )


def _outcome(
    row: dict[str, Any], receipt: dict[str, Any] | None, horizon: str, delay: int, now_ms: int
) -> dict[str, Any]:
    observed = (row["evidence"] or {}).get("observed_at_ms")
    reference = None if row["mark_price"] is None else str(row["mark_price"])
    result = {
        "horizon": horizon,
        "source": None,
        "price": None,
        "sampled_at_ms": None,
        "reference_price": reference,
        "reference_at_ms": observed,
        "target_at_ms": None if observed is None else int(observed) + delay,
        "return_bps": None,
    }
    if receipt is None:
        result["status"] = (
            "not_scheduled" if observed is None else "not_due" if int(observed) + delay > now_ms else "pending"
        )
        return result
    result.update(receipt)
    if receipt["price"] is None or receipt["source"] == "unavailable":
        result["status"] = "unavailable"
    elif not receipt["reference_price"] or Decimal(receipt["reference_price"]) <= 0:
        result["status"] = "missing_reference"
    elif not _verified_reference(row) or receipt["source"] != VERIFIED_WALLET_PRICE_SOURCE:
        result["status"] = "identity_unverified"
    else:
        result["status"] = "measured"
        change = (Decimal(receipt["price"]) / Decimal(receipt["reference_price"]) - 1) * 10000
        result["return_bps"] = max(-10000000, min(10000000, int(change.to_integral_value(rounding=ROUND_HALF_UP))))
    return result
