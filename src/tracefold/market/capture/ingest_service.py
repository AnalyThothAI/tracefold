from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any

from tracefold.market.capture.entity_extractor import TextSurface, extract_entities_from_surfaces
from tracefold.market.capture.entity_repository import EntityRepository
from tracefold.market.capture.event_contracts import EventRead, materialize_event
from tracefold.market.capture.evidence_repository import EvidenceRepository
from tracefold.market.capture.ingest_contracts import IngestedEvent
from tracefold.market.capture.twitter_event import TwitterEvent
from tracefold.market.identity.discovery_repository import DiscoveryRepository
from tracefold.market.identity.identity_evidence_policy import (
    CONFIDENCE_MENTION_ONLY,
    CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_GMGN_PAYLOAD_EXACT,
    EVIDENCE_TWEET_CONTRACT_MENTION,
)
from tracefold.market.identity.identity_evidence_repository import IdentityEvidenceRepository
from tracefold.market.identity.intent_resolution_repository import (
    IntentResolutionRepository,
    token_intent_resolution_id,
)
from tracefold.market.identity.registry_repository import RegistryRepository
from tracefold.market.identity.token_evidence_builder import build_token_evidence
from tracefold.market.identity.token_evidence_repository import TokenEvidenceRepository
from tracefold.market.identity.token_intent_builder import TokenIntentInput, build_token_intents
from tracefold.market.identity.token_intent_lookup_repository import TokenIntentLookupRepository
from tracefold.market.identity.token_intent_repository import TokenIntentRepository
from tracefold.market.identity.token_intent_resolver import TokenIntentResolutionDecision, TokenIntentResolver
from tracefold.market.pricing.enriched_event_repository import EnrichedEventRepository
from tracefold.market.pricing.event_anchor_backfill_job_repository import EventAnchorBackfillJobRepository
from tracefold.market.pricing.event_market_capture import CaptureResult
from tracefold.market.pricing.market_tick import EnrichedEventCapture, MarketTick
from tracefold.market.pricing.market_tick_current_repository import MarketTickCurrentRepository
from tracefold.market.pricing.market_tick_persistence import MarketTickPersistenceService
from tracefold.market.pricing.market_tick_repository import MarketTickRepository
from tracefold.market.radar.radar_source_edge_repository import RadarSourceEdgeRepository
from tracefold.platform.postgres.persisted_live import PersistedLiveEventRepository


@dataclass(frozen=True, slots=True)
class PreparedIngest:
    raw_event: TwitterEvent
    event_read: EventRead
    event_id: str
    event_ms: int
    event_row: dict[str, Any]
    entities: list[Any]
    evidence_inputs: list[Any]
    intents: list[TokenIntentInput]


IngestCaptureInput = CaptureResult | EnrichedEventCapture


class IngestService:
    def __init__(
        self,
        *,
        evidence: EvidenceRepository,
        entities: EntityRepository,
        registry: RegistryRepository,
        identity_evidence: IdentityEvidenceRepository,
        token_intent_lookup: TokenIntentLookupRepository,
        token_evidence: TokenEvidenceRepository,
        token_intents: TokenIntentRepository,
        intent_resolutions: IntentResolutionRepository,
        discovery: DiscoveryRepository,
        market_ticks: MarketTickRepository,
        market_tick_current: MarketTickCurrentRepository,
        enriched_events: EnrichedEventRepository,
        event_anchor_jobs: EventAnchorBackfillJobRepository,
        radar_source_edges: RadarSourceEdgeRepository,
        persisted_live: PersistedLiveEventRepository,
        transaction: Callable[[], AbstractContextManager[None]],
        event_anchor_active_window_ms: int,
    ) -> None:
        self.conn = evidence.conn
        self.evidence = evidence
        self.entities = entities
        self.registry = registry
        self.identity_evidence = identity_evidence
        self.token_intent_lookup = token_intent_lookup
        self.token_evidence = token_evidence
        self.token_intents = token_intents
        self.intent_resolutions = intent_resolutions
        self.discovery = discovery
        self.market_ticks = market_ticks
        self.market_tick_current = market_tick_current
        self.enriched_events = enriched_events
        self.event_anchor_jobs = event_anchor_jobs
        self.radar_source_edges = radar_source_edges
        self.persisted_live = persisted_live
        self.transaction = transaction
        self.event_anchor_active_window_ms = require_event_anchor_active_window_ms(event_anchor_active_window_ms)

    def require_transaction(self, *, operation: str) -> None:
        self.evidence.require_transaction(operation=operation)

    def insert_raw_frame(self, **kwargs: Any) -> bool:
        with self.transaction():
            result: bool = self.evidence.insert_raw_frame(**kwargs)
            return result

    def ingest_event(self, event: TwitterEvent) -> IngestedEvent:
        prepared = self.prepare_event(event)
        # Registry preparation participates in the same application transaction
        # as the material event facts, so a later failure cannot leave orphan assets.
        with self.transaction():
            if self.event_already_exists(prepared):
                return self.duplicate_result(prepared)
            self.prepare_registry_for_resolution(prepared)
            decisions = self.resolve_prepared(prepared, persist=False)
            captures = [
                _unavailable_capture(prepared, market_resolution, reason="missing_capture_service")
                for decision in decisions
                if (market_resolution := self.market_resolution_for_decision(decision)) is not None
            ]
            return self.commit_prepared_event(prepared, resolutions=decisions, captures=captures)

    @staticmethod
    def prepare_event(event: TwitterEvent) -> PreparedIngest:
        extracted = extract_entities_from_surfaces(_event_surfaces(event))
        evidence_inputs = build_token_evidence(
            event_id=event.event_id,
            entities=extracted,
            token_snapshot=event.token_snapshot,
            created_at_ms=event.received_at_ms,
        )
        intent_inputs = build_token_intents(
            event_id=event.event_id,
            evidence=evidence_inputs,
            created_at_ms=event.received_at_ms,
        )
        event_row, event_read = materialize_event(event, now_ms=_now_ms())
        return PreparedIngest(
            raw_event=event,
            event_read=event_read,
            event_id=event.event_id,
            event_ms=event.received_at_ms,
            event_row=event_row,
            entities=extracted,
            evidence_inputs=evidence_inputs,
            intents=intent_inputs,
        )

    def event_already_exists(self, prepared: PreparedIngest) -> bool:
        return self.evidence.event_exists(
            event_id=prepared.event_id,
            logical_dedup_key=str(prepared.event_row["logical_dedup_key"]),
        )

    def duplicate_result(self, prepared: PreparedIngest) -> IngestedEvent:
        return IngestedEvent(
            event=prepared.event_read,
            entities=[],
            token_intents=[],
            token_resolutions=[],
            inserted=False,
        )

    def prepare_registry_for_resolution(self, prepared: PreparedIngest) -> None:
        self._upsert_gmgn_payload_registry_asset(prepared.raw_event)
        self._upsert_chain_intent_registry_assets(prepared.raw_event, prepared.intents)

    def resolve_prepared(
        self,
        prepared: PreparedIngest,
        *,
        persist: bool = False,
    ) -> list[TokenIntentResolutionDecision]:
        resolver = TokenIntentResolver(
            registry=self.registry,
            resolutions=self.intent_resolutions,
        )
        return [
            resolver.resolve(
                self._intent_with_prepared_chain_hint(intent),
                prepared.evidence_inputs,
                decision_time_ms=prepared.event_ms,
                persist=persist,
            )
            for intent in prepared.intents
        ]

    def commit_prepared_event(
        self,
        prepared: PreparedIngest,
        *,
        resolutions: list[TokenIntentResolutionDecision],
        captures: Sequence[IngestCaptureInput],
    ) -> IngestedEvent:
        capture_results = [_require_capture_result(item) for item in captures]
        self.require_transaction(operation="commit_prepared_event")
        inserted = self.evidence.insert_event_row(prepared.event_row)
        if not inserted:
            return self.duplicate_result(prepared)
        self.entities.insert_event_entities(prepared.raw_event, prepared.entities)
        self.token_evidence.insert_many(prepared.evidence_inputs)
        token_intents = self.token_intents.insert_many(prepared.intents)
        self._upsert_gmgn_payload_registry(prepared.raw_event)
        self._upsert_chain_intent_registry(prepared.raw_event, prepared.intents)
        for decision in resolutions:
            _require_resolution_decision(decision)
            self.intent_resolutions.insert_resolution(decision)
            decision_intent_id = decision.intent_id
            intent = _token_intent_by_id(prepared.intents, decision_intent_id)
            self.token_intent_lookup.replace_lookup_keys(
                intent_id=decision_intent_id,
                event_id=decision.event_id,
                keys=decision.lookup_keys,
                source_evidence_id=intent.primary_evidence_id,
                created_at_ms=prepared.event_ms,
            )
        discovery_lookup_keys = _discovery_lookup_keys_for_resolutions(resolutions)
        if discovery_lookup_keys:
            self.discovery.enqueue_lookup_keys(
                discovery_lookup_keys,
                reason="intent_resolution_unresolved",
                now_ms=prepared.event_ms,
            )
        self.radar_source_edges.sync_event(
            event_id=prepared.event_id,
            now_ms=prepared.event_ms,
        )
        capture_ticks = [item.tick for item in capture_results if item.tick is not None]
        if capture_ticks:
            MarketTickPersistenceService(self).persist_ticks(
                capture_ticks,
                now_ms=prepared.event_ms,
            )
        for item in capture_results:
            self.enriched_events.insert_capture(item.capture)
            self.event_anchor_jobs.enqueue_for_capture(
                item.capture,
                active_window_ms=self.event_anchor_active_window_ms,
            )
        token_resolutions = self.intent_resolutions.resolutions_for_event(prepared.event_id)
        result = IngestedEvent(
            event=prepared.event_read,
            entities=[_entity_payload(entity) for entity in prepared.entities],
            token_intents=token_intents,
            token_resolutions=token_resolutions,
            inserted=True,
        )
        self.persisted_live.append(
            source_key=f"event:{result.event['event_id']}",
            event_kind="event",
            payload={
                "type": "event",
                "event": result.event,
                "entities": result.entities,
                "token_intents": result.token_intents,
                "token_resolutions": result.token_resolutions,
            },
            committed_at_ms=int(result.event["received_at_ms"]),
        )
        return result

    def market_resolution_for_decision(self, decision: TokenIntentResolutionDecision) -> dict[str, Any] | None:
        _require_resolution_decision(decision)
        target_type = decision.target_type
        target_id = decision.target_id
        if not target_type or not target_id:
            return None
        resolution_id = token_intent_resolution_id(decision)
        if target_type == "Asset":
            target = self.registry.chain_token_market_target(str(target_id))
            if target is None:
                return None
            return {
                "event_id": decision.event_id,
                "intent_id": decision.intent_id,
                "resolution_id": resolution_id,
                **target,
            }
        if target_type == "CexToken":
            pricefeed = self._cex_pricefeed_for_decision(decision)
            if not pricefeed:
                return None
            provider = str(pricefeed.get("provider") or "").strip().lower()
            native_market_id = str(pricefeed.get("native_market_id") or "").strip().upper()
            if not provider or not native_market_id:
                return None
            return {
                "event_id": decision.event_id,
                "intent_id": decision.intent_id,
                "resolution_id": resolution_id,
                "target_type": "cex_symbol",
                "target_id": f"{provider}:{native_market_id}",
                "pricefeed_id": pricefeed.get("pricefeed_id"),
            }
        return None

    def _cex_pricefeed_for_decision(self, decision: TokenIntentResolutionDecision) -> dict[str, Any] | None:
        _require_resolution_decision(decision)
        target_id = decision.target_id
        pricefeed_id = decision.pricefeed_id
        return self.registry.cex_pricefeed_for_token(
            cex_token_id=str(target_id),
            pricefeed_id=str(pricefeed_id) if pricefeed_id else None,
        )

    def _upsert_gmgn_payload_registry(self, event: TwitterEvent) -> dict[str, Any] | None:
        asset = self._upsert_gmgn_payload_registry_asset(event)
        if asset is None:
            return None
        snapshot = event.token_snapshot
        if snapshot is None:
            return None
        self.identity_evidence.upsert_identity_evidence(
            asset_id=str(asset["asset_id"]),
            evidence_kind=EVIDENCE_GMGN_PAYLOAD_EXACT,
            provider="gmgn",
            lookup_mode="provider_payload",
            chain_id=str(asset["chain_id"]),
            address=str(asset["address"]),
            symbol=snapshot.symbol,
            name=None,
            decimals=None,
            confidence=CONFIDENCE_PROVIDER_EXACT,
            source_event_id=event.event_id,
            raw_payload={**snapshot.raw, "payload_hash": _payload_hash(snapshot.raw)},
            observed_at_ms=event.received_at_ms,
        )
        self.identity_evidence.recompute_current_identity(
            str(asset["asset_id"]),
            now_ms=event.received_at_ms,
        )
        return asset

    def _upsert_gmgn_payload_registry_asset(self, event: TwitterEvent) -> dict[str, Any] | None:
        snapshot = event.token_snapshot
        if snapshot is None:
            return None
        if not snapshot.address or not snapshot.chain:
            return None
        asset = self.registry.upsert_chain_asset(
            chain_id=snapshot.chain,
            address=snapshot.address,
            observed_at_ms=event.received_at_ms,
        )
        return asset

    def _upsert_chain_intent_registry_assets(self, event: TwitterEvent, intents: list[TokenIntentInput]) -> None:
        for item in intents:
            intent = _require_token_intent(item)
            if not intent.chain_hint or not intent.address_hint:
                continue
            self.registry.upsert_chain_asset(
                chain_id=str(intent.chain_hint),
                address=str(intent.address_hint),
                observed_at_ms=event.received_at_ms,
            )

    def _intent_with_prepared_chain_hint(self, intent: TokenIntentInput) -> TokenIntentInput:
        _require_token_intent(intent)
        if intent.chain_hint or not intent.address_hint:
            return intent
        rows = self.registry.find_assets_by_address(
            chain_id=None,
            address=str(intent.address_hint),
        )
        if len(rows) != 1 or not rows[0].get("chain_id"):
            return intent
        return replace(intent, chain_hint=str(rows[0]["chain_id"]))

    def _upsert_chain_intent_registry(self, event: TwitterEvent, intents: list[TokenIntentInput]) -> None:
        for item in intents:
            intent = _require_token_intent(item)
            if not intent.chain_hint or not intent.address_hint:
                continue
            asset = self.registry.upsert_chain_asset(
                chain_id=str(intent.chain_hint),
                address=str(intent.address_hint),
                observed_at_ms=event.received_at_ms,
            )
            self.identity_evidence.upsert_identity_evidence(
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
                source_event_id=event.event_id,
                source_intent_id=intent.intent_id,
                observed_at_ms=event.received_at_ms,
            )
            self.identity_evidence.recompute_current_identity(
                str(asset["asset_id"]),
                now_ms=event.received_at_ms,
            )


def _event_surfaces(event: TwitterEvent) -> list[TextSurface]:
    surfaces = []
    if event.content.text:
        surfaces.append(TextSurface("primary", event.content.text))
    if event.reference and event.reference.text:
        surfaces.append(TextSurface("reference", event.reference.text))
    return surfaces


def _entity_payload(entity: Any) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "raw_value": entity.raw_value,
        "normalized_value": entity.normalized_value,
        "chain": entity.chain,
        "token_resolution_status": entity.token_resolution_status,
        "confidence": entity.confidence,
        "source": entity.source,
        "text_surface": entity.text_surface,
        "span_start": entity.span_start,
        "span_end": entity.span_end,
        "sentence_id": entity.sentence_id,
        "local_group_key": entity.local_group_key,
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def require_event_anchor_active_window_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("event_anchor_active_window_ms_required")
    return int(value)


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_resolution_decision(decision: Any) -> TokenIntentResolutionDecision:
    if not isinstance(decision, TokenIntentResolutionDecision):
        raise RuntimeError("ingest_resolution_decision_contract_required")
    return decision


def _require_token_intent(intent: Any) -> TokenIntentInput:
    if not isinstance(intent, TokenIntentInput):
        raise RuntimeError("ingest_token_intent_contract_required")
    return intent


def _token_intent_by_id(intents: list[TokenIntentInput], intent_id: str) -> TokenIntentInput:
    for intent in intents:
        formal_intent = _require_token_intent(intent)
        if formal_intent.intent_id == intent_id:
            return formal_intent
    raise RuntimeError("ingest_token_intent_contract_required")


def _require_capture_result(item: Any) -> CaptureResult:
    if isinstance(item, EnrichedEventCapture):
        return CaptureResult(tick=None, capture=item)
    if not isinstance(item, CaptureResult):
        raise RuntimeError("ingest_capture_result_contract_required")
    if item.tick is not None and not isinstance(item.tick, MarketTick):
        raise RuntimeError("ingest_capture_result_contract_required")
    if not isinstance(item.capture, EnrichedEventCapture):
        raise RuntimeError("ingest_capture_result_contract_required")
    return item


def _discovery_lookup_keys_for_resolutions(
    resolutions: list[TokenIntentResolutionDecision],
) -> list[str]:
    lookup_keys: set[str] = set()
    for decision in resolutions:
        formal_decision = _require_resolution_decision(decision)
        status = str(formal_decision.resolution_status or "")
        target_type = formal_decision.target_type
        target_id = formal_decision.target_id
        if status not in {"NIL", "AMBIGUOUS"} and target_type and target_id:
            continue
        for key in formal_decision.lookup_keys:
            text = str(key or "").strip()
            if text.startswith(("symbol:", "address:")):
                lookup_keys.add(text)
    return sorted(lookup_keys)


def _unavailable_capture(
    prepared: PreparedIngest,
    market_resolution: dict[str, Any],
    *,
    reason: str,
) -> EnrichedEventCapture:
    return EnrichedEventCapture(
        event_id=prepared.event_id,
        intent_id=str(market_resolution["intent_id"]),
        resolution_id=str(market_resolution["resolution_id"]),
        target_type=market_resolution["target_type"],
        target_id=str(market_resolution["target_id"]),
        t_event_ms=prepared.event_ms,
        tick_observed_at_ms=None,
        tick_id=None,
        tick_lag_ms=None,
        capture_method="unavailable",
        capture_reason=reason,
        created_at_ms=_now_ms(),
    )
