from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from tracefold.app.model_generation_coordinator import select_model_frontier

NEW_YORK = ZoneInfo("America/New_York")
SESSION_DATE = date(2026, 7, 30)


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(
        datetime(
            SESSION_DATE.year,
            SESSION_DATE.month,
            SESSION_DATE.day,
            hour,
            minute,
            second,
            tzinfo=NEW_YORK,
        ).timestamp()
        * 1000
    )


def _row(kind: str, key: str, *, deadline_ms: int) -> dict[str, object]:
    return {
        "candidate_kind": kind,
        "shard_key": key,
        "status": "dirty",
        "deadline_at_ms": deadline_ms,
        "next_attempt_at_ms": None,
        "claimed_until_ms": None,
    }


def test_model_capacity_is_reserved_at_0845_and_thesis_wins_at_0850():
    thesis = _row(
        "macro_thesis",
        SESSION_DATE.isoformat(),
        deadline_ms=_ms(9, 0),
    )
    news = _row("news_brief", "current", deadline_ms=_ms(8, 30))

    selected, reason = select_model_frontier(
        [news, thesis],
        now_ms=_ms(8, 45),
    )
    assert selected is None
    assert reason == "macro_thesis_capacity_reserved"

    selected, reason = select_model_frontier(
        [news, thesis],
        now_ms=_ms(8, 50),
    )
    assert selected == thesis
    assert reason == "macro_thesis_wins"


def test_model_candidate_cannot_start_if_its_budget_overlaps_0845_reservation():
    thesis = _row(
        "macro_thesis",
        SESSION_DATE.isoformat(),
        deadline_ms=_ms(9, 0),
    )
    document = _row(
        "macro_document_analysis",
        "ready",
        deadline_ms=_ms(8, 0),
    )

    selected, reason = select_model_frontier(
        [document, thesis],
        now_ms=_ms(8, 43),
    )
    assert selected is None
    assert reason == "macro_thesis_capacity_reserved"


def test_model_edf_runs_other_due_candidate_when_thesis_is_terminal():
    news = _row("news_brief", "current", deadline_ms=_ms(8, 30))
    document = _row(
        "macro_document_analysis",
        "ready",
        deadline_ms=_ms(8, 40),
    )

    selected, reason = select_model_frontier(
        [document, news],
        now_ms=_ms(8, 50),
    )
    assert selected == news
    assert reason == "edf"
