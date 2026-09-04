"""The one production Alpha policy: `source_native_oi_smart_money_long_v5`.

Pure, deterministic, long-only, and the whole of what decides a Case. It answers `long` or `no_trade`
and nothing else — no permission, no execution environment, no venue. The result is an engine-neutral
Signal; execution authority belongs to a later Runtime boundary.

A Case decided under an earlier policy id is never re-executed under this code: the id is the claim
about which rules decided a row, so a changed rulebook takes a new id rather than reusing one.

There is no `allow_short`, pinned or otherwise: a `fall` frame is refused by `not_oi_rise` before any
side exists and no other path produces one, so the key's only effect would be on the digest.

The template this implements is two conditions on one frame:

    5-minute OI rise  >= 5%
    smart-money / OI  >  50%

plus a confirmed price direction and a chasing ceiling above it.

**The template's third condition is gone, and its absence is the v5 identity.** `whale_long_profit_bps
> 0` passed on 310 of 310 admitted frames — the provider's own floor for the metric is far above zero
— so it added a key to the digest and a check to every Case and refused nothing (#537 PR-3). The
measurement itself is still frozen on the Case and still rendered: it is data about the frame, and
deleting a rule that never fires does not delete what it read.

**The OI floor is 5% by operator decision, not by measurement.** The template's own number is 10%,
which starves this lane. Lowering it bought throughput for a lane whose purpose is receipts; it did
not buy evidence.

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

ALPHA_POLICY_ID: Final = "source_native_oi_smart_money_long_v5"


@dataclass(frozen=True, slots=True)
class AlphaPolicyConfig:
    """Every number the policy executes, and nothing the Admission Gate already owns.

    The absolute OI liquidity floor is absent on purpose: it is a universe rule with one owner in
    Admission. A policy re-checking it would make the same number both an admission rule and an Alpha
    rule, so neither answer would be the reason a frame was refused.
    """

    # Not a preference: the frame must *prove* it was measured over this window, or the Case has no
    # basis for reading "10%" as a five-minute move.
    measurement_window_ms: int = 300_000
    # 5%, not the template's 10%: an operator throughput decision, recorded above.
    min_oi_change_bps: int = 500
    # Strictly above. 5000 bps is exactly half and does not qualify.
    min_whale_oi_ratio_bps: int = 5_000
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

        The order is the order an operator reads the template in — source, then the two conditions,
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
            # Only the rules that decided it. The frame's other measurements stay on the manifest,
            # where a reader can see them without the sentence claiming they were conditions.
            setup=(
                f"{config.measurement_window_ms // 60_000}m 持仓上升 "
                f"{oi.oi_change_bps / 100:.2f}%，鲸鱼占比 {oi.whale_oi_ratio_bps / 100:.2f}%，"
                f"价格已确认方向 {confirmed / 100:.2f}%"
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
