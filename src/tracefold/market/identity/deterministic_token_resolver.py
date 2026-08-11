from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tracefold.market.identity.resolver_policy import TOKEN_RESOLVER_POLICY_VERSION
from tracefold.market.identity.token_fact_inputs import DeterministicResolution, MentionKeys

RESOLVER_POLICY_VERSION = TOKEN_RESOLVER_POLICY_VERSION
MIN_DOMINANT_MARKET_CAP_USD = Decimal("250000")
MIN_DOMINANT_HOLDERS = Decimal("1000")
MIN_DOMINANT_LIQUIDITY_USD = Decimal("100000")
RESOLUTION_MARKET_FRESH_MS = 24 * 60 * 60 * 1000
MAX_AUDIT_CANDIDATE_IDS = 20
ADDRESS_CHAIN_PRIORITY = (
    "solana",
    "eip155:1",
    "eip155:8453",
    "eip155:56",
    "robinhood",
    "eip155:42161",
    "eip155:43114",
    "ton",
)
ADDRESS_CHAIN_PRIORITY_INDEX = {chain_id: index for index, chain_id in enumerate(ADDRESS_CHAIN_PRIORITY)}

__all__ = [
    "RESOLVER_POLICY_VERSION",
    "DeterministicResolution",
    "DeterministicTokenResolver",
    "MentionKeys",
]


class DeterministicTokenResolver:
    def __init__(self, *, registry: Any):
        self.registry = registry

    def resolve(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: MentionKeys,
        decision_time_ms: int,
    ) -> DeterministicResolution:
        lookup_keys = _lookup_keys(keys)
        if keys.cex_pricefeed_id and keys.exchange:
            return self._resolve_cex_pricefeed(
                intent_id=intent_id,
                event_id=event_id,
                keys=keys,
                lookup_keys=lookup_keys,
                decision_time_ms=decision_time_ms,
            )
        if keys.address and keys.chain_id:
            return self._resolve_chain_address(
                intent_id=intent_id,
                event_id=event_id,
                keys=keys,
                lookup_keys=lookup_keys,
                decision_time_ms=decision_time_ms,
            )
        if keys.address:
            return self._resolve_address_without_chain(
                intent_id=intent_id,
                event_id=event_id,
                keys=keys,
                lookup_keys=lookup_keys,
                decision_time_ms=decision_time_ms,
            )
        if keys.symbol:
            return self._resolve_symbol(
                intent_id=intent_id,
                event_id=event_id,
                keys=keys,
                lookup_keys=lookup_keys,
                decision_time_ms=decision_time_ms,
            )
        return _resolution(
            intent_id=intent_id,
            event_id=event_id,
            status="NIL",
            target_type=None,
            target_id=None,
            reason_codes=["NO_RESOLVABLE_MENTION"],
            lookup_keys=lookup_keys,
            decision_time_ms=decision_time_ms,
        )

    def _resolve_cex_pricefeed(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: MentionKeys,
        lookup_keys: list[str],
        decision_time_ms: int,
    ) -> DeterministicResolution:
        if str(keys.exchange or "").strip().lower() != "binance":
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="NIL",
                target_type=None,
                target_id=None,
                reason_codes=["CEX_EXCHANGE_NOT_SUPPORTED"],
                lookup_keys=lookup_keys,
                decision_time_ms=decision_time_ms,
            )
        row = self.registry.find_cex_pricefeed(
            exchange=str(keys.exchange),
            native_market_id=str(keys.cex_pricefeed_id),
        )
        if row:
            target_type = str(row["subject_type"])
            target_id = str(row["subject_id"])
            pricefeed_id = str(row["pricefeed_id"])
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="EXACT",
                target_type=target_type,
                target_id=target_id,
                pricefeed_id=pricefeed_id,
                reason_codes=["CEX_NATIVE_PRICEFEED_EXACT"],
                lookup_keys=lookup_keys,
                candidate_ids=[pricefeed_id, target_id],
                decision_time_ms=decision_time_ms,
            )
        return _resolution(
            intent_id=intent_id,
            event_id=event_id,
            status="NIL",
            target_type=None,
            target_id=None,
            reason_codes=["CEX_PRICEFEED_NOT_IN_REGISTRY"],
            lookup_keys=lookup_keys,
            decision_time_ms=decision_time_ms,
        )

    def _resolve_chain_address(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: MentionKeys,
        lookup_keys: list[str],
        decision_time_ms: int,
    ) -> DeterministicResolution:
        rows = self.registry.find_assets_by_address(chain_id=keys.chain_id, address=keys.address)
        if len(rows) == 1:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="EXACT",
                target_type="Asset",
                target_id=str(rows[0]["asset_id"]),
                reason_codes=["CHAIN_ADDRESS_EXACT"],
                lookup_keys=lookup_keys,
                candidate_ids=[str(rows[0]["asset_id"])],
                decision_time_ms=decision_time_ms,
            )
        return _resolution(
            intent_id=intent_id,
            event_id=event_id,
            status="NIL",
            target_type=None,
            target_id=None,
            reason_codes=["ADDRESS_NOT_IN_REGISTRY"],
            lookup_keys=lookup_keys,
            decision_time_ms=decision_time_ms,
        )

    def _resolve_address_without_chain(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: MentionKeys,
        lookup_keys: list[str],
        decision_time_ms: int,
    ) -> DeterministicResolution:
        rows = self.registry.find_assets_by_address(chain_id=None, address=keys.address)
        rows = _rank_address_candidates([row for row in rows if row.get("asset_id")])
        candidate_ids = [str(row["asset_id"]) for row in rows]
        if len(candidate_ids) == 1:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="UNIQUE_BY_CONTEXT",
                target_type="Asset",
                target_id=candidate_ids[0],
                reason_codes=["ADDRESS_UNIQUE_ACROSS_TRACKED_CHAINS"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        if candidate_ids:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="UNIQUE_BY_CONTEXT",
                target_type="Asset",
                target_id=candidate_ids[0],
                reason_codes=["RESOLVED_BY_CHAIN_PRIORITY"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        return _resolution(
            intent_id=intent_id,
            event_id=event_id,
            status="NIL",
            target_type=None,
            target_id=None,
            reason_codes=["ADDRESS_NOT_IN_REGISTRY"],
            lookup_keys=lookup_keys,
            decision_time_ms=decision_time_ms,
        )

    def _resolve_symbol(
        self,
        *,
        intent_id: str,
        event_id: str,
        keys: MentionKeys,
        lookup_keys: list[str],
        decision_time_ms: int,
    ) -> DeterministicResolution:
        symbol = _normalize_symbol(keys.symbol)
        cex_token = self.registry.find_cex_token(symbol)
        if cex_token:
            target_id = str(cex_token["cex_token_id"])
            pricefeed = self.registry.find_preferred_cex_pricefeed(symbol)
            if pricefeed:
                pricefeed_id = str(pricefeed["pricefeed_id"])
                return _resolution(
                    intent_id=intent_id,
                    event_id=event_id,
                    status="UNIQUE_BY_CONTEXT",
                    target_type="CexToken",
                    target_id=target_id,
                    pricefeed_id=pricefeed_id,
                    reason_codes=["CONFIRMED_CEX_TOKEN"],
                    lookup_keys=lookup_keys,
                    candidate_ids=[pricefeed_id, target_id],
                    decision_time_ms=decision_time_ms,
                )
        us_equity = self.registry.find_us_equity_symbol(symbol)
        if us_equity:
            target_id = str(us_equity["market_instrument_id"])
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="NON_CRYPTO",
                target_type="MarketInstrument",
                target_id=target_id,
                reason_codes=["CONFIRMED_US_EQUITY"],
                lookup_keys=[],
                candidate_ids=[target_id],
                decision_time_ms=decision_time_ms,
            )
        assets = self.registry.find_assets_by_symbol_with_identity_metadata(symbol)
        assets = [row for row in assets if str(row.get("asset_id") or "")]
        assets = [{**row, "decision_time_ms": decision_time_ms} for row in assets]
        candidate_ids = _candidate_ids(assets)
        if len(assets) == 1:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="UNIQUE_BY_CONTEXT",
                target_type="Asset",
                target_id=str(assets[0]["asset_id"]),
                reason_codes=["SINGLE_ACTIVE_CHAIN_ASSET"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        dominant = _market_dominant_asset(assets)
        if dominant is not None:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="UNIQUE_BY_CONTEXT",
                target_type="Asset",
                target_id=str(dominant["asset_id"]),
                reason_codes=["MARKET_DOMINANT_CHAIN_ASSET"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        provider_ranked = _provider_rank_asset(assets)
        if provider_ranked is not None:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="UNIQUE_BY_CONTEXT",
                target_type="Asset",
                target_id=str(provider_ranked["asset_id"]),
                reason_codes=["RESOLVED_BY_PROVIDER_RANK"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        if candidate_ids:
            return _resolution(
                intent_id=intent_id,
                event_id=event_id,
                status="AMBIGUOUS",
                target_type=None,
                target_id=None,
                reason_codes=["NO_MARKET_DOMINANT_CHAIN_ASSET"],
                lookup_keys=lookup_keys,
                candidate_ids=candidate_ids,
                decision_time_ms=decision_time_ms,
            )
        return _resolution(
            intent_id=intent_id,
            event_id=event_id,
            status="NIL",
            target_type=None,
            target_id=None,
            reason_codes=["SYMBOL_NOT_IN_REGISTRY"],
            lookup_keys=lookup_keys,
            decision_time_ms=decision_time_ms,
        )


def _market_dominant_asset(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [row for row in rows if _dominance_eligible(row)]
    if not eligible:
        return None
    ranked = sorted(eligible, key=_dominance_score, reverse=True)
    top = ranked[0]
    top_score = _dominance_score(top)
    second_score = _dominance_score(ranked[1]) if len(ranked) > 1 else Decimal("-1")
    if len(ranked) > 1 and top_score <= second_score:
        return None
    if (
        _decimal(top.get("market_cap_usd")) < MIN_DOMINANT_MARKET_CAP_USD
        and _decimal(top.get("holders")) < MIN_DOMINANT_HOLDERS
        and _decimal(top.get("liquidity_usd")) < MIN_DOMINANT_LIQUIDITY_USD
    ):
        return None
    return top


def _candidate_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["asset_id"]) for row in rows[:MAX_AUDIT_CANDIDATE_IDS] if str(row.get("asset_id") or "")]


def _rank_address_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_address_candidate_rank_key)


def _address_candidate_rank_key(row: dict[str, Any]) -> tuple[int, str]:
    chain_id = str(row.get("chain_id") or "")
    return (ADDRESS_CHAIN_PRIORITY_INDEX.get(chain_id, len(ADDRESS_CHAIN_PRIORITY)), str(row.get("asset_id") or ""))


def _dominance_eligible(row: dict[str, Any]) -> bool:
    present = sum(
        1
        for key in ("market_cap_usd", "holders", "liquidity_usd")
        if row.get(key) is not None and _decimal(row.get(key)) > 0
    )
    if present < 1:
        return False
    return _fresh_resolution_market_fields(row) >= 1


def _provider_rank_asset(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = [row for row in rows if _fresh_provider_rank(row)]
    if not ranked:
        return None
    return sorted(ranked, key=lambda row: (int(row["provider_rank"]), str(row.get("asset_id") or "")))[0]


def _fresh_provider_rank(row: dict[str, Any]) -> bool:
    rank_value = row.get("provider_rank")
    observed_value = row.get("provider_rank_observed_at_ms")
    if rank_value is None or observed_value is None:
        return False
    try:
        rank = int(rank_value)
        observed_at_ms = int(observed_value)
        decision_time_ms = int(row.get("decision_time_ms") or 0)
    except (TypeError, ValueError):
        return False
    if rank < 0:
        return False
    return max(0, decision_time_ms - observed_at_ms) <= RESOLUTION_MARKET_FRESH_MS


def _fresh_resolution_market_fields(row: dict[str, Any]) -> int:
    count = 0
    for value_key, observed_key in (
        ("market_cap_usd", "market_cap_observed_at_ms"),
        ("liquidity_usd", "liquidity_observed_at_ms"),
        ("holders", "holders_observed_at_ms"),
    ):
        if row.get(value_key) is None or row.get(observed_key) is None:
            continue
        try:
            age_ms = max(0, int(row.get("decision_time_ms") or 0) - int(row[observed_key]))
        except (TypeError, ValueError):
            continue
        if age_ms <= RESOLUTION_MARKET_FRESH_MS:
            count += 1
    return count


def _dominance_score(row: dict[str, Any]) -> Decimal:
    return (
        Decimal("0.55") * _log10(_decimal(row.get("market_cap_usd")) + 1)
        + Decimal("0.30") * _log10(_decimal(row.get("holders")) + 1)
        + Decimal("0.15") * _log10(_decimal(row.get("liquidity_usd")) + 1)
    )


def _log10(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    return value.log10()


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _lookup_keys(keys: MentionKeys) -> list[str]:
    out: list[str] = []
    if keys.symbol:
        symbol = _normalize_symbol(keys.symbol)
        out.extend([f"symbol:{symbol}", f"project_symbol:{symbol}", f"cex_token:{symbol}"])
    if keys.address:
        chain = keys.chain_id or "unknown"
        out.append(f"address:{chain}:{str(keys.address).lower()}")
    if keys.cex_pricefeed_id:
        exchange = keys.exchange or "unknown"
        out.append(f"cex_pricefeed:{exchange}:{keys.cex_pricefeed_id}")
    return out


def _resolution(
    *,
    intent_id: str,
    event_id: str,
    status: str,
    target_type: str | None,
    target_id: str | None,
    reason_codes: list[str],
    lookup_keys: list[str],
    decision_time_ms: int,
    candidate_ids: list[str] | None = None,
    pricefeed_id: str | None = None,
) -> DeterministicResolution:
    return DeterministicResolution(
        intent_id=intent_id,
        event_id=event_id,
        resolution_status=status,
        target_type=target_type,
        target_id=target_id,
        pricefeed_id=pricefeed_id,
        resolver_policy_version=RESOLVER_POLICY_VERSION,
        reason_codes=reason_codes,
        candidate_ids=candidate_ids or [],
        lookup_keys=lookup_keys,
        decision_time_ms=int(decision_time_ms),
        created_at_ms=int(decision_time_ms),
    )


def _normalize_symbol(symbol: str | None) -> str:
    return str(symbol or "").strip().lstrip("$").upper()
