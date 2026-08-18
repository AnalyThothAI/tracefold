from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.macro import MacroRepository
from tracefold.market import (
    EnrichedEventRepository,
    EntityRepository,
    EventAnchorBackfillJobRepository,
    EventTokenProjectionQuery,
    EvidenceRepository,
    GeneralMarketRepository,
    IdentityEvidenceRepository,
    IntentResolutionRepository,
    MarketTickCurrentRepository,
    MarketTickRepository,
    RegistryRepository,
    TokenEvidenceRepository,
    TokenIntentLookupRepository,
    TokenIntentRepository,
    TokenTargetRepository,
)
from tracefold.news.repository import NewsRepository
from tracefold.platform.postgres.persisted_live import PersistedLiveEventRepository
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    require_transaction,
    transaction,
    with_password_from_file,
)
from tracefold.platform.postgres.projection_frontier import ProjectionFrontierRepository
from tracefold.platform.postgres.provider_circuit import ProviderCircuitRepository


@dataclass(frozen=True, slots=True)
class RepositorySession:
    conn: Any
    evidence: EvidenceRepository
    entities: EntityRepository
    token_evidence: TokenEvidenceRepository
    token_intents: TokenIntentRepository
    intent_resolutions: IntentResolutionRepository
    registry: RegistryRepository
    identity_evidence: IdentityEvidenceRepository
    market_ticks: MarketTickRepository
    market_tick_current: MarketTickCurrentRepository
    enriched_events: EnrichedEventRepository
    event_anchor_jobs: EventAnchorBackfillJobRepository
    token_intent_lookup: TokenIntentLookupRepository
    event_tokens: EventTokenProjectionQuery
    token_targets: TokenTargetRepository
    news: NewsRepository
    macro: MacroRepository
    macro_market: GeneralMarketRepository
    persisted_live: PersistedLiveEventRepository
    projection_frontiers: ProjectionFrontierRepository
    provider_circuits: ProviderCircuitRepository
    transaction_observer: Callable[[float], None] | None = None
    projection_transitions: list[tuple[str, str]] | None = None

    def transaction(self) -> AbstractContextManager[None]:
        return self._observed_transaction()

    @contextmanager
    def _observed_transaction(self) -> Iterator[None]:
        started = time.perf_counter()
        transition_marker = len(self.projection_transitions or ())
        try:
            with transaction(self.conn):
                yield
        except BaseException:
            if self.projection_transitions is not None:
                del self.projection_transitions[transition_marker:]
            raise
        finally:
            if self.transaction_observer is not None:
                self.transaction_observer(max(0.0, time.perf_counter() - started))

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)


def repositories_for_connection(
    conn: Any,
    *,
    transaction_observer: Callable[[float], None] | None = None,
    projection_transitions: list[tuple[str, str]] | None = None,
) -> RepositorySession:
    projection_transition_observer = projection_transitions.append if projection_transitions is not None else None
    return RepositorySession(
        conn=conn,
        evidence=EvidenceRepository(conn),
        entities=EntityRepository(conn),
        token_evidence=TokenEvidenceRepository(conn),
        token_intents=TokenIntentRepository(conn),
        intent_resolutions=IntentResolutionRepository(conn),
        registry=RegistryRepository(conn),
        identity_evidence=IdentityEvidenceRepository(conn),
        market_ticks=MarketTickRepository(conn),
        market_tick_current=MarketTickCurrentRepository(conn),
        enriched_events=EnrichedEventRepository(conn),
        event_anchor_jobs=EventAnchorBackfillJobRepository(conn),
        token_intent_lookup=TokenIntentLookupRepository(conn),
        event_tokens=EventTokenProjectionQuery(conn),
        token_targets=TokenTargetRepository(conn),
        news=NewsRepository(conn),
        macro=MacroRepository(conn),
        macro_market=GeneralMarketRepository(conn),
        persisted_live=PersistedLiveEventRepository(conn),
        projection_frontiers=ProjectionFrontierRepository(
            conn,
            transition_observer=projection_transition_observer,
        ),
        provider_circuits=ProviderCircuitRepository(conn),
        transaction_observer=transaction_observer,
        projection_transitions=projection_transitions,
    )


@contextmanager
def postgres_connection(
    settings: Any,
    *,
    role: Literal["serve", "workers", "migrate"],
) -> Iterator[Any]:
    """Open the short-lived PostgreSQL connection used by application operations."""
    postgres = settings.storage.postgres
    dsn = with_password_from_file(
        settings.postgres_dsn(role),
        settings.postgres_password_file(role),
    )
    conn = connect_postgres(dsn, connect_timeout_seconds=postgres.connect_timeout_seconds)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def repositories(
    settings: Any,
    *,
    role: Literal["serve", "workers"] = "workers",
) -> Iterator[RepositorySession]:
    """Open one short-lived repository session for a CLI/application operation."""
    with postgres_connection(settings, role=role) as conn:
        yield repositories_for_connection(conn)
