"""The sole wallet detector: committed receipts to durable token episodes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from ..bus import now_ms
from ..pipeline.admission import admit_market_item, prepare_wallet_observation, wallet_item_id
from ..wallet_contracts import NetBuySnapshot, WalletEvent
from .contracts import ClassifiedFill
from .loop import ChainTapeDatabasePort
from .rules import SLOW_WINDOW_MS, WalletRules, calculate_windows, effective_buy, trigger_age_reason
from .tape_io import FAILED, TapePasses


@dataclass(frozen=True, slots=True)
class DetectionResult:
    receipts: int = 0
    opened: int = 0
    updated: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Update:
    item_id: str
    snapshot_json: str
    matched: bool
    reason: str
    last_effective_buy_at_ms: int
    ended_at_ms: int | None


class NetBuyDetector(TapePasses):
    _read_timeout_seconds = 5.0
    _write_timeout_seconds = 10.0
    _failure_stage = "net_buy"

    def __init__(
        self,
        *,
        db: ChainTapeDatabasePort,
        rules: WalletRules | None = None,
        notifications_enabled: bool = True,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.db = db
        self.rules = rules or WalletRules()
        self.notifications_enabled = notifications_enabled
        self._clock = clock
        self._active_after = ""

    async def aclose(self) -> None:
        pass

    async def advance(self) -> DetectionResult:
        errors: list[str] = []
        receipts = await self._read(
            "news_wallet_pending_receipts",
            lambda repos: repos.news.wallet_pending_receipts(),
            errors,
        )
        if receipts is FAILED:
            return DetectionResult(errors=tuple(errors))
        opened = processed = updated = 0
        for receipt in receipts:
            result = await self._receipt(receipt, errors)
            if result is None:
                return DetectionResult(processed, opened, updated, tuple(errors))
            processed += 1
            opened += result[0]
            updated += result[1]
        # Only after pending transactions have drained may chain-time sliding expiry advance.
        pending = await self._read(
            "news_wallet_backlog_remaining", lambda repos: repos.news.wallet_has_pending_receipts(), errors
        )
        if pending is False:
            updated += await self._slide(errors)
        return DetectionResult(processed, opened, updated, tuple(errors))

    async def _receipt(self, receipt: Sequence[ClassifiedFill], errors: list[str]) -> tuple[int, int] | None:
        first = receipt[0]
        cutoff = max(receipt, key=lambda fill: (fill.block_number, fill.log_index))
        tokens = sorted({fill.token for fill in receipt})

        def read(repos: Any) -> Any:
            news = repos.news
            return (
                news.chain_tape_state(),
                news.chain_tape_members(first.roster_version),
                [
                    (
                        token,
                        news.wallet_window_fills(
                            chain_id=first.chain_id,
                            token=token,
                            from_ms=first.event_at_ms - SLOW_WINDOW_MS,
                            to_ms=first.event_at_ms,
                            block=cutoff.block_number,
                            log=cutoff.log_index,
                        ),
                        news.wallet_active_event(chain_id=first.chain_id, token=token),
                    )
                    for token in tokens
                ],
            )

        data = await self._read("news_wallet_net_windows", read, errors)
        if data is FAILED:
            return None
        state, members, subjects = data
        stamp = self._clock()
        events = []
        updates = []
        reasons: dict[str, str] = {}
        for token, fills, active_row in subjects:
            active = active_row
            snapshot = calculate_windows(
                fills=fills,
                members=members,
                chain_id=first.chain_id,
                token=token,
                cutoff_at_ms=first.event_at_ms,
                cutoff_block=cutoff.block_number,
                cutoff_log=cutoff.log_index,
                coverage_from_ms=state["coverage_from_ms"],
                coverage_gap_at_ms=state["gap_at_ms"],
                roster_version=first.roster_version,
                rules=self.rules,
            )
            reasons[token] = "conditions_not_met"
            changes = [fill for fill in receipt if fill.token == token]
            increased = effective_buy(changes, snapshot)
            if active is not None and first.event_at_ms - active["last_effective_buy_at_ms"] >= SLOW_WINDOW_MS:
                updates.append(_update(active, snapshot, "inactivity_window", stamp, ended=True))
                active = None
            if active is not None:
                reasons[token] = "active_episode"
                update = _changed(active, snapshot, changes, increased)
                if update is not None:
                    updates.append(update)
                continue
            if not snapshot.matched or not increased:
                continue
            reason = (
                trigger_age_reason(
                    event_at_ms=first.event_at_ms,
                    received_at_ms=first.received_at_ms,
                    now_ms=stamp,
                    max_age_s=self.rules.trigger_max_age_s,
                )
                or "selected"
            )
            if first.event_at_ms <= state["detection_cutover_at_ms"]:
                reason = "before_cutover"
            reasons[token] = reason
            if reason != "selected":
                continue
            reasons[token] = "selected" if self.notifications_enabled else "wallet_notifications_disabled"
            event = WalletEvent(
                item_id="",
                chain_id=first.chain_id,
                token=token,
                token_symbol=snapshot.token_symbol,
                trigger_tx_hash=first.tx_hash,
                event_at_ms=first.event_at_ms,
                received_at_ms=first.received_at_ms,
                detected_at_ms=stamp,
                initial_snapshot=snapshot,
                trigger_max_age_s=self.rules.trigger_max_age_s,
                notification_eligible=self.notifications_enabled,
                notification_reason=None if self.notifications_enabled else "wallet_notifications_disabled",
            )
            events.append(replace(event, item_id=wallet_item_id(event)))
        prepared = [prepare_wallet_observation(event) for event in events]

        def commit(repos: Any) -> tuple[int, int]:
            news = repos.news
            if not news.wallet_receipt_pending(chain_id=first.chain_id, tx_hash=first.tx_hash):
                return 0, 0
            for update in updates:
                _save(news, update, stamp)
            for candidate in prepared:
                admit_market_item(repos, candidate, ingest_mode="live", trace_id="chain-tape:net-buy", now_ms=stamp)
            news.wallet_mark_receipt_derived(
                chain_id=first.chain_id, tx_hash=first.tx_hash, now_ms=stamp, reasons=reasons
            )
            return len(events), len(updates)

        result = await self._write("news_wallet_net_buy_commit", commit, errors)
        return None if result is FAILED else result

    async def _slide(self, errors: list[str]) -> int:
        data = await self._read(
            "news_wallet_active_events",
            lambda repos: (
                repos.news.chain_tape_state(),
                repos.news.wallet_active_events(after_id=self._active_after),
            ),
            errors,
        )
        if data is FAILED:
            return 0
        state, events = data
        if not state or state["scanned_at_ms"] is None:
            return 0
        self._active_after = events[-1]["item_id"] if len(events) == 100 else ""
        changed = 0
        for event in events:
            data = await self._read(
                "news_wallet_slide_window",
                partial(_read_slide, event=event, state=state),
                errors,
            )
            if data is FAILED:
                break
            members, fills = data
            snapshot = calculate_windows(
                fills=fills,
                members=members,
                chain_id=event["chain_id"],
                token=event["token"],
                cutoff_at_ms=state["scanned_at_ms"],
                cutoff_block=state["scanned_block"],
                cutoff_log=state["scanned_log"],
                coverage_from_ms=state["coverage_from_ms"],
                coverage_gap_at_ms=state["gap_at_ms"],
                roster_version=state["roster_version"],
                rules=self.rules,
            )
            ended = snapshot.cutoff_at_ms - event["last_effective_buy_at_ms"] >= SLOW_WINDOW_MS
            reason = (
                "roster_changed"
                if snapshot.roster_version != event["latest_snapshot"]["roster_version"]
                else "window_expiry"
            )
            if state["gap_at_ms"] is not None and state["gap_at_ms"] > snapshot.slow.from_ms:
                reason = "collection_gap"
            if not ended and _business(snapshot) == _business(NetBuySnapshot.model_validate(event["latest_snapshot"])):
                continue
            update = _update(event, snapshot, reason, self._clock(), ended=ended)
            result = await self._write(
                "news_wallet_slide_commit", partial(_save_slide, update=update, stamp=self._clock()), errors
            )
            if result is FAILED:
                break
            changed += 1
        return changed


def _business(snapshot: NetBuySnapshot) -> dict[str, Any]:
    value = snapshot.model_dump(mode="json")
    for key in ("cutoff_at_ms", "cutoff_block", "cutoff_log"):
        value.pop(key)
    for window in ("fast", "slow"):
        value[window].pop("from_ms")
        value[window].pop("to_ms")
    return value


def _changed(
    active: dict[str, Any],
    snapshot: NetBuySnapshot,
    changes: Sequence[ClassifiedFill],
    increased: bool,
) -> _Update | None:
    last = snapshot.cutoff_at_ms if increased else active["last_effective_buy_at_ms"]
    if last == active["last_effective_buy_at_ms"] and _business(snapshot) == _business(
        NetBuySnapshot.model_validate(active["latest_snapshot"])
    ):
        return None
    reason = "buy"
    if any(fill.kind == "transfer_out" for fill in changes):
        reason = "transfer_out_incomplete"
    elif any(fill.usd is None for fill in changes):
        reason = "unpriced_trade"
    elif any(fill.kind == "sell" for fill in changes):
        reason = "sell"
    return _Update(active["item_id"], snapshot.model_dump_json(), snapshot.matched, reason, last, None)


def _update(
    active: dict[str, Any],
    snapshot: NetBuySnapshot,
    reason: str,
    stamp: int,
    *,
    ended: bool,
) -> _Update:
    del stamp
    return _Update(
        active["item_id"],
        snapshot.model_dump_json(),
        snapshot.matched,
        reason,
        active["last_effective_buy_at_ms"],
        active["last_effective_buy_at_ms"] + SLOW_WINDOW_MS if ended else None,
    )


def _save(news: Any, update: _Update, stamp: int) -> None:
    news.wallet_update_event(
        item_id=update.item_id,
        snapshot_json=update.snapshot_json,
        matched=update.matched,
        reason=update.reason,
        last_effective_buy_at_ms=update.last_effective_buy_at_ms,
        ended_at_ms=update.ended_at_ms,
        now_ms=stamp,
    )


def _read_slide(repos: Any, *, event: dict[str, Any], state: dict[str, Any]) -> Any:
    return (
        repos.news.chain_tape_members(state["roster_version"]),
        repos.news.wallet_window_fills(
            chain_id=event["chain_id"],
            token=event["token"],
            from_ms=state["scanned_at_ms"] - SLOW_WINDOW_MS,
            to_ms=state["scanned_at_ms"],
            block=state["scanned_block"],
            log=state["scanned_log"],
        ),
    )


def _save_slide(repos: Any, *, update: _Update, stamp: int) -> None:
    _save(repos.news, update, stamp)
