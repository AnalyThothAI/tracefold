from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
)
from tracefold.market.radar.payload_hash import stable_token_radar_payload_hash
from tracefold.market.radar.token_radar_projector import TokenRadarProjector
from tracefold.market.radar.token_radar_repository import stable_generation_id

PROJECTION_VERSION = TOKEN_RADAR_PROJECTION_VERSION
ASSET_PROFILE_REFRESH_PROVIDERS = ("gmgn_dex_profile", "binance_web3_profile")


class TokenRadarPublisher:
    """Publishes stable Token Radar rows and acknowledges their durable dirty claims."""

    def __init__(self, *, repos: Any, projector: TokenRadarProjector) -> None:
        self.repos = repos
        self.projector = projector

    def rebuild_dirty_targets(
        self,
        *,
        windows: tuple[str, ...] = (),
        work_items: tuple[tuple[str, ...], ...] | None = None,
        score_work_items: tuple[tuple[str, ...], ...] | None = None,
        now_ms: int | None = None,
        limit: int,
        rank_limit: int,
        lease_ms: int,
        retry_ms: int,
        max_attempts: int,
        lease_owner: str,
        claimed_targets: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        dirty_lease_ms = _positive_worker_policy(
            lease_ms,
            error="token_radar_projection_lease_ms_invalid",
        )
        dirty_retry_ms = _positive_worker_policy(
            retry_ms,
            error="token_radar_projection_retry_ms_invalid",
        )
        dirty_max_attempts = _positive_worker_policy(
            max_attempts,
            error="token_radar_projection_max_attempts_invalid",
        )
        with self.repos.transaction():
            return self._rebuild_dirty_targets_in_transaction(
                windows=windows,
                work_items=work_items,
                score_work_items=score_work_items,
                now_ms=now_ms,
                limit=limit,
                rank_limit=rank_limit,
                lease_ms=dirty_lease_ms,
                retry_ms=dirty_retry_ms,
                max_attempts=dirty_max_attempts,
                lease_owner=lease_owner,
                claimed_targets=claimed_targets,
            )

    def _rebuild_dirty_targets_in_transaction(
        self,
        *,
        windows: tuple[str, ...],
        work_items: tuple[tuple[str, ...], ...] | None,
        score_work_items: tuple[tuple[str, ...], ...] | None,
        now_ms: int | None,
        limit: int,
        rank_limit: int,
        lease_ms: int,
        retry_ms: int,
        max_attempts: int,
        lease_owner: str,
        claimed_targets: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        computed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        source_work_items = _resolve_score_work_items(
            windows=windows,
            work_items=score_work_items if score_work_items is not None else work_items,
        )
        due_work_items = _resolve_due_work_items(work_items=work_items)
        target_claims = (
            [dict(claim) for claim in claimed_targets]
            if claimed_targets is not None
            else self.repos.token_radar_dirty_targets.claim_due(
                limit=limit,
                lease_ms=lease_ms,
                now_ms=computed_at_ms,
                lease_owner=lease_owner,
            )
        )
        result: dict[str, Any] = {
            "computed_at_ms": computed_at_ms,
            "rows_written": 0,
            "source_rows": 0,
            "claimed": len(target_claims),
            "catch_up_enqueued": 0,
            "windows": {},
            "status": "idle" if not target_claims else "ready",
        }
        if not target_claims and not due_work_items:
            return result

        claim_keys = {_claim_identity_key(claim): _claim_key(claim) for claim in target_claims}
        successful_claims: list[tuple[dict[str, str | int], set[tuple[str, str]]]] = []
        touched: set[tuple[str, str]] = set()
        failures = 0
        first_error: str | None = None
        first_publish_error: str | None = None
        failed_publish_items: set[tuple[str, str]] = set()

        if target_claims:
            projected_claims = self.projector.project_claims(
                claimed_targets=target_claims,
                work_items=source_work_items,
                now_ms=computed_at_ms,
            )
            for projected in projected_claims:
                claim_key = claim_keys[_claim_identity_key(projected.claim)]
                result["source_rows"] += projected.source_rows
                if projected.error is not None:
                    failures += 1
                    first_error = first_error or projected.error
                    self.repos.token_radar_dirty_targets.mark_error(
                        [claim_key],
                        error=projected.error,
                        retry_ms=retry_ms,
                        max_attempts=max_attempts,
                        worker_name=lease_owner,
                        now_ms=computed_at_ms,
                    )
                    continue
                rank_sets = set(projected.rank_sets)
                touched.update(rank_sets)
                successful_claims.append((claim_key, rank_sets))

        publish_items = set(due_work_items)
        publish_items.update(touched)
        if publish_items:
            result["status"] = "ready"
        for window, venues in _group_publish_items(publish_items).items():
            rank_results = self.publish_rank_sets(
                window=window,
                venues=venues,
                now_ms=computed_at_ms,
                limit=rank_limit,
            )
            for venue in venues:
                rank_result = rank_results[venue]
                rank_status = str(rank_result.get("status") or "")
                if rank_status not in {"ready", "unchanged"}:
                    error = str(rank_result.get("error") or f"rank refresh did not publish current rows: {rank_status}")
                    failures += 1
                    first_error = first_error or error
                    first_publish_error = first_publish_error or error
                    failed_publish_items.add((window, venue))
                result["windows"][f"{window}:{venue}"] = rank_result
                result["rows_written"] += int(rank_result.get("rows_written") or 0)

        if failures:
            result["status"] = "failed"
            errors = [str(item.get("error")) for item in result["windows"].values() if item.get("error")]
            result["error"] = errors[0] if errors else first_error or "dirty target projection failed"
            self._finish_successful_claims(
                successful_claims=successful_claims,
                failed_publish_items=failed_publish_items,
                error=first_publish_error or first_error or str(result["error"]),
                retry_ms=retry_ms,
                max_attempts=max_attempts,
                worker_name=lease_owner,
                now_ms=computed_at_ms,
            )
        elif successful_claims:
            self.repos.token_radar_dirty_targets.mark_done(
                [claim_key for claim_key, _rank_sets in successful_claims],
                now_ms=computed_at_ms,
            )
        return result

    def _finish_successful_claims(
        self,
        *,
        successful_claims: list[tuple[dict[str, str | int], set[tuple[str, str]]]],
        failed_publish_items: set[tuple[str, str]],
        error: str,
        retry_ms: int,
        max_attempts: int,
        worker_name: str,
        now_ms: int,
    ) -> None:
        done_claims = [
            claim_key
            for claim_key, rank_sets in successful_claims
            if not rank_sets or not rank_sets.intersection(failed_publish_items)
        ]
        retry_claims = [
            claim_key
            for claim_key, rank_sets in successful_claims
            if rank_sets and rank_sets.intersection(failed_publish_items)
        ]
        if done_claims:
            self.repos.token_radar_dirty_targets.mark_done(done_claims, now_ms=now_ms)
        if retry_claims:
            self.repos.token_radar_dirty_targets.mark_error(
                retry_claims,
                error=error,
                retry_ms=retry_ms,
                max_attempts=max_attempts,
                worker_name=worker_name,
                now_ms=now_ms,
            )

    def publish_rank_set(
        self,
        *,
        window: str,
        now_ms: int,
        limit: int,
        venue: str = TOKEN_RADAR_DEFAULT_VENUE,
    ) -> dict[str, Any]:
        result = self.publish_rank_sets(
            window=window,
            venues=(venue,),
            now_ms=now_ms,
            limit=limit,
        )[venue]
        if result.get("status") == "failed":
            raise RuntimeError(str(result.get("error") or "rank publication failed"))
        return result

    def publish_rank_sets(
        self,
        *,
        window: str,
        venues: tuple[str, ...],
        now_ms: int,
        limit: int,
    ) -> dict[str, dict[str, Any]]:
        computed_at_ms = int(now_ms)
        requested_venues = tuple(dict.fromkeys(str(venue) for venue in venues))
        try:
            projected_by_venue = self.projector.build_rank_sets(
                window=window,
                venues=requested_venues,
                now_ms=computed_at_ms,
                limit=limit,
            )
        except Exception as exc:
            return {
                venue: self._failed_rank_publication(
                    window=window,
                    venue=venue,
                    computed_at_ms=computed_at_ms,
                    error=exc,
                )
                for venue in requested_venues
            }
        results: dict[str, dict[str, Any]] = {}
        for venue in requested_venues:
            try:
                results[venue] = self._publish_rank_projection(
                    window=window,
                    venue=venue,
                    computed_at_ms=computed_at_ms,
                    projected=projected_by_venue[venue],
                )
            except Exception as exc:
                results[venue] = self._failed_rank_publication(
                    window=window,
                    venue=venue,
                    computed_at_ms=computed_at_ms,
                    error=exc,
                )
        return results

    def _publish_rank_projection(
        self,
        *,
        window: str,
        venue: str,
        computed_at_ms: int,
        projected: Any,
    ) -> dict[str, Any]:
        rows = list(projected.rows)
        generation_id = stable_generation_id(
            projection_version=PROJECTION_VERSION,
            window=window,
            venue=venue,
            rows=rows,
        )
        source_frontier_ms = max((int(row.get("source_max_received_at_ms") or 0) for row in rows), default=0)
        with self.repos.transaction():
            publication_result = self.repos.token_radar.publish_current_generation(
                projection_version=PROJECTION_VERSION,
                window=window,
                venue=venue,
                generation_id=generation_id,
                published_at_ms=computed_at_ms,
                source_frontier_ms=source_frontier_ms,
                rows=rows,
                source_rows=projected.source_rows,
                started_at_ms=computed_at_ms,
                finished_at_ms=_now_ms(),
                on_current_changes=self._enqueue_downstream_dirty_targets,
            )
        status = str(publication_result.get("status") or "")
        published_generation_id = str(publication_result.get("generation_id") or generation_id)
        rows_written = int(publication_result.get("rows_written") or 0)
        if status == "stale_skipped":
            return {
                "rows_written": 0,
                "source_rows": projected.source_rows,
                "computed_at_ms": computed_at_ms,
                "generation_id": published_generation_id,
                "status": "stale_skipped",
            }
        if status not in {"published", "unchanged"}:
            raise RuntimeError(f"rank refresh did not publish current rows: {status or 'unknown'}")
        return {
            "rows_written": rows_written,
            "source_rows": projected.source_rows,
            "computed_at_ms": computed_at_ms,
            "generation_id": published_generation_id,
            "status": "ready" if status == "published" else "unchanged",
        }

    def _failed_rank_publication(
        self,
        *,
        window: str,
        venue: str,
        computed_at_ms: int,
        error: Exception,
    ) -> dict[str, Any]:
        with self.repos.transaction():
            self.repos.token_radar.mark_publication_failed(
                projection_version=PROJECTION_VERSION,
                window=window,
                venue=venue,
                generation_id=f"attempt:{PROJECTION_VERSION}:{window}:{venue}:{computed_at_ms}",
                started_at_ms=computed_at_ms,
                finished_at_ms=_now_ms(),
                error=str(error),
            )
        return {
            "rows_written": 0,
            "source_rows": 0,
            "computed_at_ms": computed_at_ms,
            "status": "failed",
            "error": str(error),
        }

    def _enqueue_downstream_dirty_targets(
        self,
        *,
        window: str,
        venue: str,
        rows: list[dict[str, Any]],
        exited_rows: list[dict[str, Any]],
        previous_by_key: dict[tuple[str, str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> None:
        if str(venue) != TOKEN_RADAR_DEFAULT_VENUE:
            return
        self._enqueue_token_profiles(
            window=window,
            rows=rows,
            exited_rows=exited_rows,
            previous_by_key=previous_by_key,
            computed_at_ms=computed_at_ms,
        )
        self._enqueue_asset_profiles(
            window=window,
            rows=rows,
            previous_by_key=previous_by_key,
            computed_at_ms=computed_at_ms,
        )

    def _enqueue_token_profiles(
        self,
        *,
        window: str,
        rows: list[dict[str, Any]],
        exited_rows: list[dict[str, Any]],
        previous_by_key: dict[tuple[str, str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> None:
        targets: list[dict[str, Any]] = []
        for row in rows:
            previous = previous_by_key.get(_current_key(row))
            if previous is not None and _payload_hash(previous) == _payload_hash(row):
                continue
            target = _token_profile_target(
                row,
                previous=previous,
                window=window,
                computed_at_ms=computed_at_ms,
                exited=False,
            )
            if target is not None:
                targets.append(target)
        for row in exited_rows:
            target = _token_profile_target(
                row,
                previous=row,
                window=window,
                computed_at_ms=computed_at_ms,
                exited=True,
            )
            if target is not None:
                targets.append(target)
        _enqueue_by_reason(self.repos.token_profile_current_dirty_targets, targets, now_ms=computed_at_ms)

    def _enqueue_asset_profiles(
        self,
        *,
        window: str,
        rows: list[dict[str, Any]],
        previous_by_key: dict[tuple[str, str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> None:
        targets: list[dict[str, Any]] = []
        for row in rows:
            previous = previous_by_key.get(_current_key(row))
            if previous is not None and _payload_hash(previous) == _payload_hash(row):
                continue
            targets.extend(
                _asset_profile_targets(
                    row,
                    previous=previous,
                    window=window,
                    computed_at_ms=computed_at_ms,
                )
            )
        _enqueue_by_reason(self.repos.asset_profile_refresh_targets, targets, now_ms=computed_at_ms)


def _resolve_work_items(
    *,
    windows: tuple[str, ...],
    venues: tuple[str, ...],
    work_items: tuple[tuple[str, ...], ...] | None,
) -> tuple[tuple[str, str], ...]:
    if work_items is not None:
        return tuple(dict.fromkeys(_normalize_work_item(item) for item in work_items if item))
    resolved_venues = venues or (TOKEN_RADAR_DEFAULT_VENUE,)
    return tuple((window, venue) for window in windows for venue in resolved_venues)


def _resolve_score_work_items(
    *,
    windows: tuple[str, ...],
    work_items: tuple[tuple[str, ...], ...] | None,
) -> tuple[str, ...]:
    if work_items is not None:
        return tuple(dict.fromkeys(str(item[0]) for item in work_items if item))
    return tuple(dict.fromkeys(windows))


def _resolve_due_work_items(
    *,
    work_items: tuple[tuple[str, ...], ...] | None,
) -> tuple[tuple[str, str], ...]:
    if work_items is None:
        return ()
    return _resolve_work_items(windows=(), venues=(), work_items=work_items)


def _group_publish_items(
    work_items: set[tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for window, venue in sorted(work_items):
        grouped.setdefault(window, []).append(venue)
    return {key: tuple(venues) for key, venues in grouped.items()}


def _normalize_work_item(item: tuple[str, ...]) -> tuple[str, str]:
    window = str(item[0])
    venue = str(item[1]) if len(item) >= 2 and item[1] else TOKEN_RADAR_DEFAULT_VENUE
    return (window, venue)


def _positive_worker_policy(value: int, *, error: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(error)
    return value


def _claim_key(claim: Mapping[str, Any]) -> dict[str, str | int]:
    return {
        "target_type_key": _required_claim_text(
            claim,
            "target_type_key",
            error="token_radar_dirty_claim_identity_contract_required",
        ),
        "identity_id": _required_claim_text(
            claim,
            "identity_id",
            error="token_radar_dirty_claim_identity_contract_required",
        ),
        "payload_hash": _required_claim_text(
            claim,
            "payload_hash",
            error="token_radar_dirty_claim_payload_hash_contract_required",
        ),
        "lease_owner": _required_claim_text(
            claim,
            "lease_owner",
            error="token_radar_dirty_claim_lease_owner_contract_required",
        ),
        "attempt_count": _claim_attempt_count(claim),
    }


def _claim_identity_key(claim: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _required_claim_text(claim, "target_type_key", error="token_radar_dirty_claim_identity_contract_required"),
        _required_claim_text(claim, "identity_id", error="token_radar_dirty_claim_identity_contract_required"),
    )


def _required_claim_text(claim: Mapping[str, Any], column: str, *, error: str) -> str:
    value = claim.get(column)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise RuntimeError(error)
    return text


def _claim_attempt_count(claim: Mapping[str, Any]) -> int:
    value = claim.get("attempt_count")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("token_radar_dirty_claim_attempt_contract_required")
    return value


def _current_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (_required_current_text(row, "lane"), *_required_identity(row))


def _required_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return (_required_current_text(row, "target_type_key"), _required_current_text(row, "identity_id"))


def _required_current_text(row: Mapping[str, Any], column: str) -> str:
    value = row.get(column)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise RuntimeError("token_radar_current_identity_required")
    return text


def _payload_hash(row: Mapping[str, Any]) -> str:
    value = row.get("payload_hash")
    payload_hash = str(value).strip() if value is not None else ""
    if not payload_hash:
        raise RuntimeError("token_radar_rank_change_payload_hash_required")
    return payload_hash


def _resolved_target(row: Mapping[str, Any]) -> tuple[str, str] | None:
    target_type, target_id = _required_identity(row)
    if target_type not in {"Asset", "CexToken"}:
        return None
    return target_type, target_id


def _token_profile_target(
    row: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
    window: str,
    computed_at_ms: int,
    exited: bool,
) -> dict[str, Any] | None:
    resolved = _resolved_target(row)
    if resolved is None:
        return None
    target_type, target_id = resolved
    reason = _change_reason(row, previous=previous, exited=exited)
    source_watermark_ms = _source_watermark_ms(row)
    payload = {
        "target_type": target_type,
        "target_id": target_id,
        "window": str(window),
        "rank": row.get("rank"),
        "lane": row.get("lane"),
        "decision": row.get("decision"),
        "exited": exited,
        "source_watermark_ms": source_watermark_ms,
        "token_radar_payload_hash": row.get("payload_hash"),
        "reason": reason,
    }
    return {
        "target_type": target_type,
        "target_id": target_id,
        "dirty_reason": reason,
        "payload_hash": stable_token_radar_payload_hash(payload),
        "source_watermark_ms": source_watermark_ms,
        "priority": 60 if exited else 70,
        "due_at_ms": computed_at_ms,
    }


def _asset_profile_targets(
    row: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
    window: str,
    computed_at_ms: int,
) -> list[dict[str, Any]]:
    resolved = _resolved_target(row)
    if resolved is None or resolved[0] != "Asset":
        return []
    target_type, target_id = resolved
    subject = _rank_subject(row)
    chain_id = _optional_text(subject.get("chain"))
    address = _optional_text(subject.get("address"))
    if not chain_id or not address:
        raise RuntimeError("asset_profile_refresh_target_identity_required")
    symbol = _first_real_symbol(row.get("asset_symbol"), subject.get("symbol"), row.get("display_symbol"))
    reason = _change_reason(row, previous=previous, exited=False)
    source_watermark_ms = _source_watermark_ms(row)
    targets: list[dict[str, Any]] = []
    for provider in ASSET_PROFILE_REFRESH_PROVIDERS:
        payload = {
            "provider": provider,
            "target_type": target_type,
            "target_id": target_id,
            "chain_id": chain_id,
            "address": address,
            "symbol": symbol,
            "window": str(window),
            "rank": row.get("rank"),
            "lane": row.get("lane"),
            "decision": row.get("decision"),
            "source_watermark_ms": source_watermark_ms,
            "token_radar_payload_hash": row.get("payload_hash"),
            "reason": reason,
        }
        targets.append(
            {
                "provider": provider,
                "target_type": target_type,
                "target_id": target_id,
                "chain_id": chain_id,
                "address": address,
                "symbol": symbol,
                "dirty_reason": reason,
                "payload_hash": stable_token_radar_payload_hash(payload),
                "source_watermark_ms": source_watermark_ms,
                "priority": 80,
                "due_at_ms": computed_at_ms,
            }
        )
    return targets


def _change_reason(row: Mapping[str, Any], *, previous: Mapping[str, Any] | None, exited: bool) -> str:
    if exited:
        return "token_radar_exited"
    if previous is None:
        return "token_radar_entered"
    if str(previous.get("lane") or "") != str(row.get("lane") or ""):
        return "token_radar_visibility_changed"
    if str(previous.get("decision") or "") != str(row.get("decision") or ""):
        return "token_radar_visibility_changed"
    if int(previous.get("rank") or 0) != int(row.get("rank") or 0):
        return "token_radar_rank_changed"
    if int(previous.get("source_max_received_at_ms") or 0) != int(row.get("source_max_received_at_ms") or 0):
        return "token_radar_source_watermark_changed"
    return "token_radar_changed"


def _enqueue_by_reason(repo: Any, targets: list[dict[str, Any]], *, now_ms: int) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        item = dict(target)
        grouped.setdefault(str(item.pop("dirty_reason")), []).append(item)
    for reason, reason_targets in grouped.items():
        repo.enqueue_targets(reason_targets, reason=reason, now_ms=now_ms)


def _source_watermark_ms(row: Mapping[str, Any]) -> int:
    value = row.get("source_max_received_at_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("token_radar_downstream_source_watermark_required")
    return value


def _rank_subject(row: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshot = _json_ready(row.get("factor_snapshot_json"))
    if not isinstance(snapshot, Mapping):
        return {}
    subject = snapshot.get("subject")
    return subject if isinstance(subject, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_real_symbol(*values: Any) -> str | None:
    for value in values:
        symbol = _real_symbol(value)
        if symbol is not None:
            return symbol
    return None


def _real_symbol(value: Any) -> str | None:
    symbol = str(value or "").strip().upper()
    if not symbol or symbol in {"UNKNOWN", "N/A", "NA", "NULL", "NONE"}:
        return None
    if _is_address_like_symbol(symbol):
        return None
    return symbol


def _is_address_like_symbol(symbol: str) -> bool:
    normalized = str(symbol).strip()
    if normalized.startswith("0X") and len(normalized) >= 18:
        return True
    return len(normalized) >= 30 and all(char.isalnum() for char in normalized)


def _cex_pricefeed_target(value: Any) -> tuple[str | None, str | None]:
    parts = str(value or "").strip().split(":")
    if len(parts) < 5 or parts[0] != "pricefeed" or parts[1] != "cex":
        return None, None
    return parts[2].strip().lower() or None, parts[-1].strip().upper() or None


def _json_ready(value: Any) -> Any:
    raw = getattr(value, "obj", value)
    if isinstance(raw, Mapping):
        return {str(key): _json_ready(item) for key, item in raw.items()}
    if isinstance(raw, list | tuple | set):
        return [_json_ready(item) for item in raw]
    return raw


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["TokenRadarPublisher"]
