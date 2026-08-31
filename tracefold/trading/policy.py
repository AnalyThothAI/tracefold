"""The one production Alpha policy: `source_native_oi_smart_money_long_v4`.

Pure, deterministic, long-only, and the whole of what decides a Case. It answers `long` or `no_trade`
and nothing else — no permission, no execution environment, no venue. The result is an engine-neutral
Signal; execution authority belongs to a later Runtime boundary.

**Why a new identity rather than a retuned `oi_smart_money_momentum_v1` (#331).** The arithmetic is
carried over unchanged, deliberately: this hard cut is not the place to move a threshold. What changed
is everything around it — the trigger is provider-native, the manifest lost the quadrant and the News
counterpart, the config lost `allow_short`, and the decision now carries frozen per-check evidence. A
Case decided under the old identity cannot be replayed under this code, and reusing the id would make
every one of production's 153 historical rows claim rules they were never decided by. The old decoder
is gone with the registry, so those rows stay readable as rows and are never re-executed.

`allow_short` is dropped rather than pinned to `False`. It could not change a decision: a `fall` frame
is refused by `not_oi_rise` before any side exists and no other path produces one, so it was a config
key whose only effect was on the digest.

The template this implements is three conditions on one frame:

    5-minute OI rise  >= 5%
    smart-money / OI  >  50%
    profit metric     >  0

plus a confirmed price direction and a chasing ceiling above it.

**The OI floor is 5% and the ceiling is 10% by operator decision, not by measurement (#273).** The
template's own number was 10%, and at 10% this lane is starved: over the seven days to 2026-08-27,
462 parsed frames yielded four that cleared all three conditions. At 5% the same corpus yields 32.
What that bought is throughput for a lane whose purpose is receipts; what it did not buy is evidence.

**The 1000 bps ceiling contradicts a measurement, and recording the contradiction is the point.**
`docs/research/oi-agent-design-2026-08-22.md` §1.6 bucketed the full OI corpus by the same 1 h
pre-move this rule reads: 1-3% returned +1.27% at 4 h, 3-6% returned +0.80%, and **6-12% returned
-0.77% on N=151, with a median 1 h MAE of -3.35%**. The ceiling admits the bottom half of that
measured-negative bucket. Two things make it a decision an operator may take: the measurement is over
the *whole* corpus and whether the three smart-money conditions change its sign inside their own
cohort is unmeasured. The Signal itself grants no execution authority.

**Inclusivity is in the field names and it is not negotiable.** `min_` reads `>=` and the two `above`
conditions read `>`: `500` qualifies and `499` does not; `5001` qualifies and `5000` does not; `1`
qualifies and `0` does not.

**Every condition it executes is written down with the Case.** `AlphaDecision.checks` carries the
threshold, the operator, the measured value and the pass/fail for each rule the policy reached, so a
console holding only today's configuration can still explain a Case frozen a week ago.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .contracts import (
    AlphaDecision,
    FrozenPolicyContext,
    PolicyCheck,
    PolicyOperator,
    canonical_sha256,
)

ALPHA_POLICY_ID: Final = "source_native_oi_smart_money_long_v4"


@dataclass(frozen=True, slots=True)
class AlphaPolicyConfig:
    """Every number the policy executes, and nothing the Admission Gate already owns.

    The absolute OI liquidity floor is absent on purpose: it is a universe/routability rule with one
    owner in Admission (#264). A policy re-checking it made the same number both an admission rule and
    an Alpha rule, which is why moving the canary floor from 20M to 5M moved neither.
    """

    # Not a preference: the frame must *prove* it was measured over this window, or the Case has no
    # basis for reading "10%" as a five-minute move.
    measurement_window_ms: int = 300_000
    # 5%, not the template's 10%: an operator throughput decision recorded above and in #273.
    min_oi_change_bps: int = 500
    # Strictly above. 5000 bps is exactly half and does not qualify.
    min_whale_oi_ratio_bps: int = 5_000
    # Strictly above. The provider's own `Whale Long Profit N%` percentage — not an account count and
    # not a dollar PnL.
    min_whale_long_profit_bps: int = 0
    # Direction confirmation only. Zero, and stated as zero rather than inherited from a shared band.
    min_price_move_bps: int = 0
    # Above the measured 6-12% loss bucket's floor. See the module docstring.
    max_price_move_bps: int = 1_000

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "max_price_move_bps": self.max_price_move_bps,
            "measurement_window_ms": self.measurement_window_ms,
            "min_oi_change_bps": self.min_oi_change_bps,
            "min_price_move_bps": self.min_price_move_bps,
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
            "min_whale_oi_ratio_bps": self.min_whale_oi_ratio_bps,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


@dataclass(frozen=True, slots=True)
class AlphaPolicy:
    """The production policy identity. One instance, code-owned, no registry and no lookup order."""

    config: AlphaPolicyConfig = AlphaPolicyConfig()
    policy_id: str = ALPHA_POLICY_ID
    policy_version: str = ALPHA_POLICY_ID

    @property
    def config_digest(self) -> str:
        return self.config.digest

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]:
        return self.config.snapshot

    def decide(self, context: FrozenPolicyContext) -> AlphaDecision:
        """Frozen numbers in, one named answer plus its evidence out. No clock, no read, no model.

        The order is the order an operator reads the template in — source, then the three conditions,
        then the price confirmation — so the reason a Case carries is the first thing about it that
        failed rather than whichever rule happened to be written first. Checks accumulate as they are
        executed, so a refusal carries the rules that passed before it as well as the one that did not.
        """

        config = self.config
        oi = context.oi
        checks: list[PolicyCheck] = []

        def record(check: str, operator: PolicyOperator, threshold: object, measured: object, passed: bool) -> bool:
            checks.append(
                PolicyCheck(
                    check=check,
                    operator=operator,
                    threshold=str(threshold),
                    measured=None if measured is None else str(measured),
                    passed=passed,
                )
            )
            return passed

        def refuse(rule: str) -> AlphaDecision:
            return AlphaDecision(
                decision="no_trade",
                rule=rule,
                setup="",
                invalidation="",
                checks=tuple(checks),
                policy_id=self.policy_id,
                policy_version=self.policy_version,
            )

        # The window first. Without it "10%" is a number with no interval attached, and every replay of
        # the Case would be a claim about a measurement nobody checked.
        if not record(
            "source_measurement_window_ms",
            "==",
            config.measurement_window_ms,
            oi.measurement_window_ms,
            oi.measurement_window_ms == config.measurement_window_ms,
        ):
            return refuse("source_window_mismatch")
        if not record("oi_direction", "==", "rise", oi.oi_direction, oi.oi_direction == "rise"):
            return refuse("not_oi_rise")
        if not record(
            "oi_change_bps",
            ">=",
            config.min_oi_change_bps,
            oi.oi_change_bps,
            oi.oi_change_bps >= config.min_oi_change_bps,
        ):
            return refuse("smart_money_oi_change_below_floor")
        if not record(
            "whale_oi_ratio_bps",
            ">",
            config.min_whale_oi_ratio_bps,
            oi.whale_oi_ratio_bps,
            oi.whale_oi_ratio_bps > config.min_whale_oi_ratio_bps,
        ):
            return refuse("smart_money_ratio_below_or_equal_floor")
        if not record(
            "whale_long_profit_bps",
            ">",
            config.min_whale_long_profit_bps,
            oi.whale_long_profit_bps,
            oi.whale_long_profit_bps > config.min_whale_long_profit_bps,
        ):
            return refuse("smart_money_profit_not_positive")

        pre_move = context.market.pre_move_bps
        # A missing pre-move is not "no confirmation", it is "no evidence", and both refuse. Admission
        # already deferred every frame with no candle at all, so reaching here with `None` means the
        # mark existed and the lookback end did not.
        if not record(
            "pre_move_bps",
            ">=",
            config.min_price_move_bps,
            pre_move,
            pre_move is not None and pre_move >= config.min_price_move_bps,
        ):
            return refuse("price_direction_not_confirmed")
        if not record(
            "pre_move_bps",
            "<=",
            config.max_price_move_bps,
            pre_move,
            pre_move is not None and pre_move <= config.max_price_move_bps,
        ):
            return refuse("move_above_band_chasing")

        # Proved by the two checks above: both refuse when `pre_move` is `None`.
        confirmed = int(pre_move or 0)
        return AlphaDecision(
            decision="long",
            rule="smart_money_momentum_long",
            setup=(
                f"{config.measurement_window_ms // 60_000}m 持仓上升 "
                f"{oi.oi_change_bps / 100:.2f}%，鲸鱼占比 {oi.whale_oi_ratio_bps / 100:.2f}%，"
                f"盈利指标 {oi.whale_long_profit_bps / 100:.2f}%，价格已确认方向 {confirmed / 100:.2f}%"
            ),
            invalidation="价格跌破冻结止损，或持仓时限到期",
            checks=tuple(checks),
            policy_id=self.policy_id,
            policy_version=self.policy_version,
        )


ALPHA_POLICY: Final = AlphaPolicy()

__all__ = [
    "ALPHA_POLICY",
    "ALPHA_POLICY_ID",
    "AlphaPolicy",
    "AlphaPolicyConfig",
]
