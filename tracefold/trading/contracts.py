"""Trading domain models: the typed vocabulary the whole bounded context shares.

Everything here is pure data. No provider payload, no credential, no database handle, no clock. The
package depends on `platform` and third-party libraries only, so these shapes are also the seam the
composition root converts News projections into.

Two conventions carried over from News on purpose:

* thresholds are integer basis points, so a stored number and the comparison against it cannot
  disagree because of a float;
* money and quantities are `Decimal`, never `float`.

**The #433 vocabulary.** One word, one meaning, and the writer and every read surface use the same
one:

    Source        a persisted, citable provider-native OI market fact.
    Admission     the durable Gate answer taken *before* a Case exists.
    Case          a frozen candidate that passed live Admission and may run the Alpha policy.
    Decision      the one terminal business answer about a Case: NO_TRADE, BLOCKED, SIGNAL_EMITTED.
    Signal        the immutable, engine-neutral Alpha conclusion handed to a Runtime.

`CaseState` is the whole closed vocabulary, and `trading_cases_state_check` admits exactly it.
`CURRENT_TERMINAL_STATES` is the subset a decision may land on, and `settle_case` refuses anything else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bumped whenever the manifest layout or pure policy changes shape: a Case frozen under one version is
# not comparable with a Case frozen under another. A pending Case from an earlier version cannot
# validate against this layout and is `BLOCKED / manifest_invalid` on its next claim.
TRADING_MANIFEST_VERSION: Final = "trading_manifest_v11"
# The `execution_strategy` every execution-stream row is keyed on, spelled once: a second spelling
# elsewhere is a predicate that selects nothing, silently. It lives in Trading rather than beside the
# runtime because the ledger column is Trading's, and `tracefold.trading.storage` cannot import from
# `tracefold.app`.
EXECUTION_STRATEGY_ID: Final = "oi_nautilus_v1"
# Code-owned persistence timing shared by the Signal lane.
TRADING_COLD_WRITE_TIMEOUT_SECONDS = 10.0


# No News identity of any kind: the measurements, the two clocks and the venue are the fact, and every
# admission rule is about those. Pinning an upstream version here would make an upstream bump a Trading
# edit; what upstream calls its judge is upstream's business.

# ---------------------------------------------------------------------------- upstream input rows
# What the composition root must hand this context to produce candidates. Trading owns these because
# they are *its* requirements, not News's SELECT lists: News may add a column, rename one, or publish a
# second projection without this file moving, and the App-side mapper is where the two meet.
#
# They are `TypedDict`s rather than validating models on purpose. Source normalization fails closed on
# a named rejection for every value it cannot use — a missing clock, an unknown direction — and a
# model that raised on the same row would turn a counted admission answer into an exception nothing
# durable sees. Deliberately loose where the source is loose: `venue` is provider text that may be
# absent.


def oi_source_key(event_id: object, metric_version: object) -> str:
    """The deterministic OI lane's source identity, from either a raw row or a typed candidate.

    A row rejected at the source stage never becomes an `OiTradeCandidate`, and its admission decision
    still has to be filed under the same key the Case would have used — so the construction lives here
    rather than only on the model.
    """

    return f"oi:{event_id}:{metric_version}"


class OiCandidateRow(TypedDict):
    """One parsed deterministic OI telemetry fact offered to the Signal lane.

    Sixteen keys, all of them a property of the measured frame: what moved, by how much, on which
    venue, when it was observed, when it became durable, and what the provider proves about the
    interval it measured. Nothing here names a judge, a Program, a policy, a cohort or a decision.
    """

    event_id: str
    metric_version: str
    source_item_id: str
    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    observed_at_ms: int
    # When the upstream fact became durable, and therefore the earliest instant this lane could have
    # read it. It is the Case's `persisted_at_ms`.
    available_at_ms: int
    ingest_mode: str
    # What the provider proves about the measurement (#265). Nullable together; `None` means unproven.
    source_strategy_id: str | None
    source_contract_version: str | None
    measurement_window_ms: int | None
    venue: str | None


# One live trigger kind. The column keeps its name and its historical `news` / `liquidation` values;
# the writer only ever produces `oi`.
TriggerKind = Literal["oi"]
PolicyDecision = Literal["no_trade", "long"]


class CaseState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    NO_TRADE = "NO_TRADE"
    SIGNAL_EMITTED = "SIGNAL_EMITTED"
    BLOCKED = "BLOCKED"


# A claim moves a Case through `PENDING` and `RUNNING`; only these three are terminal.
CURRENT_TERMINAL_STATES: Final[frozenset[CaseState]] = frozenset(
    {CaseState.NO_TRADE, CaseState.SIGNAL_EMITTED, CaseState.BLOCKED}
)

# Decision could not safely run; Policy stays `not_run` and no Signal is emitted. `source_stale` is
# the only clock here: a second budget measured from the Case's own creation could not expire before
# it at any `max_age_ms` an operator would set, so it never decided anything (#537 PR-3).
DecisionBlockReason = Literal[
    "manifest_invalid",
    "policy_identity_retired",
    "source_stale",
]


def canonical_base_symbol(value: object) -> str:
    """The one place a provider spelling becomes an underlying identity.

    Provider coin tags carry an `XYZ-` prefix for the same instrument, exactly as the Gate strips it.
    Doing this before the blacklist lookup is what lets one `CL` row block `CL` and `XYZ-CL` without
    the operator enumerating spellings.
    """

    return str(value or "").strip().upper().removeprefix("XYZ-")


def underlying_key(base_symbol: object) -> str:
    """Venue-independent identity. One issuer, one bucket, on whichever book it is listed.

    The single owner of the construction: nothing else assembles a `crypto:` key, or the copies drift
    apart one canonicalisation rule at a time.
    """

    canonical = canonical_base_symbol(base_symbol)
    return f"crypto:{canonical}" if canonical else ""


def canonical_sha256(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    """Content address for a frozen manifest or an order payload. Sorted keys, no whitespace drift."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bar(_Frozen):
    """One closed interval from a public venue REST catalogue.

    Trading keeps its own shape rather than importing `tracefold.news.market_review.pricing.Candle`: the
    dependency rule is `trading -> platform`, and the composition root converts. `close_at_ms` is the
    exclusive end.
    """

    open_at_ms: int
    close_at_ms: int
    close: Decimal


class OiTradeCandidate(_Frozen):
    """The public projection of one deterministic OI telemetry fact, frozen into a Case."""

    event_id: str
    metric_version: str
    # The Item the parser read. Provenance the operator can follow upstream, and never a rule.
    source_item_id: str
    observed_at_ms: int
    # When the upstream fact became durable, as opposed to when the frame was observed. The two are
    # separate stages and the gap between them is the one latency Trading does not own (#211).
    available_at_ms: int
    base_symbol: str
    venue: str

    oi_direction: Literal["rise", "fall"]
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int

    # The provider's own measurement contract, frozen into the manifest so a Case is a claim about a
    # *specific* interval rather than about "OI rose". `None` means the interval could not be proven —
    # the frame is still a usable fact, and the policy refuses it by name (#265).
    source_strategy_id: str | None = None
    source_contract_version: str | None = None
    measurement_window_ms: int | None = None

    @property
    def source_key(self) -> str:
        return oi_source_key(self.event_id, self.metric_version)

    @property
    def underlying_key(self) -> str:
        return underlying_key(self.base_symbol)


PolicyOperator = Literal[">=", ">", "<=", "<", "==", "!="]


class PolicyCheck(_Frozen):
    """One condition the Alpha policy executed, frozen with everything needed to re-read it.

    The threshold, the measured value and the operator all travel with the Case (#331). A console that
    holds only today's configuration cannot explain yesterday's Case — it reads a floor that has since
    moved and prints a conflict on a row that passed — so the evidence is written down at decision time
    and every surface renders what is stored.
    """

    check: str
    operator: PolicyOperator
    threshold: str
    measured: str | None
    passed: bool


class AlphaDecision(_Frozen):
    """The pure Alpha policy's whole answer: LONG or NO_TRADE with frozen evidence.

    It contains no permission, execution environment, venue route, account, size, leverage, or order
    instruction.  A Runtime may independently accept or reject the resulting Signal.
    """

    decision: PolicyDecision
    rule: str
    setup: str
    invalidation: str
    checks: tuple[PolicyCheck, ...]
    policy_id: str
    # Required, and always the deciding policy's own version — never a separate default, or one row
    # carries two identities on the surface whose whole job is being readable against the identity that
    # decided it.
    policy_version: str

    def evidence(self) -> dict[str, Any]:
        """The document persisted beside the Case, and the one the read model renders."""

        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "decision": self.decision,
            "rule": self.rule,
            "setup": self.setup,
            "invalidation": self.invalidation,
            "checks": [check.model_dump(mode="json") for check in self.checks],
        }


class OiMarketTrigger(_Frozen):
    """One frozen trigger identity: which fact, when it happened, when it could first be read."""

    kind: Literal["oi"] = "oi"
    source_key: str
    observed_at_ms: int
    # The source's own `available_at_ms`, never a downstream pipeline's insert stamp: the Case's second
    # clock is a property of the fact.
    persisted_at_ms: int
    venue: str


class FrozenMarketContext(_Frozen):
    """The price window the Case was frozen against. Nothing later than `observed_at_ms` entered it."""

    mark_price: Decimal = Field(gt=0)
    observed_at_ms: int
    pre_move_bps: int | None
    pre_move_lookback_ms: int = Field(gt=0)


class FrozenPolicyContext(_Frozen):
    """Only point-in-time facts visible at the trigger's cutoff, and only what the policy reads."""

    oi: OiTradeCandidate
    market: FrozenMarketContext


class TradingCaseManifest(_Frozen):
    """The frozen, content-addressed input to one decision. Nothing later than `cutoff_ms` may enter."""

    manifest_version: Literal["trading_manifest_v11"] = TRADING_MANIFEST_VERSION
    primary_trigger: OiMarketTrigger
    contexts: FrozenPolicyContext
    policy_id: str
    policy_version: str
    policy_config: dict[str, bool | int | str]
    policy_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    underlying_key: str
    base_symbol: str
    cutoff_ms: int
    market_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")

    @model_validator(mode="after")
    def _config_digest_matches_snapshot(self) -> TradingCaseManifest:
        if canonical_sha256(self.policy_config) != self.policy_config_digest:
            raise ValueError("trading_policy_config_digest_mismatch")
        return self

    @property
    def trigger_kind(self) -> TriggerKind:
        return "oi"

    @property
    def oi(self) -> OiTradeCandidate:
        return self.contexts.oi

    @property
    def mark_price(self) -> Decimal:
        return self.contexts.market.mark_price

    @property
    def pre_move_bps(self) -> int | None:
        return self.contexts.market.pre_move_bps

    def digest(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


__all__ = [
    "CURRENT_TERMINAL_STATES",
    "EXECUTION_STRATEGY_ID",
    "TRADING_COLD_WRITE_TIMEOUT_SECONDS",
    "TRADING_MANIFEST_VERSION",
    "AlphaDecision",
    "Bar",
    "CaseState",
    "DecisionBlockReason",
    "FrozenMarketContext",
    "FrozenPolicyContext",
    "OiCandidateRow",
    "OiMarketTrigger",
    "OiTradeCandidate",
    "PolicyCheck",
    "PolicyDecision",
    "PolicyOperator",
    "TradingCaseManifest",
    "TriggerKind",
    "canonical_base_symbol",
    "canonical_sha256",
    "oi_source_key",
    "underlying_key",
]
