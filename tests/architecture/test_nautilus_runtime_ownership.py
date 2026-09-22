"""Nautilus owns execution state, and the Runtime reaches it only through public API (#680 PR-1).

Three rules, each one the shape of a defect this Runtime had:

* no private member of a Nautilus object is read or called anywhere in the Runtime. The deleted
  private account proof reached into `engine._clients`, `_fetch_algo_orders` and a replayed Cache
  repair, and an upstream rename would have surfaced as a crash on the start-up path;
* no private helper on the Strategy shadows a Nautilus lifecycle hook. A helper named `_dispose`
  replaced `Component._dispose` and made the node unable to shut down;
* the machinery the cut removed stays removed: a separate recovery path, the compatibility seam, the
  generation-counted stop replacement and the PnL-completeness label have no spelling left.
"""

from __future__ import annotations

import ast
from pathlib import Path

from nautilus_trader.trading.strategy import Strategy

from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SOURCES = (
    ROOT / "tracefold" / "integrations" / "nautilus",
    ROOT / "tracefold" / "app" / "nautilus",
)


def _runtime_files() -> list[Path]:
    return sorted(
        path for source in RUNTIME_SOURCES for path in source.rglob("*.py") if "__pycache__" not in path.parts
    )


def test_the_runtime_reads_no_private_member_of_any_object_but_its_own() -> None:
    offenders = []
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not node.attr.startswith("_") or node.attr.startswith("__"):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                continue
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{node.attr}")
    assert offenders == []


def test_no_strategy_helper_shadows_a_nautilus_member() -> None:
    base = set(dir(Strategy))
    own = {name for name in vars(OiNautilusStrategy) if name.startswith("_") and not name.startswith("__")}
    assert sorted(own & base) == []


def test_the_replaced_runtime_machinery_has_no_spelling_left() -> None:
    retired_modules = (
        "tracefold/integrations/nautilus/oi_runtime/nautilus_1231_binance_compat.py",
        "tracefold/integrations/nautilus/oi_runtime/recovery.py",
        "tracefold/integrations/nautilus/oi_runtime/protection.py",
        "tracefold/integrations/nautilus/oi_runtime/exit.py",
        "tracefold/integrations/nautilus/oi_runtime/state.py",
        "tracefold/integrations/nautilus/oi_runtime/audit_sink.py",
        "tracefold/integrations/nautilus/oi_runtime/trade_plans.py",
        "tracefold/integrations/nautilus/oi_runtime/quotes.py",
        "tracefold/app/nautilus/reconciliation.py",
    )
    assert [module for module in retired_modules if (ROOT / module).exists()] == []
    retired_words = (
        "recovery_safety_flatten",
        "native_pnl_complete",
        "history_gap_reason",
        "protection_generation",
        "bind_reconciled_order_account",
        "trade_plan_risk_changed",
        "QUOTE_WARMUP_NS",
        "_MAX_SPREAD_BPS",
        "max_total_risk_usd",
        "reconciliation=False",
    )
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{word}"
        for path in (*_runtime_files(), *sorted((ROOT / "tracefold" / "trading").rglob("*.py")))
        for word in retired_words
        if word in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
