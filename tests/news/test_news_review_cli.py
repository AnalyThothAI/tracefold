from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.cli.commands import news_review
from tracefold.app.cli.parser import build_parser


def test_review_submit_requires_and_uses_the_named_reviewer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["news", "review", "submit", "evt.1.1.pin", "--version", "1" * 64, "--file", "review.json"])

    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps({"kind": "event_rubric"}), encoding="utf-8")
    captured: dict[str, Any] = {}

    class _Submission:
        @classmethod
        def model_validate(cls, _payload: Any) -> object:
            return object()

    class _Desk:
        def __init__(self, _conn: Any) -> None:
            pass

        def submit(self, _task: Any, _submission: Any, *, principal: Any, idempotency_key: str) -> dict[str, Any]:
            captured["reviewer"] = principal.subject
            captured["idempotency_key"] = idempotency_key
            return {"receipt": {"review_id": "review-1"}}

    @contextmanager
    def fake_postgres_connection(_settings: Any):
        yield object()

    @contextmanager
    def fake_transaction(_conn: Any):
        yield

    monkeypatch.setattr(news_review, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.platform.postgres.client.transaction", fake_transaction)
    monkeypatch.setattr("tracefold.news.review.desk.EventRubricSubmission", _Submission)
    monkeypatch.setattr("tracefold.news.review.desk.ReviewDesk", _Desk)

    args = parser.parse_args(
        [
            "news",
            "review",
            "submit",
            "evt.1.1.pin",
            "--version",
            "1" * 64,
            "--file",
            str(review_file),
            "--reviewer",
            "reviewer-alice",
        ]
    )
    code, payload = news_review._handle_review(args)

    assert code == 0 and payload["data"]["receipt"]["review_id"] == "review-1"
    assert captured["reviewer"] == "reviewer-alice"
    assert captured["idempotency_key"]


def test_the_review_group_has_no_draft_pairwise_or_proposal_surface() -> None:
    parser = build_parser()

    for retired in (
        ["news", "review", "accept-drafts", "--file", "batch.json"],
        ["news", "review", "audit-report", "--file", "batch.json"],
        ["news", "review", "queue", "--mode", "pairwise"],
        ["news", "review", "queue", "--view", "proposals"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(retired)
    args = parser.parse_args(["news", "review", "queue", "--view", "coverage"])
    assert (args.view, args.status, args.hours) == ("coverage", "pending", 24)
