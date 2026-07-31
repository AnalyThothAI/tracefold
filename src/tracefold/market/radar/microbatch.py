from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_VENUES,
    WINDOW_MS,
)
from tracefold.market.radar.output_envelope import (
    OutputRowOversized,
    split_bounded_rows,
)
from tracefold.market.radar.stocks_projection import (
    compute_stocks_radar_target_feature,
    rank_stocks_radar,
)
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    compute_token_radar_target_projection,
    rank_token_radar_closure,
    token_radar_venue_for_rank_input,
)
from tracefold.market.radar.token_radar_rank_source_query import (
    TokenRadarFeatureSourceRequest,
)
from tracefold.market.radar.token_radar_repository import stable_generation_id
from tracefold.platform.postgres.projection_frontier import (
    PROFILE_FRONTIER,
    RADAR_FRONTIER,
)

_CLAIM_LEASE_MS = 15_000
_CLAIM_TRANSACTION_TIMEOUT_SECONDS = 0.5
_CONTROL_TRANSACTION_TIMEOUT_SECONDS = 1.0
_PUBLISH_TRANSACTION_TIMEOUT_SECONDS = 3.0
_STEADY_STATEMENT_TIMEOUT_SECONDS = 3.0
_MAINTENANCE_STATEMENT_TIMEOUT_SECONDS = 120.0
_MAINTENANCE_TRANSACTION_TIMEOUT_SECONDS = 120.0
_TARGET_BATCH_SIZE = 4
_INPUT_ROW_CAP = 10_000
_INPUT_BYTE_CAP = 4 * 1024 * 1024
_OUTPUT_BYTE_CAP = 1 * 1024 * 1024
_PROFILE_DEADLINE_MS = 30_000
_RANK_LIMIT = 100


class RadarShardOversized(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RadarTargetClaim:
    target_type: str
    target_id: str
    input_fingerprint: str
    projection_version: str
    first_dirty_at_ms: int
    deadline_at_ms: int

    def key(self, *, window: str, venue: str) -> dict[str, str]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "window_key": window,
            "venue": venue,
        }


@dataclass(frozen=True, slots=True)
class RadarMicroBatchClaim:
    window: str
    venue: str
    runtime_id: str
    targets: tuple[RadarTargetClaim, ...]

    @property
    def deadline_at_ms(self) -> int:
        return min(target.deadline_at_ms for target in self.targets)


class RadarMicroBatchService:
    """One bounded Radar reducer for a durable window/venue dirty-target set."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str = "radar_projection",
    ) -> None:
        self.db = db
        self.worker_name = worker_name

    def next_due(self, *, now_ms: int) -> dict[str, Any] | None:
        with self._session() as repos:
            frontier = repos.conn.execute(
                """
                SELECT window_key, venue, min(deadline_at_ms) AS deadline_at_ms
                FROM radar_projection_frontiers
                WHERE deadline_at_ms IS NOT NULL
                  AND (
                    (
                      status = 'dirty'
                      AND COALESCE(
                        next_attempt_at_ms,
                        first_dirty_at_ms,
                        deadline_at_ms
                      ) <= %(now_ms)s
                    )
                    OR (
                      status = 'retry_wait'
                      AND COALESCE(next_attempt_at_ms, deadline_at_ms)
                            <= %(now_ms)s
                    )
                    OR (
                      status = 'running'
                      AND claimed_until_ms <= %(now_ms)s
                    )
                  )
                GROUP BY window_key, venue
                ORDER BY
                  min(deadline_at_ms),
                  window_key,
                  venue
                LIMIT 1
                """,
                {"now_ms": int(now_ms)},
            ).fetchone()
            expiry = repos.conn.execute(
                """
                SELECT window_key, venue, expires_at_ms AS deadline_at_ms
                  FROM radar_source_edges
                 WHERE expires_at_ms <= %(now_ms)s
                 ORDER BY expires_at_ms, target_type, target_id,
                          window_key, venue, source_kind, source_id
                 LIMIT 1
                """,
                {"now_ms": int(now_ms)},
            ).fetchone()
        candidates = [dict(row) for row in (frontier, expiry) if row is not None]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda row: (
                int(row["deadline_at_ms"]),
                str(row["window_key"]),
                str(row["venue"]),
            ),
        )

    def claim_batch(
        self,
        *,
        window: str,
        venue: str,
        runtime_id: str,
        now_ms: int,
    ) -> RadarMicroBatchClaim | None:
        owner = UUID(str(runtime_id))
        with (
            self._session(
                transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.radar_source_edges.expire_due(
                now_ms=int(now_ms),
                limit=100,
            )
            rows = repos.conn.execute(
                """
                WITH candidates AS (
                  SELECT target_type, target_id, window_key, venue
                  FROM radar_projection_frontiers
                  WHERE window_key = %(window)s
                    AND venue = %(venue)s
                    AND deadline_at_ms IS NOT NULL
                    AND (
                      (
                        status = 'dirty'
                        AND COALESCE(
                          next_attempt_at_ms,
                          first_dirty_at_ms,
                          deadline_at_ms
                        ) <= %(now_ms)s
                      )
                      OR (
                        status = 'retry_wait'
                        AND COALESCE(next_attempt_at_ms, deadline_at_ms)
                              <= %(now_ms)s
                      )
                      OR (
                        status = 'running'
                        AND claimed_until_ms <= %(now_ms)s
                      )
                    )
                  ORDER BY deadline_at_ms, target_type, target_id
                  LIMIT %(limit)s
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE radar_projection_frontiers frontier
                SET status = 'running',
                    claimed_by = %(runtime_id)s,
                    claimed_until_ms = %(claimed_until_ms)s,
                    claimed_input_fingerprint = frontier.input_fingerprint,
                    claimed_projection_version = frontier.projection_version,
                    updated_at_ms = %(now_ms)s
                FROM candidates
                WHERE frontier.target_type = candidates.target_type
                  AND frontier.target_id = candidates.target_id
                  AND frontier.window_key = candidates.window_key
                  AND frontier.venue = candidates.venue
                RETURNING frontier.*
                """,
                {
                    "window": str(window),
                    "venue": str(venue),
                    "runtime_id": owner,
                    "claimed_until_ms": int(now_ms) + _CLAIM_LEASE_MS,
                    "now_ms": int(now_ms),
                    "limit": _TARGET_BATCH_SIZE,
                },
            ).fetchall()
        if not rows:
            return None
        targets = tuple(
            RadarTargetClaim(
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                input_fingerprint=str(row["claimed_input_fingerprint"]),
                projection_version=str(row["claimed_projection_version"]),
                first_dirty_at_ms=int(row["first_dirty_at_ms"]),
                deadline_at_ms=int(row["deadline_at_ms"]),
            )
            for row in sorted(
                rows,
                key=lambda item: (
                    int(item["deadline_at_ms"]),
                    str(item["target_type"]),
                    str(item["target_id"]),
                ),
            )
        )
        return RadarMicroBatchClaim(
            window=str(window),
            venue=str(venue),
            runtime_id=str(owner),
            targets=targets,
        )

    def load_targets(
        self,
        claim: RadarMicroBatchClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        token_targets = [target for target in claim.targets if target.target_type != "MarketInstrument"]
        stock_targets = [target for target in claim.targets if target.target_type == "MarketInstrument"]
        window_ms = WINDOW_MS[claim.window]
        requests = tuple(
            TokenRadarFeatureSourceRequest(
                request_key=_target_key(target),
                target_type_key=target.target_type,
                identity_id=target.target_id,
                window=claim.window,
                analysis_since_ms=max(
                    int(now_ms) - 7 * window_ms,
                    int(now_ms) - 48 * 60 * 60 * 1000,
                ),
                score_since_ms=int(now_ms) - window_ms,
                now_ms=int(now_ms),
            )
            for target in token_targets
        )
        try:
            with self._session() as repos:
                token_rows = (
                    repos.radar_projection_sources.load_rows_for_requests(
                        requests,
                        row_cap=_INPUT_ROW_CAP,
                    )
                    if requests
                    else {}
                )
                old_features = (
                    repos.conn.execute(
                        """
                WITH targets(target_type, target_id) AS (
                  SELECT *
                  FROM unnest(%s::text[], %s::text[])
                )
                SELECT feature.*
                FROM token_radar_target_features feature
                JOIN targets
                  ON targets.target_type = feature.target_type_key
                 AND targets.target_id = feature.identity_id
                WHERE feature.projection_version = %s
                  AND feature."window" = %s
                ORDER BY
                  feature.target_type_key,
                  feature.identity_id,
                  feature.lane
                """,
                        (
                            [target.target_type for target in token_targets],
                            [target.target_id for target in token_targets],
                            TOKEN_RADAR_PROJECTION_VERSION,
                            claim.window,
                        ),
                    ).fetchall()
                    if token_targets
                    else []
                )
                stock_rows = (
                    repos.conn.execute(
                        """
                SELECT edge.target_id,
                       edge.source_id AS event_id,
                       edge.observed_at_ms AS received_at_ms,
                       event.author_handle,
                       COALESCE(event.text_clean, event.text) AS text,
                       equity.symbol,
                       equity.security_name,
                       equity.exchange,
                       equity.instrument_type
                FROM radar_source_edges edge
                JOIN events event
                  ON event.event_id = edge.source_id
                JOIN us_equity_symbols equity
                  ON equity.market_instrument_id = edge.target_id
                 AND equity.status = 'active'
                WHERE edge.target_type = 'MarketInstrument'
                  AND edge.target_id = ANY(%s::text[])
                  AND edge.window_key = %s
                  AND edge.venue = %s
                  AND edge.source_kind = 'event'
                  AND edge.observed_at_ms >= %s
                ORDER BY
                  edge.target_id,
                  edge.observed_at_ms DESC,
                  edge.source_id DESC
                LIMIT %s
                """,
                        (
                            [target.target_id for target in stock_targets],
                            claim.window,
                            claim.venue,
                            int(now_ms) - window_ms,
                            _INPUT_ROW_CAP + 1,
                        ),
                    ).fetchall()
                    if stock_targets
                    else []
                )
        except RuntimeError as exc:
            if "shard_oversized" in str(exc):
                raise RadarShardOversized(str(exc)) from exc
            raise
        if len(stock_rows) > _INPUT_ROW_CAP:
            raise RadarShardOversized("stocks_radar_microbatch_oversized")
        old_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in old_features:
            key = (
                str(row["target_type_key"]),
                str(row["identity_id"]),
            )
            old_by_target.setdefault(key, []).append(dict(row))
        stocks_by_target: dict[str, list[dict[str, Any]]] = {}
        for row in stock_rows:
            stocks_by_target.setdefault(str(row["target_id"]), []).append(dict(row))
        targets: list[dict[str, Any]] = []
        for target in claim.targets:
            if target.target_type == "MarketInstrument":
                targets.append(
                    {
                        "kind": "stocks",
                        "target_type": target.target_type,
                        "target_id": target.target_id,
                        "window": claim.window,
                        "now_ms": int(now_ms),
                        "rows": stocks_by_target.get(target.target_id, []),
                    }
                )
                continue
            old_venues = {TOKEN_RADAR_DEFAULT_VENUE}
            for row in old_by_target.get(
                (target.target_type, target.target_id),
                [],
            ):
                venue = token_radar_venue_for_rank_input(row)
                if venue in TOKEN_RADAR_VENUES:
                    old_venues.add(venue)
            targets.append(
                {
                    "kind": "token",
                    "target_type": target.target_type,
                    "target_id": target.target_id,
                    "window": claim.window,
                    "now_ms": int(now_ms),
                    "source_rows": token_rows.get(_target_key(target), []),
                    "old_venues": sorted(old_venues),
                }
            )
        payload = {
            "window": claim.window,
            "venue": claim.venue,
            "now_ms": int(now_ms),
            "targets": targets,
        }
        _require_bounded_input(payload)
        return payload

    def load_rank_inputs(
        self,
        claim: RadarMicroBatchClaim,
        *,
        projections: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        token_projections = [item for item in projections["targets"] if item["kind"] == "token"]
        stock_projections = [item for item in projections["targets"] if item["kind"] == "stocks"]
        venues = {TOKEN_RADAR_DEFAULT_VENUE}
        for item in token_projections:
            venues.update(str(value) for value in item["projection"]["old_venues"])
            venues.add(str(item["projection"]["target_venue"]))
        venues &= set(TOKEN_RADAR_VENUES)
        try:
            with self._session() as repos:
                compact_inputs = (
                    repos.token_radar.list_compact_rank_inputs(
                        projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                        window=claim.window,
                        min_latest_event_received_at_ms=(int(now_ms) - WINDOW_MS[claim.window]),
                        row_cap=_INPUT_ROW_CAP,
                    )
                    if token_projections
                    else []
                )
                current_stock_features = (
                    [
                        dict(row)
                        for row in repos.conn.execute(
                            """
                        SELECT *
                        FROM stock_attention_target_features
                        WHERE window_key = %s
                        ORDER BY target_id
                        LIMIT %s
                        """,
                            (claim.window, _INPUT_ROW_CAP + 1),
                        ).fetchall()
                    ]
                    if stock_projections
                    else []
                )
        except RuntimeError as exc:
            if "shard_oversized" in str(exc):
                raise RadarShardOversized(str(exc)) from exc
            raise
        if len(current_stock_features) > _INPUT_ROW_CAP:
            raise RadarShardOversized("stocks_radar_rank_input_oversized")
        payload = {
            "window": claim.window,
            "venue": claim.venue,
            "now_ms": int(now_ms),
            "venues": sorted(venues),
            "compact_inputs": compact_inputs,
            "current_stock_features": current_stock_features,
            "target_projections": projections["targets"],
        }
        _require_bounded_input(payload)
        return payload

    def load_hydration(
        self,
        claim: RadarMicroBatchClaim,
        *,
        ranked: dict[str, Any],
    ) -> list[dict[str, Any]]:
        batch_identities = {
            (
                str(feature["lane"]),
                str(feature["target_type_key"]),
                str(feature["identity_id"]),
            )
            for feature in ranked["features"]
        }
        identities = [
            tuple(str(part) for part in identity)
            for identity in ranked["selected_identities"]
            if tuple(str(part) for part in identity) not in batch_identities
        ]
        if len(identities) > len(TOKEN_RADAR_VENUES) * 2 * _RANK_LIMIT:
            raise RadarShardOversized("radar_rank_hydration_shard_oversized")
        with self._session() as repos:
            rows = cast(
                list[dict[str, Any]],
                repos.token_radar.hydrate_rank_inputs(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    identities=identities,
                ),
            )
        _require_bounded_input({"rows": rows})
        return rows

    def publish(
        self,
        claim: RadarMicroBatchClaim,
        *,
        projections: dict[str, Any],
        ranked: dict[str, Any],
        closure: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        _require_bounded_output(closure)
        stock_projection = ranked.get("stock_projection")
        if isinstance(stock_projection, dict):
            _require_bounded_output(stock_projection)
        with (
            self._session(
                transaction_timeout_seconds=self._publish_transaction_timeout_seconds(),
            ) as repos,
            repos.transaction(),
        ):
            if not self._lock_claims(repos, claim):
                return {
                    "projection_status": "stale_snapshot",
                    "rows_written": 0,
                    "targets_loaded": len(claim.targets),
                }
            feature_writes = 0
            for item in projections["targets"]:
                target = _claim_target(
                    claim,
                    target_type=str(item["target_type"]),
                    target_id=str(item["target_id"]),
                )
                projection = dict(item["projection"])
                if item["kind"] == "stocks":
                    feature_writes += _write_stock_target_feature(
                        repos,
                        window=claim.window,
                        target_id=target.target_id,
                        feature=projection.get("feature"),
                    )
                else:
                    feature_writes += _write_token_target_feature(
                        repos,
                        window=claim.window,
                        target=target,
                        projection=projection,
                        now_ms=int(now_ms),
                    )
            publication_writes = 0
            publication_statuses: list[str] = []
            for venue, rows_value in sorted(dict(closure.get("rows_by_venue") or {}).items()):
                rows = [dict(row) for row in rows_value]
                generation_id = stable_generation_id(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    venue=str(venue),
                    rows=rows,
                )
                result = repos.token_radar.publish_current_generation(
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    window=claim.window,
                    venue=str(venue),
                    generation_id=generation_id,
                    published_at_ms=int(now_ms),
                    source_frontier_ms=max(
                        (int(row.get("source_max_received_at_ms") or 0) for row in rows),
                        default=0,
                    ),
                    rows=rows,
                    source_rows=int(ranked["source_rows_by_venue"].get(str(venue), 0)),
                    started_at_ms=int(now_ms),
                    finished_at_ms=int(now_ms),
                    on_current_changes=profile_frontier_callback(repos),
                )
                status = str(result["status"])
                if status not in {"published", "unchanged"}:
                    raise RuntimeError(f"radar_atomic_publication_failed:{venue}:{status}")
                publication_statuses.append(status)
                publication_writes += int(result["rows_written"])
            if isinstance(stock_projection, dict):
                stock_writes = _publish_stock_rank_rows(
                    repos,
                    window=claim.window,
                    projection=stock_projection,
                    now_ms=int(now_ms),
                )
                publication_writes += stock_writes
                publication_statuses.append("published" if stock_writes else "unchanged")
            remaining_dirty = 0
            for target in claim.targets:
                if not repos.projection_frontiers.complete(
                    RADAR_FRONTIER,
                    key=target.key(window=claim.window, venue=claim.venue),
                    runtime_id=claim.runtime_id,
                    input_fingerprint=target.input_fingerprint,
                    version=target.projection_version,
                    now_ms=int(now_ms),
                ):
                    raise RuntimeError("radar_microbatch_completion_cas_mismatch")
            remaining_dirty = int(
                repos.conn.execute(
                    """
                    SELECT count(*) AS count
                    FROM radar_projection_frontiers
                    WHERE window_key = %s
                      AND venue = %s
                      AND status IN ('dirty', 'retry_wait')
                    """,
                    (claim.window, claim.venue),
                ).fetchone()["count"]
            )
        rows_written = feature_writes + publication_writes
        return {
            "projection_status": (
                "published" if "published" in publication_statuses or feature_writes else "unchanged"
            ),
            "rows_written": rows_written,
            "feature_rows_written": feature_writes,
            "publication_rows_written": publication_writes,
            "targets_loaded": len(claim.targets),
            "window": claim.window,
            "venue": claim.venue,
            "remaining_dirty_targets": remaining_dirty,
        }

    def fail_transient(
        self,
        claim: RadarMicroBatchClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> int:
        return self._fail(
            claim,
            error_code=error_code,
            now_ms=now_ms,
            deterministic=False,
        )["failed_targets"]

    def release_prework(self, claim: RadarMicroBatchClaim, *, now_ms: int) -> int:
        released = 0
        with (
            self._session(
                transaction_timeout_seconds=_CONTROL_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            for target in claim.targets:
                released += int(
                    repos.projection_frontiers.release_prework(
                        RADAR_FRONTIER,
                        key=target.key(window=claim.window, venue=claim.venue),
                        runtime_id=claim.runtime_id,
                        now_ms=int(now_ms),
                    )
                )
        return released

    def fail_deterministic(
        self,
        claim: RadarMicroBatchClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> dict[str, int]:
        return self._fail(
            claim,
            error_code=error_code,
            now_ms=now_ms,
            deterministic=True,
        )

    def _fail(
        self,
        claim: RadarMicroBatchClaim,
        *,
        error_code: str,
        now_ms: int,
        deterministic: bool,
    ) -> dict[str, int]:
        failed = 0
        quarantined = 0
        with (
            self._session(
                transaction_timeout_seconds=_CONTROL_TRANSACTION_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            for target in claim.targets:
                row = repos.conn.execute(
                    """
                    SELECT input_fingerprint, projection_version,
                           claimed_input_fingerprint,
                           claimed_projection_version
                    FROM radar_projection_frontiers
                    WHERE target_type = %s
                      AND target_id = %s
                      AND window_key = %s
                      AND venue = %s
                      AND status = 'running'
                      AND claimed_by = %s
                    FOR UPDATE
                    """,
                    (
                        target.target_type,
                        target.target_id,
                        claim.window,
                        claim.venue,
                        UUID(claim.runtime_id),
                    ),
                ).fetchone()
                if row is None:
                    continue
                failed += 1
                latest_is_claimed = (
                    str(row["input_fingerprint"]) == target.input_fingerprint
                    and str(row["projection_version"]) == target.projection_version
                    and str(row["claimed_input_fingerprint"]) == target.input_fingerprint
                    and str(row["claimed_projection_version"]) == target.projection_version
                )
                key = target.key(window=claim.window, venue=claim.venue)
                if not latest_is_claimed:
                    repos.projection_frontiers.release_stale(
                        RADAR_FRONTIER,
                        key=key,
                        runtime_id=claim.runtime_id,
                        now_ms=int(now_ms),
                    )
                    continue
                if deterministic:
                    result = repos.projection_frontiers.fail_deterministic(
                        RADAR_FRONTIER,
                        key=key,
                        runtime_id=claim.runtime_id,
                        error_code=error_code,
                        now_ms=int(now_ms),
                    )
                    if result and result["status"] == "quarantined":
                        quarantined += 1
                else:
                    repos.projection_frontiers.fail_transient(
                        RADAR_FRONTIER,
                        key=key,
                        runtime_id=claim.runtime_id,
                        error_code=error_code,
                        now_ms=int(now_ms),
                    )
        return {
            "failed_targets": failed,
            "quarantined_targets": quarantined,
        }

    @staticmethod
    def _lock_claims(repos: Any, claim: RadarMicroBatchClaim) -> bool:
        rows = repos.conn.execute(
            """
            WITH targets(target_type, target_id, claimed_fingerprint,
                         claimed_version) AS (
              SELECT *
              FROM unnest(
                %s::text[], %s::text[], %s::text[], %s::text[]
              )
            )
            SELECT frontier.target_type, frontier.target_id
            FROM radar_projection_frontiers frontier
            JOIN targets
              ON targets.target_type = frontier.target_type
             AND targets.target_id = frontier.target_id
             AND targets.claimed_fingerprint
                   = frontier.claimed_input_fingerprint
             AND targets.claimed_version
                   = frontier.claimed_projection_version
            WHERE frontier.window_key = %s
              AND frontier.venue = %s
              AND frontier.status = 'running'
              AND frontier.claimed_by = %s
            ORDER BY frontier.target_type, frontier.target_id
            FOR UPDATE OF frontier
            """,
            (
                [target.target_type for target in claim.targets],
                [target.target_id for target in claim.targets],
                [target.input_fingerprint for target in claim.targets],
                [target.projection_version for target in claim.targets],
                claim.window,
                claim.venue,
                UUID(claim.runtime_id),
            ),
        ).fetchall()
        return len(rows) == len(claim.targets)

    def _session(
        self,
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=(
                _MAINTENANCE_STATEMENT_TIMEOUT_SECONDS
                if self.worker_name == "radar_maintenance_rebuild"
                else _STEADY_STATEMENT_TIMEOUT_SECONDS
            ),
            transaction_timeout_seconds=transaction_timeout_seconds,
        )

    def _publish_transaction_timeout_seconds(self) -> float:
        if self.worker_name == "radar_maintenance_rebuild":
            return _MAINTENANCE_TRANSACTION_TIMEOUT_SECONDS
        return _PUBLISH_TRANSACTION_TIMEOUT_SECONDS


def compute_radar_target_batch(payload: dict[str, Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for item in payload["targets"]:
        if item["kind"] == "stocks":
            projection = compute_stocks_radar_target_feature(item)
        else:
            projection = compute_token_radar_target_projection(item)
        targets.append(
            {
                "kind": str(item["kind"]),
                "target_type": str(item["target_type"]),
                "target_id": str(item["target_id"]),
                "projection": projection,
            }
        )
    return {"targets": targets}


def rank_radar_microbatch(payload: dict[str, Any]) -> dict[str, Any]:
    token_items = [item for item in payload["target_projections"] if item["kind"] == "token"]
    stock_items = [item for item in payload["target_projections"] if item["kind"] == "stocks"]
    replaced_token_targets = {
        (
            str(item["target_type"]),
            str(item["target_id"]),
        )
        for item in token_items
    }
    features = [
        dict(item["projection"]["feature"])
        for item in token_items
        if isinstance(item["projection"].get("feature"), dict)
    ]
    compact_inputs = [
        dict(row)
        for row in payload["compact_inputs"]
        if (
            str(row.get("target_type_key") or ""),
            str(row.get("identity_id") or ""),
        )
        not in replaced_token_targets
    ]
    result: dict[str, Any] = {
        "features": features,
        "selected_by_venue": {},
        "selected_identities": [],
        "source_rows_by_venue": {},
        "stock_projection": None,
    }
    if token_items:
        result.update(
            rank_token_radar_closure(
                {
                    "window": payload["window"],
                    "now_ms": payload["now_ms"],
                    "features": features,
                    "compact_inputs": compact_inputs,
                    "venues": payload["venues"],
                    "rank_limit": _RANK_LIMIT,
                }
            )
        )
    if stock_items:
        replaced = {str(item["target_id"]): item["projection"].get("feature") for item in stock_items}
        current = [dict(row) for row in payload["current_stock_features"] if str(row["target_id"]) not in replaced]
        current.extend(dict(feature) for feature in replaced.values() if isinstance(feature, dict))
        result["stock_projection"] = rank_stocks_radar(
            {
                "window": payload["window"],
                "now_ms": payload["now_ms"],
                "current_features": current,
            }
        )
    return result


def hydrate_radar_microbatch(
    *,
    ranked: dict[str, Any],
    hydrated_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not ranked["selected_by_venue"]:
        return {"rows_by_venue": {}}
    return build_token_radar_current_closure(
        {
            "selected_by_venue": ranked["selected_by_venue"],
            "features": ranked["features"],
            "hydrated_inputs": hydrated_inputs,
        }
    )


def _write_token_target_feature(
    repos: Any,
    *,
    window: str,
    target: RadarTargetClaim,
    projection: dict[str, Any],
    now_ms: int,
) -> int:
    feature = projection.get("feature")
    raw_projected = projection.get("projected")
    writes = 0
    if isinstance(feature, dict) and isinstance(raw_projected, dict):
        writes += int(
            repos.token_radar.upsert_target_feature(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=window,
                row=raw_projected,
                computed_at_ms=int(now_ms),
            )
        )
        opposite_lane = "attention" if str(feature["lane"]) == "resolved" else "resolved"
        writes += int(
            repos.token_radar.delete_target_feature(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=window,
                lane=opposite_lane,
                target_type_key=target.target_type,
                identity_id=target.target_id,
            )
        )
        return writes
    for lane in ("resolved", "attention"):
        writes += int(
            repos.token_radar.delete_target_feature(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                window=window,
                lane=lane,
                target_type_key=target.target_type,
                identity_id=target.target_id,
            )
        )
    return writes


def _write_stock_target_feature(
    repos: Any,
    *,
    window: str,
    target_id: str,
    feature: Any,
) -> int:
    if not isinstance(feature, dict):
        return int(
            repos.conn.execute(
                """
                DELETE FROM stock_attention_target_features
                WHERE window_key = %s
                  AND target_id = %s
                """,
                (window, target_id),
            ).rowcount
            or 0
        )
    return int(
        repos.conn.execute(
            """
            INSERT INTO stock_attention_target_features (
              window_key, target_id, symbol, security_name,
              exchange, instrument_type, mentions, unique_authors,
              latest_seen_ms, latest_event_id,
              latest_author_handle, latest_text, source_event_ids,
              state_fingerprint, computed_at_ms
            )
            VALUES (
              %(window_key)s, %(target_id)s, %(symbol)s,
              %(security_name)s, %(exchange)s,
              %(instrument_type)s, %(mentions)s,
              %(unique_authors)s, %(latest_seen_ms)s,
              %(latest_event_id)s, %(latest_author_handle)s,
              %(latest_text)s, %(source_event_ids)s,
              %(state_fingerprint)s, %(computed_at_ms)s
            )
            ON CONFLICT (window_key, target_id) DO UPDATE SET
              symbol = EXCLUDED.symbol,
              security_name = EXCLUDED.security_name,
              exchange = EXCLUDED.exchange,
              instrument_type = EXCLUDED.instrument_type,
              mentions = EXCLUDED.mentions,
              unique_authors = EXCLUDED.unique_authors,
              latest_seen_ms = EXCLUDED.latest_seen_ms,
              latest_event_id = EXCLUDED.latest_event_id,
              latest_author_handle = EXCLUDED.latest_author_handle,
              latest_text = EXCLUDED.latest_text,
              source_event_ids = EXCLUDED.source_event_ids,
              state_fingerprint = EXCLUDED.state_fingerprint,
              computed_at_ms = EXCLUDED.computed_at_ms
            WHERE stock_attention_target_features.state_fingerprint
                  IS DISTINCT FROM EXCLUDED.state_fingerprint
            """,
            feature,
        ).rowcount
        or 0
    )


def _publish_stock_rank_rows(
    repos: Any,
    *,
    window: str,
    projection: dict[str, Any],
    now_ms: int,
) -> int:
    state = repos.conn.execute(
        """
        SELECT state_fingerprint
        FROM stocks_radar_publication_state
        WHERE window_key = %s
        FOR UPDATE
        """,
        (window,),
    ).fetchone()
    if state is not None and str(state["state_fingerprint"]) == str(projection["state_fingerprint"]):
        return 0
    writes = int(
        repos.conn.execute(
            "DELETE FROM stocks_radar_current_rows WHERE window_key = %s",
            (window,),
        ).rowcount
        or 0
    )
    for row in projection["rows"]:
        writes += int(
            repos.conn.execute(
                """
                INSERT INTO stocks_radar_current_rows (
                  window_key, target_id, rank, symbol,
                  security_name, exchange, instrument_type,
                  mentions, unique_authors, latest_seen_ms,
                  latest_event_id, latest_author_handle,
                  latest_text, source_event_ids,
                  state_fingerprint, computed_at_ms
                )
                VALUES (
                  %(window_key)s, %(target_id)s, %(rank)s,
                  %(symbol)s, %(security_name)s, %(exchange)s,
                  %(instrument_type)s, %(mentions)s,
                  %(unique_authors)s, %(latest_seen_ms)s,
                  %(latest_event_id)s, %(latest_author_handle)s,
                  %(latest_text)s, %(source_event_ids)s,
                  %(state_fingerprint)s, %(computed_at_ms)s
                )
                """,
                row,
            ).rowcount
            or 0
        )
    writes += int(
        repos.conn.execute(
            """
            INSERT INTO stocks_radar_publication_state (
              window_key, state_fingerprint,
              source_frontier_ms, published_at_ms
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (window_key) DO UPDATE SET
              state_fingerprint = EXCLUDED.state_fingerprint,
              source_frontier_ms = EXCLUDED.source_frontier_ms,
              published_at_ms = EXCLUDED.published_at_ms
            """,
            (
                window,
                projection["state_fingerprint"],
                projection["source_frontier_ms"],
                int(now_ms),
            ),
        ).rowcount
        or 0
    )
    return writes


def profile_frontier_callback(repos: Any) -> Any:
    def callback(
        *,
        window: str,
        venue: str,
        rows: list[dict[str, Any]],
        exited_rows: list[dict[str, Any]],
        previous_by_key: dict[tuple[str, str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> None:
        del window
        if venue != TOKEN_RADAR_DEFAULT_VENUE:
            return
        current_counts = Counter(
            (str(row["target_type_key"]), str(row["identity_id"]))
            for row in rows
            if str(row.get("target_type_key") or "") in {"Asset", "CexToken"}
        )
        previous_counts = Counter(
            (str(row["target_type_key"]), str(row["identity_id"]))
            for row in previous_by_key.values()
            if str(row.get("target_type_key") or "") in {"Asset", "CexToken"}
        )
        for row in exited_rows:
            target = (
                str(row.get("target_type_key") or ""),
                str(row.get("identity_id") or ""),
            )
            if target[0] in {"Asset", "CexToken"} and target not in previous_counts:
                previous_counts[target] = 1
        changed = {
            target
            for target in set(current_counts) | set(previous_counts)
            if bool(current_counts[target]) != bool(previous_counts[target])
        }
        if not changed:
            return
        ordered = sorted(changed)
        counts = {
            (str(row["target_type"]), str(row["target_id"])): int(row["serving_rows"])
            for row in repos.conn.execute(
                """
                WITH targets(target_type, target_id) AS (
                  SELECT *
                  FROM unnest(%s::text[], %s::text[])
                )
                SELECT target.target_type, target.target_id,
                       count(current.row_id) AS serving_rows
                FROM targets target
                LEFT JOIN token_radar_current_rows current
                  ON current.projection_version = %s
                 AND current.target_type_key = target.target_type
                 AND current.identity_id = target.target_id
                GROUP BY target.target_type, target.target_id
                """,
                (
                    [target_type for target_type, _target_id in ordered],
                    [target_id for _target_type, target_id in ordered],
                    TOKEN_RADAR_PROJECTION_VERSION,
                ),
            ).fetchall()
        }
        for target_type, target_id in ordered:
            new_count = counts[(target_type, target_id)]
            old_count = new_count - current_counts[(target_type, target_id)] + previous_counts[(target_type, target_id)]
            serving = new_count > 0
            if serving == (old_count > 0):
                continue
            repos.projection_frontiers.mark_dirty(
                PROFILE_FRONTIER,
                key={
                    "target_type": target_type,
                    "target_id": target_id,
                },
                dirty_at_ms=int(computed_at_ms),
                deadline_at_ms=int(computed_at_ms) + _PROFILE_DEADLINE_MS,
                input_fingerprint=_fingerprint(
                    {
                        "kind": "profile_serving_membership",
                        "target_type": target_type,
                        "target_id": target_id,
                        "serving": serving,
                    },
                ),
                version="token-profile-current-serving-v1",
            )

    return callback


def _claim_target(
    claim: RadarMicroBatchClaim,
    *,
    target_type: str,
    target_id: str,
) -> RadarTargetClaim:
    for target in claim.targets:
        if target.target_type == target_type and target.target_id == target_id:
            return target
    raise RuntimeError("radar_microbatch_projection_target_unclaimed")


def _target_key(target: RadarTargetClaim) -> str:
    return json.dumps(
        [target.target_type, target.target_id],
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _require_bounded_input(payload: dict[str, Any]) -> None:
    row_count = sum(
        len(payload.get(key, []))
        for key in (
            "compact_inputs",
            "current_stock_features",
            "hydrated_inputs",
            "rows",
            "target_projections",
        )
    )
    for target in payload.get("targets", []):
        if not isinstance(target, dict):
            continue
        row_count += len(target.get("source_rows", []))
        row_count += len(target.get("rows", []))
    if row_count > _INPUT_ROW_CAP:
        raise RadarShardOversized("radar_input_row_overflow")
    if _serialized_size(payload) > _INPUT_BYTE_CAP:
        raise RadarShardOversized("radar_input_byte_overflow")


def _require_bounded_output(payload: dict[str, Any]) -> None:
    _require_bounded_output_rows(
        payload.get("rows", []),
        context={"output_kind": "rows"},
    )
    for venue, rows in dict(payload.get("rows_by_venue") or {}).items():
        rows_by_lane: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            lane = str(row.get("lane") or "")
            rows_by_lane.setdefault(lane, []).append(dict(row))
        for lane, lane_rows in rows_by_lane.items():
            _require_bounded_output_rows(
                lane_rows,
                context={"window_venue_lane": [str(venue), lane]},
            )


def _require_bounded_output_rows(
    rows: Any,
    *,
    context: dict[str, Any],
) -> None:
    try:
        split_bounded_rows(
            [dict(row) for row in rows],
            context=context,
            byte_cap=_OUTPUT_BYTE_CAP,
        )
    except OutputRowOversized as exc:
        raise RadarShardOversized("radar_output_byte_overflow") from exc


__all__ = [
    "RadarMicroBatchClaim",
    "RadarMicroBatchService",
    "RadarShardOversized",
    "RadarTargetClaim",
    "compute_radar_target_batch",
    "hydrate_radar_microbatch",
    "profile_frontier_callback",
    "rank_radar_microbatch",
]
