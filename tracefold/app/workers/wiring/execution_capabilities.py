"""Compile per-binding execution truth outside database transactions (#376)."""

from __future__ import annotations

import time

from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.nautilus.capabilities import (
    load_binance_usdm_execution_evidence,
    load_hyperliquid_perp_execution_evidence,
)
from tracefold.integrations.nautilus.config import (
    NAUTILUS_RELEASE,
    installed_nautilus_wheel_identity,
)
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
    HYPERLIQUID_PERP_ADAPTER_CONTRACT_SHA256,
    PROTECTION_CONTRACT_SHA256,
    QUOTE_CONTRACT_SHA256,
    VenueInstrumentCatalogSnapshotV1,
)
from tracefold.trading.capabilities import (
    ExecutionCapabilitySnapshotV2,
    ExecutionInstrumentEvidenceV1,
    build_execution_capability_snapshot,
)


class ExecutionCapabilityCompileError(RuntimeError):
    """One binding failed after its durable compile-error projection was written."""


class ExecutionCapabilityCompiler:
    """The one current writer for complete Capability V2 partitions."""

    def __init__(self, database: WorkerTradingDatabase) -> None:
        self._database = database

    async def compile(
        self,
        catalog: VenueInstrumentCatalogSnapshotV1,
    ) -> ExecutionCapabilitySnapshotV2:
        try:
            evidence, adapter_contract = await self._load(catalog)
            identity = runtime_identity()
            snapshot = build_execution_capability_snapshot(
                catalog=catalog,
                execution_rows=evidence,
                app_revision=identity.runtime_revision,
                app_image_digest=identity.image_digest,
                adapter_contract_sha256=adapter_contract,
                quote_contract_sha256=QUOTE_CONTRACT_SHA256,
                protection_contract_sha256=PROTECTION_CONTRACT_SHA256,
                client_runtime_identity=(
                    f"nautilus-trader=={NAUTILUS_RELEASE.version};wheel={installed_nautilus_wheel_identity()}"
                ),
            )
            now_ms = int(time.time() * 1_000)
            await self._database.tx(
                "trading_execution_capability_publish",
                lambda repos: repos.trading.append_and_activate_execution_capability_snapshot(
                    snapshot,
                    created_at_ms=now_ms,
                ),
                timeout_seconds=10.0,
            )
            return snapshot
        except Exception as exc:
            reason = _bounded_error(exc)
            now_ms = int(time.time() * 1_000)
            await self._database.tx(
                "trading_execution_capability_error",
                lambda repos: repos.trading.mark_execution_capability_compile_error(
                    binding=catalog.binding,
                    reason=reason,
                    now_ms=now_ms,
                ),
                timeout_seconds=10.0,
            )
            raise ExecutionCapabilityCompileError(
                f"execution_capability_compile_failed:{catalog.binding}:{reason}"
            ) from exc

    @staticmethod
    async def _load(
        catalog: VenueInstrumentCatalogSnapshotV1,
    ) -> tuple[list[ExecutionInstrumentEvidenceV1], str]:
        if catalog.binding == "BINANCE_USDM":
            return (
                await load_binance_usdm_execution_evidence(catalog),
                BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
            )
        if catalog.binding == "HYPERLIQUID_PERP":
            return (
                await load_hyperliquid_perp_execution_evidence(catalog),
                HYPERLIQUID_PERP_ADAPTER_CONTRACT_SHA256,
            )
        raise ValueError("execution_capability_binding_unsupported")


def _bounded_error(exc: Exception) -> str:
    code = str(exc).strip().splitlines()[0].split(":", 1)[0]
    if code.startswith(("execution_capability_", "binance_capability_", "hyperliquid_capability_")):
        normalized = "".join(character if character.isalnum() or character in "_.-" else "_" for character in code)
        return normalized[:128]
    error_type = "".join(character.lower() for character in type(exc).__name__ if character.isalnum())
    return f"execution_capability_{error_type or 'unknown'}_failed"[:128]


__all__ = ["ExecutionCapabilityCompileError", "ExecutionCapabilityCompiler"]
