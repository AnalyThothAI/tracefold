"""Append price_path_v2 corrections for settled historical v1 labels.

The old row and its archive remain untouched. Missing historical market data
settles a v2 missing label with an archived reason; it never copies v1 return.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.integrations.marketdata.binance import BinanceMarketData
from tracefold.platform.config.loader import load_settings


async def relabel(*, batch_size: int, max_batches: int) -> dict[str, int]:
    settings = load_settings(require_ws_token=False)
    market = BinanceMarketData(
        max_connections=settings.trading.analysis.market_max_connections,
        max_cached_rows=settings.trading.analysis.market_max_cached_rows,
        weight_soft_limit_1m=settings.trading.analysis.market_weight_soft_limit_1m,
    )
    runner = AnalysisRunner(
        settings=settings,
        market_data=market,
        analyst=None,
        files_root=settings.app_home / "archive" / "trading-analysis",
    )
    queued = processed = 0
    try:
        for _ in range(max_batches):
            added = await runner._db_async(
                lambda repos: repos.trading.queue_price_path_v2_corrections(limit=batch_size),
                transaction=True,
            )
            queued += int(added)
            handled = await runner.label_once(limit=batch_size, label_version="price_path_v2")
            processed += handled
            if added == 0 and handled == 0:
                break
        counts: dict[str, Any] = await runner._db_async(
            lambda repos: dict(
                repos.conn.execute(
                    """
                    SELECT count(*) FILTER (WHERE newer.status='ok') AS corrected,
                           count(*) FILTER (WHERE newer.status='missing') AS unverifiable,
                           count(*) FILTER (WHERE newer.status='pending') AS pending
                      FROM trading_case_outcomes old
                      JOIN trading_case_outcomes newer
                        ON newer.case_id=old.case_id AND newer.axis=old.axis
                       AND newer.horizon_seconds=old.horizon_seconds
                     WHERE old.label_version='price_path_v1'
                       AND newer.label_version='price_path_v2'
                    """
                ).fetchone()
            ),
        )
        return {"queued": queued, "processed": processed, **{key: int(value) for key, value in counts.items()}}
    finally:
        await market.aclose()
        runner._db_executor.shutdown(wait=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 128 or not 1 <= args.max_batches <= 10_000:
        parser.error("batch bounds invalid")
    print(json.dumps(asyncio.run(relabel(batch_size=args.batch_size, max_batches=args.max_batches)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
