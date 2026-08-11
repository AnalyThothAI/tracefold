from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tracefold.market.identity.chain_identity import canonical_chain_id
from tracefold.market.identity.deterministic_token_resolver import (
    DeterministicTokenResolver,
)
from tracefold.market.identity.token_fact_inputs import (
    DeterministicResolution,
    MentionKeys,
    TokenEvidenceInput,
    TokenIntentInput,
)

TokenIntentResolutionDecision = DeterministicResolution
TokenIntentResolveInput = TokenIntentInput | Mapping[str, Any]
TokenEvidenceResolveInput = TokenEvidenceInput | Mapping[str, Any]


class TokenIntentResolver:
    def __init__(self, *, registry: Any, resolutions: Any) -> None:
        self.registry = registry
        self.resolutions = resolutions
        self.resolver = DeterministicTokenResolver(registry=registry)

    def resolve(
        self,
        intent: TokenIntentResolveInput,
        evidence: Sequence[TokenEvidenceResolveInput],
        *,
        decision_time_ms: int | None = None,
        persist: bool = False,
    ) -> DeterministicResolution:
        now_ms = int(decision_time_ms or _intent_created_at(intent) or 0)
        decision = self.resolver.resolve(
            intent_id=str(_intent_value(intent, "intent_id")),
            event_id=str(_intent_value(intent, "event_id")),
            keys=_mention_keys(intent, evidence),
            decision_time_ms=now_ms,
        )
        if persist:
            self.resolutions.insert_resolution(decision)
        return decision


def _mention_keys(
    intent: TokenIntentResolveInput,
    evidence: Sequence[TokenEvidenceResolveInput],
) -> MentionKeys:
    cex_pricefeed_id = _cex_pricefeed_id(evidence)
    return MentionKeys(
        symbol=_intent_value(intent, "display_symbol"),
        chain_id=_chain_id(_intent_value(intent, "chain_hint")),
        address=_address(_intent_value(intent, "address_hint")),
        cex_pricefeed_id=cex_pricefeed_id,
        exchange=_exchange_hint(evidence),
    )


def _cex_pricefeed_id(evidence: Sequence[TokenEvidenceResolveInput]) -> str | None:
    for item in evidence:
        evidence_type = _evidence_value(item, "evidence_type")
        if evidence_type == "cex_pricefeed":
            ref = _evidence_value(item, "provider_ref")
            return str(ref) if ref is not None else None
    return None


def _exchange_hint(evidence: Sequence[TokenEvidenceResolveInput]) -> str | None:
    for item in evidence:
        provider = _evidence_value(item, "provider")
        if provider:
            return str(provider).lower()
    return None


def _intent_value(intent: TokenIntentResolveInput, key: str) -> Any:
    if isinstance(intent, TokenIntentInput):
        values = {
            "intent_id": intent.intent_id,
            "event_id": intent.event_id,
            "display_symbol": intent.display_symbol,
            "chain_hint": intent.chain_hint,
            "address_hint": intent.address_hint,
            "created_at_ms": intent.created_at_ms,
        }
        return values.get(key)
    if isinstance(intent, Mapping):
        return intent.get(key)
    raise TypeError("token_intent_resolver_input_contract_required")


def _evidence_value(evidence: TokenEvidenceResolveInput, key: str) -> Any:
    if isinstance(evidence, TokenEvidenceInput):
        values = {
            "evidence_type": evidence.evidence_type,
            "provider": evidence.provider,
            "provider_ref": evidence.provider_ref,
        }
        return values.get(key)
    if isinstance(evidence, Mapping):
        return evidence.get(key)
    raise TypeError("token_intent_resolver_evidence_contract_required")


def _intent_created_at(intent: TokenIntentResolveInput) -> int | None:
    value = _intent_value(intent, "created_at_ms")
    return int(value) if value is not None else None


def _chain_id(chain: Any) -> str | None:
    if chain is None:
        return None
    normalized = str(chain).strip().lower()
    if normalized in {"", "unknown", "evm_unknown"}:
        return None
    return canonical_chain_id(normalized)


def _address(address: Any) -> str | None:
    if address is None:
        return None
    value = str(address).strip()
    return value.lower() if value.startswith("0x") else value
