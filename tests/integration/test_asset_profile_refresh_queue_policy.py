from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any, ClassVar

from psycopg import pq

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.app.worker_capabilities import FiniteOperations
from tracefold.market import (
    BINANCE_WEB3_PROFILE_PROVIDER,
    GMGN_DEX_PROFILE_PROVIDER,
    AssetProfileRefresh,
    TokenImageMirror,
)
from tracefold.market.profiles.asset_profile_refresh_worker import _missing_retry_delay_ms
from tracefold.market.provider_contracts import (
    DexProfileSource,
    DexProviderTemporarilyUnavailable,
)

NOW_MS = 1_779_000_000_000
PROVIDER = GMGN_DEX_PROFILE_PROVIDER


class _SessionTrackingDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._lock = Lock()
        self._active_sessions = 0

    @property
    def active_sessions(self) -> int:
        with self._lock:
            return self._active_sessions

    async def run_business(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        del operation_timeout_seconds
        return function(*args, **kwargs)

    @contextmanager
    def worker_session(self, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        with self._lock:
            self._active_sessions += 1
        try:
            with repository_session_for_connection(self.conn) as repos:
                yield repos
        finally:
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                self.conn.rollback()
            with self._lock:
                self._active_sessions -= 1


class _ProviderFailureMarket:
    def __init__(self, db: _SessionTrackingDB) -> None:
        self.db = db
        self.calls = 0

    def token_profile(self, *, chain_id: str, address: str) -> None:
        del chain_id, address
        self.calls += 1
        assert self.db.active_sessions == 0
        raise DexProviderTemporarilyUnavailable("provider unavailable")


class _SupersedingProfileMarket:
    def __init__(self, db: _SessionTrackingDB) -> None:
        self.db = db
        self.calls = 0

    def token_profile(self, *, chain_id: str, address: str) -> None:
        del chain_id, address
        self.calls += 1
        assert self.db.active_sessions == 0
        with repository_session_for_connection(self.db.conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [
                    {
                        "provider": PROVIDER,
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "chain_id": "sol",
                        "address": "address-2",
                        "symbol": "TWO",
                        "source_watermark_ms": NOW_MS + 1,
                        "priority": 20,
                        "heat_tier": "hot",
                        "payload_hash": "sha256:evidence-v2",
                    }
                ],
                reason="newer_evidence",
                now_ms=NOW_MS + 1,
            )

    def close(self) -> None:
        return None


class _ImageResponse:
    url = "https://gmgn.ai/external-res/logo.png"
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "image/png"}
    content = b"\x89PNG\r\n\x1a\nfixture"


class _ImageHttpClient:
    def __init__(self, db: _SessionTrackingDB) -> None:
        self.db = db
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _ImageResponse:
        del url, kwargs
        self.calls += 1
        assert self.db.active_sessions == 0
        return _ImageResponse()


class _SupersedingImageHttpClient(_ImageHttpClient):
    def get(self, url: str, **kwargs: Any) -> _ImageResponse:
        response = super().get(url, **kwargs)
        with repository_session_for_connection(self.db.conn) as repos, repos.transaction():
            repos.token_image_source_dirty_targets.enqueue_targets(
                [
                    {
                        "source_url": _ImageResponse.url,
                        "source_provider": PROVIDER,
                        "source_kind": "logo",
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "raw_ref_json": {"version": 2},
                        "source_watermark_ms": NOW_MS + 1,
                        "priority": 20,
                    }
                ],
                reason="newer_profile_image_candidate",
                now_ms=NOW_MS + 1,
            )
        return response


def _enqueue_profile_target(conn: Any, *, target_id: str = "asset-1") -> None:
    with repository_session_for_connection(conn) as repos, repos.transaction():
        repos.asset_profile_refresh_targets.enqueue_targets(
            [
                {
                    "provider": PROVIDER,
                    "target_type": "Asset",
                    "target_id": target_id,
                    "chain_id": "sol",
                    "address": "address-1",
                    "symbol": "ONE",
                    "source_watermark_ms": NOW_MS,
                    "priority": 20,
                    "heat_tier": "hot",
                    "payload_hash": "sha256:evidence-v1",
                }
            ],
            reason="target_entered",
            now_ms=NOW_MS,
        )


def _enqueue_image_target(conn: Any) -> None:
    with repository_session_for_connection(conn) as repos, repos.transaction():
        repos.token_image_source_dirty_targets.enqueue_targets(
            [
                {
                    "source_url": _ImageResponse.url,
                    "source_provider": PROVIDER,
                    "source_kind": "logo",
                    "target_type": "Asset",
                    "target_id": "asset-1",
                    "raw_ref_json": {"version": 1},
                    "source_watermark_ms": NOW_MS,
                    "priority": 20,
                }
            ],
            reason="profile_image_candidate",
            now_ms=NOW_MS,
        )


def test_missing_profile_retry_schedule_is_code_owned_and_exact() -> None:
    assert [_missing_retry_delay_ms(attempt) for attempt in range(1, 5)] == [
        15 * 60_000,
        30 * 60_000,
        60 * 60_000,
        120 * 60_000,
    ]


def test_startup_reconcile_deletes_only_inactive_provider_targets() -> None:
    conn = connect_postgres_test()
    finite = FiniteOperations()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [
                    {
                        "provider": provider,
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "chain_id": "sol",
                        "address": "address-1",
                        "symbol": "ONE",
                        "source_watermark_ms": NOW_MS,
                        "priority": 20,
                        "heat_tier": "hot",
                        "payload_hash": f"sha256:{provider}",
                    }
                    for provider in (PROVIDER, BINANCE_WEB3_PROFILE_PROVIDER)
                ],
                reason="profile_provider_reconcile",
                now_ms=NOW_MS,
            )
        worker = AssetProfileRefresh(
            db=_SessionTrackingDB(conn),
            dex_profile_sources=(DexProfileSource(provider=PROVIDER, market=object()),),
            finite_operations=finite,
            runtime_id="integration-test",
        )

        result = asyncio.run(worker.reconcile())

        assert result == {
            "active_providers": [PROVIDER],
            "inactive_targets_deleted": 1,
        }
        assert conn.execute("SELECT provider FROM asset_profile_refresh_targets ORDER BY provider").fetchall() == [
            {"provider": PROVIDER}
        ]
    finally:
        finite.close()
        conn.close()


def test_inactive_provider_cleanup_is_bounded_by_one_exact_batch() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [
                    {
                        "provider": BINANCE_WEB3_PROFILE_PROVIDER,
                        "target_type": "Asset",
                        "target_id": f"asset-{index}",
                        "chain_id": "sol",
                        "address": f"address-{index}",
                        "symbol": f"TOKEN{index}",
                        "source_watermark_ms": NOW_MS,
                        "priority": 20,
                        "heat_tier": "hot",
                        "payload_hash": f"sha256:inactive-{index}",
                    }
                    for index in range(2)
                ],
                reason="bounded_inactive_provider_cleanup",
                now_ms=NOW_MS,
            )
            deleted = repos.asset_profile_refresh_targets.delete_inactive_provider_targets(
                inactive_providers=(BINANCE_WEB3_PROFILE_PROVIDER,),
                limit=1,
            )

        assert deleted == 1
        assert conn.execute("SELECT count(*) AS count FROM asset_profile_refresh_targets").fetchone() == {"count": 1}
    finally:
        conn.close()


def test_provider_failure_releases_database_and_consumes_no_target_attempt() -> None:
    conn = connect_postgres_test()
    finite = FiniteOperations()
    try:
        reset_postgres_schema(conn)
        _enqueue_profile_target(conn)
        db = _SessionTrackingDB(conn)
        market = _ProviderFailureMarket(db)
        worker = AssetProfileRefresh(
            db=db,
            dex_profile_sources=(DexProfileSource(provider=PROVIDER, market=market),),
            finite_operations=finite,
            runtime_id="integration-test",
        )

        result = asyncio.run(worker.turn(now_ms=NOW_MS))

        row = conn.execute(
            """
            SELECT attempt_count, lease_owner, due_at_ms
            FROM asset_profile_refresh_targets
            WHERE provider = %s AND target_id = 'asset-1'
            """,
            (PROVIDER,),
        ).fetchone()
        circuit = conn.execute(
            """
            SELECT status, consecutive_failures, next_probe_at_ms
            FROM provider_circuit_state
            WHERE provider = %s
            """,
            (PROVIDER,),
        ).fetchone()
        conn.commit()

        assert market.calls == 1
        assert result == "failed"
        assert row == {
            "attempt_count": 0,
            "lease_owner": None,
            "due_at_ms": NOW_MS + 300_000,
        }
        assert circuit == {
            "status": "open",
            "consecutive_failures": 1,
            "next_probe_at_ms": NOW_MS + 300_000,
        }
    finally:
        finite.close()
        conn.close()


def test_superseded_profile_claim_retries_without_publishing_stale_result() -> None:
    conn = connect_postgres_test()
    finite = FiniteOperations()
    try:
        reset_postgres_schema(conn)
        _enqueue_profile_target(conn)
        db = _SessionTrackingDB(conn)
        market = _SupersedingProfileMarket(db)
        worker = AssetProfileRefresh(
            db=db,
            dex_profile_sources=(DexProfileSource(provider=PROVIDER, market=market),),
            finite_operations=finite,
            runtime_id="integration-test",
        )

        result = asyncio.run(worker.turn(now_ms=NOW_MS))

        target = conn.execute(
            """
            SELECT payload_hash, address, attempt_count, lease_owner
            FROM asset_profile_refresh_targets
            WHERE provider = %s AND target_id = 'asset-1'
            """,
            (PROVIDER,),
        ).fetchone()
        profile_count = conn.execute(
            "SELECT count(*) AS count FROM asset_profiles WHERE asset_id = 'asset-1'"
        ).fetchone()["count"]
        circuit_count = conn.execute(
            "SELECT count(*) AS count FROM provider_circuit_state WHERE provider = %s",
            (PROVIDER,),
        ).fetchone()["count"]
        conn.commit()

        assert market.calls == 1
        assert result is None
        assert target == {
            "payload_hash": "sha256:evidence-v2",
            "address": "address-2",
            "attempt_count": 0,
            "lease_owner": None,
        }
        assert profile_count == 0
        assert circuit_count == 0
    finally:
        finite.close()
        conn.close()


def test_image_fetch_holds_no_database_session(tmp_path: Any) -> None:
    conn = connect_postgres_test()
    finite = FiniteOperations()
    try:
        reset_postgres_schema(conn)
        db = _SessionTrackingDB(conn)
        http_client = _ImageHttpClient(db)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.token_image_source_dirty_targets.enqueue_targets(
                [
                    {
                        "source_url": "https://gmgn.ai/external-res/logo.png",
                        "source_provider": PROVIDER,
                        "source_kind": "logo",
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "raw_ref_json": {},
                        "source_watermark_ms": NOW_MS,
                        "priority": 20,
                    }
                ],
                reason="profile_image_candidate",
                now_ms=NOW_MS,
            )
        worker = TokenImageMirror(
            db=db,
            app_home=tmp_path,
            http_client=http_client,
            finite_operations=finite,
            runtime_id="integration-test",
        )

        result = asyncio.run(worker.turn(now_ms=NOW_MS))

        asset = conn.execute(
            """
            SELECT status, storage_path
            FROM token_image_assets
            WHERE source_url = 'https://gmgn.ai/external-res/logo.png'
            """
        ).fetchone()
        queue_depth = conn.execute("SELECT count(*) AS count FROM token_image_source_dirty_targets").fetchone()["count"]
        conn.commit()
        assert http_client.calls == 1
        assert result == "processed"
        assert asset["status"] == "ready"
        assert (tmp_path / "cache" / "token-images" / asset["storage_path"]).is_file()
        assert queue_depth == 0
    finally:
        finite.close()
        conn.close()


def test_superseded_image_claim_retries_without_publishing_stale_result(tmp_path: Any) -> None:
    conn = connect_postgres_test()
    finite = FiniteOperations()
    try:
        reset_postgres_schema(conn)
        db = _SessionTrackingDB(conn)
        http_client = _SupersedingImageHttpClient(db)
        _enqueue_image_target(conn)
        worker = TokenImageMirror(
            db=db,
            app_home=tmp_path,
            http_client=http_client,
            finite_operations=finite,
            runtime_id="integration-test",
        )

        result = asyncio.run(worker.turn(now_ms=NOW_MS))

        target = conn.execute(
            """
            SELECT source_watermark_ms, attempt_count, lease_owner
            FROM token_image_source_dirty_targets
            WHERE target_type = 'Asset' AND target_id = 'asset-1'
            """
        ).fetchone()
        asset = conn.execute(
            "SELECT status FROM token_image_assets WHERE source_url = %s",
            (_ImageResponse.url,),
        ).fetchone()
        conn.commit()

        assert http_client.calls == 1
        assert result is None
        assert target == {
            "source_watermark_ms": NOW_MS + 1,
            "attempt_count": 0,
            "lease_owner": None,
        }
        assert asset == {"status": "pending"}
    finally:
        finite.close()
        conn.close()


def test_hot_profile_target_claims_before_older_cold_target() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        common = {
            "provider": "gmgn_dex_profile",
            "target_type": "Asset",
            "chain_id": "sol",
            "source_watermark_ms": NOW_MS,
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [
                    {
                        **common,
                        "target_id": "cold",
                        "address": "cold-address",
                        "heat_tier": "cold",
                        "due_at_ms": NOW_MS - 60_000,
                    },
                    {
                        **common,
                        "target_id": "hot",
                        "address": "hot-address",
                        "heat_tier": "hot",
                        "due_at_ms": NOW_MS,
                    },
                ],
                reason="queue-policy",
                now_ms=NOW_MS,
            )
            [claimed] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert claimed["target_id"] == "hot"
            assert claimed["priority"] == 20
    finally:
        conn.close()


def test_terminal_profile_target_only_reactivates_for_new_evidence() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        target = {
            "provider": "gmgn_dex_profile",
            "target_type": "Asset",
            "target_id": "asset-1",
            "chain_id": "sol",
            "address": "address-1",
            "symbol": "ONE",
            "source_watermark_ms": NOW_MS,
            "priority": 20,
            "heat_tier": "hot",
            "payload_hash": "sha256:evidence-v1",
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [target],
                reason="target_entered",
                now_ms=NOW_MS,
            )
            [claim] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert claim["heat_tier"] == "hot"
            assert (
                repos.asset_profile_refresh_targets.mark_terminal(
                    [claim],
                    reason="profile_missing_after_max_attempts",
                    now_ms=NOW_MS,
                )
                == 1
            )
            terminal = conn.execute(
                """
                SELECT final_reason, operator_action
                FROM queue_terminal_events
                WHERE owner_key = 'asset_profile_refresh'
                  AND source_table = 'asset_profile_refresh_targets'
                """
            ).fetchone()
            assert terminal == {
                "final_reason": "profile_missing_after_max_attempts",
                "operator_action": None,
            }

        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [target],
                reason="rank_changed",
                now_ms=NOW_MS + 1,
            )
            assert (
                repos.asset_profile_refresh_targets.claim_due(
                    provider="gmgn_dex_profile",
                    now_ms=NOW_MS + 1,
                    limit=1,
                    lease_owner="test-worker",
                    lease_ms=60_000,
                )
                == []
            )

        changed = {
            **target,
            "source_watermark_ms": NOW_MS + 2,
            "payload_hash": "sha256:evidence-v2",
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [changed],
                reason="source_watermark_changed",
                now_ms=NOW_MS + 2,
            )
            [reactivated] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS + 2,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert reactivated["attempt_count"] == 1
            assert reactivated["terminal_reason"] is None
            audit = conn.execute(
                """
                SELECT operator_action, operator_reason
                FROM queue_terminal_events
                WHERE owner_key = 'asset_profile_refresh'
                  AND source_table = 'asset_profile_refresh_targets'
                """
            ).fetchone()
            assert audit == {
                "operator_action": "retry",
                "operator_reason": "reactivated_by_new_evidence",
            }
    finally:
        conn.close()
