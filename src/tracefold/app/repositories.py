from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from tracefold.macro import (
    MacroIntelRepository,
    MacroResearchRepository,
)
from tracefold.market import (
    AssetProfileRefreshTargetRepository,
    AssetProfileRepository,
    CexTokenProfileRepository,
    DiscoveryRepository,
    EnrichedEventRepository,
    EntityRepository,
    EventAnchorBackfillJobRepository,
    EventTokenProjectionQuery,
    EvidenceRepository,
    IdentityEvidenceRepository,
    IntentResolutionRepository,
    MarketTickCurrentRepository,
    MarketTickRepository,
    RegistryRepository,
    SignalRepository,
    TokenEvidenceRepository,
    TokenImageAssetRepository,
    TokenImageSourceDirtyTargetRepository,
    TokenIntentLookupRepository,
    TokenIntentRepository,
    TokenProfileCurrentDirtyTargetRepository,
    TokenProfileCurrentRepository,
    TokenProfileSourceQuery,
    TokenRadarDirtyTargetRepository,
    TokenRadarRankSourceRepository,
    TokenRadarRepository,
    TokenTargetRepository,
    WatchlistQuery,
)
from tracefold.news import NewsRepository
from tracefold.notifications import NotificationRepository
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    require_transaction,
    transaction,
    with_password_from_file,
)


@dataclass(frozen=True, slots=True)
class RepositorySession:
    conn: Any
    evidence: EvidenceRepository
    entities: EntityRepository
    signals: SignalRepository
    asset_profiles: AssetProfileRepository
    asset_profile_refresh_targets: AssetProfileRefreshTargetRepository
    source_query: TokenProfileSourceQuery
    cex_token_profiles: CexTokenProfileRepository
    token_profiles: TokenProfileCurrentRepository
    token_profile_current_dirty_targets: TokenProfileCurrentDirtyTargetRepository
    token_image_assets: TokenImageAssetRepository
    token_image_source_dirty_targets: TokenImageSourceDirtyTargetRepository
    token_evidence: TokenEvidenceRepository
    token_intents: TokenIntentRepository
    intent_resolutions: IntentResolutionRepository
    registry: RegistryRepository
    identity_evidence: IdentityEvidenceRepository
    discovery: DiscoveryRepository
    market_ticks: MarketTickRepository
    market_tick_current: MarketTickCurrentRepository
    enriched_events: EnrichedEventRepository
    event_anchor_jobs: EventAnchorBackfillJobRepository
    token_intent_lookup: TokenIntentLookupRepository
    event_tokens: EventTokenProjectionQuery
    token_radar_dirty_targets: TokenRadarDirtyTargetRepository
    token_radar_rank_sources: TokenRadarRankSourceRepository
    token_radar: TokenRadarRepository
    token_targets: TokenTargetRepository
    notifications: NotificationRepository
    watchlist: WatchlistQuery
    news: NewsRepository
    macro_intel: MacroIntelRepository
    macro_research: MacroResearchRepository

    def transaction(self) -> AbstractContextManager[None]:
        return transaction(self.conn)

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)


def repositories_for_connection(
    conn: Any,
    *,
    notification_delivery_running_timeout_ms: int,
    notification_delivery_stale_running_terminalization_batch_size: int,
) -> RepositorySession:
    return RepositorySession(
        conn=conn,
        evidence=EvidenceRepository(conn),
        entities=EntityRepository(conn),
        signals=SignalRepository(conn),
        asset_profiles=AssetProfileRepository(conn),
        asset_profile_refresh_targets=AssetProfileRefreshTargetRepository(conn),
        source_query=TokenProfileSourceQuery(conn),
        cex_token_profiles=CexTokenProfileRepository(conn),
        token_profiles=TokenProfileCurrentRepository(conn),
        token_profile_current_dirty_targets=TokenProfileCurrentDirtyTargetRepository(conn),
        token_image_assets=TokenImageAssetRepository(conn),
        token_image_source_dirty_targets=TokenImageSourceDirtyTargetRepository(conn),
        token_evidence=TokenEvidenceRepository(conn),
        token_intents=TokenIntentRepository(conn),
        intent_resolutions=IntentResolutionRepository(conn),
        registry=RegistryRepository(conn),
        identity_evidence=IdentityEvidenceRepository(conn),
        discovery=DiscoveryRepository(conn),
        market_ticks=MarketTickRepository(conn),
        market_tick_current=MarketTickCurrentRepository(conn),
        enriched_events=EnrichedEventRepository(conn),
        event_anchor_jobs=EventAnchorBackfillJobRepository(conn),
        token_intent_lookup=TokenIntentLookupRepository(conn),
        event_tokens=EventTokenProjectionQuery(conn),
        token_radar_dirty_targets=TokenRadarDirtyTargetRepository(conn),
        token_radar_rank_sources=TokenRadarRankSourceRepository(conn),
        token_radar=TokenRadarRepository(conn),
        token_targets=TokenTargetRepository(conn),
        notifications=NotificationRepository(
            conn,
            running_timeout_ms=notification_delivery_running_timeout_ms,
            stale_running_terminalization_batch_size=notification_delivery_stale_running_terminalization_batch_size,
        ),
        watchlist=WatchlistQuery(conn),
        news=NewsRepository(conn),
        macro_intel=MacroIntelRepository(conn),
        macro_research=MacroResearchRepository(conn),
    )


@contextmanager
def postgres_connection(settings: Any) -> Iterator[Any]:
    """Open the short-lived PostgreSQL connection used by application operations."""
    postgres = settings.storage.postgres
    dsn = with_password_from_file(postgres.dsn, settings.postgres_password_file)
    conn = connect_postgres(dsn, connect_timeout_seconds=postgres.connect_timeout_seconds)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def repositories(settings: Any) -> Iterator[RepositorySession]:
    """Open one short-lived repository session for a CLI/application operation."""
    with postgres_connection(settings) as conn:
        yield repositories_for_connection(
            conn,
            notification_delivery_running_timeout_ms=int(settings.workers.notification_delivery.running_timeout_ms),
            notification_delivery_stale_running_terminalization_batch_size=int(
                settings.workers.notification_delivery.stale_running_terminalization_batch_size
            ),
        )
