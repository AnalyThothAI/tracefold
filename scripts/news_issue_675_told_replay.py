"""Measure the #675 §2 told recency reservation against the 37 reviewer-labelled duplicate pairs.

Read-only. It reads the delivered-card ledger through the same projection online retrieval uses, rebuilds
each duplicate's told ledger with `build_reader_history` + `ToldLedgerSnapshot.select`, and reports how
many pairs now have the card they repeat *inside* the ledger the model would have judged against. The
restatement guard can only fire on an entry the model was shown, so membership is the precondition for the
whole semantic de-duplication path; the 2026-09-22 audit found the earlier card missing in 21 of 37.

It is a verification script rather than a committed test because the evidence is the production delivery
ledger, which a test may not read. Run it against a database that still holds the window:

    uv run python scripts/news_issue_675_told_replay.py \
        --reviews /path/to/merged_reviews.json --sim /path/to/sim_24h.json

Both inputs are the recorded audit artifacts; `tests/fixtures/news/policy_v15_replay_24h.jsonl` carries the
same rows for the decision-table replay, which needs no database at all.

What this can and cannot establish. The rebuilt ledger is not the ledger production showed, and the script
prints both numbers so the difference is visible rather than assumed. Three inputs it cannot reproduce:
the selection ran under the *preliminary* storyline key and the delivery ledger records the final one, the
title-similarity band was a pg_trgm top-32 inside PostgreSQL rather than a local score over the whole
pool, and the asset band resolved through `news_symbol_aliases` rather than through the receipt's own
`canonical_assets`. Each of those makes a rebuilt ledger more generous than the real one, so "inside told,
rebuilt" is an upper bound. Read the `rescued` line for what the reservation itself contributes on this
evidence, not the totals.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tracefold.news import told_context
from tracefold.news.models import base_symbol
from tracefold.news.reader_history import build_reader_history
from tracefold.news.similarity import trigram_similarity
from tracefold.news.storage.decisions import delivered_history_rows
from tracefold.news.told_context import TOLD_MAX, ToldLedgerSnapshot, _take_with_tier_caps

PAIR_WINDOW_MS = 6 * 3_600_000


def _connect(dsn: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def _symbols(row: Mapping[str, Any]) -> frozenset[str]:
    values = [
        *(row.get("canonical_assets") or ()),
        *(row.get("grounded_assets") or ()),
        *(asset.get("symbol") for asset in (row.get("assets") or ()) if isinstance(asset, Mapping)),
    ]
    return frozenset(base_symbol(str(value)) for value in values if value)


def _partner(candidate: Mapping[str, Any], earlier: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The nearest earlier delivered card this one plausibly repeats: same asset, same key, or same wording."""

    best: tuple[float, Mapping[str, Any]] | None = None
    for row in earlier:
        gap = int(candidate["at_ms"]) - int(row["at_ms"])
        if not 0 < gap <= PAIR_WINDOW_MS:
            continue
        shared_asset = bool(_symbols(candidate) & _symbols(row))
        same_key = bool(candidate["storyline_key"]) and candidate["storyline_key"] == row["storyline_key"]
        score = trigram_similarity(str(candidate["comparison_title"]), str(row["comparison_title"]))
        if not (shared_asset or same_key or score >= 0.25):
            continue
        rank = score + (0.5 if same_key else 0.0) + (0.25 if shared_asset else 0.0) - gap / (PAIR_WINDOW_MS * 100)
        if best is None or rank > best[0]:
            best = (rank, row)
    return None if best is None else best[1]


def _selected_ids(candidate: Mapping[str, Any], pool: Sequence[Mapping[str, Any]], *, reserved: bool) -> set[str]:
    now_ms = int(candidate["at_ms"])
    history = build_reader_history(
        [row for row in pool if int(row["at_ms"]) < now_ms],
        now_ms=now_ms,
        dedupe_family=str(candidate.get("dedupe_family") or "general"),
        comparison_fingerprint=str(candidate.get("comparison_fingerprint") or ""),
        canonical_assets=sorted(_symbols(candidate)),
        comparison_title=str(candidate.get("comparison_title") or ""),
    )
    rows = [row.as_told_row() for row in history.told_source_rows]
    if reserved:
        snapshot = ToldLedgerSnapshot.select(
            rows,
            now_ms=now_ms,
            storyline_key=str(candidate.get("storyline_key") or ""),
            symbols=sorted(_symbols(candidate)),
            comparison_title=str(candidate.get("comparison_title") or ""),
            exclude_event_id=str(candidate["event_id"]),
        )
        return {entry.event_id for entry in snapshot.entries}

    # The pre-#675 selector: identical ranking, no reservation. The one function that changed is
    # swapped for the tier caps alone, so the two numbers below differ by exactly that function and by
    # nothing else -- a second hand-written selector here would be a second thing to keep in step.
    original = told_context._take_with_recency_reservation

    def _tiers_only(ranked: Sequence[Any], *, limit: int, now_ms: int) -> list[Any]:
        return _take_with_tier_caps(ranked, limit=limit)

    told_context._take_with_recency_reservation = _tiers_only  # type: ignore[assignment]
    try:
        snapshot = ToldLedgerSnapshot.select(
            rows,
            now_ms=now_ms,
            storyline_key=str(candidate.get("storyline_key") or ""),
            symbols=sorted(_symbols(candidate)),
            comparison_title=str(candidate.get("comparison_title") or ""),
            exclude_event_id=str(candidate["event_id"]),
        )
    finally:
        told_context._take_with_recency_reservation = original  # type: ignore[assignment]
    return {entry.event_id for entry in snapshot.entries}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="postgresql://tracefold@127.0.0.1:56533/tracefold")
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--sim", type=Path, required=True)
    parser.add_argument(
        "--verdicts",
        type=Path,
        default=None,
        help="the recorded verdict export, whose `told` is the ledger production actually showed the model",
    )
    args = parser.parse_args()

    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))["reviews"]
    sim = json.loads(args.sim.read_text(encoding="utf-8"))
    duplicates = {
        row["event_id"]
        for row in sim
        if row["sent"] and reviews.get(row["event_id"], {}).get("category") == "duplicate"
    }
    print(f"reviewer-labelled duplicates among delivered cards: {len(duplicates)}")

    with _connect(args.dsn) as conn:
        latest = conn.execute(
            "SELECT max(settled_at_ms) AS at_ms FROM news_deliveries WHERE kind='first' AND state='sent'"
        ).fetchone()
        pool = list(delivered_history_rows(conn, cutoff_at_ms=int(latest["at_ms"]) + 1))
    print(f"delivered rows read: {len(pool)}")

    by_id = {str(row["event_id"]): row for row in pool}
    ordered = sorted(pool, key=lambda row: int(row["at_ms"]))

    recorded: dict[str, list[str]] = {}
    if args.verdicts is not None:
        for line in args.verdicts.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            recorded[str(row["event_id"])] = [str(entry.get("headline") or "") for entry in row.get("told") or ()]

    paired = 0
    inside_old = 0
    inside_new = 0
    inside_recorded = 0
    within_hour = 0
    rescued = 0
    unreachable: list[str] = []
    missing: list[str] = []
    for event_id in sorted(duplicates):
        candidate = by_id.get(event_id)
        if candidate is None:
            missing.append(event_id)
            continue
        earlier = [row for row in ordered if int(row["at_ms"]) < int(candidate["at_ms"])]
        partner = _partner(candidate, earlier)
        if partner is None:
            continue
        paired += 1
        old = _selected_ids(candidate, ordered, reserved=False)
        new = _selected_ids(candidate, ordered, reserved=True)
        inside_old += str(partner["event_id"]) in old
        inside_new += str(partner["event_id"]) in new
        if event_id in recorded:
            inside_recorded += str(partner["headline_zh"]) in recorded[event_id]
        gap_min = (int(candidate["at_ms"]) - int(partner["at_ms"])) // 60_000
        if gap_min < 60:
            within_hour += 1
            rescued += str(partner["event_id"]) not in old and str(partner["event_id"]) in new
        if str(partner["event_id"]) not in new:
            unreachable.append(f"{event_id[:8]} <- {str(partner['event_id'])[:8]} ({gap_min} min)")

    print(f"pairs formed (nearest earlier card within 6 h): {paired}")
    print(f"  of which the earlier card is inside the 60-minute window: {within_hour}")
    print(f"  pairs the reservation alone brings into the ledger: {rescued}")
    if unreachable:
        print("  pairs whose earlier card is still outside the ledger: " + ", ".join(unreachable))
    if recorded:
        print(f"earlier card inside the told ledger production actually showed: {inside_recorded}")
    print(f"earlier card inside told, rebuilt with the pre-#675 selector: {inside_old}")
    print(f"earlier card inside told, rebuilt with the 6-slot 60-minute reservation: {inside_new}")
    print(f"told slots: {TOLD_MAX}; duplicates not found in the delivery ledger: {len(missing)}")


if __name__ == "__main__":
    main()
