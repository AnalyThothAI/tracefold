"""Frozen #651 novelty sequences and the one way to replay a step of one.

`issue_651_raw_cases.json` is a read-only production export: the Event, its judged verdict, its editorial
envelope, its told ledger and its deliveries exactly as PostgreSQL held them. `issue_651_novelty_sequences.json`
orders a few of those cases into the chains the reader actually received and names, per step, the gold novelty
target (`expected_duplicate_of` and `told_index_of_target`).

Everything here reconstructs production inputs from that evidence and hands them to the real
`storyline_status` / `decide()`; nothing here re-implements a policy condition.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from tracefold.news.reader_history import build_reader_history
from tracefold.news.taxonomy import NewsTaxonomyV1
from tracefold.news.triage_rules import DecidePolicy, GateFacts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
RAW_CASES_PATH = FIXTURES / "issue_651_raw_cases.json"
SEQUENCES_PATH = FIXTURES / "issue_651_novelty_sequences.json"


@lru_cache(maxsize=1)
def raw_cases() -> Mapping[str, Any]:
    document = json.loads(RAW_CASES_PATH.read_text(encoding="utf-8"))
    assert document["schema"] == "tracefold.news.issue_651_raw_cases.v1"
    return dict(document["cases"])


@lru_cache(maxsize=1)
def sequences() -> tuple[Mapping[str, Any], ...]:
    document = json.loads(SEQUENCES_PATH.read_text(encoding="utf-8"))
    assert document["schema"] == "tracefold.news.novelty_sequences.v1"
    assert document["raw_cases"] == RAW_CASES_PATH.name
    return tuple(document["sequences"])


def sequence(sequence_id: str) -> Mapping[str, Any]:
    return next(item for item in sequences() if item["id"] == sequence_id)


def case(case_key: str) -> Mapping[str, Any]:
    return raw_cases()[case_key]


def event(case_key: str) -> Mapping[str, Any]:
    return dict(case(case_key)["event"][0])


def verdict_row(case_key: str) -> Mapping[str, Any]:
    """The latest persisted triage verdict of the case, with its trace."""

    return dict(case(case_key)["verdicts"][-1])


def told(case_key: str) -> list[dict[str, Any]]:
    """The ledger the model was shown, in the order and at the indices it saw."""

    return [dict(entry) for entry in verdict_row(case_key)["trace"]["told"]]


def storyline_key(case_key: str) -> str:
    return str(verdict_row(case_key)["trace"]["storyline_key"])


def triage_stamp(case_key: str) -> int:
    """The clock `decide()` measured the storyline budget from for this card."""

    return int(verdict_row(case_key)["created_at_ms"])


def settled_at_ms(case_key: str) -> int | None:
    """When the reader was proven to have received the card, or None when none was sent."""

    delivered = [
        row for row in case(case_key)["deliveries"] if row["kind"] == "first" and row["state"] == "sent"
    ]
    return int(delivered[0]["settled_at_ms"]) if delivered else None


def frozen_policy(case_key: str) -> DecidePolicy:
    """The exact knob values the arm ran, carried by the verdict trace rather than read from the process."""

    return DecidePolicy(**dict(verdict_row(case_key)["trace"]["policy"]))


def judgment(case_key: str, **overrides: Any) -> ScoredJudgment:
    """The judgment this Event actually produced, optionally with one field controlled.

    The stored taxonomy predates `news_editorial_v3`, where `source_authority` moved from the model's
    taxonomy onto the code-owned envelope. Moving it back on read is the migration, not a fixture edit:
    the exported row is audit truth and is never rewritten.
    """

    row = verdict_row(case_key)
    values = {**row["verdict"], **overrides}
    taxonomy = dict(row["editorial"].get("taxonomy") or {})
    authority = str(taxonomy.pop("source_authority", "") or "unknown")
    return ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(values),
        editorial=EditorialEnvelope.issue(
            relevance=TradeRelevanceV1.model_validate(row["editorial"]["relevance"]),
            source_authority=authority,  # type: ignore[arg-type]
            taxonomy=NewsTaxonomyV1.model_validate(taxonomy) if taxonomy else None,
            taxonomy_error_code=None if taxonomy else "news_program_taxonomy_unavailable",
        ),
    )


def gate_facts(case_key: str) -> GateFacts:
    """The objective Gate facts of the Event, as `_gate_facts` builds them for the worker."""

    row = event(case_key)
    return GateFacts(
        grounded_assets=tuple(str(value) for value in row["grounded_assets"] or ()),
        watchlist_symbols=frozenset(str(value) for value in row["watchlist_hits"] or ()),
        admission=str(row["admission"]),
        source_age_s=None,
        member_count=int(row["member_count"] or 1),
    )


def history_row(case_key: str) -> dict[str, Any]:
    """One delivered card of the sequence, in the shape `ReaderHistoryRow` is built from."""

    row = event(case_key)
    record = verdict_row(case_key)
    at_ms = settled_at_ms(case_key)
    assert at_ms is not None, f"{case_key} was never delivered"
    return {
        "event_id": str(record["event_id"]),
        "at_ms": at_ms,
        "storyline_key": storyline_key(case_key),
        "comparison_title": str(row["comparison_title"] or ""),
        "comparison_fingerprint": str(row["comparison_fingerprint"] or ""),
        "dedupe_family": str(row["dedupe_family"] or "general"),
        "grounded_assets": list(row["grounded_assets"] or ()),
        "assets": [
            {"symbol": asset["symbol"], "market_type": asset.get("market_type")}
            for asset in record["verdict"]["assets"]
        ],
        "canonical_assets": sorted(
            {str(asset["symbol"]) for asset in case(case_key)["event_assets"] if asset.get("symbol")}
        ),
        "magnitude": int(record["verdict"]["magnitude"]),
        "direction": str(record["verdict"]["direction"]),
        "headline_zh": str(record["verdict"]["headline_zh"]),
        "why_zh": str(record["verdict"]["why_zh"] or ""),
    }


def seen_rows(delivered_case_keys: Sequence[str], *, case_key: str) -> list[dict[str, Any]]:
    """The 4 h received-card ledger `decide()` measures against, built the way the worker builds it.

    `_recent_seen` in the worker is `[row.as_told_row() for row in history.recent_seen_rows]` over the
    snapshot PostgreSQL returned. Here the same pure assembler is given the sequence's own delivered cards,
    so the rows carry the fields the duplicate and template rules read rather than the told projection's.
    """

    current = event(case_key)
    snapshot = build_reader_history(
        [history_row(key) for key in delivered_case_keys],
        now_ms=triage_stamp(case_key),
        dedupe_family=str(current["dedupe_family"] or "general"),
        comparison_fingerprint=str(current["comparison_fingerprint"] or ""),
        canonical_assets=[str(value) for value in current["grounded_assets"] or ()],
        comparison_title=str(current["comparison_title"] or ""),
        include_targeted=False,
    )
    return [row.as_told_row() for row in snapshot.recent_seen_rows]


__all__ = [
    "RAW_CASES_PATH",
    "SEQUENCES_PATH",
    "case",
    "event",
    "frozen_policy",
    "gate_facts",
    "history_row",
    "judgment",
    "raw_cases",
    "seen_rows",
    "sequence",
    "sequences",
    "settled_at_ms",
    "storyline_key",
    "told",
    "triage_stamp",
    "verdict_row",
]
