from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tracefold.app.provider_ownership import gmgn_stream_enabled
from tracefold.platform.config.settings import Settings

ProviderOperationalState = Literal["ok", "degraded", "inactive"]
ProviderFreshnessState = Literal["current", "stale", "no_evidence", "not_applicable"]

_GMGN_STREAM_PROVIDER = "gmgn_direct_ws"
_GMGN_DEX_QUOTE_PROVIDER = "gmgn_dex_quote"
_BINANCE_CEX_PROVIDER = "binance_cex_rest"


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
    """Return durable Provider freshness and circuit state (no queue ownership: providers own no durable queues)."""

    specs = _provider_specs(settings)
    facts = _latest_provider_facts(conn)
    provider_ids = tuple(specs)
    circuits = _provider_circuits(conn, providers=provider_ids)

    items: list[dict[str, Any]] = []
    aggregate_reasons: list[str] = []
    for provider in sorted(specs):
        spec = specs[provider]
        latest_fact_at_ms = facts.get(provider)
        circuit = circuits.get(provider)
        freshness = _freshness(
            latest_fact_at_ms=latest_fact_at_ms,
            freshness_budget_ms=spec.freshness_budget_ms,
            now_ms=now_ms,
        )
        reasons: list[str] = []
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
                "has_backlog": False,
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
    gmgn_stream_owned = gmgn_stream_enabled(settings)
    gmgn_stream_budget_ms = max(300_000, int(float(settings.upstream.idle_timeout) * 2_000))
    specs = (
        _ProviderSpec(
            provider=_GMGN_STREAM_PROVIDER,
            owned=gmgn_stream_owned,
            freshness_budget_ms=gmgn_stream_budget_ms if gmgn_stream_owned else None,
        ),
        _ProviderSpec(provider=_GMGN_DEX_QUOTE_PROVIDER, owned=settings.gmgn_configured),
        _ProviderSpec(provider=_BINANCE_CEX_PROVIDER, owned=settings.providers.binance.enabled),
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
