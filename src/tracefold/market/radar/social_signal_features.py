from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


def source_weighted_effective_authors(rows: Sequence[Mapping[str, Any]]) -> float:
    weights = _author_weights(rows)
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    concentration = sum((weight / total) ** 2 for weight in weights.values())
    if concentration <= 0:
        return 0.0
    return 1.0 / concentration


def time_to_nth_independent_author_ms(rows: Sequence[Mapping[str, Any]], n: int) -> int | None:
    if n <= 0:
        return None
    ordered_rows = _ordered_rows(rows)
    if not ordered_rows:
        return None

    first_seen_ms: int | None = None
    seen: set[str] = set()
    for row in ordered_rows:
        handle = _handle(row)
        if handle is None or handle in seen:
            continue
        seen_ms = _int(row.get("received_at_ms"))
        if seen_ms is None:
            continue
        if first_seen_ms is None:
            first_seen_ms = seen_ms
        seen.add(handle)
        if len(seen) == n:
            return max(0, seen_ms - first_seen_ms)
    return None


def followup_author_count(rows: Sequence[Mapping[str, Any]]) -> int:
    ordered_rows = _ordered_rows(rows)
    seed_author: str | None = None
    followup_authors: set[str] = set()
    for row in ordered_rows:
        handle = _handle(row)
        if handle is None:
            continue
        if seed_author is None:
            seed_author = handle
            continue
        if handle != seed_author:
            followup_authors.add(handle)
    return len(followup_authors)


def author_entropy(rows: Sequence[Mapping[str, Any]]) -> float:
    counts = _author_counts(rows)
    total = sum(counts.values())
    if total <= 0 or len(counts) <= 1:
        return 0.0

    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log(probability)
    return entropy


def _author_counts(rows: Sequence[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        handle = _handle(row)
        if handle is not None:
            counts[handle] += 1
    return counts


def _author_weights(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for row in rows:
        handle = _handle(row)
        if handle is None:
            continue
        weight = _source_weight(row)
        if weight <= 0:
            continue
        weights[handle] = weights.get(handle, 0.0) + weight
    return weights


def _ordered_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row
        for _, row in sorted(
            enumerate(rows),
            key=lambda item: (_sort_ms(item[1]), item[0]),
        )
    ]


def _sort_ms(row: Mapping[str, Any]) -> int:
    return _int(row.get("received_at_ms")) or 0


def _handle(row: Mapping[str, Any]) -> str | None:
    value = row.get("author_handle")
    if value is None:
        value = row.get("author")
    if value is None:
        return None
    handle = str(value).strip().lstrip("@").lower()
    return handle or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_weight(row: Mapping[str, Any]) -> float:
    if "_source_weight" not in row:
        return 1.0
    value = row.get("_source_weight")
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(weight) or weight < 0:
        return 0.0
    return weight
