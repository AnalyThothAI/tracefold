from __future__ import annotations

from typing import Any

from tracefold.market.capture.entity_extractor import TextSurface, extract_entities_from_surfaces
from tracefold.market.capture.twitter_event import TokenSnapshot
from tracefold.market.identity.event_rebuild_query import EventRebuildQuery
from tracefold.market.identity.identity_evidence_policy import (
    CONFIDENCE_MENTION_ONLY,
    EVIDENCE_TWEET_CONTRACT_MENTION,
)
from tracefold.market.identity.token_evidence_builder import build_token_evidence
from tracefold.market.identity.token_intent_builder import build_token_intents
from tracefold.market.identity.token_intent_resolver import TokenIntentResolver
from tracefold.market.windows import PRODUCT_WINDOW_MS


def rebuild_recent_token_intents(
    *,
    repos: Any,
    now_ms: int,
    window: str,
    limit: int,
) -> dict[str, Any]:
    since_ms = int(now_ms) - PRODUCT_WINDOW_MS[window]
    rows = EventRebuildQuery(repos.conn).recent_events(since_ms=since_ms, limit=limit)

    rebuilt_events = 0
    intents_written = 0
    resolved_intents = 0
    with repos.transaction():
        for row in rows:
            result = _rebuild_event_token_intents(repos=repos, event_row=row)
            rebuilt_events += 1
            intents_written += result["intents_written"]
            resolved_intents += result["resolved_intents"]
    return {
        "window": window,
        "since_ms": since_ms,
        "events_selected": len(rows),
        "events_rebuilt": rebuilt_events,
        "intents_written": intents_written,
        "resolved_intents": resolved_intents,
    }


def _rebuild_event_token_intents(*, repos: Any, event_row: dict[str, Any]) -> dict[str, int]:
    repos.require_transaction(operation="token_intent_rebuild")
    event_id = str(event_row["event_id"])
    received_at_ms = int(event_row["received_at_ms"])
    repos.token_intents.delete_by_event_id(event_id)
    repos.token_evidence.delete_by_event_id(event_id)

    evidence_inputs = build_token_evidence(
        event_id=event_id,
        entities=extract_entities_from_surfaces(_surfaces(event_row)),
        token_snapshot=_token_snapshot(event_row.get("event_json")),
        created_at_ms=received_at_ms,
    )
    repos.token_evidence.insert_many(evidence_inputs)
    intent_inputs = build_token_intents(
        event_id=event_id,
        evidence=evidence_inputs,
        created_at_ms=received_at_ms,
    )
    repos.token_intents.insert_many(intent_inputs)
    _upsert_chain_intent_registry(repos=repos, intents=intent_inputs, observed_at_ms=received_at_ms)

    resolver = TokenIntentResolver(registry=repos.registry, resolutions=repos.intent_resolutions)
    resolved = 0
    for intent in intent_inputs:
        decision = resolver.resolve(
            intent,
            evidence_inputs,
            decision_time_ms=received_at_ms,
            persist=True,
        )
        repos.token_intent_lookup.replace_lookup_keys(
            intent_id=decision.intent_id,
            event_id=decision.event_id,
            keys=decision.lookup_keys,
            source_evidence_id=intent.primary_evidence_id,
            created_at_ms=received_at_ms,
        )
        if decision.target_type and decision.target_id:
            resolved += 1
    return {"intents_written": len(intent_inputs), "resolved_intents": resolved}


def _surfaces(event_row: dict[str, Any]) -> list[TextSurface]:
    surfaces: list[TextSurface] = []
    text = event_row.get("text")
    if text:
        surfaces.append(TextSurface("primary", str(text)))
    reference_text = _reference_text(event_row.get("reference_json"))
    if reference_text:
        surfaces.append(TextSurface("reference", reference_text))
    return surfaces


def _reference_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("text")
    return str(value) if value else None


def _token_snapshot(event_json: Any) -> TokenSnapshot | None:
    if not isinstance(event_json, dict):
        return None
    payload = event_json.get("token_snapshot")
    if not isinstance(payload, dict):
        return None
    address = str(payload.get("address") or "")
    chain = str(payload.get("chain") or "")
    if not address or not chain:
        return None
    return TokenSnapshot(
        address=address,
        chain=chain,
        symbol=payload.get("symbol"),
        icon_url=payload.get("icon_url"),
        trigger_type=payload.get("trigger_type"),
        raw=payload["raw"] if isinstance(payload.get("raw"), dict) else payload,
    )


def _upsert_chain_intent_registry(*, repos: Any, intents: list[Any], observed_at_ms: int) -> None:
    for intent in intents:
        if not intent.chain_hint or not intent.address_hint:
            continue
        asset = repos.registry.upsert_chain_asset(
            chain_id=str(intent.chain_hint),
            address=str(intent.address_hint),
            observed_at_ms=observed_at_ms,
        )
        repos.identity_evidence.upsert_identity_evidence(
            asset_id=str(asset["asset_id"]),
            evidence_kind=EVIDENCE_TWEET_CONTRACT_MENTION,
            provider="twitter",
            lookup_mode="tweet_mention",
            chain_id=str(asset["chain_id"]),
            address=str(asset["address"]),
            symbol=intent.display_symbol,
            name=None,
            decimals=None,
            confidence=CONFIDENCE_MENTION_ONLY,
            source_intent_id=intent.intent_id,
            observed_at_ms=observed_at_ms,
        )
        repos.identity_evidence.recompute_current_identity(str(asset["asset_id"]), now_ms=observed_at_ms)
