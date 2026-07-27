from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracefold.market.pricing.message_price_payload import message_price_payload


@dataclass(frozen=True)
class TokenTargetStageBuild:
    stages: list[dict[str, Any]]
    annotations: dict[str, dict[str, Any]]


def build_token_target_stages(rows: list[dict[str, Any]]) -> TokenTargetStageBuild:
    ordered = sorted(rows, key=_row_sort_key)
    annotations: dict[str, dict[str, Any]] = {}
    segments: list[dict[str, Any]] = []
    author_counts: dict[str, int] = {}
    prefix_rows: list[dict[str, Any]] = []
    first_ready_price: float | None = None
    previous_ready_price: float | None = None

    for index, row in enumerate(ordered):
        prefix_rows.append(row)
        price = _ready_price(row)
        if first_ready_price is None and price is not None:
            first_ready_price = price
        phase = _phase(prefix_rows, first_ready_price)
        current = segments[-1] if segments else None
        if current is None or current["phase"] != phase:
            current = {"phase": phase, "rows": [], "start_index": index}
            segments.append(current)
        current["rows"].append(row)

        event_id = str(row.get("event_id") or "")
        annotations[event_id] = {
            "author_role": _author_role(row, index=index, author_counts=author_counts),
            "price_delta_from_previous_post_pct": _price_delta(previous_ready_price, price),
        }
        author = _author(row)
        if author:
            author_counts[author] = author_counts.get(author, 0) + 1
        if price is not None:
            previous_ready_price = price

    stages: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        stage = _stage(segment, ordered=ordered, index=index)
        stages.append(stage)
        representative_ids = set(stage["representative_event_ids"])
        for row in segment["rows"]:
            event_id = str(row.get("event_id") or "")
            annotations[event_id].update(
                {
                    "stage_id": stage["stage_id"],
                    "stage_phase": stage["phase"],
                    "is_stage_representative": event_id in representative_ids,
                }
            )
    return TokenTargetStageBuild(stages=stages, annotations=annotations)


def _stage(segment: dict[str, Any], *, ordered: list[dict[str, Any]], index: int) -> dict[str, Any]:
    rows = list(segment["rows"])
    start_ms = int(rows[0].get("received_at_ms") or 0)
    end_ms = int(rows[-1].get("received_at_ms") or start_ms)
    representative_ids = _representative_event_ids(rows)
    price_start = _previous_ready_price(ordered, int(segment["start_index"])) if index > 0 else None
    price_end = _last_ready_price(rows)
    if price_start is None:
        price_start = _first_ready_price(rows)
    return {
        "stage_id": f"{segment['phase']}:{start_ms}:{index + 1}",
        "phase": segment["phase"],
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": max(0, end_ms - start_ms),
        "trigger_reason": _trigger_reason(segment["phase"]),
        "confidence": _stage_confidence(rows),
        "people": _people(rows),
        "representative_event_ids": representative_ids,
        "price": {
            "status": "ready" if price_end is not None else "pending_observation",
            "start_price": price_start,
            "end_price": price_end,
            "delta_pct": _price_delta(price_start, price_end),
            "observation_ids": _observation_ids(rows),
            "max_observation_lag_ms": _max_price_lag(rows),
        },
        "risks": _stage_risks(rows, segment["phase"]),
    }


def _phase(rows: list[dict[str, Any]], first_ready_price: float | None) -> str:
    posts = len(rows)
    if posts <= 1:
        return "seed"
    latest_price = _last_ready_price(rows)
    price_delta = _price_delta(first_ready_price, latest_price)
    if price_delta is not None and price_delta >= 0.5:
        return "chase"
    top_share = _top_author_share(rows)
    if posts >= 3 and top_share >= 0.75:
        return "concentration"
    authors = len({_author(row) for row in rows if _author(row)})
    if posts >= 4 and authors >= 3:
        return "expansion"
    return "ignition"


def _people(rows: list[dict[str, Any]]) -> dict[str, Any]:
    authors = [_author(row) for row in rows if _author(row)]
    return {
        "posts": len(rows),
        "authors": len(set(authors)),
        "new_authors": len(set(authors)),
        "top_author_share": _top_author_share(rows),
    }


def _author_role(row: dict[str, Any], *, index: int, author_counts: dict[str, int]) -> str:
    author = _author(row)
    if author and author_counts.get(author, 0) > 0:
        return "repeater"
    if index == 0:
        return "seed"
    if index <= 2:
        return "early_amplifier"
    return "amplifier"


def _representative_event_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["event_id"]) for row in rows[:3] if row.get("event_id")]


def _stage_risks(rows: list[dict[str, Any]], phase: str) -> list[str]:
    risks: list[str] = []
    if phase == "concentration":
        risks.append("author_concentration")
    if phase == "chase":
        risks.append("price_chase_risk")
    if not any(message_price_payload(row).get("observation_id") for row in rows):
        risks.append("missing_message_price")
    return risks


def _stage_confidence(rows: list[dict[str, Any]]) -> float:
    authors = len({_author(row) for row in rows if _author(row)})
    return round(min(0.95, 0.45 + len(rows) * 0.08 + authors * 0.08), 4)


def _trigger_reason(phase: str) -> str:
    return {
        "seed": "first_token_evidence",
        "ignition": "followup_or_new_author",
        "expansion": "independent_author_expansion",
        "concentration": "single_author_concentration",
        "chase": "price_moved_after_social",
        "fade": "attention_decay",
    }.get(phase, "stage_transition")


def _observation_ids(rows: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        observation_id = message_price_payload(row).get("observation_id")
        if observation_id:
            ids.append(str(observation_id))
    return ids


def _max_price_lag(rows: list[dict[str, Any]]) -> int | None:
    lags = [message_price_payload(row).get("observation_lag_ms") for row in rows]
    numeric = [int(lag) for lag in lags if lag is not None]
    return max(numeric) if numeric else None


def _previous_ready_price(rows: list[dict[str, Any]], before_index: int) -> float | None:
    for row in reversed(rows[:before_index]):
        price = _ready_price(row)
        if price is not None:
            return price
    return None


def _first_ready_price(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        price = _ready_price(row)
        if price is not None:
            return price
    return None


def _last_ready_price(rows: list[dict[str, Any]]) -> float | None:
    for row in reversed(rows):
        price = _ready_price(row)
        if price is not None:
            return price
    return None


def _ready_price(row: dict[str, Any]) -> float | None:
    price = message_price_payload(row)
    if price.get("status") != "ready" or price.get("price_usd") is None:
        return None
    return float(price["price_usd"])


def _price_delta(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return round((float(end) / float(start)) - 1.0, 6)


def _top_author_share(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    counts: dict[str, int] = {}
    for row in rows:
        author = _author(row)
        if author:
            counts[author] = counts.get(author, 0) + 1
    return round((max(counts.values()) / len(rows)) if counts else 0.0, 6)


def _author(row: dict[str, Any]) -> str:
    return str(row.get("author_handle") or "").strip()


def _row_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    return (int(row.get("received_at_ms") or 0), str(row.get("event_id") or ""))
