"""The single canonical reference to the 433-B OI Runtime.

433-C removes the legacy lifecycle bridge. Activation remains fail closed until
433-E wires paper/live startup and deployment.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.identifiers import AccountId

from tracefold.app.nautilus.oi_runtime import OiRuntimeReadiness
from tracefold.app.nautilus.oi_runtime import run_nautilus as run_oi_runtime
from tracefold.integrations.nautilus.oi_runtime.config import OiRiskLimits, OiRuntimeProfile
from tracefold.platform.config.models import Settings
from tracefold.trading import canonical_sha256

_RUNTIME_RELEASE = "nautilus-1.231.0+oi-v1"


def run_nautilus(settings: Settings) -> OiRuntimeReadiness:
    """Reach only the disabled 433-B state; no TradingNode or credential is loaded."""

    execution = settings.trading.execution
    if execution.mode != "disabled":
        raise RuntimeError("oi_runtime_activation_not_available_before_433e")
    identity = {
        "mode": execution.mode,
        "profile_id": execution.profile_id,
        "account_slot": execution.account_slot,
        "runtime_release": _RUNTIME_RELEASE,
    }
    profile = OiRuntimeProfile(
        mode="disabled",
        profile_id=execution.profile_id,
        account_slot=execution.account_slot,
        account_id=AccountId("BINANCE-001"),
        runtime_release=_RUNTIME_RELEASE,
        config_sha256=canonical_sha256(identity),
        credential_namespace=f"tracefold:{execution.profile_id}:disabled",
        cache_namespace=f"tracefold:{execution.profile_id}:disabled",
        client_order_namespace=f"tracefold:{execution.profile_id}:disabled",
        routes=(),
        risk=OiRiskLimits(
            risk_fraction_per_trade=Decimal("0.01"),
            max_risk_per_trade_usd=Decimal("10"),
            max_total_risk_usd=Decimal("25"),
            max_positions=1,
            max_leverage=1,
            max_daily_loss_usd=Decimal("25"),
            market_stale_after_ns=5_000_000_000,
            account_stale_after_ns=5_000_000_000,
            reconciliation_stale_after_ns=10_000_000_000,
        ),
    )
    return run_oi_runtime(profile)


__all__ = ["run_nautilus"]
