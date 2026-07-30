from __future__ import annotations

from typing import Any

from tracefold.market.identity.token_intent_resolver import (
    TokenIntentResolver,
)
from tracefold.market.radar.constants import WINDOW_MS

TOKEN_REPROCESS_WINDOW = "24h"


def reprocess_recent_token_intents(
    *,
    repos: Any,
    now_ms: int,
    window: str,
    limit: int,
    lookup_keys: list[str] | None = None,
) -> dict[str, Any]:
    with repos.transaction():
        return _reprocess_recent_token_intents(
            repos=repos,
            now_ms=now_ms,
            window=window,
            limit=limit,
            lookup_keys=lookup_keys,
        )


def _reprocess_recent_token_intents(
    *,
    repos: Any,
    now_ms: int,
    window: str,
    limit: int,
    lookup_keys: list[str] | None,
) -> dict[str, Any]:
    repos.require_transaction(operation="token_resolution_refresh")
    since_ms = int(now_ms) - WINDOW_MS[window]
    if lookup_keys:
        intents = repos.token_intent_lookup.recent_intents_for_lookup_keys(
            lookup_keys,
            since_ms=since_ms,
            limit=limit,
        )
    else:
        intents = repos.token_intents.recent_unresolved(since_ms=since_ms, limit=limit)
    resolver = TokenIntentResolver(
        registry=repos.registry,
        resolutions=repos.intent_resolutions,
    )
    reprocessed = 0
    resolved = 0
    touched_event_ids: set[str] = set()
    discovery_lookup_keys: set[str] = set()
    evidence_by_intent = repos.token_evidence.evidence_for_intents([str(intent["intent_id"]) for intent in intents])
    for intent in intents:
        evidence = evidence_by_intent.get(str(intent["intent_id"]), [])
        decision = resolver.resolve(
            intent,
            evidence,
            decision_time_ms=now_ms,
            persist=True,
        )
        touched_event_ids.add(str(decision.event_id))
        repos.token_intent_lookup.replace_lookup_keys(
            intent_id=decision.intent_id,
            event_id=decision.event_id,
            keys=decision.lookup_keys,
            source_evidence_id=intent.get("primary_evidence_id"),
            created_at_ms=now_ms,
        )
        reprocessed += 1
        if decision.target_type and decision.target_id:
            resolved += 1
        else:
            discovery_lookup_keys.update(
                key for key in decision.lookup_keys if str(key).startswith(("symbol:", "address:"))
            )
    if discovery_lookup_keys:
        repos.discovery.enqueue_lookup_keys(
            sorted(discovery_lookup_keys),
            reason="resolution_refresh_unresolved",
            now_ms=now_ms,
        )
    radar_edge_mutations = sum(
        repos.radar_source_edges.sync_event(
            event_id=event_id,
            now_ms=now_ms,
        )
        for event_id in sorted(touched_event_ids)
    )
    return {
        "window": window,
        "lookup_keys": lookup_keys or [],
        "reprocessed_intents": reprocessed,
        "resolved_intents": resolved,
        "radar_edge_mutations": radar_edge_mutations,
        "since_ms": since_ms,
    }
