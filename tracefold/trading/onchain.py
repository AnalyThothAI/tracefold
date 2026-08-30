"""Provider-neutral onchain asset resolution and route analysis.

Ticker strings are only search seeds.  Material asset identity is always an EVM chain ID plus a
canonical contract address, and route analysis compares quotes for one exact identity and amount.
This module owns no provider I/O and no wallet authority.
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OnchainProvider = Literal["okx", "oneinch", "binance"]
OnchainDiscoveryProvider = Literal["okx", "oneinch", "binance", "dexscreener"]

_CONTRACT_RE = re.compile(r"0x[0-9a-f]{40}")
_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,19}")


class OnchainProviderUnavailable(RuntimeError):
    """A stable provider capability failure without provider response text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_contract(value: object) -> str:
    normalized = str(value).strip().lower()
    if _CONTRACT_RE.fullmatch(normalized) is None:
        raise ValueError("onchain_contract_address_invalid")
    return normalized


def _canonical_symbol(value: object) -> str:
    normalized = str(value).strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError("onchain_symbol_invalid")
    return normalized


def canonical_onchain_asset_seed(value: object) -> str:
    """Canonicalize one exact TG target as either an EVM CA or ticker search seed."""

    normalized = str(value).strip()
    if _CONTRACT_RE.fullmatch(normalized.lower()) is not None:
        return normalized.lower()
    return _canonical_symbol(normalized)


class OnchainNewsSource(BaseModel):
    """One ticker or CA exactly as displayed on a sent Telegram News card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_event_id: Annotated[str, Field(min_length=1, max_length=160)]
    delivery_target_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    delivery_message_id: Annotated[int, Field(gt=0)]
    headline_zh: Annotated[str, Field(min_length=1, max_length=500)]
    ticker: str
    source_observed_at_ms: Annotated[int, Field(gt=0)]

    _normalize_ticker = field_validator("ticker", mode="before")(canonical_onchain_asset_seed)


class OnchainProviderToken(BaseModel):
    """One bounded token-directory observation from a provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: OnchainDiscoveryProvider
    chain_id: Annotated[int, Field(gt=0)]
    chain_name: Annotated[str, Field(min_length=1, max_length=40)]
    contract_address: str
    symbol: str
    name: Annotated[str, Field(min_length=1, max_length=120)]
    decimals: Annotated[int, Field(ge=0, le=255)]
    verified: bool = False
    liquidity_usd: Decimal | None = None
    pair_count: Annotated[int, Field(ge=0, le=10_000)] = 0

    _normalize_contract = field_validator("contract_address", mode="before")(_canonical_contract)
    _normalize_symbol = field_validator("symbol", mode="before")(_canonical_symbol)

    @field_validator("liquidity_usd", mode="before")
    @classmethod
    def parse_liquidity_usd(cls, value: object) -> Decimal | None:
        return _optional_nonnegative_decimal(value)


class OnchainAssetCandidate(BaseModel):
    """Resolved candidate identity presented to the operator before quote collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chain_id: Annotated[int, Field(gt=0)]
    chain_name: Annotated[str, Field(min_length=1, max_length=40)]
    contract_address: str
    symbol: str
    name: Annotated[str, Field(min_length=1, max_length=120)]
    decimals: Annotated[int, Field(ge=0, le=255)]
    providers: tuple[OnchainDiscoveryProvider, ...]
    verified: bool
    confidence_bps: Annotated[int, Field(ge=0, le=10_000)]
    liquidity_usd: Decimal | None = None
    pair_count: Annotated[int, Field(ge=0, le=10_000)] = 0

    _normalize_contract = field_validator("contract_address", mode="before")(_canonical_contract)
    _normalize_symbol = field_validator("symbol", mode="before")(_canonical_symbol)

    @field_validator("providers")
    @classmethod
    def validate_providers(
        cls,
        value: tuple[OnchainDiscoveryProvider, ...],
    ) -> tuple[OnchainDiscoveryProvider, ...]:
        if not value or len(value) > 4 or len(set(value)) != len(value):
            raise ValueError("onchain_candidate_providers_invalid")
        order = {"okx": 0, "oneinch": 1, "binance": 2, "dexscreener": 3}
        return tuple(sorted(value, key=order.__getitem__))

    @field_validator("liquidity_usd", mode="before")
    @classmethod
    def parse_liquidity_usd(cls, value: object) -> Decimal | None:
        return _optional_nonnegative_decimal(value)

    @property
    def identity(self) -> tuple[int, str]:
        return self.chain_id, self.contract_address


class OnchainQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chain_id: Annotated[int, Field(gt=0)]
    input_contract: str
    output_contract: str
    input_amount_raw: Annotated[int, Field(gt=0)]
    slippage_bps: Annotated[int, Field(gt=0, le=5_000)] = 100

    _normalize_input = field_validator("input_contract", mode="before")(_canonical_contract)
    _normalize_output = field_validator("output_contract", mode="before")(_canonical_contract)

    @model_validator(mode="after")
    def require_distinct_assets(self) -> OnchainQuoteRequest:
        if self.input_contract == self.output_contract:
            raise ValueError("onchain_quote_assets_identical")
        return self


def _optional_nonnegative_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("onchain_quote_decimal_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("onchain_quote_decimal_invalid")
    return parsed


class OnchainRouteQuote(BaseModel):
    """Normalized read-only quote; it intentionally carries no transaction calldata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_version: Literal["onchain_route_quote_v1"] = "onchain_route_quote_v1"
    provider: OnchainProvider
    chain_id: Annotated[int, Field(gt=0)]
    input_contract: str
    output_contract: str
    input_amount_raw: Annotated[int, Field(gt=0)]
    expected_output_raw: Annotated[int, Field(gt=0)]
    minimum_output_raw: Annotated[int, Field(gt=0)] | None = None
    expected_output_usd: Decimal | None = None
    provider_fee_usd: Decimal | None = None
    gas_fee_usd: Decimal | None = None
    gas_limit: Annotated[int, Field(gt=0)] | None = None
    price_impact_bps: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    slippage_bps: Annotated[int, Field(gt=0, le=5_000)]
    route_labels: tuple[str, ...] = ()
    latency_ms: Annotated[int, Field(ge=0, le=60_000)]
    received_at_ms: Annotated[int, Field(gt=0)]
    expires_at_ms: Annotated[int, Field(gt=0)] | None = None
    simulation_passed: bool | None = None
    risk_checked: bool = False
    risk_blocked: bool = False

    _normalize_input = field_validator("input_contract", mode="before")(_canonical_contract)
    _normalize_output = field_validator("output_contract", mode="before")(_canonical_contract)
    _normalize_usd = field_validator("expected_output_usd", "provider_fee_usd", "gas_fee_usd", mode="before")(
        _optional_nonnegative_decimal
    )

    @field_validator("route_labels")
    @classmethod
    def validate_route_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(label).strip()[:80] for label in value if str(label).strip()))
        if len(normalized) > 12:
            raise ValueError("onchain_quote_route_labels_invalid")
        return normalized

    @model_validator(mode="after")
    def validate_shape(self) -> OnchainRouteQuote:
        if self.input_contract == self.output_contract:
            raise ValueError("onchain_quote_assets_identical")
        if self.minimum_output_raw is not None and self.minimum_output_raw > self.expected_output_raw:
            raise ValueError("onchain_quote_minimum_output_invalid")
        if self.expires_at_ms is not None and self.expires_at_ms < self.received_at_ms:
            raise ValueError("onchain_quote_expiry_invalid")
        if self.risk_blocked and not self.risk_checked:
            raise ValueError("onchain_quote_risk_state_invalid")
        return self

    @property
    def complete_for_definitive_ranking(self) -> bool:
        return bool(
            self.minimum_output_raw is not None
            and self.expected_output_usd is not None
            and self.provider_fee_usd is not None
            and self.gas_fee_usd is not None
            and self.simulation_passed is True
            and self.risk_checked
        )

    @property
    def net_receive_usd(self) -> Decimal | None:
        if self.expected_output_usd is None or self.provider_fee_usd is None or self.gas_fee_usd is None:
            return None
        return self.expected_output_usd - self.provider_fee_usd - self.gas_fee_usd


class RouteAnalysisState(StrEnum):
    DEFINITIVE = "definitive"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


class OnchainRouteAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_version: Literal["onchain_route_analysis_v1"] = "onchain_route_analysis_v1"
    state: RouteAnalysisState
    winner_provider: OnchainProvider | None
    winner_net_receive_usd: Decimal | None
    eligible_quotes: tuple[OnchainRouteQuote, ...]
    rejected_providers: tuple[OnchainProvider, ...]
    reason_codes: tuple[str, ...]
    analyzed_at_ms: Annotated[int, Field(gt=0)]


class OnchainAnalysisState(StrEnum):
    AWAITING_TICKER = "AWAITING_TICKER"
    RESOLVING = "RESOLVING"
    AWAITING_CONTRACT = "AWAITING_CONTRACT"
    QUOTING = "QUOTING"
    ANALYZED = "ANALYZED"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"


class OnchainInteractionReplyState(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    AMBIGUOUS = "AMBIGUOUS"


class OnchainTelegramEditState(StrEnum):
    SENDING = "SENDING"
    SENT = "SENT"
    AMBIGUOUS = "AMBIGUOUS"


class OnchainTelegramEditPayload(BaseModel):
    """Exact idempotent Telegram edit desired by one callback update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: Annotated[int, Field(gt=0)]
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    keyboard: tuple[tuple[str, str], ...] = ()

    @field_validator("keyboard")
    @classmethod
    def validate_keyboard(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        if len(value) > 8:
            raise ValueError("onchain_telegram_edit_keyboard_invalid")
        for label, callback in value:
            if not label.strip() or len(label) > 80 or not callback or len(callback.encode()) > 64:
                raise ValueError("onchain_telegram_edit_keyboard_invalid")
        return value


class OnchainTelegramEditEffect(BaseModel):
    """Durable effect fence for an idempotent editMessageText call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")]
    update_id: Annotated[int, Field(ge=0)]
    payload: OnchainTelegramEditPayload
    result_code: Annotated[str, Field(min_length=1, max_length=100)]
    state: OnchainTelegramEditState
    error_code: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    attempted_at_ms: Annotated[int, Field(gt=0)]
    settled_at_ms: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def validate_effect_shape(self) -> OnchainTelegramEditEffect:
        if self.state is OnchainTelegramEditState.SENDING:
            valid = self.settled_at_ms is None and self.error_code is None
        elif self.state is OnchainTelegramEditState.SENT:
            valid = self.settled_at_ms is not None and self.error_code is None
        else:
            valid = self.settled_at_ms is not None and self.error_code is not None
        if not valid or (self.settled_at_ms is not None and self.settled_at_ms < self.attempted_at_ms):
            raise ValueError("onchain_telegram_edit_effect_invalid")
        return self


class OnchainAnalysisSession(BaseModel):
    """Durable Telegram interaction state, separate from all futures session and intent tables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")]
    sources: tuple[OnchainNewsSource, ...]
    actor_user_id: Annotated[int, Field(gt=0)]
    chat_id: int
    source_message_id: Annotated[int, Field(gt=0)]
    interaction_message_id: Annotated[int, Field(gt=0)] | None = None
    interaction_reply_attempted_at_ms: Annotated[int, Field(gt=0)] | None = None
    interaction_reply_state: OnchainInteractionReplyState = OnchainInteractionReplyState.PENDING
    interaction_reply_error_code: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    state: OnchainAnalysisState
    selected_ticker: str | None = None
    candidates: tuple[OnchainAssetCandidate, ...] = ()
    selected_candidate: OnchainAssetCandidate | None = None
    analysis: OnchainRouteAnalysis | None = None
    provider_errors: tuple[str, ...] = ()
    created_at_ms: Annotated[int, Field(gt=0)]
    updated_at_ms: Annotated[int, Field(gt=0)]

    _normalize_selected_ticker = field_validator("selected_ticker", mode="before")(
        lambda value: None if value is None else canonical_onchain_asset_seed(value)
    )

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, value: tuple[OnchainNewsSource, ...]) -> tuple[OnchainNewsSource, ...]:
        if not 1 <= len(value) <= 4:
            raise ValueError("onchain_session_sources_invalid")
        if len({source.ticker for source in value}) != len(value):
            raise ValueError("onchain_session_sources_invalid")
        return value

    @field_validator("provider_errors")
    @classmethod
    def validate_provider_errors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(code).strip() for code in value if str(code).strip()))
        if len(normalized) > 6 or any(len(code) > 100 for code in normalized):
            raise ValueError("onchain_session_provider_errors_invalid")
        return normalized

    @model_validator(mode="after")
    def validate_session_shape(self) -> OnchainAnalysisSession:
        tickers = {source.ticker for source in self.sources}
        if self.selected_ticker is not None and self.selected_ticker not in tickers:
            raise ValueError("onchain_session_ticker_not_displayed")
        if self.selected_candidate is not None and self.selected_candidate not in self.candidates:
            raise ValueError("onchain_session_candidate_not_resolved")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("onchain_session_time_invalid")
        reply_shape = {
            OnchainInteractionReplyState.PENDING: (
                self.interaction_reply_attempted_at_ms is None
                and self.interaction_message_id is None
                and self.interaction_reply_error_code is None
            ),
            OnchainInteractionReplyState.SENDING: (
                self.interaction_reply_attempted_at_ms is not None
                and self.interaction_message_id is None
                and self.interaction_reply_error_code is None
            ),
            OnchainInteractionReplyState.SENT: (
                self.interaction_reply_attempted_at_ms is not None
                and self.interaction_message_id is not None
                and self.interaction_reply_error_code is None
            ),
            OnchainInteractionReplyState.AMBIGUOUS: (
                self.interaction_reply_attempted_at_ms is not None
                and self.interaction_message_id is None
                and self.interaction_reply_error_code is not None
            ),
        }[self.interaction_reply_state]
        if not reply_shape:
            raise ValueError("onchain_session_interaction_reply_invalid")
        return self


def resolve_onchain_candidates(
    ticker: str,
    observations: tuple[OnchainProviderToken, ...],
) -> tuple[OnchainAssetCandidate, ...]:
    """Group exact ticker or CA observations by material chain/contract identity."""

    seed = canonical_onchain_asset_seed(ticker)
    exact_contract = seed if _CONTRACT_RE.fullmatch(seed) is not None else None
    groups: dict[tuple[int, str], list[OnchainProviderToken]] = defaultdict(list)
    for observation in observations:
        if (exact_contract is not None and observation.contract_address == exact_contract) or (
            exact_contract is None and observation.symbol == seed
        ):
            groups[(observation.chain_id, observation.contract_address)].append(observation)
    candidates: list[OnchainAssetCandidate] = []
    for values in groups.values():
        representative = sorted(values, key=lambda value: (not value.verified, value.provider))[0]
        providers = tuple(value.provider for value in values)
        verified = any(value.verified for value in values)
        provider_count = len(set(providers))
        liquidity_values = [value.liquidity_usd for value in values if value.liquidity_usd is not None]
        liquidity_usd = max(liquidity_values) if liquidity_values else None
        pair_count = max(value.pair_count for value in values)
        liquidity_score = (
            1_000
            if liquidity_usd is not None and liquidity_usd >= Decimal("100000")
            else 750
            if liquidity_usd is not None and liquidity_usd >= Decimal("10000")
            else 250
            if liquidity_usd is not None and liquidity_usd > 0
            else 0
        )
        confidence = min(
            10_000,
            4_500
            + (1_500 if verified else 0)
            + (1_500 if exact_contract is not None else 0)
            + 1_000 * (provider_count - 1)
            + liquidity_score
            + (500 if pair_count >= 2 else 0),
        )
        candidates.append(
            OnchainAssetCandidate(
                chain_id=representative.chain_id,
                chain_name=representative.chain_name,
                contract_address=representative.contract_address,
                symbol=representative.symbol,
                name=representative.name,
                decimals=representative.decimals,
                providers=tuple(set(providers)),
                verified=verified,
                confidence_bps=confidence,
                liquidity_usd=liquidity_usd,
                pair_count=pair_count,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda value: (-value.confidence_bps, value.chain_id, value.contract_address),
        )
    )


def analyze_onchain_routes(
    quotes: tuple[OnchainRouteQuote, ...],
    *,
    now_ms: int,
) -> OnchainRouteAnalysis:
    """Rank comparable routes without promoting missing safety/cost data to zero."""

    eligible: list[OnchainRouteQuote] = []
    rejected: list[OnchainProvider] = []
    identity: tuple[int, str, str, int] | None = None
    for quote in quotes:
        current_identity = (
            quote.chain_id,
            quote.input_contract,
            quote.output_contract,
            quote.input_amount_raw,
        )
        if identity is None:
            identity = current_identity
        if current_identity != identity:
            raise ValueError("onchain_route_quotes_not_comparable")
        if (
            (quote.expires_at_ms is not None and quote.expires_at_ms < now_ms)
            or quote.simulation_passed is False
            or quote.risk_blocked
        ):
            rejected.append(quote.provider)
            continue
        eligible.append(quote)
    if not eligible:
        return OnchainRouteAnalysis(
            state=RouteAnalysisState.UNAVAILABLE,
            winner_provider=None,
            winner_net_receive_usd=None,
            eligible_quotes=(),
            rejected_providers=tuple(rejected),
            reason_codes=("no_eligible_route",),
            analyzed_at_ms=now_ms,
        )
    complete = all(quote.complete_for_definitive_ranking for quote in eligible)
    if complete:
        winner = max(
            eligible,
            key=lambda quote: quote.net_receive_usd if quote.net_receive_usd is not None else Decimal("-Infinity"),
        )
        reasons: tuple[str, ...] = ()
        state = RouteAnalysisState.DEFINITIVE
    else:
        winner = max(eligible, key=lambda quote: quote.expected_output_raw)
        missing: list[str] = []
        if any(
            quote.expected_output_usd is None
            or quote.provider_fee_usd is None
            or quote.gas_fee_usd is None
            or quote.minimum_output_raw is None
            for quote in eligible
        ):
            missing.append("cost_incomplete")
        if any(quote.simulation_passed is not True for quote in eligible):
            missing.append("simulation_incomplete")
        if any(not quote.risk_checked for quote in eligible):
            missing.append("risk_check_incomplete")
        reasons = tuple(missing)
        state = RouteAnalysisState.PROVISIONAL
    return OnchainRouteAnalysis(
        state=state,
        winner_provider=winner.provider,
        winner_net_receive_usd=winner.net_receive_usd if complete else None,
        eligible_quotes=tuple(eligible),
        rejected_providers=tuple(rejected),
        reason_codes=reasons,
        analyzed_at_ms=now_ms,
    )


__all__ = [
    "OnchainAnalysisSession",
    "OnchainAnalysisState",
    "OnchainAssetCandidate",
    "OnchainDiscoveryProvider",
    "OnchainInteractionReplyState",
    "OnchainNewsSource",
    "OnchainProvider",
    "OnchainProviderToken",
    "OnchainProviderUnavailable",
    "OnchainQuoteRequest",
    "OnchainRouteAnalysis",
    "OnchainRouteQuote",
    "OnchainTelegramEditEffect",
    "OnchainTelegramEditPayload",
    "OnchainTelegramEditState",
    "RouteAnalysisState",
    "analyze_onchain_routes",
    "canonical_onchain_asset_seed",
    "resolve_onchain_candidates",
]
