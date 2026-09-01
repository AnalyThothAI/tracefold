"""``tracefold trading oi-corpus`` and ``tracefold trading oi-replay``: #459 Stage A, offline.

Two commands, never one. #377 forbids a collector that also scores: the corpus is a fact about what
Binance served in a window it will not serve again, and the replay is a claim about a rule -- folding
them together means every re-scoring silently re-collects, and no receipt can name the data it ran on.

This is the app seam, so it is where the three owners meet: the provider walk from
`integrations.venues.open_interest_history`, the sealed format from `trading.research.oi_corpus`, and
the pre-registered rule from `trading.research.oi_replay`. Neither business module reaches the
network, and neither owns an argument parser.

Nothing here touches PostgreSQL, the broker, or capital. The corpus lives under `~/.tracefold`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from tracefold.integrations.venues.open_interest_history import (
    CANDLE_WEIGHT_PER_MIN,
    OPEN_INTEREST_REQUESTS_PER_MIN,
    Budget,
    OpenInterestHistoryError,
    fetch_candle_history,
    fetch_open_interest_history,
    fetch_usdt_perpetuals,
    history_client,
)

# By name, not through the package: `tracefold.trading.research` is not a declared business
# interface, and naming what this seam uses is what keeps the boundary checkable.
from tracefold.trading.research.oi_corpus import (
    SymbolRecord,
    append_progress,
    dated_corpus_dir,
    fix_window,
    load_progress,
    seal,
    store_universe,
    window_now,
    write_payload,
)
from tracefold.trading.research.oi_replay import render_table, run_replay


def handle_trading_oi_corpus(args: Any) -> tuple[int, dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    corpus_dir = Path(args.out) if args.out else dated_corpus_dir(now_ms=now_ms)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    if args.corpus_action == "seal":
        manifest = seal(corpus_dir, now_ms=now_ms)
        return 0, {
            "corpus": str(corpus_dir),
            "coverage": manifest["coverage"],
            "manifest_sha256": manifest["manifest_sha256"],
        }

    manifest = asyncio.run(
        _pull(
            corpus_dir,
            days=int(args.days),
            symbols=tuple(args.symbols) if args.symbols else None,
            concurrency=int(args.concurrency),
            now_ms=now_ms,
        )
    )
    return 0, {
        "corpus": str(corpus_dir),
        "coverage": manifest["coverage"],
        "manifest_sha256": manifest["manifest_sha256"],
    }


async def _pull(
    corpus_dir: Path,
    *,
    days: int,
    symbols: tuple[str, ...] | None,
    concurrency: int,
    now_ms: int,
) -> dict[str, Any]:
    raw_root = corpus_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    window = fix_window(corpus_dir, window_now(days=days, now_ms=now_ms))

    async with history_client(max_connections=max(4, concurrency * 2)) as client:
        oi_budget = Budget(OPEN_INTEREST_REQUESTS_PER_MIN)
        candle_budget = Budget(CANDLE_WEIGHT_PER_MIN)

        universe = store_universe(
            corpus_dir,
            await fetch_usdt_perpetuals(client, budget=candle_budget),
            now_ms=now_ms,
        )
        if symbols is not None:
            wanted = set(symbols)
            universe = tuple(symbol for symbol in universe if symbol in wanted)

        done = load_progress(corpus_dir)
        todo = [symbol for symbol in universe if symbol not in done]
        started = time.monotonic()
        print(
            f"[oi-corpus] window {window.start_ms}..{window.end_ms} universe={len(universe)} todo={len(todo)}",
            flush=True,
        )

        gate = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()
        completed = 0
        failures: list[str] = []

        async def run(symbol: str) -> None:
            nonlocal completed
            async with gate:
                try:
                    oi_task = asyncio.create_task(
                        fetch_open_interest_history(
                            client, symbol, start_ms=window.start_ms, end_ms=window.end_ms, budget=oi_budget
                        )
                    )
                    candle_task = asyncio.create_task(
                        fetch_candle_history(
                            client,
                            symbol,
                            start_ms=window.candle_start_ms,
                            end_ms=window.candle_end_ms,
                            budget=candle_budget,
                        )
                    )
                    oi_rows = await oi_task
                    candle_rows = await candle_task
                except OpenInterestHistoryError as error:
                    async with lock:
                        failures.append(f"{symbol}: {error}")
                        print(f"[oi-corpus] FAILED {symbol}: {error}", flush=True)
                    return
                oi_sha, oi_bytes = write_payload(raw_root, oi_rows)
                candle_sha, candle_bytes = write_payload(raw_root, candle_rows)
                record = SymbolRecord(
                    symbol=symbol,
                    oi_sha256=oi_sha,
                    oi_points=len(oi_rows),
                    oi_first_ms=int(oi_rows[0]["timestamp"]) if oi_rows else None,
                    oi_last_ms=int(oi_rows[-1]["timestamp"]) if oi_rows else None,
                    candle_sha256=candle_sha,
                    candle_points=len(candle_rows),
                    candle_first_ms=int(candle_rows[0][0]) if candle_rows else None,
                    candle_last_ms=int(candle_rows[-1][0]) if candle_rows else None,
                    stored_bytes=oi_bytes + candle_bytes,
                    pulled_at_ms=int(time.time() * 1000),
                )
                async with lock:
                    append_progress(corpus_dir, record)
                    completed += 1
                    if completed % 10 == 0 or completed == len(todo):
                        elapsed = time.monotonic() - started
                        rate = completed / elapsed if elapsed else 0.0
                        remaining = (len(todo) - completed) / rate if rate else 0.0
                        print(
                            f"[oi-corpus] {completed}/{len(todo)} ({symbol} oi={record.oi_points} "
                            f"k={record.candle_points}) elapsed={elapsed / 60:.1f}m eta={remaining / 60:.1f}m",
                            flush=True,
                        )

        await asyncio.gather(*(run(symbol) for symbol in todo))

    manifest = seal(corpus_dir, now_ms=int(time.time() * 1000))
    coverage = manifest["coverage"]
    print(
        f"[oi-corpus] sealed {coverage['symbols_stored']} symbols, {coverage['open_interest_points']} OI points, "
        f"{coverage['stored_bytes'] / 1e6:.0f} MB, manifest_sha256={manifest['manifest_sha256']}",
        flush=True,
    )
    for line in failures[:20]:
        print(f"[oi-corpus] failure {line}", flush=True)
    return manifest


def handle_trading_oi_replay(args: Any) -> tuple[int, dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    corpus_dir = Path(args.corpus) if args.corpus else dated_corpus_dir(now_ms=now_ms)
    out_path = Path(args.out) if args.out else corpus_dir / "replay_receipt.json"
    report = run_replay(corpus_dir, trials=int(args.trials), now_ms=now_ms)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(render_table(report))
    print()
    print(
        f"baseline (every holdout bar): 4H net {report['baseline_mean_net_4h_bps']} bps · "
        f"4H hold {report['baseline_mean_hold_4h_bps']} bps · stopped {report['baseline_stop_rate']}"
    )
    return 0, {
        "receipt": str(out_path),
        "corpus_manifest_sha256": report["corpus_manifest_sha256"],
        "verdict": report["verdict"],
    }


__all__ = ["handle_trading_oi_corpus", "handle_trading_oi_replay"]
