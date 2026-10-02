"""Explicit offline model preparation and restartable claim-index maintenance."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from argparse import Namespace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from psycopg import Error as PostgresError

from tracefold.platform.config.loader import load_settings


def handle_embedding(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.claim_embedding import ClaimEmbedder, prepare_model

    settings = load_settings(require_ws_token=False)
    route = settings.llm.news_embedding
    if not route.configured:
        return 2, {"ok": False, "error": "news_embedding_not_configured"}
    try:
        from tracefold.news.claim_recall import CALIBRATION

        if route.model != CALIBRATION.embedder.model:
            raise ValueError("news_embedding_calibration_identity_mismatch")
        if args.embedding_command == "prepare":
            return 0, {"ok": True, "data": prepare_model(settings.news_embedding_cache_dir())}
        if args.embedding_command == "backfill" and not 32 <= args.batch_size <= 512:
            return 2, {"ok": False, "error": "news_embedding_batch_size_invalid"}

        async def run() -> tuple[int, dict[str, Any]]:
            embedder = ClaimEmbedder(
                model=str(route.model),
                cache_dir=settings.news_embedding_cache_dir(),
                max_batch_size=route.max_batch_size,
            )
            try:
                if not await embedder.self_test():
                    return 1, {"ok": False, "error": "news_embedding_self_test_failed"}
                if args.embedding_command == "check":
                    return 0, {"ok": True, "data": {"ready": True, "embedder": embedder.identity.key}}
                return await _backfill(args, settings, embedder)
            finally:
                await embedder.aclose()

        return asyncio.run(run())
    except (OSError, ValueError, RuntimeError, PostgresError) as exc:
        error = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("news_") else type(exc).__name__
        return 1, {"ok": False, "error": error, "operation": args.embedding_command}


class _CommandDatabase:
    """Each command operation owns one bounded transaction; inference owns no connection."""

    def __init__(self, settings: Any, executor: ThreadPoolExecutor) -> None:
        self.settings = settings
        self.executor = executor

    def _run(self, fn: Callable[[Any], Any], repeatable_read: bool, read_only: bool, timeout_seconds: float) -> Any:
        from tracefold.app.repository_session import repositories

        with repositories(self.settings) as repos, repos.transaction():
            if repeatable_read:
                repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            elif read_only:
                repos.conn.execute("SET TRANSACTION READ ONLY")
            repos.conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_seconds * 1000)),))
            repos.conn.execute("SET LOCAL transaction_timeout = '8s'")
            return fn(repos)

    async def read(
        self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0, repeatable_read: bool = False
    ) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(self._run, fn, repeatable_read, True, timeout_seconds)
        )

    async def tx(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(self._run, fn, False, False, timeout_seconds)
        )


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_checkpoint(value: str, database_key: str, embedder_key: str) -> tuple[Path, Any]:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        return path, None
    saved = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(saved, dict)
        or saved.get("database") != database_key
        or saved.get("embedder") != embedder_key
        or not isinstance(saved.get("resume"), dict)
    ):
        raise ValueError("news_claim_backfill_checkpoint_mismatch")
    return path, saved["resume"]


async def _backfill(args: Namespace, settings: Any, embedder: Any) -> tuple[int, dict[str, Any]]:
    from psycopg.conninfo import conninfo_to_dict

    from tracefold.news.storage.claim_recall import PgClaimRecall
    from tracefold.news.updates.identity import digest

    connection = conninfo_to_dict(settings.storage.postgres.dsn)
    database_key = digest({key: connection.get(key) for key in ("host", "port", "dbname", "user")})
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="claim-backfill") as executor:
        loop = asyncio.get_running_loop()
        checkpoint_path, resume = await loop.run_in_executor(
            executor, partial(_read_checkpoint, args.checkpoint, database_key, embedder.identity.key)
        )

        async def checkpoint(state: dict[str, Any]) -> None:
            await loop.run_in_executor(
                executor,
                partial(
                    _write_checkpoint,
                    checkpoint_path,
                    {"database": database_key, "embedder": embedder.identity.key, "resume": state},
                ),
            )

        recall = PgClaimRecall(
            _CommandDatabase(settings, executor), embedder=embedder, embedding_batch_size=args.batch_size
        )
        report = await recall.bulk_backfill(batch_size=args.batch_size, resume=resume, checkpoint=checkpoint)
    return 0, {"ok": True, "data": {**report, "checkpoint": str(checkpoint_path)}}
