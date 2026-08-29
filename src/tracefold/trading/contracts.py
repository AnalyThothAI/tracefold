"""Trading domain models: the typed vocabulary the whole bounded context shares.

Everything here is pure data. No provider payload, no credential, no database handle, no clock. The
package depends on `platform` and third-party libraries only, so these shapes are also the seam the
composition root converts News projections into.

Two conventions carried over from News on purpose:

* thresholds are integer basis points, so a stored number and the comparison against it cannot
  disagree because of a float;
* money and quantities are `Decimal`, never `float`.

**The #331 vocabulary.** One word, one meaning, and the writer and every read surface use the same
one:

    Source        a persisted, citable market fact. The live trigger is a Binance OI frame and
                  nothing else.
    Admission     the durable Gate answer taken *before* a Case exists.
    RESEARCH_ONLY a legitimate research source with no live capital authority (Hyperliquid).
    Case          a frozen candidate that passed live Admission and may run the capital policy.
    Decision      the one terminal business answer about a Case: NO_TRADE, BLOCKED, INTENT_EMITTED.
    Intent        the immutable capital request handed to Nautilus. Not an order.
    Outcome       the durable result of execution and the position lifecycle.

`POLICY_REJECTED` and `ORDER_PREPARED` survive here for one reason only: production holds rows in
both states and they must stay readable. `CURRENT_TERMINAL_STATES` is what the writer may reach, and
`settle_case` refuses anything outside it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bumped whenever the manifest layout or the pure policy changes shape: a Case frozen under one
# version is not comparable with a Case frozen under another. v8 is #350's no-key hard cut: a Case
# pins the credential-free public catalogue it resolved from, never an execution capability that
# requires a configured binding and belongs to #355.
TRADING_MANIFEST_VERSION: Final = "trading_manifest_v8"
# Code-owned execution timing shared by the capital lane and the one-attempt protocol.
TRADING_COLD_WRITE_TIMEOUT_SECONDS = 10.0

# No `NewsLearningEpoch` literal (#314). Trading pins the two upstream contracts it actually reasons
# about — `program_version` and `policy_version` — and a News epoch label is neither: it names *when* a
# cohort opened, which News owns and re-derives per deployment. Pinning it here made every News identity
# move edit this file to restate a fact the two version pins already carried.

# ---------------------------------------------------------------------------- upstream input rows
# What the composition root must hand this context to produce candidates. Trading owns these because
# they are *its* requirements, not News's SELECT lists: News may add a column, rename one, or publish a
# second projection without this file moving, and the App-side mapper is where the two meet.
#
# They are `TypedDict`s rather than validating models on purpose. Source normalization fails closed on
# a named rejection for every value it cannot use — an unparseable rank, an unknown direction — and a
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
    """One parsed deterministic OI telemetry fact offered to the capital lane."""

    event_id: str
    verdict_created_at_ms: int
    # The reader's own judgment of this frame, and the named rule behind it. Audit, not admission: since
    # #264 the Gate decides whether the fact may trigger, and a reader policy change must not silently
    # open or close the capital lane.
    final_decision: str
    source_rule: str | None
    # What the provider proves about the measurement (#265). Nullable together; `None` means unproven.
    source_strategy_id: str | None
    source_contract_version: str | None
    measurement_window_ms: int | None
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    editorial_origin: str
    editorial_sha256: str
    scored_judgment_sha256: str
    runtime_manifest_sha: str
    metric_version: str
    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int
    observed_at_ms: int
    ingest_mode: str
    venue: str | None


class InstrumentCandidateRow(TypedDict):
    """One catalogue row offered to the research venue resolver.

    Live routing does **not** read this (#331). The active execution capability snapshot is the live
    instrument universe, and resolving from a second catalogue is how a Case came to be frozen against
    an instrument the Intent writer would later refuse. Replay still resolves both venues from here,
    because research is allowed to look at books this lane may not trade.
    """

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


ControlState = Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
# One live trigger kind. The column keeps its name and its historical `news` / `liquidation` values;
# the writer only ever produces `oi`.
TriggerKind = Literal["oi"]
ExchangeId = Literal["binance", "hyperliquid"]
# The one venue that may carry live capital. Hyperliquid is `RESEARCH_ONLY`: a legitimate source of
# facts with no capital authority, and the Gate says so before a Case exists rather than letting the
# Intent writer discover it four stages later.
LIVE_EXCHANGE_ID: Final[ExchangeId] = "binance"
LIVE_VENUE: Final = "binance.perp"
PolicyDecision = Literal["no_trade", "long"]


def utc_day_key(now_ms: int) -> str:
    """Stable UTC budget key derived from an injected timestamp."""

    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


class CaseState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    NO_TRADE = "NO_TRADE"
    INTENT_EMITTED = "INTENT_EMITTED"
    BLOCKED = "BLOCKED"
    # Read-only history. `POLICY_REJECTED` was the old writer's word for both "the policy said no" and
    # "the lane could not decide"; `ORDER_PREPARED` belonged to the retired Paper/OpenTrade writer.
    # Production holds a few hundred rows in the first and two in the second, so both stay readable —
    # and neither is reachable from `CURRENT_TERMINAL_STATES`.
    POLICY_REJECTED = "POLICY_REJECTED"
    ORDER_PREPARED = "ORDER_PREPARED"


# Exactly two answers the #350 writer may reach. `INTENT_EMITTED` remains readable history but is
# deliberately absent until #360 owns reservation + Intent in one transaction.
CURRENT_TERMINAL_STATES: Final[frozenset[CaseState]] = frozenset({CaseState.NO_TRADE, CaseState.BLOCKED})

# Decision could not safely run; Policy stays `not_run` and Capital is not applicable.
DecisionBlockReason = Literal[
    "case_stale",
    "manifest_invalid",
    "policy_identity_retired",
    "source_generation_retired",
]

# Policy answered LONG, but independent capital authority refused it. #360 will extend this closed
# vocabulary with grant, arm and risk reasons while keeping Policy attribution unchanged.
CapitalBlockReason = Literal[
    "capital_paused",
    "capital_close_only",
    "credentials_unconfigured",
    "credentials_invalid",
    "catalog_mismatch",
    "catalog_stale",
    "unexpected_exposure",
    "binding_unready",
    "promotion_authority_unavailable",
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

    The single owner of the construction (#331 §3). It was being hand-assembled as `'crypto:' || x` in
    the capability builder, in SQL and on the candidate at once, so the three could drift apart one
    canonicalisation rule at a time.
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


class InstrumentRef(_Frozen):
    """One exactly-resolved contract. `(exchange_id, provider_symbol)` is the execution identity.

    `base_symbol` is a join hint and never an order field: two venues spell the same underlying
    differently, and a display symbol has never been safe to submit.
    """

    exchange_id: ExchangeId
    venue: str
    provider_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None = None
    observed_at_ms: int


class OiTradeCandidate(_Frozen):
    """The public projection of one deterministic telemetry verdict plus its rank-ledger row."""

    event_id: str
    observed_at_ms: int
    # When the deterministic verdict became durable, as opposed to when the frame was observed. The
    # two are separate stages and the gap between them is the one latency Trading does not own (#211).
    verdict_created_at_ms: int
    base_symbol: str
    venue: str

    oi_direction: Literal["rise", "fall"]
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int

    # The reader's verdict on the same frame, frozen into the manifest so a capital decision can be read
    # beside the judgment that accompanied it. Deliberately `str` rather than a `Literal`: it is no longer
    # an admission rule, and pinning the reader's decision vocabulary here would turn a News policy change
    # into a Trading validation failure — the exact coupling #264 removes.
    final_decision: str
    source_rule: str
    metric_version: str
    # The provider's own measurement contract, frozen into the manifest so a Case is a claim about a
    # *specific* interval rather than about "OI rose". `None` means the interval could not be proven —
    # the frame is still a usable fact, and the policy refuses it by name (#265).
    source_strategy_id: str | None = None
    source_contract_version: str | None = None
    measurement_window_ms: int | None = None
    learning_epoch: str = Field(min_length=1, max_length=64)
    program_version: Literal["news_oi_signal_v1"]
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Literal["news_triage_policy_v10"]
    editorial_origin: Literal["telemetry_deterministic"]
    editorial_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_judgment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def source_key(self) -> str:
        return oi_source_key(self.event_id, self.metric_version)

    @property
    def underlying_key(self) -> str:
        return underlying_key(self.base_symbol)


PolicyOperator = Literal[">=", ">", "<=", "<", "==", "!="]


class PolicyCheck(_Frozen):
    """One condition the capital policy executed, frozen with everything needed to re-read it.

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


class CapitalDecision(_Frozen):
    """The pure policy's whole answer. LONG or NO_TRADE, and the evidence for it.

    No permission, no execution environment, no venue. Capital authority belongs to the lane and to the
    durable capability snapshot; a strategy string was never the place to keep it.
    """

    decision: PolicyDecision
    rule: str
    setup: str
    invalidation: str
    checks: tuple[PolicyCheck, ...]
    policy_id: str
    # Required, and always the deciding policy's own version. It defaulted to a separate
    # `trading_capital_policy_v2` constant, so one Case row said `binance_oi_smart_money_long_v2` in
    # `strategy_version` and something else in `policy_checks.policy_version` — two names for one
    # version in one row, on the surface whose whole job is being readable against the exact identity
    # that decided it.
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
    kind: Literal["oi"] = "oi"
    source_key: str
    observed_at_ms: int
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

    manifest_version: Literal["trading_manifest_v8"] = TRADING_MANIFEST_VERSION
    primary_trigger: OiMarketTrigger
    contexts: FrozenPolicyContext
    policy_id: str
    policy_version: str
    policy_config: dict[str, bool | int | str]
    policy_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    underlying_key: str
    base_symbol: str
    cutoff_ms: int
    instrument: InstrumentRef
    # Public instrument truth only. #355 later compiles execution capability from this catalogue and
    # a closed binding; the Decision Plane may freeze and run policy without either credential.
    venue_catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

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
    "LIVE_EXCHANGE_ID",
    "LIVE_VENUE",
    "TRADING_COLD_WRITE_TIMEOUT_SECONDS",
    "TRADING_MANIFEST_VERSION",
    "Bar",
    "CapitalBlockReason",
    "CapitalDecision",
    "CaseState",
    "ControlState",
    "DecisionBlockReason",
    "ExchangeId",
    "FrozenMarketContext",
    "FrozenPolicyContext",
    "InstrumentCandidateRow",
    "InstrumentRef",
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
    "utc_day_key",
]
