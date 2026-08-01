from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


class OutputRowOversized(RuntimeError):
    pass


def split_bounded_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    context: Mapping[str, Any],
    byte_cap: int,
) -> list[list[dict[str, Any]]]:
    """Split an ordered row set without changing its order or contents."""

    if byte_cap <= 0:
        raise ValueError("output_envelope_byte_cap_required")
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    empty_size = _serialized_size({**dict(context), "rows": []})
    current_size = empty_size
    for source_row in rows:
        row = dict(source_row)
        row_size = _serialized_size(row)
        candidate_size = current_size + row_size + (1 if current else 0)
        if candidate_size <= byte_cap:
            current.append(row)
            current_size = candidate_size
            continue
        if not current:
            raise OutputRowOversized("output_envelope_single_row_oversized")
        batches.append(current)
        current = [row]
        current_size = empty_size + row_size
        if current_size > byte_cap:
            raise OutputRowOversized("output_envelope_single_row_oversized")
    if current:
        batches.append(current)
    return batches


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


__all__ = ["OutputRowOversized", "split_bounded_rows"]
