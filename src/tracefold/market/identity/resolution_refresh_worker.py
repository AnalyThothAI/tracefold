from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from tracefold.market.identity.token_resolution_refresh import (
    TOKEN_REPROCESS_WINDOW,
    reprocess_recent_token_intents,
)
from tracefold.market.provider_contracts import (
    DexProviderTemporarilyUnavailable,
    DexTokenCandidate,
)
from tracefold.market.radar.constants import WINDOW_MS
from tracefold.platform.config.settings import ResolutionRefreshWorkerSettings
from tracefold.platform.validation import require_nonnegative_int, require_positive_int
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

from .discovery_repository import DISCOVERY_PROVIDER
from .identity_evidence_policy import (
    CONFIDENCE_PROVIDER_CANDIDATE,
    CONFIDENCE_PROVIDER_EXACT,
    EVIDENCE_OKX_DEX_EXACT_ADDRESS,
    EVIDENCE_OKX_DEX_SYMBOL_CANDIDATE,
)

FOUND_SYMBOL_REFRESH_MS = 15 * 60 * 1000
NOT_FOUND_SYMBOL_REFRESH_MS = 5 * 60 * 1000
FOUND_ADDRESS_REFRESH_MS = 24 * 60 * 60 * 1000
NOT_FOUND_ADDRESS_REFRESH_MS = 5 * 60 * 1000
HOT_LOOKBACK_MS = WINDOW_MS["1h"]
HOT_PROJECTION_WINDOWS = ("5m", "1h")
HOT_PROJECTION_LIMIT = 100
ERROR_REFRESH_BACKOFF_MS = (30_000, 60_000, 300_000, 1_800_000, 3_600_000)
MAX_DEX_SYMBOL_CANDIDATES_PER_CHAIN = 3


class ResolutionRefreshWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: ResolutionRefreshWorkerSettings,
        db: Any,
        telemetry: Any,
        dex_discovery_market: Any,
    ) -> None:
        if dex_discovery_market is None:
            raise RuntimeError("resolution_refresh_provider_required")
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.dex_discovery_market = dex_discovery_market
        self.chain_ids = tuple(str(item).strip() for item in settings.chain_ids if str(item).strip())
        self.max_attempts = settings.max_attempts
        self.lease_ms = settings.lease_ms
        self.hot_not_found_retry_ms = settings.hot_not_found_retry_ms

    async def run_once(self, *, now_ms: int | None = None) -> WorkerResult:
        observed_at_ms = int(now_ms if now_ms is not None else _now_ms())
        resources = self.require_runtime_resources()
        result = _empty_result(observed_at_ms)
        lookups, circuit_open = await resources.run_background_db(
            self._claim_due_lookups,
            observed_at_ms,
        )
        result["lookups_selected"] = len(lookups)
        if circuit_open:
            result["provider_unavailable"] = 1
            result["errors"].append(
                {
                    "lookup_key": "",
                    "error": "provider_circuit_open",
                }
            )
        affected_lookup_keys: set[str] = set()
        processed_claims: list[dict[str, Any]] = []
        queue_due_by_lookup_key: dict[str, int] = {}
        for index, lookup in enumerate(lookups):
            lookup_key = str(lookup.get("lookup_key") or "")
            lookup_type = str(lookup.get("lookup_type") or "")
            await resources.run_background_db(
                self._start_lookup,
                lookup_key,
                lookup_type,
                observed_at_ms,
            )
            try:
                async with self.require_provider_governor().acquire(host="asset_discovery"):
                    lookup_result = await resources.run_provider_io(
                        _fetch_lookup_provider_result,
                        lookup_key=lookup_key,
                        lookup_type=lookup_type,
                        dex_discovery_market=self.dex_discovery_market,
                        chain_ids=self.chain_ids,
                    )
            except DexProviderTemporarilyUnavailable as exc:
                unavailable = await resources.run_background_db(
                    self._publish_provider_unavailable,
                    lookups[index:],
                    lookup_key,
                    lookup_type,
                    observed_at_ms,
                    exc,
                )
                result["provider_unavailable"] += int(unavailable["claims"])
                result["errors"].append(
                    {
                        "lookup_key": lookup_key,
                        "error": unavailable["last_error"],
                    }
                )
                break
            except Exception as exc:
                failed = await resources.run_background_db(
                    self._publish_lookup_error,
                    lookup,
                    lookup_key,
                    lookup_type,
                    observed_at_ms,
                    exc,
                )
                result["lookups_failed"] += 1
                result["provider_errors"] += 1
                result["lookups_terminalized"] += int(failed["terminalized"])
                result["errors"].append(
                    {
                        "lookup_key": lookup_key,
                        "error": str(exc),
                    }
                )
                continue

            published = await resources.run_background_db(
                self._publish_lookup_success,
                lookup,
                lookup_result,
                observed_at_ms,
            )
            processed_claims.append(dict(lookup))
            queue_due_by_lookup_key[lookup_key] = int(published["queue_due_at_ms"])
            _merge_lookup_result(result, published["lookup_result"])
            result["lookups_done"] += 1
            affected_lookup_keys.update(str(key) for key in published["affected_lookup_keys"])

        resolved_lookup_keys: set[str] = set()
        if affected_lookup_keys:
            sorted_lookup_keys = sorted(affected_lookup_keys)
            result["affected_lookup_keys"] = sorted_lookup_keys
            reprocess_result = await resources.run_background_db(
                self._reprocess_lookup_keys,
                sorted_lookup_keys,
                observed_at_ms,
            )
            result["reprocess"] = reprocess_result
            result["reprocessed_intents"] = int(reprocess_result["reprocessed_intents"])
            if reprocess_result["resolved_intents"]:
                resolved_lookup_keys.update(sorted_lookup_keys)
        if processed_claims:
            result["lookups_terminalized"] += await resources.run_background_db(
                self._complete_processed_claims,
                processed_claims,
                resolved_lookup_keys,
                queue_due_by_lookup_key,
                observed_at_ms,
            )
        result["discovery_result_counts"] = await resources.run_background_db(self._discovery_counts)
        notes: dict[str, Any] = {"result": result}
        if int(result.get("provider_unavailable") or 0) > 0:
            notes["status"] = "degraded"
            notes["degraded"] = True
        return WorkerResult(
            processed=int(result.get("lookups_done") or 0) + int(result.get("reprocessed_intents") or 0),
            failed=int(result.get("lookups_failed") or 0),
            notes=notes,
        )

    def _claim_due_lookups(
        self,
        now_ms: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        with self.db.worker_session(self.name) as repos, repos.transaction():
            if not repos.provider_circuits.can_attempt(
                provider=DISCOVERY_PROVIDER,
                now_ms=now_ms,
            ):
                return [], True
            lookups = repos.discovery.claim_due_lookup_keys(
                now_ms=now_ms,
                limit=self.settings.batch_size,
                lease_ms=self.lease_ms,
                running_timeout_ms=self.lease_ms,
                lease_owner=self.claim_owner,
                hot_since_ms=int(now_ms) - HOT_LOOKBACK_MS,
                hot_not_found_retry_ms=self.hot_not_found_retry_ms,
            )
        return [dict(row) for row in lookups], False

    def _start_lookup(
        self,
        lookup_key: str,
        lookup_type: str,
        now_ms: int,
    ) -> None:
        with self.db.worker_session(self.name) as repos, repos.transaction():
            repos.discovery.start_lookup(
                provider=DISCOVERY_PROVIDER,
                lookup_key=lookup_key,
                lookup_type=lookup_type,
                now_ms=now_ms,
                running_timeout_ms=self.lease_ms,
            )

    def _publish_lookup_success(
        self,
        lookup: dict[str, Any],
        lookup_result: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        lookup_key = str(lookup["lookup_key"])
        lookup_type = str(lookup["lookup_type"])
        with self.db.worker_session(self.name) as repos, repos.transaction():
            _persist_lookup_provider_result(
                repos=repos,
                lookup_result=lookup_result,
                now_ms=now_ms,
            )
            candidate_ids = sorted(set(lookup_result["candidate_ids"]))
            status = "found" if candidate_ids else "not_found"
            next_refresh_at_ms = now_ms + _refresh_ms(
                lookup_key=lookup_key,
                status=status,
            )
            repos.discovery.finish_lookup(
                provider=DISCOVERY_PROVIDER,
                lookup_key=lookup_key,
                lookup_type=lookup_type,
                status=status,
                candidate_ids=candidate_ids,
                result_hash=_result_hash(candidate_ids),
                next_refresh_at_ms=next_refresh_at_ms,
                now_ms=now_ms,
            )
            repos.provider_circuits.close(
                provider=DISCOVERY_PROVIDER,
                now_ms=now_ms,
            )
        return {
            "lookup_result": lookup_result,
            "affected_lookup_keys": list(lookup_result["affected_lookup_keys"]),
            "queue_due_at_ms": _next_queue_due_at_ms(
                lookup=lookup,
                status=status,
                next_refresh_at_ms=next_refresh_at_ms,
                now_ms=now_ms,
                hot_not_found_retry_ms=self.hot_not_found_retry_ms,
            ),
        }

    def _publish_provider_unavailable(
        self,
        claims: list[dict[str, Any]],
        lookup_key: str,
        lookup_type: str,
        now_ms: int,
        error: Exception,
    ) -> dict[str, Any]:
        retry_due_at_ms = now_ms + _refresh_ms(
            lookup_key=lookup_key,
            status="error",
            error_count=_claim_error_count(claims[0]),
        )
        last_error = _provider_unavailable_error(error)
        with self.db.worker_session(self.name) as repos, repos.transaction():
            repos.discovery.fail_lookup(
                provider=DISCOVERY_PROVIDER,
                lookup_key=lookup_key,
                lookup_type=lookup_type or _lookup_type(lookup_key),
                last_error=last_error,
                next_refresh_at_ms=retry_due_at_ms,
                now_ms=now_ms,
            )
            released = repos.discovery.reschedule_lookup_claims_without_attempt(
                claims,
                due_at_ms=retry_due_at_ms,
                now_ms=now_ms,
                last_error=last_error,
            )
            if released != len(claims):
                raise RuntimeError("resolution_refresh_provider_release_cas_mismatch")
            repos.provider_circuits.open(
                provider=DISCOVERY_PROVIDER,
                error=last_error,
                now_ms=now_ms,
                retry_ms=retry_due_at_ms - now_ms,
            )
        return {
            "claims": len(claims),
            "last_error": last_error,
        }

    def _publish_lookup_error(
        self,
        lookup: dict[str, Any],
        lookup_key: str,
        lookup_type: str,
        now_ms: int,
        error: Exception,
    ) -> dict[str, int]:
        retry_due_at_ms = now_ms + _refresh_ms(
            lookup_key=lookup_key,
            status="error",
            error_count=_claim_error_count(lookup),
        )
        terminalized = 0
        with self.db.worker_session(self.name) as repos, repos.transaction():
            repos.discovery.fail_lookup(
                provider=DISCOVERY_PROVIDER,
                lookup_key=lookup_key,
                lookup_type=lookup_type or _lookup_type(lookup_key),
                last_error=str(error),
                next_refresh_at_ms=retry_due_at_ms,
                now_ms=now_ms,
            )
            if _claim_retry_budget_exhausted(
                lookup,
                max_attempts=self.max_attempts,
            ):
                terminal = repos.discovery.terminalize_lookup_claims(
                    [lookup],
                    worker_name=self.name,
                    final_status="error",
                    final_reason="provider_error_retry_budget_exhausted",
                    now_ms=now_ms,
                )
                terminalized = int(terminal.get("terminalized") or 0)
            else:
                repos.discovery.reschedule_lookup_claims(
                    [lookup],
                    due_at_ms=retry_due_at_ms,
                    now_ms=now_ms,
                    last_error=str(error),
                )
        return {"terminalized": terminalized}

    def _reprocess_lookup_keys(
        self,
        lookup_keys: list[str],
        now_ms: int,
    ) -> dict[str, Any]:
        with self.db.worker_session(self.name) as repos:
            return reprocess_recent_token_intents(
                repos=repos,
                lookup_keys=lookup_keys,
                now_ms=now_ms,
                window=TOKEN_REPROCESS_WINDOW,
                limit=self.settings.reprocess_limit,
            )

    def _complete_processed_claims(
        self,
        claims: list[dict[str, Any]],
        resolved_lookup_keys: set[str],
        due_by_lookup_key: dict[str, int],
        now_ms: int,
    ) -> int:
        summary = {"lookups_terminalized": 0}
        _complete_lookup_claims(
            db=self.db,
            worker_name=self.name,
            claims=claims,
            resolved_lookup_keys=resolved_lookup_keys,
            due_by_lookup_key=due_by_lookup_key,
            now_ms=now_ms,
            max_attempts=self.max_attempts,
            hot_not_found_retry_ms=self.hot_not_found_retry_ms,
            result=summary,
        )
        return int(summary["lookups_terminalized"])

    def _discovery_counts(self) -> dict[str, int]:
        with self.db.worker_session(self.name) as repos:
            return {str(key): int(value) for key, value in dict(repos.discovery.counts()).items()}


def _fetch_lookup_provider_result(
    *,
    lookup_key: str,
    lookup_type: str,
    dex_discovery_market: Any,
    chain_ids: tuple[str, ...],
) -> dict[str, Any]:
    if lookup_type == "dex_symbol_lookup":
        return _fetch_dex_symbol_lookup_result(
            lookup_key=lookup_key,
            dex_discovery_market=dex_discovery_market,
            chain_ids=chain_ids,
        )
    if lookup_type == "address_lookup":
        return _fetch_address_lookup_result(
            lookup_key=lookup_key,
            dex_discovery_market=dex_discovery_market,
            chain_ids=chain_ids,
        )
    return _lookup_result()


def _fetch_dex_symbol_lookup_result(
    *,
    lookup_key: str,
    dex_discovery_market: Any,
    chain_ids: tuple[str, ...],
) -> dict[str, Any]:
    if dex_discovery_market is None:
        raise RuntimeError("dex discovery client is not configured")
    symbol = _normalize_symbol(lookup_key.removeprefix("symbol:"))
    if not symbol:
        return _lookup_result()
    candidates = dex_discovery_market.search_tokens(query=symbol, chain_ids=chain_ids)
    candidates = _required_dex_token_candidates(candidates, reason="symbol_search")
    provider_ranks = _provider_ranks(candidates)
    result = _lookup_result(search_requests=1)
    matched_candidates = [candidate for candidate in candidates if _normalize_symbol(candidate.symbol) == symbol]
    retained_candidates = _retained_symbol_candidates(
        matched_candidates,
        per_chain_limit=MAX_DEX_SYMBOL_CANDIDATES_PER_CHAIN,
    )
    result["search_candidates_seen"] = len(matched_candidates)
    result["search_candidates_rejected"] = max(0, len(matched_candidates) - len(retained_candidates))
    result["_candidate_writes"] = [
        {
            "candidate": candidate,
            "evidence_kind": EVIDENCE_OKX_DEX_SYMBOL_CANDIDATE,
            "confidence": CONFIDENCE_PROVIDER_CANDIDATE,
            "lookup_mode": "symbol_search",
            "provider_rank": provider_ranks.get(_candidate_identity_key(candidate)),
        }
        for candidate in retained_candidates
    ]
    if retained_candidates:
        result["affected_lookup_keys"].extend([f"symbol:{symbol}", f"project_symbol:{symbol}", f"cex_token:{symbol}"])
    return result


def _fetch_address_lookup_result(
    *,
    lookup_key: str,
    dex_discovery_market: Any,
    chain_ids: tuple[str, ...],
) -> dict[str, Any]:
    if dex_discovery_market is None:
        raise RuntimeError("dex discovery client is not configured")
    parsed = _parse_address_lookup_key(lookup_key)
    address = parsed["address"]
    if not address:
        return _lookup_result()
    chain_id = _chain_id(parsed["chain_id"])
    requested_chains = (chain_id,) if chain_id else chain_ids
    requested_chains = tuple(chain for chain in requested_chains if chain)
    if not requested_chains:
        return _lookup_result()
    candidates = dex_discovery_market.search_tokens(query=address, chain_ids=requested_chains)
    candidates = _required_dex_token_candidates(candidates, reason="address_search")
    result = _lookup_result(search_requests=1)
    writes = []
    for candidate in candidates:
        candidate_address = _normalize_address(candidate.address)
        candidate_chain = _chain_id(candidate.chain_id)
        if candidate_address != address:
            continue
        if chain_id and candidate_chain != chain_id:
            continue
        writes.append(
            {
                "candidate": candidate,
                "evidence_kind": EVIDENCE_OKX_DEX_EXACT_ADDRESS,
                "confidence": CONFIDENCE_PROVIDER_EXACT,
                "lookup_mode": "exact_address",
                "provider_rank": None,
            }
        )
        if candidate_chain:
            result["affected_lookup_keys"].append(f"address:{candidate_chain}:{address}")
    if writes:
        result["affected_lookup_keys"].append(f"address:{chain_id or 'unknown'}:{address}")
    result["_candidate_writes"] = writes
    return result


def _persist_lookup_provider_result(*, repos: Any, lookup_result: dict[str, Any], now_ms: int) -> None:
    for item in lookup_result.pop("_candidate_writes", []):
        asset_id = _write_dex_candidate(
            repos=repos,
            candidate=item["candidate"],
            now_ms=now_ms,
            evidence_kind=str(item["evidence_kind"]),
            confidence=str(item["confidence"]),
            lookup_mode=str(item["lookup_mode"]),
            provider_rank=item.get("provider_rank"),
        )
        if not asset_id:
            continue
        lookup_result["candidate_ids"].append(asset_id)
        lookup_result["search_hits"] += 1
        lookup_result["assets_written"] += 1


def _write_dex_candidate(
    *,
    repos: Any,
    candidate: DexTokenCandidate,
    now_ms: int,
    evidence_kind: str,
    confidence: str,
    lookup_mode: str,
    provider_rank: int | None = None,
) -> str | None:
    candidate = _require_dex_token_candidate(candidate, reason="write_candidate")
    chain_id = _chain_id(candidate.chain_id)
    address = _normalize_address(candidate.address)
    symbol = _normalize_symbol(candidate.symbol)
    if not chain_id or not address or not symbol:
        return None
    asset = repos.registry.upsert_chain_asset(
        chain_id=chain_id,
        address=address,
        observed_at_ms=now_ms,
    )
    raw = _required_candidate_raw(candidate)
    raw_payload = {**raw, "payload_hash": _payload_hash(raw)}
    if provider_rank is not None:
        raw_payload["provider_rank"] = provider_rank
    repos.identity_evidence.upsert_identity_evidence(
        asset_id=str(asset["asset_id"]),
        evidence_kind=evidence_kind,
        provider="okx",
        lookup_mode=lookup_mode,
        chain_id=str(asset["chain_id"]),
        address=str(asset["address"]),
        symbol=symbol,
        name=candidate.name,
        decimals=None,
        confidence=confidence,
        raw_payload=raw_payload,
        observed_at_ms=now_ms,
    )
    repos.identity_evidence.recompute_current_identity(str(asset["asset_id"]), now_ms=now_ms)
    return str(asset["asset_id"])


def _merge_lookup_result(result: dict[str, Any], lookup_result: dict[str, Any]) -> None:
    for key in (
        "search_requests",
        "search_hits",
        "search_candidates_seen",
        "search_candidates_rejected",
        "assets_written",
    ):
        result[key] += int(lookup_result.get(key) or 0)


def _empty_result(now_ms: int) -> dict[str, Any]:
    return {
        "now_ms": int(now_ms),
        "lookups_selected": 0,
        "lookups_done": 0,
        "lookups_failed": 0,
        "lookups_terminalized": 0,
        "provider_errors": 0,
        "provider_unavailable": 0,
        "search_requests": 0,
        "search_hits": 0,
        "search_candidates_seen": 0,
        "search_candidates_rejected": 0,
        "assets_written": 0,
        "reprocessed_intents": 0,
        "reprocess": None,
        "affected_lookup_keys": [],
        "discovery_result_counts": {},
        "errors": [],
    }


def _provider_unavailable_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return "provider_unavailable"
    return f"provider_unavailable: {message}"


def _complete_lookup_claims(
    *,
    db: Any,
    worker_name: str,
    claims: list[dict[str, Any]],
    resolved_lookup_keys: set[str],
    due_by_lookup_key: dict[str, int],
    now_ms: int,
    max_attempts: int,
    hot_not_found_retry_ms: int,
    result: dict[str, Any],
) -> None:
    with db.worker_session(worker_name) as repos:
        _finish_lookup_claims(
            repos=repos,
            claims=claims,
            resolved_lookup_keys=resolved_lookup_keys,
            due_by_lookup_key=due_by_lookup_key,
            now_ms=now_ms,
            worker_name=worker_name,
            max_attempts=max_attempts,
            hot_not_found_retry_ms=hot_not_found_retry_ms,
            result=result,
        )


def _finish_lookup_claims(
    *,
    repos: Any,
    claims: list[dict[str, Any]],
    resolved_lookup_keys: set[str],
    due_by_lookup_key: dict[str, int],
    now_ms: int,
    worker_name: str,
    max_attempts: int,
    hot_not_found_retry_ms: int,
    result: dict[str, Any],
) -> None:
    with repos.transaction():
        done = [claim for claim in claims if str(claim.get("lookup_key") or "") in resolved_lookup_keys]
        if done:
            repos.discovery.mark_lookup_done(done, now_ms=now_ms)
        for claim in claims:
            lookup_key = str(claim.get("lookup_key") or "")
            if lookup_key in resolved_lookup_keys:
                continue
            if _claim_retry_budget_exhausted(claim, max_attempts=max_attempts):
                terminal = repos.discovery.terminalize_lookup_claims(
                    [claim],
                    worker_name=worker_name,
                    final_status="not_found",
                    final_reason="not_found_retry_budget_exhausted",
                    now_ms=now_ms,
                )
                result["lookups_terminalized"] += int(terminal.get("terminalized") or 0)
                continue
            repos.discovery.reschedule_lookup_claims(
                [claim],
                due_at_ms=due_by_lookup_key.get(
                    lookup_key,
                    now_ms + hot_not_found_retry_ms,
                ),
                now_ms=now_ms,
            )


def _claim_retry_budget_exhausted(claim: dict[str, Any], *, max_attempts: int) -> bool:
    return _claim_attempt_count(claim) >= max_attempts


def _claim_attempt_count(claim: dict[str, Any]) -> int:
    try:
        value = claim["attempt_count"]
    except KeyError as exc:
        raise ValueError("resolution_refresh_claim_attempt_count_required") from exc
    return require_positive_int(value, error_code="resolution_refresh_claim_attempt_count_required")


def _claim_error_count(claim: dict[str, Any]) -> int:
    try:
        value = claim["error_count"]
    except KeyError as exc:
        raise ValueError("resolution_refresh_claim_error_count_required") from exc
    return require_nonnegative_int(value, error_code="resolution_refresh_claim_error_count_required")


def _next_queue_due_at_ms(
    *,
    lookup: dict[str, Any],
    status: str,
    next_refresh_at_ms: int,
    now_ms: int,
    hot_not_found_retry_ms: int,
) -> int:
    latest_seen_ms = int(lookup.get("latest_seen_ms") or 0)
    if status == "not_found" and latest_seen_ms >= int(now_ms) - HOT_LOOKBACK_MS:
        return int(now_ms) + hot_not_found_retry_ms
    return int(next_refresh_at_ms)


def _lookup_result(
    *,
    search_requests: int = 0,
    search_hits: int = 0,
) -> dict[str, Any]:
    return {
        "search_requests": int(search_requests),
        "search_hits": int(search_hits),
        "search_candidates_seen": 0,
        "search_candidates_rejected": 0,
        "assets_written": 0,
        "candidate_ids": [],
        "affected_lookup_keys": [],
    }


def _retained_symbol_candidates(
    candidates: list[DexTokenCandidate],
    *,
    per_chain_limit: int,
) -> list[DexTokenCandidate]:
    by_chain: dict[str, dict[str, DexTokenCandidate]] = {}
    for candidate in candidates:
        formal_candidate = _require_dex_token_candidate(candidate, reason="retain_symbol_candidate")
        chain_id = _chain_id(formal_candidate.chain_id)
        address = _normalize_address(formal_candidate.address)
        if not chain_id or not address:
            continue
        chain_bucket = by_chain.setdefault(chain_id, {})
        existing = chain_bucket.get(address)
        if existing is None or _candidate_rank_key(formal_candidate) < _candidate_rank_key(existing):
            chain_bucket[address] = formal_candidate
    retained: list[DexTokenCandidate] = []
    for chain_id in sorted(by_chain):
        ranked = sorted(by_chain[chain_id].values(), key=_candidate_rank_key)
        retained.extend(ranked[:per_chain_limit])
    return retained


def _candidate_rank_key(candidate: DexTokenCandidate) -> tuple[float, int, str]:
    candidate = _require_dex_token_candidate(candidate, reason="candidate_rank")
    address = _normalize_address(candidate.address)
    has_price_rank = 0 if candidate.price_usd is not None else 1
    return (-_candidate_quality_score(candidate), has_price_rank, address)


def _provider_ranks(candidates: list[DexTokenCandidate]) -> dict[tuple[str | None, str], int]:
    ranks: dict[tuple[str | None, str], int] = {}
    for index, candidate in enumerate(candidates):
        key = _candidate_identity_key(candidate)
        if key[1] and key not in ranks:
            ranks[key] = index
    return ranks


def _candidate_identity_key(candidate: DexTokenCandidate) -> tuple[str | None, str]:
    candidate = _require_dex_token_candidate(candidate, reason="candidate_identity")
    return (
        _chain_id(candidate.chain_id),
        _normalize_address(candidate.address),
    )


def _candidate_quality_score(candidate: DexTokenCandidate) -> float:
    candidate = _require_dex_token_candidate(candidate, reason="candidate_quality")
    return (
        0.5 * _log10_number(candidate.market_cap_usd)
        + 0.3 * _log10_number(candidate.liquidity_usd)
        + 0.2 * _log10_number(candidate.holders)
    )


def _required_dex_token_candidates(value: Any, *, reason: str) -> list[DexTokenCandidate]:
    if not isinstance(value, list):
        raise RuntimeError(f"dex_token_candidate_list_contract_required:{reason}")
    return [_require_dex_token_candidate(candidate, reason=reason) for candidate in value]


def _require_dex_token_candidate(candidate: Any, *, reason: str) -> DexTokenCandidate:
    if not isinstance(candidate, DexTokenCandidate):
        raise RuntimeError(f"dex_token_candidate_contract_required:{reason}")
    return candidate


def _required_candidate_raw(candidate: DexTokenCandidate) -> dict[str, Any]:
    raw = candidate.raw
    if not isinstance(raw, dict):
        raise RuntimeError("dex_token_candidate_raw_contract_required")
    return dict(raw)


def _log10_number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric <= 0:
        return 0.0
    return math.log10(numeric + 1.0)


def _lookup_type(lookup_key: str) -> str:
    if lookup_key.startswith("symbol:"):
        return "dex_symbol_lookup"
    if lookup_key.startswith("address:"):
        return "address_lookup"
    return "unsupported"


def _parse_address_lookup_key(lookup_key: str) -> dict[str, str | None]:
    value = lookup_key.removeprefix("address:")
    chain_id, separator, address = value.rpartition(":")
    if not separator:
        return {"chain_id": None, "address": _normalize_address(value)}
    if chain_id == "unknown":
        chain_id = ""
    return {"chain_id": chain_id or None, "address": _normalize_address(address)}


def _refresh_ms(*, lookup_key: str, status: str, error_count: int | None = None) -> int:
    if status == "error":
        index = min(
            int(error_count or 0),
            len(ERROR_REFRESH_BACKOFF_MS) - 1,
        )
        return ERROR_REFRESH_BACKOFF_MS[index]
    if lookup_key.startswith("address:"):
        return FOUND_ADDRESS_REFRESH_MS if status == "found" else NOT_FOUND_ADDRESS_REFRESH_MS
    return FOUND_SYMBOL_REFRESH_MS if status == "found" else NOT_FOUND_SYMBOL_REFRESH_MS


def _result_hash(candidate_ids: list[str]) -> str:
    payload = json.dumps(sorted(set(candidate_ids)), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _chain_id(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized.startswith("eip155:"):
        return normalized
    if normalized in {"eth", "ethereum"}:
        return "eip155:1"
    if normalized in {"bsc", "bnb", "bnb_chain"}:
        return "eip155:56"
    if normalized == "base":
        return "eip155:8453"
    if normalized in {"sol", "solana"}:
        return "solana"
    return normalized


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().lstrip("$").upper()


def _normalize_address(value: Any) -> str:
    text = str(value or "").strip()
    return text.lower() if text.lower().startswith("0x") else text


def _now_ms() -> int:
    return int(time.time() * 1000)
