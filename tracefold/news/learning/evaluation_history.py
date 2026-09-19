"""Arm-local reader receipts and shared history assembly for CandidateEvaluator."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..events.storyline import NO_STORYLINE_KEY
from ..market_review.instrument_storage import InstrumentsRepository
from ..models import MarketAsset, base_symbol
from ..reader_history import TARGETED_HISTORY_WINDOW_MS, ReaderHistorySnapshot, build_reader_history
from ..storage.decisions import delivered_history_rows


@dataclass(slots=True)
class Receipt:
    event_id: str
    at_ms: int
    storyline_key: str
    magnitude: int
    direction: str
    headline_zh: str
    why_zh: str
    comparison_title: str = ""
    comparison_fingerprint: str = ""
    dedupe_family: str = "general"
    grounded_assets: tuple[str, ...] = ()
    # Typed, like `ReaderHistoryRow.assets` (#651 §6.2): a replayed receipt has to carry the same asset
    # identity the production row carries, or the two overlap rules cannot be the same rule.
    assets: tuple[MarketAsset, ...] = ()
    canonical_assets: tuple[str, ...] = ()
    provenance_status: str = "delivery_bound"

    def __post_init__(self) -> None:
        """Establish the one comparable asset identity here, because two sources build a Receipt.

        `receipt_from_output` hands over `MarketAsset`s; `seed_receipts` hands over the `{symbol,
        market_type}` objects PostgreSQL returned for the jsonb column, and `Receipt(**row)` is how both
        `DatasetRepository._project_episodes` and `CandidateEvaluator` build the replay's opening ledger.
        Without this the second kind reached `as_told_row` as plain dicts and raised on `asset.symbol`,
        so a replay seeded from real receipts could not build a told ledger at all.
        """

        self.assets = tuple(MarketAsset.of(value) for value in self.assets)
        self.grounded_assets = tuple(str(value) for value in self.grounded_assets)
        self.canonical_assets = tuple(str(value) for value in self.canonical_assets)

    def as_told_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provenance_status": self.provenance_status,
            "at_ms": self.at_ms,
            "storyline_key": self.storyline_key,
            "comparison_title": self.comparison_title,
            "comparison_fingerprint": self.comparison_fingerprint,
            "dedupe_family": self.dedupe_family,
            "magnitude": self.magnitude,
            "direction": self.direction,
            "headline_zh": self.headline_zh,
            "why_zh": self.why_zh,
            "grounded_assets": list(self.grounded_assets),
            "assets": [{"symbol": asset.symbol, "market_type": asset.market_type} for asset in self.assets],
            "canonical_assets": list(self.canonical_assets),
        }


@dataclass(slots=True)
class ArmState:
    receipts: deque[Receipt] = field(default_factory=deque)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def expire(self, at_ms: int) -> None:
        cutoff = at_ms - TARGETED_HISTORY_WINDOW_MS
        while self.receipts and self.receipts[0].at_ms < cutoff:
            self.receipts.popleft()


class EvaluationReaderHistory:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._symbol_aliases: dict[str, str] | None = None
        # The same catalogue reader production uses, over the same session (#651 §A). A replay that
        # resolved candidates by a second copy of that query could disagree with the live judge about
        # what the catalogue holds, which is the one thing a replay exists to rule out.
        self._instruments = InstrumentsRepository(conn)

    def catalog_candidates(self, symbols: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """What the catalogue holds for each symbol a replayed Event names, uncollapsed."""

        return self._instruments.instrument_class_candidates(symbols)

    def _alias_map(self) -> dict[str, str]:
        if self._symbol_aliases is None:
            rows = self._conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
            self._symbol_aliases = {
                str(row["alias"]): base_symbol(str(row["base_symbol"]))
                for row in rows
                if row.get("alias") and row.get("base_symbol")
            }
        return self._symbol_aliases

    def canonical_assets(self, symbols: Sequence[str]) -> tuple[str, ...]:
        aliases = self._alias_map()
        return tuple(
            sorted(
                {
                    aliases.get(str(symbol), aliases.get(base_symbol(str(symbol)), base_symbol(str(symbol))))
                    for symbol in symbols
                    if symbol
                }
            )
        )

    def build(self, case: Mapping[str, Any], state: ArmState) -> ReaderHistorySnapshot:
        event = dict((case.get("snapshot") or {}).get("card") or {})
        grounded = tuple(str(value) for value in event.get("grounded_assets") or ())
        return build_reader_history(
            [receipt.as_told_row() for receipt in state.receipts],
            now_ms=int(case["opened_at_ms"]),
            dedupe_family=str(event.get("dedupe_family") or "general"),
            comparison_fingerprint=str(event.get("comparison_fingerprint") or ""),
            canonical_assets=tuple(case["canonical_assets"])
            if "canonical_assets" in case
            else self.canonical_assets(grounded),
            comparison_title=str(event.get("comparison_title") or ""),
        )

    def seed_receipts(self, *, from_ms: int) -> tuple[dict[str, Any], ...]:
        """Project the same frozen delivery binding production uses into an evaluator receipt source.

        Every delivered card in the bounded look-back, whatever arm produced it (#651 §9). The told ledger
        is the *reader's* ledger: what they had already been shown when the next card arrived is a fact
        about the deliveries, not about which Program wrote them, and clamping it to one bundle and one
        epoch meant the first hours after a deploy replayed against an empty history the reader never had.
        """

        return delivered_history_rows(self._conn, cutoff_at_ms=from_ms)


def receipt_from_output(*, event_id: str, at_ms: int, output: Mapping[str, Any], verdict: Mapping[str, Any]) -> Receipt:
    return Receipt(
        event_id=event_id,
        at_ms=at_ms,
        storyline_key=str(output.get("storyline_key") or NO_STORYLINE_KEY),
        magnitude=int(verdict.get("magnitude") or 0),
        direction=str(verdict.get("direction") or "unclear"),
        headline_zh=str(verdict.get("headline_zh") or ""),
        why_zh=str(verdict.get("why_zh") or ""),
        comparison_title=str(output.get("comparison_title") or ""),
        comparison_fingerprint=str(output.get("comparison_fingerprint") or ""),
        dedupe_family=str(output.get("dedupe_family") or "general"),
        grounded_assets=tuple(str(value) for value in output.get("grounded_assets") or ()),
        assets=tuple(MarketAsset.of(asset) for asset in verdict.get("assets") or () if isinstance(asset, Mapping)),
        canonical_assets=tuple(str(value) for value in output.get("canonical_assets") or ()),
    )


__all__ = ["ArmState", "EvaluationReaderHistory", "Receipt", "receipt_from_output"]
