"""Closed source-binding identities and the smaller enabled execution set.

A binding is durable release evidence, not provider discovery.  It freezes the exact account
generation and the exact catalogue, capability, adapter, quote, protection, and client identities
that one lifecycle may use.  Credentials never enter the value; only the redacted fingerprint
produced by the operator-owned configuration boundary does.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapter_contracts import BINANCE_USDM_ADAPTER_CONTRACT_SHA256
from .contracts import VenueBinding, canonical_sha256
from .execution_policy import PROTECTION_CONTRACT_SHA256
from .quote_authority import QUOTE_CONTRACT_SHA256

EXECUTION_BINDING_VERSION: Final[Literal["execution_binding_v1"]] = "execution_binding_v1"
ExecutionVenue = Literal["binance.usdm", "hyperliquid.perp"]

# Hyperliquid remains a source-native research/catalog binding, but it is deliberately not a private
# execution binding in the Demo-first runtime.  Keeping this one closed set at the domain seam avoids
# config, Workers, Nautilus and authority each inventing a different answer.
EXECUTION_ENABLED_BINDINGS: Final[frozenset[VenueBinding]] = frozenset({"BINANCE_USDM"})
EXECUTION_DISABLED_BINDINGS: Final[frozenset[VenueBinding]] = frozenset({"HYPERLIQUID_PERP"})

BINDING_VENUE: Final[dict[VenueBinding, ExecutionVenue]] = {
    "BINANCE_USDM": "binance.usdm",
    "HYPERLIQUID_PERP": "hyperliquid.perp",
}
_SOURCE_BINDING: Final[dict[str, VenueBinding]] = {
    "binance": "BINANCE_USDM",
    "binance.perp": "BINANCE_USDM",
    "binance.usdm": "BINANCE_USDM",
    "hyperliquid": "HYPERLIQUID_PERP",
    "hl.perp": "HYPERLIQUID_PERP",
    "hyperliquid.perp": "HYPERLIQUID_PERP",
}


def binding_for_source_venue(value: object) -> VenueBinding | None:
    """Resolve only the two source-native venue families admitted by Production V3."""

    return _SOURCE_BINDING.get(str(value or "").strip().lower())


def venue_for_binding(binding: VenueBinding) -> ExecutionVenue:
    return BINDING_VENUE[binding]


def require_execution_binding_enabled(binding: VenueBinding) -> None:
    if binding not in EXECUTION_ENABLED_BINDINGS:
        raise ValueError(f"execution_binding_disabled:{binding}")


def require_current_execution_contracts(
    *,
    binding: VenueBinding,
    adapter_contract_sha256: str,
    quote_contract_sha256: str,
    protection_contract_sha256: str,
) -> None:
    """Reject execution evidence from any contract other than this release."""

    require_execution_binding_enabled(binding)
    if (
        adapter_contract_sha256 != BINANCE_USDM_ADAPTER_CONTRACT_SHA256
        or quote_contract_sha256 != QUOTE_CONTRACT_SHA256
        or protection_contract_sha256 != PROTECTION_CONTRACT_SHA256
    ):
        raise ValueError("execution_contract_mismatch")


class ExecutionBindingV1(BaseModel):
    """One content-addressed, redacted binding to an exact provider account generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_version: Literal["execution_binding_v1"] = EXECUTION_BINDING_VERSION
    binding: VenueBinding
    venue: ExecutionVenue
    account_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_generation: int = Field(ge=1)
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_runtime_identity: str = Field(min_length=1, max_length=256)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def binding_matches_venue(self) -> Self:
        if self.venue != BINDING_VENUE[self.binding]:
            raise ValueError("execution_binding_venue_mismatch")
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


__all__ = [
    "BINDING_VENUE",
    "EXECUTION_BINDING_VERSION",
    "EXECUTION_DISABLED_BINDINGS",
    "EXECUTION_ENABLED_BINDINGS",
    "ExecutionBindingV1",
    "ExecutionVenue",
    "binding_for_source_venue",
    "require_current_execution_contracts",
    "require_execution_binding_enabled",
    "venue_for_binding",
]
