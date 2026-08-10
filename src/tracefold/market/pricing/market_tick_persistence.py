from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tracefold.market.pricing.live_market import live_market_update_payload
from tracefold.market.pricing.market_tick import MarketTick
from tracefold.market.pricing.market_tick_current_repository import (
    market_tick_current_row,
)


@dataclass(frozen=True, slots=True)
class MarketTickPersistenceResult:
    inserted_ids: list[str]
    current_rows: list[dict[str, Any]]
    live_market_rows: list[dict[str, Any]]

    @property
    def inserted(self) -> int:
        return len(self.inserted_ids)

    @property
    def changed_targets(self) -> list[tuple[str, str]]:
        return [(str(row["target_type"]), str(row["target_id"])) for row in self.current_rows]


@dataclass(frozen=True, slots=True)
class MarketTickCurrentRebuildResult:
    scanned_targets: int
    changed_targets: tuple[tuple[str, str], ...]
    next_cursor: tuple[str, str] | None


class MarketTickPersistenceService:
    def __init__(self, repos: Any) -> None:
        self.repos = repos

    def persist_ticks(
        self,
        ticks: Iterable[MarketTick],
        *,
        now_ms: int,
    ) -> MarketTickPersistenceResult:
        materialized = list(ticks)
        self.repos.require_transaction(operation="market_tick_persistence")
        if not materialized:
            return MarketTickPersistenceResult(inserted_ids=[], current_rows=[], live_market_rows=[])

        inserted_rows = self.repos.market_ticks.insert_ticks_returning_rows(materialized)
        latest_rows = _latest_rows_by_target(inserted_rows)
        current_rows = self._advance_current(latest_rows)
        product_targets = self._product_targets(current_rows)
        live_market_rows = [
            {
                **row,
                "product_target_type": product_targets[key][0],
                "product_target_id": product_targets[key][1],
            }
            for row in current_rows
            if (key := (str(row["target_type"]), str(row["target_id"]))) in product_targets
        ]
        for row in live_market_rows:
            payload = live_market_update_payload(row)
            self.repos.persisted_live.append(
                source_key=f"market_tick:{row['tick_id']}",
                event_kind="live_market_update",
                target_type=str(payload["target_type"]),
                target_id=str(payload["target_id"]),
                payload=payload,
                committed_at_ms=int(now_ms),
            )
        return MarketTickPersistenceResult(
            inserted_ids=[str(row["tick_id"]) for row in inserted_rows],
            current_rows=current_rows,
            live_market_rows=live_market_rows,
        )

    def rebuild_current_batch(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        now_ms: int,
    ) -> MarketTickCurrentRebuildResult:
        self.repos.require_transaction(operation="market_tick_current_rebuild")
        latest_rows = self.repos.market_ticks.latest_target_ticks_after(after=after, limit=limit)
        current_rows = self._advance_current(latest_rows)
        self._product_targets(current_rows)
        next_cursor = None
        if latest_rows:
            last = latest_rows[-1]
            next_cursor = (str(last["target_type"]), str(last["target_id"]))
        return MarketTickCurrentRebuildResult(
            scanned_targets=len(latest_rows),
            changed_targets=tuple(_target_keys(current_rows)),
            next_cursor=next_cursor,
        )

    def _advance_current(
        self,
        latest_rows: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            market_tick_current_row(row)
            for row in latest_rows
            if self.repos.market_tick_current.upsert_current_from_tick(row)
        ]

    def _product_targets(
        self,
        current_rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], tuple[str, str]]:
        changed_targets = _target_keys(current_rows)
        product_targets: dict[tuple[str, str], tuple[str, str]] = (
            self.repos.registry.product_targets_for_market_targets(changed_targets)
        )
        return product_targets


def _latest_rows_by_target(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["target_type"]), str(row["target_id"]))
        current = latest.get(key)
        if current is None or _tick_order(row) > _tick_order(current):
            latest[key] = row
    return list(latest.values())


def _target_keys(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(row["target_type"]), str(row["target_id"])) for row in rows]


def _tick_order(row: dict[str, Any]) -> tuple[int, int, str]:
    return (int(row["observed_at_ms"]), int(row["received_at_ms"]), str(row["tick_id"]))
