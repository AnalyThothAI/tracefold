#!/usr/bin/env python3
"""Fetch the roster provider's full list and profit factors under two windows (#649 §5.3).

The deployed roster asked `/api/traders?window=7d` for the list and `/api/trader/{handle}` with no
window at all for the factor. On a seven-day window almost nothing clears a 1.2 profit factor, so the
quality pool -- the only list whose buys can raise an alert -- collapsed to one address while 147
were being watched. This script is the measurement behind changing that window: it walks *both*
windows completely, at the client's own two-second pace, so the comparison is made on complete data
rather than on whatever survived a 429.

Read-only and offline in the sense that matters: it touches no database and nothing in this
repository, and it makes exactly `1 + candidates` requests per window.

    uv run python scripts/compare_roster_windows.py --windows 7d,30d --out report.json

The near-7-day hit-bucket column of the §5.3 table is a PostgreSQL question, not a provider one, so
this script prints the SQL for it (`--print-sql`) rather than pretending to answer it. Run that
against the deployment's own database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from tracefold.integrations.robinhoodtrenches import (
    ROBINHOODTRENCHES_BASE_URL,
    RobinhoodTrenchesClient,
    RosterProviderError,
)
from tracefold.news.chain_tape.roster import RosterRules, quality_candidates, select_roster

# The buckets the §5.3 table's last column wants, asked of the production-shaped fills table. Two
# windows, one net-buy floor per wallet, and the three membership definitions the Issue compares:
# any watched address, any address at the closed-trade floor, and the quality list itself.
HIT_BUCKET_SQL = """
-- #649 §5.3: how often N distinct roster wallets each net-bought >= $1000 of one token inside one
-- window, over the last seven days. Run on the deployment's database; read-only.
WITH net AS (
    SELECT f.token,
           f.wallet,
           width_bucket(f.event_at_ms,
                        (extract(epoch FROM now()) * 1000)::bigint - 604800000,
                        (extract(epoch FROM now()) * 1000)::bigint,
                        %(buckets)s) AS bucket,
           sum(CASE WHEN f.kind = 'buy' THEN f.usd WHEN f.kind = 'sell' THEN -f.usd ELSE 0 END) AS net_usd,
           count(*) FILTER (WHERE f.usd IS NULL OR f.kind = 'transfer_out') AS incomplete
      FROM news_market_wallet_fills f
     WHERE f.event_at_ms >= (extract(epoch FROM now()) * 1000)::bigint - 604800000
     GROUP BY 1, 2, 3
), qualified AS (
    SELECT n.token, n.bucket, n.wallet
      FROM net n
      JOIN news_market_wallet_roster r
        ON r.wallet = n.wallet
       AND r.roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
     WHERE n.incomplete = 0 AND n.net_usd >= 1000
       AND (%(membership)s = 'watched'
            OR (%(membership)s = 'at_floor' AND r.closed_trades >= 10)
            OR (%(membership)s = 'quality' AND r.rank_quality IS NOT NULL))
)
SELECT count(*) AS hit_buckets FROM (
    SELECT token, bucket FROM qualified GROUP BY 1, 2 HAVING count(DISTINCT wallet) >= %(required_n)s
) hits;
-- 5m buckets:  buckets = 2016, required_n = 3
-- 30m buckets: buckets = 336,  required_n = 5
-- membership:  'watched' | 'at_floor' | 'quality'
"""


async def _window_report(
    client: RobinhoodTrenchesClient, *, window: str, rules: RosterRules, limit: int | None
) -> dict[str, Any]:
    started = time.monotonic()
    candidates = await client.traders(window=window)
    at_floor = quality_candidates(candidates, rules=rules)
    if limit is not None:
        at_floor = at_floor[:limit]
    factors: dict[str, float | None] = {}
    failures: list[dict[str, str]] = []
    for row in at_floor:
        handle = str(getattr(row, "handle", "") or "")
        if not handle or handle in factors:
            continue
        try:
            stats = await client.trader(handle, window=window)
        except RosterProviderError as exc:
            # Recorded, never smoothed into "unknown": a failed lookup is why the comparison would
            # not be complete, and that is the whole point of running it with a two-second pace.
            failures.append({"handle": handle, "code": exc.code})
            continue
        factors[handle] = None if stats is None else stats.profit_factor
    members = select_roster(candidates, profit_factors=factors, rules=rules)
    known = [value for value in factors.values() if value is not None]
    return {
        "window": window,
        "seconds": round(time.monotonic() - started, 1),
        "source_addresses": len(candidates),
        "candidates_at_closed_trade_floor": len(at_floor),
        "profit_factor_known": len(known),
        "profit_factor_unknown": sum(1 for value in factors.values() if value is None),
        "profit_factor_failed": len(failures),
        "failures": failures,
        "passing_profit_factor": sum(1 for value in known if value >= rules.min_profit_factor),
        "quality": sum(1 for member in members if member.rank_quality is not None),
        "whale": sum(1 for member in members if member.rank_whale is not None),
        "selected": len(members),
        "quality_handles": sorted(
            (member.handle, member.profit_factor, member.closed_trades)
            for member in members
            if member.rank_quality is not None
        ),
        "complete": not failures,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    rules = RosterRules(
        min_closed_trades=args.min_closed_trades,
        min_profit_factor=args.min_profit_factor,
        top_quality=args.top_quality,
    )
    client = RobinhoodTrenchesClient(base_url=args.base_url, pace_seconds=args.pace_seconds)
    try:
        windows = [
            await _window_report(client, window=window, rules=rules, limit=args.limit)
            for window in [value.strip() for value in args.windows.split(",") if value.strip()]
        ]
    finally:
        await client.aclose()
    return {
        "base_url": args.base_url,
        "pace_seconds": args.pace_seconds,
        "rules": {
            "min_closed_trades": rules.min_closed_trades,
            "min_profit_factor": rules.min_profit_factor,
            "top_quality": rules.top_quality,
        },
        "windows": windows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", default="7d,30d", help="comma-separated provider statistics windows")
    parser.add_argument("--base-url", default=ROBINHOODTRENCHES_BASE_URL)
    parser.add_argument("--pace-seconds", type=float, default=2.0, help="floor between two calls")
    parser.add_argument("--min-closed-trades", type=int, default=10)
    parser.add_argument("--min-profit-factor", type=float, default=1.2)
    parser.add_argument("--top-quality", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None, help="stop after this many per-trader lookups")
    parser.add_argument("--out", default="", help="write the full report JSON here")
    parser.add_argument("--print-sql", action="store_true", help="print the hit-bucket SQL and exit")
    args = parser.parse_args(argv)
    if args.print_sql:
        print(HIT_BUCKET_SQL)
        return 0
    report = asyncio.run(_run(args))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(window["complete"] for window in report["windows"]) else 1


if __name__ == "__main__":
    sys.exit(main())
