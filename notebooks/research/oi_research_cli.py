"""#459 Stage A, offline: seal a Binance open-interest corpus, then score one pre-registered rule.

```yaml
channel: A  # A live read-only | B frozen artifact | C committed snapshot
purpose: "Collect a 29-day Binance USD-M open-interest window that can never be served again, and
  replay the one pre-registered #459 rule over it on the symbols its originating probe never saw.
  It does not choose a rule: the pre-registration in `oi_replay.py` is fixed before the corpus is read."
window: "The corpus fixes its own window on the first `oi-corpus pull` and reuses it on every resume,
  so a pull that dies at 90% cannot slide it. `manifest.json` records it; the replay receipt quotes
  the corpus `manifest_sha256` it ran on."
identity: "SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED / SOURCE_FEATURE_DISCOVERY_REPLAY_V1; every raw
  payload is content-addressed and re-hashed on read, and the receipt names the corpus digest."
safety: "Reads public Binance USD-M REST endpoints only -- no credential, no venue write, no order.
  Writes only under the operator's own corpus directory (default `~/.tracefold/research/oi_corpus`)
  and the receipt path. Never opens PostgreSQL and never imports a service storage module."
```

Two commands, never one. #377 forbids a collector that also scores: the corpus is a fact about what
Binance served in a window it will not serve again, and the replay is a claim about a rule -- folding
them together means every re-scoring silently re-collects, and no receipt can name the data it ran on.

Run:

    uv run python notebooks/research/oi_research_cli.py oi-corpus pull [--out DIR] [--days 29]
    uv run python notebooks/research/oi_research_cli.py oi-corpus seal [--out DIR]
    uv run python notebooks/research/oi_research_cli.py oi-replay [--corpus DIR] [--out RECEIPT]

Until #537 PR-1 these were `tracefold trading oi-corpus|oi-replay`. They were the only callers of
`tracefold/trading/research/` and `integrations/venues/open_interest_history.py`, and research code
in the service package is what that PR deleted: this script is the same three owners composed here
instead -- the provider walk from `open_interest_history.py`, the sealed format from `oi_corpus.py`,
and the pre-registered rule from `oi_replay.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oi_corpus import (
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
from oi_replay import render_table, run_replay
from open_interest_history import (
    CANDLE_WEIGHT_PER_MIN,
    OPEN_INTEREST_REQUESTS_PER_MIN,
    Budget,
    OpenInterestHistoryError,
    fetch_candle_history,
    fetch_open_interest_history,
    fetch_usdt_perpetuals,
    history_client,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oi_research_cli.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    corpus = commands.add_parser("oi-corpus", help="pull or re-seal the sealed Binance open-interest corpus (#459)")
    corpus.add_argument("corpus_action", choices=("pull", "seal"))
    corpus.add_argument("--out", default=None, help="corpus directory (default: a dated one under ~/.tracefold)")
    corpus.add_argument("--days", type=int, default=29, help="window length; Binance keeps 30")
    corpus.add_argument("--symbols", nargs="*", default=None, help="restrict the universe, for a smoke pull")
    corpus.add_argument("--concurrency", type=int, default=8)

    replay = commands.add_parser("oi-replay", help="score the pre-registered #459 rule over a sealed corpus")
    replay.add_argument("--corpus", default=None, help="corpus directory (default: a dated one under ~/.tracefold)")
    replay.add_argument("--out", default=None, help="receipt path (default: <corpus>/replay_receipt.json)")
    replay.add_argument("--trials", type=int, default=2_000, help="permutation draws")
    return parser


def handle_oi_corpus(args: argparse.Namespace) -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    corpus_dir = Path(args.out) if args.out else dated_corpus_dir(now_ms=now_ms)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    if args.corpus_action == "seal":
        manifest = seal(corpus_dir, now_ms=now_ms)
    else:
        manifest = asyncio.run(
            _pull(
                corpus_dir,
                days=int(args.days),
                symbols=tuple(args.symbols) if args.symbols else None,
                concurrency=int(args.concurrency),
                now_ms=now_ms,
            )
        )
    return {
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
) -> dict[str, object]:
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


def handle_oi_replay(args: argparse.Namespace) -> dict[str, object]:
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
    return {
        "receipt": str(out_path),
        "corpus_manifest_sha256": report["corpus_manifest_sha256"],
        "verdict": report["verdict"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = handle_oi_corpus(args) if args.command == "oi-corpus" else handle_oi_replay(args)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
