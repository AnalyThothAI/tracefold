from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tracefold.market.pricing.live_market import LIVE_MARKET_STALE_AFTER_MS
from tracefold.market.radar.constants import (
    TOKEN_RADAR_CURRENT_WINDOW_MS,
    TOKEN_RADAR_INPUT_BYTE_CAP,
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_MAX_ITEMS,
    TOKEN_RADAR_OUTPUT_BYTE_CAP,
    TOKEN_RADAR_PRIOR_WINDOW_MS,
    TOKEN_RADAR_RULESET,
    TOKEN_RADAR_RULESET_FINGERPRINT,
    TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
)

_RESOLVED_STATUSES = frozenset({"EXACT", "UNIQUE_BY_CONTEXT"})
_RESOLVED_TARGET_TYPES = frozenset({"Asset", "CexToken"})
_SPACE_RE = re.compile(r"\s+")


class TokenRadarInputOverflow(RuntimeError):
    """The material-fact input exceeded a hard reducer envelope."""


class TokenRadarBudgetExceeded(RuntimeError):
    """The deterministic reducer exceeded its code-owned CPU deadline."""


class TokenRadarOutputOverflow(RuntimeError):
    """The compact public packet exceeded its uncompressed byte envelope."""


@dataclass(frozen=True, slots=True)
class ReducedTokenRadar:
    ruleset_version: str
    ruleset_fingerprint: str
    input_fingerprint: str
    state_fingerprint: str
    input_rows: int
    input_bytes: int
    output_bytes: int
    eligible_rows: int
    snapshot: dict[str, Any]


def reduce_token_radar(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
    deadline_monotonic: float | None = None,
) -> ReducedTokenRadar:
    """Reduce bounded material facts to the one public rolling Radar snapshot."""

    ruleset_fingerprint = TOKEN_RADAR_RULESET_FINGERPRINT
    parsed_now_ms = _nonnegative_int(now_ms, "token_radar_now_ms_invalid")
    input_rows = len(rows)
    if input_rows > TOKEN_RADAR_INPUT_ROW_CAP:
        raise TokenRadarInputOverflow("token_radar_input_row_overflow")
    input_bytes = _serialized_size(rows)
    if input_bytes > TOKEN_RADAR_INPUT_BYTE_CAP:
        raise TokenRadarInputOverflow("token_radar_input_byte_overflow")
    _check_deadline(deadline_monotonic)

    current_start_ms = max(0, parsed_now_ms - TOKEN_RADAR_CURRENT_WINDOW_MS)
    prior_start_ms = max(0, current_start_ms - TOKEN_RADAR_PRIOR_WINDOW_MS)
    canonical_inputs: list[dict[str, Any]] = []
    by_target: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    evidence_as_of_ms = 0

    for index, raw in enumerate(rows):
        if index % 128 == 0:
            _check_deadline(deadline_monotonic)
        row = _canonical_source_row(raw)
        received_at_ms = row["received_at_ms"]
        if received_at_ms is None or received_at_ms <= prior_start_ms or received_at_ms > parsed_now_ms:
            continue
        window = "current" if received_at_ms >= current_start_ms else "prior"
        canonical_input = {**row, "window": window}
        canonical_inputs.append(canonical_input)
        evidence_as_of_ms = max(
            evidence_as_of_ms,
            received_at_ms,
            (
                int(row["latest_price_observed_at_ms"] or 0)
                if int(row["latest_price_observed_at_ms"] or 0) <= parsed_now_ms
                else 0
            ),
        )
        if row["resolution_status"] not in _RESOLVED_STATUSES:
            continue
        target_type = row["target_type"]
        target_id = row["target_id"]
        event_id = row["event_id"]
        if target_type not in _RESOLVED_TARGET_TYPES or not target_id or not event_id:
            continue
        target_events = by_target[(target_type, target_id)]
        existing = target_events.get(event_id)
        if existing is None or _source_row_sort_key(row) < _source_row_sort_key(existing):
            target_events[event_id] = row

    fingerprint_payload = sorted(canonical_inputs, key=_canonical_input_sort_key)
    input_fingerprint = _fingerprint(fingerprint_payload)
    candidates: list[dict[str, Any]] = []
    for index, (target_key, target_events) in enumerate(sorted(by_target.items())):
        if index % 64 == 0:
            _check_deadline(deadline_monotonic)
        candidate = _reduce_target(
            target_key,
            list(target_events.values()),
            current_start_ms=current_start_ms,
            prior_start_ms=prior_start_ms,
            now_ms=parsed_now_ms,
            deadline_monotonic=deadline_monotonic,
        )
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=_candidate_rank_key)
    eligible_rows = len(candidates)
    snapshot = {
        "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
        "evidence_as_of_ms": evidence_as_of_ms,
        "eligible_total": eligible_rows,
        "items": [candidate["item"] for candidate in candidates[:TOKEN_RADAR_MAX_ITEMS]],
    }
    output_bytes = len(_canonical_json_bytes(snapshot))
    if output_bytes > TOKEN_RADAR_OUTPUT_BYTE_CAP:
        raise TokenRadarOutputOverflow("token_radar_output_byte_overflow")
    _check_deadline(deadline_monotonic)
    return ReducedTokenRadar(
        input_fingerprint=input_fingerprint,
        ruleset_version=TOKEN_RADAR_RULESET.version,
        ruleset_fingerprint=ruleset_fingerprint,
        state_fingerprint=_fingerprint(
            {
                "ruleset_version": TOKEN_RADAR_RULESET.version,
                "ruleset_fingerprint": ruleset_fingerprint,
                "snapshot": snapshot,
            }
        ),
        input_rows=input_rows,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        eligible_rows=eligible_rows,
        snapshot=snapshot,
    )


def _reduce_target(
    target_key: tuple[str, str],
    rows: list[dict[str, Any]],
    *,
    current_start_ms: int,
    prior_start_ms: int,
    now_ms: int,
    deadline_monotonic: float | None,
) -> dict[str, Any] | None:
    ordered = sorted(rows, key=_event_sort_key)
    _check_deadline(deadline_monotonic)
    current = [row for row in ordered if current_start_ms <= int(row["received_at_ms"]) <= now_ms]
    prior = [row for row in ordered if prior_start_ms < int(row["received_at_ms"]) < current_start_ms]
    current_mentions = len(current)
    prior_mentions = len(prior)
    current_by_author = _first_event_by_author(current)
    prior_by_author = _first_event_by_author(prior)
    mention_delta = current_mentions - prior_mentions
    new_author_count = len(set(current_by_author) - set(prior_by_author))
    independent_authors = sorted(current_by_author.values(), key=_event_sort_key)
    text_keys = [_text_key(row) for row in current]
    independent_text_count = len({text for text in text_keys if text is not None})
    duplicate_share = _duplicate_share(text_keys, current_mentions)
    time_to_nth_author_ms = _time_to_nth_author_ms(
        independent_authors,
        TOKEN_RADAR_RULESET.minimum_independent_authors,
    )

    rule_results = (
        mention_delta >= TOKEN_RADAR_RULESET.minimum_attention_delta,
        len(current_by_author) >= TOKEN_RADAR_RULESET.minimum_independent_authors,
        duplicate_share <= TOKEN_RADAR_RULESET.maximum_duplicate_share,
        time_to_nth_author_ms is not None and time_to_nth_author_ms <= TOKEN_RADAR_RULESET.maximum_propagation_ms,
    )
    if not all(rule_results):
        return None
    trigger = _first_admitted_event(
        current,
        prior_mentions=prior_mentions,
        deadline_monotonic=deadline_monotonic,
    )
    if trigger is None:
        return None
    identity = _target_identity(target_key, ordered)
    if identity is None:
        return None
    market = _market_since_signal(trigger, now_ms=now_ms)
    counter_evidence = "market_confirmation_unavailable" if market["status"] == "unavailable" else None
    item = {
        "target": identity,
        "trigger_event_id": trigger["event_id"],
        "triggered_at_ms": trigger["received_at_ms"],
        "why_now": {
            "current_mentions": current_mentions,
            "prior_mentions": prior_mentions,
            "mention_delta": mention_delta,
        },
        "evidence": {
            "new_independent_author_count": new_author_count,
            "independent_text_count": independent_text_count,
            "time_to_nth_author_ms": time_to_nth_author_ms,
            "duplicate_share": duplicate_share,
        },
        "market": market,
        "counter_evidence": counter_evidence,
    }
    return {
        "item": item,
        "rank": (
            -int(trigger["received_at_ms"]),
            target_key[0],
            target_key[1],
        ),
    }


def _canonical_source_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": _text(raw.get("target_type")),
        "target_id": _text(raw.get("target_id")),
        "symbol": _text(raw.get("symbol")),
        "chain": _text(raw.get("chain")),
        "exchange": _text(raw.get("exchange")),
        "address": _text(raw.get("address")),
        "resolution_status": _text(raw.get("resolution_status")),
        "event_id": _text(raw.get("event_id")),
        "received_at_ms": _optional_nonnegative_int(raw.get("received_at_ms")),
        "author_handle": _normalized_author(raw.get("author_handle")),
        "text": _text(raw.get("text")),
        "signal_price_usd": _decimal_text(raw.get("signal_price_usd")),
        "latest_price_usd": _decimal_text(raw.get("latest_price_usd")),
        "latest_price_observed_at_ms": _optional_nonnegative_int(raw.get("latest_price_observed_at_ms")),
    }


def _target_identity(target_key: tuple[str, str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = max(rows, key=_event_sort_key)
    symbol = _text(latest.get("symbol"))
    if symbol is None:
        symbols = sorted(candidate for row in rows if (candidate := _text(row.get("symbol"))) is not None)
        symbol = symbols[0] if symbols else None
    if symbol is None:
        return None
    return {
        "target_type": target_key[0],
        "target_id": target_key[1],
        "symbol": symbol,
        "chain": _text(latest.get("chain")),
        "exchange": _text(latest.get("exchange")),
        "address": _text(latest.get("address")),
    }


def _market_since_signal(trigger: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    signal = _positive_decimal(trigger.get("signal_price_usd"))
    latest = _positive_decimal(trigger.get("latest_price_usd"))
    latest_observed_at_ms = _optional_nonnegative_int(trigger.get("latest_price_observed_at_ms"))
    triggered_at_ms = _optional_nonnegative_int(trigger.get("received_at_ms"))
    if (
        signal is None
        or latest is None
        or latest_observed_at_ms is None
        or triggered_at_ms is None
        or latest_observed_at_ms < triggered_at_ms
        or latest_observed_at_ms > now_ms
        or now_ms - latest_observed_at_ms > LIVE_MARKET_STALE_AFTER_MS
    ):
        return {"status": "unavailable", "price_change_since_signal": None}
    change = float((latest - signal) / signal)
    if not math.isfinite(change):
        return {"status": "unavailable", "price_change_since_signal": None}
    return {"status": "confirmed", "price_change_since_signal": change}


def _first_event_by_author(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        author = _text(row.get("author_handle"))
        if author is not None:
            result.setdefault(author, row)
    return result


def _first_admitted_event(
    rows: list[dict[str, Any]],
    *,
    prior_mentions: int,
    deadline_monotonic: float | None,
) -> dict[str, Any] | None:
    authors: set[str] = set()
    text_counts: Counter[str] = Counter()
    duplicate_events = 0
    first_author_at_ms: int | None = None
    nth_author_at_ms: int | None = None
    for index, row in enumerate(rows):
        if index % 128 == 0:
            _check_deadline(deadline_monotonic)
        author = _text(row.get("author_handle"))
        if author is not None and author not in authors:
            authors.add(author)
            received_at_ms = int(row["received_at_ms"])
            if first_author_at_ms is None:
                first_author_at_ms = received_at_ms
            if len(authors) == TOKEN_RADAR_RULESET.minimum_independent_authors:
                nth_author_at_ms = received_at_ms
        text_key = _text_key(row) or "<missing>"
        if text_counts[text_key] > 0:
            duplicate_events += 1
        text_counts[text_key] += 1
        prefix_mentions = index + 1
        duplicate_share = duplicate_events / prefix_mentions
        propagation_ms = (
            max(0, nth_author_at_ms - first_author_at_ms)
            if first_author_at_ms is not None and nth_author_at_ms is not None
            else None
        )
        if (
            prefix_mentions - prior_mentions >= TOKEN_RADAR_RULESET.minimum_attention_delta
            and len(authors) >= TOKEN_RADAR_RULESET.minimum_independent_authors
            and duplicate_share <= TOKEN_RADAR_RULESET.maximum_duplicate_share
            and propagation_ms is not None
            and propagation_ms <= TOKEN_RADAR_RULESET.maximum_propagation_ms
        ):
            return row
    return None


def _time_to_nth_author_ms(rows: list[dict[str, Any]], nth_author: int) -> int | None:
    ordered = sorted(rows, key=_event_sort_key)
    if len(ordered) < nth_author:
        return None
    return max(0, int(ordered[nth_author - 1]["received_at_ms"]) - int(ordered[0]["received_at_ms"]))


def _duplicate_share(text_keys: list[str | None], current_mentions: int) -> float:
    if current_mentions <= 0:
        return 0.0
    counts = Counter(text if text is not None else "<missing>" for text in text_keys)
    duplicate_events = sum(max(0, count - 1) for count in counts.values())
    return duplicate_events / current_mentions


def _text_key(row: Mapping[str, Any]) -> str | None:
    text = _text(row.get("text"))
    if text is None:
        return None
    normalized = _SPACE_RE.sub(" ", text.casefold()).strip()
    return f"text:{normalized}" if normalized else None


def _serialized_size(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(
        json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _fingerprint(value: Any) -> str:
    encoded = _canonical_json_bytes(value)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(candidate["rank"])


def _canonical_input_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("target_type") or ""),
        str(row.get("target_id") or ""),
        int(row.get("received_at_ms") or 0),
        str(row.get("event_id") or ""),
        str(row.get("author_handle") or ""),
        str(row.get("window") or ""),
    )


def _source_row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("received_at_ms") or 0),
        str(row.get("author_handle") or ""),
        str(row.get("text") or ""),
    )


def _event_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    return (int(row.get("received_at_ms") or 0), str(row.get("event_id") or ""))


def _normalized_author(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return text.lstrip("@").casefold() or None


def _decimal_text(value: Any) -> str | None:
    decimal = _positive_decimal(value)
    return format(decimal, "f") if decimal is not None else None


def _positive_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_int(value: Any, error_code: str) -> int:
    parsed = _optional_nonnegative_int(value)
    if parsed is None:
        raise ValueError(error_code)
    return parsed


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
        raise TokenRadarBudgetExceeded("token_radar_reducer_budget_exceeded")


__all__ = [
    "TOKEN_RADAR_INPUT_BYTE_CAP",
    "TOKEN_RADAR_INPUT_ROW_CAP",
    "TOKEN_RADAR_OUTPUT_BYTE_CAP",
    "ReducedTokenRadar",
    "TokenRadarBudgetExceeded",
    "TokenRadarInputOverflow",
    "TokenRadarOutputOverflow",
    "reduce_token_radar",
]
