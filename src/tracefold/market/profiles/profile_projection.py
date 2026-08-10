from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from tracefold.market.profiles.profile_source_ids import (
    ASSET_PROFILE_REFRESH_PROVIDERS,
    INACTIVE_PROFILE_TARGET_DELETE_BATCH,
    inactive_asset_profile_provider_ids,
)
from tracefold.market.profiles.token_image_source_admission import (
    TokenImageSourceCandidate,
    admit_token_image_sources,
    image_source_candidates_for_target,
    inspect_token_image_sources,
)
from tracefold.market.profiles.token_profile_current_projection import (
    project_token_profile_current,
)
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER

PROFILE_PROJECTION_VERSION = "token-profile-current-serving-v1"
_CLAIM_LEASE_MS = 30_000
_CLAIM_TRANSACTION_TIMEOUT_SECONDS = 0.5
_PUBLISH_TRANSACTION_TIMEOUT_SECONDS = 1.0
_STEADY_STATEMENT_TIMEOUT_SECONDS = 3.0
_MAINTENANCE_STATEMENT_TIMEOUT_SECONDS = 120.0
_INPUT_ROW_CAP = 10_000
_INPUT_BYTE_CAP = 4 * 1024 * 1024
_OUTPUT_BYTE_CAP = 1 * 1024 * 1024
_PRIVATE_CACHE_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000


class ProfileShardOversized(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileProjectionClaim:
    target_type: str
    target_id: str
    runtime_id: str
    input_fingerprint: str
    projection_version: str
    deadline_at_ms: int

    @property
    def key(self) -> dict[str, str]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
        }


class ProfileProjectionService:
    """Typed serving-set projection and hard cleanup for one target."""

    def __init__(
        self,
        *,
        db: Any,
        active_profile_provider_ids: tuple[str, ...],
        worker_name: str = "profile_projection",
    ) -> None:
        self.db = db
        self.active_profile_provider_ids = _require_active_profile_provider_ids(active_profile_provider_ids)
        self.worker_name = worker_name

    def next_due(self, *, now_ms: int) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.next_due(
                    PROFILE_FRONTIER,
                    now_ms=now_ms,
                ),
            )

    def claim(
        self,
        *,
        target_type: str,
        target_id: str,
        runtime_id: str,
        now_ms: int,
    ) -> ProfileProjectionClaim | None:
        key = {
            "target_type": str(target_type),
            "target_id": str(target_id),
        }
        with (
            self._session(
                transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            row = repos.projection_frontiers.claim(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=now_ms,
                lease_ms=_CLAIM_LEASE_MS,
            )
        if row is None:
            return None
        return ProfileProjectionClaim(
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            runtime_id=str(UUID(str(runtime_id))),
            input_fingerprint=str(row["input_fingerprint"]),
            projection_version=str(row["projection_version"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
        )

    def load_target(
        self,
        claim: ProfileProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with self._session() as repos:
            loaded = _load_profile_snapshot(
                repos,
                target_type=claim.target_type,
                target_id=claim.target_id,
                now_ms=now_ms,
            )
        _require_bounded_input(loaded)
        return loaded

    def publish(
        self,
        claim: ProfileProjectionClaim,
        *,
        loaded: dict[str, Any],
        output: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        _require_bounded_output(output)
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            frontier = repos.conn.execute(
                """
                SELECT status, claimed_by, input_fingerprint, projection_version
                FROM token_profile_projection_frontiers
                WHERE target_type = %s AND target_id = %s
                FOR UPDATE
                """,
                (claim.target_type, claim.target_id),
            ).fetchone()
            if not _claim_still_current(frontier, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }
            current = _load_profile_snapshot(
                repos,
                target_type=claim.target_type,
                target_id=claim.target_id,
                now_ms=now_ms,
            )
            if current["snapshot_fingerprint"] != loaded["snapshot_fingerprint"]:
                repos.projection_frontiers.mark_dirty(
                    PROFILE_FRONTIER,
                    key=claim.key,
                    dirty_at_ms=int(now_ms),
                    deadline_at_ms=int(now_ms) + 30_000,
                    input_fingerprint=current["snapshot_fingerprint"],
                    version=PROFILE_PROJECTION_VERSION,
                )
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                }

            if output["operation"] == "delete":
                rows_written = _delete_outside_serving_state(
                    repos,
                    target_type=claim.target_type,
                    target_id=claim.target_id,
                )
                cursor = repos.conn.execute(
                    """
                    DELETE FROM token_profile_projection_frontiers
                    WHERE target_type = %s
                      AND target_id = %s
                      AND status = 'running'
                      AND claimed_by = %s
                      AND input_fingerprint = %s
                      AND projection_version = %s
                    """,
                    (
                        claim.target_type,
                        claim.target_id,
                        UUID(claim.runtime_id),
                        claim.input_fingerprint,
                        claim.projection_version,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError("profile_outside_serving_frontier_cas_mismatch")
                return {
                    "projection_status": "deleted_outside_serving",
                    "rows_written": rows_written,
                    "target_type": claim.target_type,
                    "target_id": claim.target_id,
                }

            candidates = [TokenImageSourceCandidate(**dict(candidate)) for candidate in output["image_candidates"]]
            admission = admit_token_image_sources(
                repos=repos,
                candidates=candidates,
                now_ms=int(now_ms),
            )
            changed = repos.token_profiles.upsert_current(dict(output["row"]))
            _ensure_asset_profile_recovery(
                repos,
                snapshot=current,
                active_profile_provider_ids=self.active_profile_provider_ids,
                now_ms=int(now_ms),
            )
            if not repos.projection_frontiers.complete(
                PROFILE_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=int(now_ms),
            ):
                raise RuntimeError("profile_frontier_completion_cas_mismatch")
        return {
            "projection_status": "published" if changed else "unchanged",
            "rows_written": int(bool(changed)) + int(admission.counts.get("admitted") or 0),
            "target_type": claim.target_type,
            "target_id": claim.target_id,
        }

    def release_stale(
        self,
        claim: ProfileProjectionClaim,
        *,
        now_ms: int,
    ) -> None:
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.projection_frontiers.release_stale(
                PROFILE_FRONTIER,
                key=claim.key,
                runtime_id=claim.runtime_id,
                now_ms=now_ms,
            )

    def release_prework(
        self,
        claim: ProfileProjectionClaim,
        *,
        now_ms: int,
    ) -> bool:
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            return bool(
                repos.projection_frontiers.release_prework(
                    PROFILE_FRONTIER,
                    key=claim.key,
                    runtime_id=claim.runtime_id,
                    now_ms=now_ms,
                )
            )

    def fail_deterministic(
        self,
        claim: ProfileProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.fail_deterministic(
                    PROFILE_FRONTIER,
                    key=claim.key,
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )

    def fail_transient(
        self,
        claim: ProfileProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with (
            self._session(
                transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            return bool(
                repos.projection_frontiers.fail_transient(
                    PROFILE_FRONTIER,
                    key=claim.key,
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                )
            )

    def _session(
        self,
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=(
                _MAINTENANCE_STATEMENT_TIMEOUT_SECONDS
                if self.worker_name == "profile_maintenance_rebuild"
                else _STEADY_STATEMENT_TIMEOUT_SECONDS
            ),
            transaction_timeout_seconds=transaction_timeout_seconds,
        )


def rebuild_all_profiles_for_maintenance(
    *,
    db: Any,
    app_home: str | Path,
    active_profile_provider_ids: tuple[str, ...],
    now_ms: int,
) -> dict[str, Any]:
    """Sweep outside-serving recovery state and rebuild serving profiles."""

    service = ProfileProjectionService(
        db=db,
        active_profile_provider_ids=active_profile_provider_ids,
        worker_name="profile_maintenance_rebuild",
    )
    serving_predicate = """
        (
          target.target_type = 'Asset'
          AND EXISTS (
            SELECT 1
            FROM registry_assets asset
            WHERE asset.asset_id = target.target_id
              AND asset.status IN ('candidate', 'canonical')
          )
        )
        OR (
          target.target_type = 'CexToken'
          AND EXISTS (
            SELECT 1
            FROM cex_tokens token
            WHERE token.cex_token_id = target.target_id
              AND token.status IN ('candidate', 'canonical')
          )
        )
    """
    inactive_providers = inactive_asset_profile_provider_ids(service.active_profile_provider_ids)
    inactive_targets_deleted = 0
    while inactive_providers:
        with service._session() as repos, repos.transaction():
            deleted = repos.asset_profile_refresh_targets.delete_inactive_provider_targets(
                inactive_providers=inactive_providers,
                limit=INACTIVE_PROFILE_TARGET_DELETE_BATCH,
            )
        inactive_targets_deleted += int(deleted)
        if int(deleted) < INACTIVE_PROFILE_TARGET_DELETE_BATCH:
            break

    cleanup_counts: dict[str, int] = {
        "inactive_profile_refresh": inactive_targets_deleted,
    }
    with service._session() as repos, repos.transaction():
        cleanup_counts["profile_current"] = int(
            repos.conn.execute(
                f"""
                DELETE FROM token_profile_current target
                WHERE NOT ({serving_predicate})
                """,
            ).rowcount
            or 0
        )
        cleanup_counts["profile_refresh_targets"] = int(
            repos.conn.execute(
                f"""
                DELETE FROM asset_profile_refresh_targets target
                WHERE NOT ({serving_predicate})
                """,
            ).rowcount
            or 0
        )
        cleanup_counts["image_refresh_targets"] = int(
            repos.conn.execute(
                f"""
                DELETE FROM token_image_source_dirty_targets target
                WHERE NOT ({serving_predicate})
                """,
            ).rowcount
            or 0
        )
        cleanup_counts["asset_profile_cache"] = int(
            repos.conn.execute(
                """
                DELETE FROM asset_profiles source
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM registry_assets asset
                  WHERE asset.asset_id = source.asset_id
                    AND asset.status IN ('candidate', 'canonical')
                )
                """,
            ).rowcount
            or 0
        )
        cleanup_counts["cex_profile_cache"] = int(
            repos.conn.execute(
                """
                DELETE FROM cex_token_profiles source
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM cex_tokens token
                  WHERE token.cex_token_id = source.cex_token_id
                    AND token.status IN ('candidate', 'canonical')
                )
                """,
            ).rowcount
            or 0
        )
        cleanup_counts["frontiers"] = int(
            repos.conn.execute("DELETE FROM token_profile_projection_frontiers").rowcount or 0
        )
        serving_targets = [
            (str(row["target_type"]), str(row["target_id"]))
            for row in repos.conn.execute(
                """
                SELECT 'Asset' AS target_type, asset_id AS target_id
                FROM registry_assets
                WHERE status IN ('candidate', 'canonical')
                UNION ALL
                SELECT 'CexToken' AS target_type, cex_token_id AS target_id
                FROM cex_tokens
                WHERE status IN ('candidate', 'canonical')
                ORDER BY target_type, target_id
                """,
            ).fetchall()
        ]

    for target_type, target_id in serving_targets:
        with service._session() as repos, repos.transaction():
            snapshot = _load_profile_snapshot(
                repos,
                target_type=target_type,
                target_id=target_id,
                now_ms=int(now_ms),
            )
            repos.projection_frontiers.mark_dirty(
                PROFILE_FRONTIER,
                key={
                    "target_type": target_type,
                    "target_id": target_id,
                },
                dirty_at_ms=int(now_ms),
                deadline_at_ms=int(now_ms),
                input_fingerprint=str(snapshot["snapshot_fingerprint"]),
                version=PROFILE_PROJECTION_VERSION,
            )

    runtime_id = str(uuid4())
    results: list[dict[str, Any]] = []
    while True:
        due = service.next_due(now_ms=int(now_ms))
        if due is None:
            break
        claim = service.claim(
            target_type=str(due["target_type"]),
            target_id=str(due["target_id"]),
            runtime_id=runtime_id,
            now_ms=int(now_ms),
        )
        if claim is None:
            raise RuntimeError("profile_maintenance_claim_missing")
        loaded = service.load_target(claim, now_ms=int(now_ms))
        output = compute_profile_current_projection(loaded)
        result = service.publish(
            claim,
            loaded=loaded,
            output=output,
            now_ms=int(now_ms),
        )
        if result["projection_status"] not in {
            "published",
            "unchanged",
            "deleted_outside_serving",
        }:
            raise RuntimeError(f"profile_maintenance_publish_failed:{result['projection_status']}")
        results.append(result)

    image_cleanup = _delete_expired_private_image_cache(
        service=service,
        app_home=Path(app_home),
        now_ms=int(now_ms),
    )
    return {
        "projection_status": "rebuilt",
        "serving_targets": len(serving_targets),
        "shards_computed": len(results),
        "rows_written": sum(int(row["rows_written"]) for row in results),
        "cleanup": cleanup_counts,
        "image_cache_cleanup": image_cleanup,
    }


def _delete_expired_private_image_cache(
    *,
    service: ProfileProjectionService,
    app_home: Path,
    now_ms: int,
) -> dict[str, int]:
    cutoff_ms = int(now_ms) - _PRIVATE_CACHE_RETENTION_MS
    with service._session() as repos, repos.transaction():
        removed = [
            dict(row)
            for row in repos.conn.execute(
                """
                DELETE FROM token_image_assets image
                WHERE image.updated_at_ms < %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM token_image_source_dirty_targets target
                    WHERE target.source_url_hash = image.source_url_hash
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM token_profile_current current
                    WHERE current.logo_url = image.public_url
                  )
                RETURNING storage_path
                """,
                (cutoff_ms,),
            ).fetchall()
        ]
    cache_root = (app_home / "cache" / "token-images").resolve()
    files_deleted = 0
    for row in removed:
        storage_path = row.get("storage_path")
        if not isinstance(storage_path, str) or not storage_path:
            continue
        relative = Path(storage_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RuntimeError("profile_image_cache_path_invalid")
        path = (cache_root / relative).resolve()
        try:
            path.relative_to(cache_root)
        except ValueError as exc:
            raise RuntimeError("profile_image_cache_path_escape") from exc
        if path.is_file():
            path.unlink()
            files_deleted += 1
    return {
        "rows_deleted": len(removed),
        "files_deleted": files_deleted,
    }


def compute_profile_current_projection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Pure spawn-safe Profile reducer for one serving target."""

    if not bool(payload["serving"]):
        return {
            "operation": "delete",
            "target_type": str(payload["target"]["target_type"]),
            "target_id": str(payload["target"]["target_id"]),
        }
    candidates = image_source_candidates_for_target(
        target=payload["target"],
        gmgn_openapi=payload["gmgn_openapi"],
        binance_web3=payload["binance_web3"],
        gmgn_stream=payload["gmgn_stream"],
        okx_dex=payload["okx_dex"],
        cex_profile=payload["cex_profile"],
    )
    row = project_token_profile_current(
        target=payload["target"],
        gmgn_openapi=payload["gmgn_openapi"],
        binance_web3=payload["binance_web3"],
        gmgn_stream=payload["gmgn_stream"],
        okx_dex=payload["okx_dex"],
        cex_profile=payload["cex_profile"],
        image_states_by_source_key=payload["image_states"],
        computed_at_ms=int(payload["now_ms"]),
    )
    return {
        "operation": "upsert",
        "row": row,
        "image_candidates": [asdict(candidate) for candidate in candidates],
    }


def _load_profile_snapshot(
    repos: Any,
    *,
    target_type: str,
    target_id: str,
    now_ms: int,
) -> dict[str, Any]:
    serving = _is_serving_identity(
        repos,
        target_type=target_type,
        target_id=target_id,
    )
    target = {
        "target_type": str(target_type),
        "target_id": str(target_id),
    }
    if not serving:
        snapshot = {
            "serving": False,
            "target": target,
            "now_ms": int(now_ms),
            "gmgn_openapi": None,
            "binance_web3": None,
            "gmgn_stream": None,
            "okx_dex": None,
            "cex_profile": None,
            "image_states": {},
            "asset_identity": None,
        }
        snapshot["snapshot_fingerprint"] = _fingerprint({"serving": False, "target": target})
        return snapshot

    gmgn_openapi = None
    binance_web3 = None
    gmgn_stream = None
    okx_dex = None
    cex_profile = None
    asset_identity = None
    if target_type == "Asset":
        gmgn_openapi = repos.source_query.gmgn_openapi_profiles([target_id]).get(target_id)
        binance_web3 = repos.source_query.binance_web3_profiles([target_id]).get(target_id)
        gmgn_stream = repos.source_query.gmgn_stream_profiles([target_id]).get(target_id)
        okx_dex = repos.source_query.okx_dex_profiles([target_id]).get(target_id)
        identity_row = repos.conn.execute(
            """
            SELECT asset.asset_id, asset.chain_id, asset.address,
                   current.canonical_symbol
            FROM registry_assets asset
            LEFT JOIN asset_identity_current current
              ON current.asset_id = asset.asset_id
            WHERE asset.asset_id = %s
            """,
            (target_id,),
        ).fetchone()
        asset_identity = dict(identity_row) if identity_row is not None else None
    elif target_type == "CexToken":
        cex_profile = repos.source_query.cex_token_profiles([target_id]).get(target_id)
    candidates = image_source_candidates_for_target(
        target=target,
        gmgn_openapi=gmgn_openapi,
        binance_web3=binance_web3,
        gmgn_stream=gmgn_stream,
        okx_dex=okx_dex,
        cex_profile=cex_profile,
    )
    admission = inspect_token_image_sources(
        repos=repos,
        candidates=candidates,
        now_ms=int(now_ms),
    )
    image_states = admission.image_states_by_source_key
    snapshot = {
        "serving": True,
        "target": target,
        "now_ms": int(now_ms),
        "gmgn_openapi": gmgn_openapi,
        "binance_web3": binance_web3,
        "gmgn_stream": gmgn_stream,
        "okx_dex": okx_dex,
        "cex_profile": cex_profile,
        "image_states": image_states,
        "asset_identity": asset_identity,
    }
    snapshot["snapshot_fingerprint"] = _fingerprint(
        {
            "serving": True,
            "gmgn_openapi": gmgn_openapi,
            "binance_web3": binance_web3,
            "gmgn_stream": gmgn_stream,
            "okx_dex": okx_dex,
            "cex_profile": cex_profile,
            "image_states": image_states,
            "asset_identity": asset_identity,
        }
    )
    return snapshot


def _is_serving_identity(
    repos: Any,
    *,
    target_type: str,
    target_id: str,
) -> bool:
    if target_type == "Asset":
        row = repos.conn.execute(
            """
            SELECT 1
            FROM registry_assets
            WHERE asset_id = %s
              AND status IN ('candidate', 'canonical')
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "CexToken":
        row = repos.conn.execute(
            """
            SELECT 1
            FROM cex_tokens
            WHERE cex_token_id = %s
              AND status IN ('candidate', 'canonical')
            """,
            (target_id,),
        ).fetchone()
    else:
        return False
    return row is not None


def _delete_outside_serving_state(
    repos: Any,
    *,
    target_type: str,
    target_id: str,
) -> int:
    mutations = 0
    for table in (
        "token_profile_current",
        "asset_profile_refresh_targets",
        "token_image_source_dirty_targets",
    ):
        cursor = repos.conn.execute(
            f"""
            DELETE FROM {table}
            WHERE target_type = %s AND target_id = %s
            """,
            (target_type, target_id),
        )
        mutations += int(cursor.rowcount or 0)
    if target_type == "Asset":
        mutations += int(
            repos.conn.execute(
                "DELETE FROM asset_profiles WHERE asset_id = %s",
                (target_id,),
            ).rowcount
            or 0
        )
    elif target_type == "CexToken":
        mutations += int(
            repos.conn.execute(
                "DELETE FROM cex_token_profiles WHERE cex_token_id = %s",
                (target_id,),
            ).rowcount
            or 0
        )
    return mutations


def _ensure_asset_profile_recovery(
    repos: Any,
    *,
    snapshot: dict[str, Any],
    active_profile_provider_ids: tuple[str, ...],
    now_ms: int,
) -> None:
    identity = snapshot.get("asset_identity")
    if not isinstance(identity, dict):
        return
    chain_id = str(identity.get("chain_id") or "").strip()
    address = str(identity.get("address") or "").strip()
    if not chain_id or not address:
        return
    target_id = str(snapshot["target"]["target_id"])
    targets = [
        {
            "provider": provider,
            "target_type": "Asset",
            "target_id": target_id,
            "chain_id": chain_id,
            "address": address,
            "symbol": identity.get("canonical_symbol"),
            "payload_hash": _fingerprint(
                {
                    "version": "asset-profile-serving-v1",
                    "provider": provider,
                    "target_id": target_id,
                    "chain_id": chain_id,
                    "address": address,
                    "symbol": identity.get("canonical_symbol"),
                }
            ),
            "source_watermark_ms": int(now_ms),
            "heat_tier": "hot",
            "priority": 20,
            "due_at_ms": int(now_ms),
        }
        for provider in active_profile_provider_ids
    ]
    repos.asset_profile_refresh_targets.enqueue_targets(
        targets,
        reason="serving_set_active",
        now_ms=int(now_ms),
    )


def _claim_still_current(
    row: Any,
    claim: ProfileProjectionClaim,
) -> bool:
    if row is None:
        return False
    return (
        str(row["status"]) == "running"
        and str(row["claimed_by"]) == claim.runtime_id
        and str(row["input_fingerprint"]) == claim.input_fingerprint
        and str(row["projection_version"]) == claim.projection_version
    )


def _require_active_profile_provider_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    providers = tuple(dict.fromkeys(str(value or "").strip() for value in values))
    unknown = sorted(provider for provider in providers if provider not in ASSET_PROFILE_REFRESH_PROVIDERS)
    if unknown:
        raise ValueError(f"profile_projection_provider_invalid:{','.join(unknown)}")
    return providers


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        _canonical_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            _canonical_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {str(key): _canonical_json_value(item) for key, item in value.items()}
        entries = [
            {
                "key": _canonical_json_value(key),
                "value": _canonical_json_value(item),
            }
            for key, item in value.items()
        ]
        entries.sort(
            key=lambda entry: json.dumps(
                entry["key"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        )
        return {"__mapping_entries__": entries}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ),
        )
    return value


def _require_bounded_input(payload: dict[str, Any]) -> None:
    row_count = sum(
        1
        for key in (
            "gmgn_openapi",
            "binance_web3",
            "gmgn_stream",
            "okx_dex",
            "cex_profile",
        )
        if payload.get(key) is not None
    ) + len(payload.get("image_states", {}))
    if row_count > _INPUT_ROW_CAP:
        raise ProfileShardOversized("profile_input_row_overflow")
    if _serialized_size(payload) > _INPUT_BYTE_CAP:
        raise ProfileShardOversized("profile_input_byte_overflow")


def _require_bounded_output(payload: dict[str, Any]) -> None:
    if _serialized_size(payload) > _OUTPUT_BYTE_CAP:
        raise ProfileShardOversized("profile_output_byte_overflow")


__all__ = [
    "PROFILE_PROJECTION_VERSION",
    "ProfileProjectionClaim",
    "ProfileProjectionService",
    "ProfileShardOversized",
    "compute_profile_current_projection",
    "rebuild_all_profiles_for_maintenance",
]
