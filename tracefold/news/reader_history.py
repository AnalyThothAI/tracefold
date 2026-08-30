"""Bounded reader-history values and pure selection rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from .artifact_identity import canonical_sha
from .models import base_symbol

RECENT_HISTORY_WINDOW_MS: Final = 4 * 3_600_000
TARGETED_HISTORY_WINDOW_MS: Final = 48 * 3_600_000
RECENT_HISTORY_MAX: Final = 128
TARGETED_EXACT_MAX: Final = 8
TARGETED_ASSET_MAX: Final = 24
READER_HISTORY_ID: Final = "news_reader_history_v2"
READER_HISTORY_CONTRACT: Final = {
    "reader_history": READER_HISTORY_ID,
    "truth": {
        "delivery_kind": "first",
        "delivery_state": "sent",
        "verdict_stage": "triage",
        "final_decisions": ["push", "escalate"],
    },
    "windows": {
        "recent": {"age": "<=", "window_ms": RECENT_HISTORY_WINDOW_MS},
        "targeted": {"age": ">recent_and<=targeted", "window_ms": TARGETED_HISTORY_WINDOW_MS},
    },
    "caps": {
        "recent": RECENT_HISTORY_MAX,
        "exact_fingerprint": TARGETED_EXACT_MAX,
        "canonical_asset_overlap": TARGETED_ASSET_MAX,
    },
    "targeted_reasons": {
        "exact_fingerprint": ["dedupe_family", "comparison_fingerprint"],
        "canonical_asset_overlap": ["news_event_assets", "news_symbol_aliases.base_symbol"],
    },
    "projection": [
        "event_id",
        "at_ms",
        "storyline_key",
        "comparison_title",
        "comparison_fingerprint",
        "dedupe_family",
        "grounded_assets",
        "assets",
        "canonical_assets",
        "magnitude",
        "direction",
        "headline_zh",
        "why_zh",
    ],
    "dedup": "event_id_exact_first",
    "ordering": "reason_then_sent_desc_event_id",
}
READER_HISTORY_SHA256: Final = canonical_sha(READER_HISTORY_CONTRACT)

HistoryScope = Literal["recent", "targeted"]
HistoryReason = Literal["recent", "exact_fingerprint", "canonical_asset_overlap"]

_READER_HISTORY_ROW_FIELDS: Final = frozenset(
    {
        "event_id",
        "at_ms",
        "storyline_key",
        "comparison_title",
        "comparison_fingerprint",
        "dedupe_family",
        "grounded_assets",
        "assets",
        "canonical_assets",
        "magnitude",
        "direction",
        "headline_zh",
        "why_zh",
        "history_scope",
        "retrieval_reason",
    }
)


def news_retrieval_sha256(*, told_selector_sha256: str) -> str:
    """Compose the retrieval root without making history depend on its selector consumer."""

    return canonical_sha(
        {
            "reader_history_sha256": READER_HISTORY_SHA256,
            "told_selector_sha256": str(told_selector_sha256),
        }
    )


@dataclass(frozen=True, slots=True)
class ReaderHistoryRow:
    event_id: str
    at_ms: int
    storyline_key: str
    comparison_title: str
    comparison_fingerprint: str
    dedupe_family: str
    grounded_assets: tuple[str, ...]
    assets: tuple[str, ...]
    canonical_assets: tuple[str, ...]
    magnitude: int
    direction: str
    headline_zh: str
    why_zh: str
    scope: HistoryScope = "recent"
    reason: HistoryReason = "recent"

    def as_told_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "at_ms": self.at_ms,
            "storyline_key": self.storyline_key,
            "comparison_title": self.comparison_title,
            "comparison_fingerprint": self.comparison_fingerprint,
            "dedupe_family": self.dedupe_family,
            "grounded_assets": list(self.grounded_assets),
            "assets": list(self.assets),
            "canonical_assets": list(self.canonical_assets),
            "magnitude": self.magnitude,
            "direction": self.direction,
            "headline_zh": self.headline_zh,
            "why_zh": self.why_zh,
            "history_scope": self.scope,
            "retrieval_reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReaderHistorySnapshot:
    recent_seen_rows: tuple[ReaderHistoryRow, ...] = ()
    targeted_told_rows: tuple[ReaderHistoryRow, ...] = ()
    ledger_revision: tuple[int, int, str] = (0, 0, "")

    @property
    def told_source_rows(self) -> tuple[ReaderHistoryRow, ...]:
        seen: set[str] = set()
        rows: list[ReaderHistoryRow] = []
        for row in (*self.targeted_told_rows, *self.recent_seen_rows):
            if row.event_id not in seen:
                seen.add(row.event_id)
                rows.append(row)
        return tuple(rows)


def build_reader_history(
    rows: Sequence[Mapping[str, Any] | ReaderHistoryRow],
    *,
    now_ms: int,
    dedupe_family: str = "general",
    comparison_fingerprint: str,
    canonical_assets: Sequence[str],
    include_targeted: bool = True,
) -> ReaderHistorySnapshot:
    """Build the recent policy ledger and the targeted semantic history from bounded receipt rows."""

    converted = _deduped_rows(rows)
    recent_cutoff, target_cutoff = _history_cutoffs(now_ms)
    current_assets = frozenset(base_symbol(str(symbol)) for symbol in canonical_assets if symbol)
    targeted = [row for row in converted if _is_targeted(row, recent_cutoff, target_cutoff)]
    exact = [
        row
        for row in targeted
        if row.dedupe_family == dedupe_family and row.comparison_fingerprint == comparison_fingerprint
    ]
    exact_ids = {row.event_id for row in exact}
    asset = [
        row for row in targeted if row.event_id not in exact_ids and current_assets.intersection(row.canonical_assets)
    ]
    return assemble_reader_history(
        recent_rows=[row for row in converted if _is_recent(row, recent_cutoff)],
        exact_rows=exact if include_targeted else (),
        asset_rows=asset if include_targeted else (),
        now_ms=now_ms,
    )


def assemble_reader_history(
    *,
    recent_rows: Sequence[Mapping[str, Any] | ReaderHistoryRow],
    exact_rows: Sequence[Mapping[str, Any] | ReaderHistoryRow] = (),
    asset_rows: Sequence[Mapping[str, Any] | ReaderHistoryRow] = (),
    now_ms: int,
) -> ReaderHistorySnapshot:
    """Apply the shared boundaries, reason precedence, caps, deduplication, and stable order to query results."""

    recent_cutoff, target_cutoff = _history_cutoffs(now_ms)
    recent = sorted(
        (row for row in _deduped_rows(recent_rows) if _is_recent(row, recent_cutoff)),
        key=_newest_first,
    )
    exact = sorted(
        (row for row in _deduped_rows(exact_rows) if _is_targeted(row, recent_cutoff, target_cutoff)),
        key=_newest_first,
    )
    exact_ids = {row.event_id for row in exact}
    asset = sorted(
        (
            row
            for row in _deduped_rows(asset_rows)
            if _is_targeted(row, recent_cutoff, target_cutoff) and row.event_id not in exact_ids
        ),
        key=_newest_first,
    )
    selected_recent = tuple(replace(row, scope="recent", reason="recent") for row in recent[:RECENT_HISTORY_MAX])
    selected_targeted = tuple(
        [replace(row, scope="targeted", reason="exact_fingerprint") for row in exact[:TARGETED_EXACT_MAX]]
        + [replace(row, scope="targeted", reason="canonical_asset_overlap") for row in asset[:TARGETED_ASSET_MAX]]
    )
    revision_rows = (*selected_recent, *selected_targeted)
    return ReaderHistorySnapshot(
        recent_seen_rows=selected_recent,
        targeted_told_rows=selected_targeted,
        ledger_revision=(
            len({row.event_id for row in revision_rows}),
            max((row.at_ms for row in revision_rows), default=0),
            max((row.event_id for row in revision_rows), default=""),
        ),
    )


def _deduped_rows(rows: Sequence[Mapping[str, Any] | ReaderHistoryRow]) -> tuple[ReaderHistoryRow, ...]:
    by_event: dict[str, ReaderHistoryRow] = {}
    for value in rows:
        row = value if isinstance(value, ReaderHistoryRow) else _history_row(value)
        prior = by_event.get(row.event_id)
        if row.event_id and (prior is None or _newest_first(row) < _newest_first(prior)):
            by_event[row.event_id] = row
    return tuple(by_event.values())


def _history_cutoffs(now_ms: int) -> tuple[int, int]:
    return int(now_ms) - RECENT_HISTORY_WINDOW_MS, int(now_ms) - TARGETED_HISTORY_WINDOW_MS


def _is_recent(row: ReaderHistoryRow, recent_cutoff: int) -> bool:
    return row.at_ms >= recent_cutoff


def _is_targeted(row: ReaderHistoryRow, recent_cutoff: int, target_cutoff: int) -> bool:
    return target_cutoff <= row.at_ms < recent_cutoff


def _history_row(row: Mapping[str, Any]) -> ReaderHistoryRow:
    unexpected = set(row).difference(_READER_HISTORY_ROW_FIELDS)
    if unexpected:
        raise ValueError(f"news_reader_history_fields_unexpected:{','.join(sorted(unexpected))}")
    required = _READER_HISTORY_ROW_FIELDS.difference({"history_scope", "retrieval_reason"})
    missing = required.difference(row)
    if missing:
        raise ValueError(f"news_reader_history_fields_missing:{','.join(sorted(missing))}")
    assets = tuple(
        str(value.get("symbol") if isinstance(value, Mapping) else value)
        for value in row["assets"] or ()
        if value and (not isinstance(value, Mapping) or value.get("symbol"))
    )
    grounded = tuple(str(value) for value in row["grounded_assets"] or () if value)
    canonical = tuple(sorted({base_symbol(str(value)) for value in row["canonical_assets"] or () if value}))
    return ReaderHistoryRow(
        event_id=str(row["event_id"]),
        at_ms=int(row["at_ms"]),
        storyline_key=str(row["storyline_key"]),
        comparison_title=str(row["comparison_title"]),
        comparison_fingerprint=str(row["comparison_fingerprint"]),
        dedupe_family=str(row["dedupe_family"]),
        grounded_assets=grounded,
        assets=assets,
        canonical_assets=canonical,
        magnitude=int(row["magnitude"]),
        direction=str(row["direction"]),
        headline_zh=str(row["headline_zh"]),
        why_zh=str(row["why_zh"]),
    )


def _newest_first(row: ReaderHistoryRow) -> tuple[int, str]:
    return (-row.at_ms, row.event_id)


__all__ = [
    "READER_HISTORY_CONTRACT",
    "READER_HISTORY_ID",
    "READER_HISTORY_SHA256",
    "RECENT_HISTORY_MAX",
    "RECENT_HISTORY_WINDOW_MS",
    "TARGETED_ASSET_MAX",
    "TARGETED_EXACT_MAX",
    "TARGETED_HISTORY_WINDOW_MS",
    "ReaderHistoryRow",
    "ReaderHistorySnapshot",
    "assemble_reader_history",
    "build_reader_history",
    "news_retrieval_sha256",
]
