"""A frozen decision can settle without decoding an obsolete reader judgment."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tracefold.news.storage.notification_work import NOTIFICATION_WAIT_MS, NotificationWorkStorage


class Connection:
    def __init__(self, decision: str) -> None:
        self.work = {
            "state": "pending",
            "attempts": 2,
            "content_revision": "head",
            "decision_ref": "original-decision",
            "plan": {
                "reader_revision": "original-reader",
                "claim_decisions": [
                    {
                        "decision": decision,
                        "reader": {
                            "judgment": {
                                "importance": {"value": 2.8, "probabilities": [0, 0, 0.2, 0.8, 0]},
                            }
                        },
                    }
                ],
            },
        }
        self.writes: list[tuple[Any, ...]] = []
        self.result: dict[str, Any] = {}

    def execute(self, sql: str, params: tuple[Any, ...]):
        if "SELECT attempts,state" in sql:
            self.result = self.work
        elif "SELECT detail->>'content_revision' AS revision" in sql:
            self.result = {"revision": "head"}
        else:
            self.writes.append(params)
        return self

    def fetchone(self):
        return self.result


@pytest.mark.parametrize(
    "decision,error,state",
    [
        ("notify", None, "done"),
        ("deferred", None, "pending"),
        ("notify", "transport-failed", "failed"),
        ("deferred", "transport-failed", "failed"),
    ],
)
def test_old_frozen_decision_completes_waits_or_fails_from_delivery_metadata(
    decision: str,
    error: str | None,
    state: str,
) -> None:
    conn = Connection(decision)
    storage = NotificationWorkStorage(conn, context=None)
    original = json.dumps(conn.work["plan"])
    storage.intent_ended("event", "head", "original-decision", now_ms=100, error_code=error)
    assert json.dumps(conn.work["plan"]) == original
    params = conn.writes[0]
    assert params[0] == state
    assert json.loads(params[1]) == {
        "content_revision": "head",
        "reader_revision": "original-reader",
        "decision_ref": "original-decision",
    }
    assert params[2] == (0 if state == "done" else 2)
    assert params[5] == (100 + NOTIFICATION_WAIT_MS if state == "pending" else 100)


def test_an_old_intent_wakes_current_work_without_completing_its_newer_decision() -> None:
    conn = Connection("notify")
    storage = NotificationWorkStorage(conn, context=None)
    storage.intent_ended("event", "older-head", "older-decision", now_ms=100)
    assert conn.writes == [(100, "event", "news", 100)]
