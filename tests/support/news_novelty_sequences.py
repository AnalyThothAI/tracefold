"""Frozen #651 production sequences, read-only.

`issue_651_raw_cases.json` is a read-only production export: the Event, its judged verdict, its editorial
envelope, its told ledger and its deliveries exactly as PostgreSQL held them. `issue_651_novelty_sequences.json`
orders a few of those cases into the chains the reader actually received. The accessors below hand those
clocks, titles and symbols to tests that rebuild the chain against PostgreSQL.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

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


def triage_stamp(case_key: str) -> int:
    """When the legacy verdict of this card was written."""

    return int(verdict_row(case_key)["created_at_ms"])
