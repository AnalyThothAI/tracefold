"""Public market capability interface.

Everything outside ``tracefold.market`` imports contracts from this module.
Implementation modules remain private to the capability.
"""

from .capture.collector import CollectorService
from .capture.entity_repository import EntityRepository
from .capture.event_contracts import (
    EVM_QUERY_CHAINS,
    Author,
    AvatarChange,
    BioChange,
    Content,
    EventRead,
    ExtractedEntity,
    Media,
    Reference,
    Source,
    TextSurface,
    TokenSnapshot,
    TwitterEvent,
    UnfollowTarget,
    decode_event_row,
    event_to_row,
    extract_entities_from_surfaces,
    materialize_event,
    normalize_ca,
)
from .capture.evidence_repository import EvidenceRepository
from .capture.gmgn_token_payload import parse_gmgn_token_payload
from .capture.ingest_contracts import IngestedEvent
from .capture.ingest_service import IngestService, require_event_anchor_active_window_ms
from .capture.normalizer import normalize_gmgn_payload, parse_gmgn_frame
from .capture.provider_contracts import GmgnStreamExpectedError, IngestStoreProtocol, UpstreamClientProtocol
from .identity.asset_market_sync import BinanceUsdtPerpRoute, sync_binance_usdt_perp_routes
from .identity.chain_identity import canonical_chain_address, canonical_chain_id, chain_address_key
from .identity.deterministic_token_resolver import DeterministicResolution, DeterministicTokenResolver, MentionKeys
from .identity.identity_evidence_policy import (
    CONFIDENCE_MANUAL,
    CONFIDENCE_MENTION_ONLY,
    CONFIDENCE_PROVIDER_EXACT,
    CONFIDENCE_UNKNOWN,
    EVIDENCE_BINANCE_CEX_INSTRUMENT,
    EVIDENCE_GMGN_OPENAPI_EXACT,
    EVIDENCE_GMGN_PAYLOAD_EXACT,
    EVIDENCE_MANUAL_IDENTITY_REPAIR,
    EVIDENCE_TWEET_CONTRACT_MENTION,
    select_current_identity,
)
from .identity.identity_evidence_repository import IdentityEvidenceRepository
from .identity.intent_resolution_repository import IntentResolutionRepository, token_intent_resolution_id
from .identity.registry_repository import RegistryRepository
from .identity.resolver_policy import TOKEN_RESOLVER_POLICY_VERSION
from .identity.token_evidence_builder import build_token_evidence
from .identity.token_evidence_repository import TokenEvidenceRepository
from .identity.token_intent_builder import TokenIntentInput, build_token_intents
from .identity.token_intent_lookup_repository import TokenIntentLookupRepository
from .identity.token_intent_rebuild import rebuild_recent_token_intents
from .identity.token_intent_repository import TokenIntentRepository
from .identity.token_intent_resolver import TokenIntentResolutionDecision, TokenIntentResolver
from .identity.us_equity_symbol_sync import NasdaqTraderSymbolClient, sync_us_equity_symbols
from .macro_market_domain import (
    GeneralMarketInstrumentSpec,
    MarketObservationFact,
    MarketPositionFact,
    MarketSettlementFact,
    MarketTrustTier,
)
from .macro_market_repository import GeneralMarketRepository
from .pricing.enriched_event_repository import EnrichedEventRepository
from .pricing.event_anchor_backfill_job_repository import EventAnchorBackfillJobRepository
from .pricing.event_anchor_backfill_worker import EventAnchorBackfill
from .pricing.event_market_capture import CaptureResult, EventMarketCaptureService, TickLookup
from .pricing.live_market import live_market_snapshot
from .pricing.market_candles_service import MarketCandlesService
from .pricing.market_tick import EnrichedEventCapture, MarketTick, MarketTickSourceProvider
from .pricing.market_tick_current_repository import MarketTickCurrentRepository
from .pricing.market_tick_id import market_tick_id
from .pricing.market_tick_persistence import MarketTickPersistenceService
from .pricing.market_tick_poll_worker import MarketTickPoll
from .pricing.market_tick_repository import MarketTickRepository
from .pricing.message_price_payload import message_price_payload
from .provider_contracts import (
    AssetMarketProviderBundle,
    CexMarketProvider,
    CexTicker,
    DexProviderTemporarilyUnavailable,
    DexTokenQuote,
    DexTokenQuoteProvider,
    DexTokenQuoteRequest,
    MarketProviderExpectedError,
)
from .views.event_token_projection_query import EventTokenProjectionQuery
from .views.search_events_query import SearchEventsQuery
from .views.search_inspect_service import SearchInspectService
from .views.search_service import SearchCursorError, SearchService
from .views.token_case_service import (
    TokenCaseService,
    TokenCaseTargetNotFound,
)
from .views.token_target_cursor import TokenTargetCursorError
from .views.token_target_posts_service import (
    TokenTargetPostsCursorError,
    TokenTargetPostsQueryError,
    TokenTargetPostsRangeError,
    TokenTargetPostsService,
)
from .views.token_target_repository import TokenTargetRepository
from .views.token_target_social_timeline_service import TokenTargetSocialTimelineService
from .views.token_target_stage_builder import build_token_target_stages

__all__ = [
    "CONFIDENCE_MANUAL",
    "CONFIDENCE_MENTION_ONLY",
    "CONFIDENCE_PROVIDER_EXACT",
    "CONFIDENCE_UNKNOWN",
    "EVIDENCE_BINANCE_CEX_INSTRUMENT",
    "EVIDENCE_GMGN_OPENAPI_EXACT",
    "EVIDENCE_GMGN_PAYLOAD_EXACT",
    "EVIDENCE_MANUAL_IDENTITY_REPAIR",
    "EVIDENCE_TWEET_CONTRACT_MENTION",
    "EVM_QUERY_CHAINS",
    "TOKEN_RESOLVER_POLICY_VERSION",
    "AssetMarketProviderBundle",
    "Author",
    "AvatarChange",
    "BinanceUsdtPerpRoute",
    "BioChange",
    "CaptureResult",
    "CexMarketProvider",
    "CexTicker",
    "CollectorService",
    "Content",
    "DeterministicResolution",
    "DeterministicTokenResolver",
    "DexProviderTemporarilyUnavailable",
    "DexTokenQuote",
    "DexTokenQuoteProvider",
    "DexTokenQuoteRequest",
    "EnrichedEventCapture",
    "EnrichedEventRepository",
    "EntityRepository",
    "EventAnchorBackfill",
    "EventAnchorBackfillJobRepository",
    "EventMarketCaptureService",
    "EventRead",
    "EventTokenProjectionQuery",
    "EvidenceRepository",
    "ExtractedEntity",
    "GeneralMarketInstrumentSpec",
    "GeneralMarketRepository",
    "GmgnStreamExpectedError",
    "IdentityEvidenceRepository",
    "IngestService",
    "IngestStoreProtocol",
    "IngestedEvent",
    "IntentResolutionRepository",
    "MarketCandlesService",
    "MarketObservationFact",
    "MarketPositionFact",
    "MarketProviderExpectedError",
    "MarketSettlementFact",
    "MarketTick",
    "MarketTickCurrentRepository",
    "MarketTickPersistenceService",
    "MarketTickPoll",
    "MarketTickRepository",
    "MarketTickSourceProvider",
    "MarketTrustTier",
    "Media",
    "MentionKeys",
    "NasdaqTraderSymbolClient",
    "Reference",
    "RegistryRepository",
    "SearchCursorError",
    "SearchEventsQuery",
    "SearchInspectService",
    "SearchService",
    "Source",
    "TextSurface",
    "TickLookup",
    "TokenCaseService",
    "TokenCaseTargetNotFound",
    "TokenEvidenceRepository",
    "TokenIntentInput",
    "TokenIntentLookupRepository",
    "TokenIntentRepository",
    "TokenIntentResolutionDecision",
    "TokenIntentResolver",
    "TokenSnapshot",
    "TokenTargetCursorError",
    "TokenTargetPostsCursorError",
    "TokenTargetPostsQueryError",
    "TokenTargetPostsRangeError",
    "TokenTargetPostsService",
    "TokenTargetRepository",
    "TokenTargetSocialTimelineService",
    "TwitterEvent",
    "UnfollowTarget",
    "UpstreamClientProtocol",
    "build_token_evidence",
    "build_token_intents",
    "build_token_target_stages",
    "canonical_chain_address",
    "canonical_chain_id",
    "chain_address_key",
    "decode_event_row",
    "event_to_row",
    "extract_entities_from_surfaces",
    "live_market_snapshot",
    "market_tick_id",
    "materialize_event",
    "message_price_payload",
    "normalize_ca",
    "normalize_gmgn_payload",
    "parse_gmgn_frame",
    "parse_gmgn_token_payload",
    "rebuild_recent_token_intents",
    "require_event_anchor_active_window_ms",
    "select_current_identity",
    "sync_binance_usdt_perp_routes",
    "sync_us_equity_symbols",
    "token_intent_resolution_id",
]
