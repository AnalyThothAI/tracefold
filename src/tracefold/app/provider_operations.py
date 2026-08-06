from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tracefold.app.provider_ownership import configured_profile_provider_ids, gmgn_stream_enabled
from tracefold.market import BINANCE_WEB3_PROFILE_PROVIDER, GMGN_DEX_PROFILE_PROVIDER
from tracefold.platform.config.settings import Settings

ProviderOperationalState = Literal["ok", "degraded", "inactive"]
ProviderFreshnessState = Literal["current", "stale", "no_evidence", "not_applicable"]

_GMGN_STREAM_PROVIDER = "gmgn_direct_ws"
_OKX_SEARCH_PROVIDER = "okx_dex_search"


@dataclass(frozen=True, slots=True)
class _ProviderSpec:
    provider: str
    owned: bool
    freshness_budget_ms: int | None = None


def provider_operational_status(
    conn: Any,
    *,
    settings: Settings,
    now_ms: int,
) -> dict[str, Any]:
    """Return durable Provider freshness, circuit, and queue ownership state."""

    specs = _provider_specs(settings)
    facts = _latest_provider_facts(conn)
    provider_ids = tuple(specs)
    circuits = _provider_circuits(conn, providers=provider_ids)
    backlogs = _provider_backlogs(conn, providers=provider_ids)

    items: list[dict[str, Any]] = []
    aggregate_reasons: list[str] = []
    for provider in sorted(specs):
        spec = specs[provider]
        latest_fact_at_ms = facts.get(provider)
        circuit = circuits.get(provider)
        has_backlog = backlogs.get(provider, False)
        freshness = _freshness(
            latest_fact_at_ms=latest_fact_at_ms,
            freshness_budget_ms=spec.freshness_budget_ms,
            now_ms=now_ms,
        )
        reasons: list[str] = []
        if not spec.owned and has_backlog:
            reasons.append("unowned_backlog")
        if spec.owned and circuit is not None and circuit["status"] == "open":
            reasons.append("circuit_open")
        if spec.owned and freshness in {"stale", "no_evidence"}:
            reasons.append("source_stale")
        if reasons:
            state: ProviderOperationalState = "degraded"
            aggregate_reasons.extend(f"{provider}:{reason}" for reason in reasons)
        elif spec.owned:
            state = "ok"
        else:
            state = "inactive"

        items.append(
            {
                "provider": provider,
                "owned": spec.owned,
                "status": state,
                "reasons": reasons,
                "freshness": freshness,
                "freshness_budget_ms": spec.freshness_budget_ms,
                "latest_fact_at_ms": latest_fact_at_ms,
                "circuit_status": circuit["status"] if circuit is not None else None,
                "consecutive_failures": int(circuit["consecutive_failures"]) if circuit is not None else 0,
                "next_probe_at_ms": circuit.get("next_probe_at_ms") if circuit is not None else None,
                "has_backlog": has_backlog,
            }
        )

    return {
        "status": "degraded" if aggregate_reasons else "ok",
        "reasons": aggregate_reasons,
        "items": items,
    }


def provider_operational_status_unavailable(*, reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reasons": [reason],
        "items": [],
    }


def _provider_specs(settings: Settings) -> dict[str, _ProviderSpec]:
    profile_providers = set(configured_profile_provider_ids(settings))
    gmgn_stream_owned = gmgn_stream_enabled(settings)
    gmgn_stream_budget_ms = max(300_000, int(float(settings.upstream.idle_timeout) * 2_000))
    specs = (
        _ProviderSpec(
            provider=_GMGN_STREAM_PROVIDER,
            owned=gmgn_stream_owned,
            freshness_budget_ms=gmgn_stream_budget_ms if gmgn_stream_owned else None,
        ),
        _ProviderSpec(
            provider=GMGN_DEX_PROFILE_PROVIDER,
            owned=GMGN_DEX_PROFILE_PROVIDER in profile_providers,
        ),
        _ProviderSpec(
            provider=BINANCE_WEB3_PROFILE_PROVIDER,
            owned=BINANCE_WEB3_PROFILE_PROVIDER in profile_providers,
        ),
        _ProviderSpec(provider=_OKX_SEARCH_PROVIDER, owned=settings.okx_dex_configured),
    )
    return {spec.provider: spec for spec in specs}


def _latest_provider_facts(conn: Any) -> dict[str, int | None]:
    row = conn.execute(
        """
        SELECT received_at_ms
        FROM raw_frames
        WHERE source = 'gmgn'
        ORDER BY received_at_ms DESC
        LIMIT 1
        """
    ).fetchone()
    return {_GMGN_STREAM_PROVIDER: int(row["received_at_ms"]) if row is not None else None}


def _provider_circuits(conn: Any, *, providers: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT provider, status, consecutive_failures, next_probe_at_ms
        FROM provider_circuit_state
        WHERE provider = ANY(%s::text[])
        ORDER BY provider
        """,
        (list(providers),),
    ).fetchall()
    return {str(row["provider"]): dict(row) for row in rows}


def _provider_backlogs(conn: Any, *, providers: tuple[str, ...]) -> dict[str, bool]:
    rows = conn.execute(
        """
        WITH providers(provider) AS (
          SELECT unnest(%s::text[])
        )
        SELECT
          provider,
          (
            SELECT profile_queue.provider
            FROM asset_profile_refresh_targets profile_queue
            WHERE profile_queue.provider = providers.provider
              AND profile_queue.terminal_reason IS NULL
            ORDER BY
              profile_queue.provider,
              profile_queue.priority,
              profile_queue.due_at_ms,
              profile_queue.updated_at_ms,
              profile_queue.target_type,
              profile_queue.target_id
            LIMIT 1
          ) IS NOT NULL OR (
            SELECT discovery_queue.provider
            FROM token_discovery_dirty_lookup_keys discovery_queue
            WHERE discovery_queue.provider = providers.provider
            ORDER BY
              discovery_queue.provider,
              discovery_queue.refresh_priority,
              discovery_queue.due_at_ms,
              discovery_queue.latest_seen_ms DESC,
              discovery_queue.updated_at_ms,
              discovery_queue.lookup_key
            LIMIT 1
          ) IS NOT NULL AS has_backlog
        FROM providers
        ORDER BY provider
        """,
        (list(providers),),
    ).fetchall()
    return {str(row["provider"]): bool(row["has_backlog"]) for row in rows}


def _freshness(
    *,
    latest_fact_at_ms: int | None,
    freshness_budget_ms: int | None,
    now_ms: int,
) -> ProviderFreshnessState:
    if freshness_budget_ms is None:
        return "not_applicable"
    if latest_fact_at_ms is None:
        return "no_evidence"
    if max(0, int(now_ms) - int(latest_fact_at_ms)) > int(freshness_budget_ms):
        return "stale"
    return "current"


__all__ = [
    "provider_operational_status",
    "provider_operational_status_unavailable",
]
