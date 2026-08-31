from __future__ import annotations

import json
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.cli.commands import news_review
from tracefold.app.cli.parser import build_parser
from tracefold.news.review.drafter import DRAFT_SCHEMA


def _args(*, dry_run: bool, only: str = "", reviewer: str = "operator") -> Namespace:
    return Namespace(
        file="drafts.json",
        min_confidence=0.0,
        only=only,
        exclude="",
        reviewer=reviewer,
        dry_run=dry_run,
    )


def test_accept_drafts_requires_an_explicit_nonempty_only_before_any_database_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_calls: list[tuple[Any, Any]] = []

    def database(*args: Any, **kwargs: Any) -> None:
        database_calls.append((args, kwargs))
        raise AssertionError("database access is forbidden")

    monkeypatch.setattr(
        news_review,
        "_read_json_or_yaml",
        lambda _path: {"schema_id": DRAFT_SCHEMA, "drafter": {}, "drafts": [], "batch_sha256": "a" * 64},
    )
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", database)

    with pytest.raises(ValueError, match="news_review_accept_drafts_only_required"):
        news_review._handle_review_accept_drafts(_args(dry_run=False), object(), object())

    code, preview = news_review._handle_review_accept_drafts(_args(dry_run=True), object(), object())
    assert code == 0 and preview["data"]["dry_run"] is True
    assert database_calls == []


def test_accept_drafts_requires_the_actual_reviewer_identity_before_any_database_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_calls: list[tuple[Any, Any]] = []

    def database(*args: Any, **kwargs: Any) -> None:
        database_calls.append((args, kwargs))
        raise AssertionError("database access is forbidden")

    monkeypatch.setattr(
        news_review,
        "_read_json_or_yaml",
        lambda _path: {"schema_id": DRAFT_SCHEMA, "drafter": {}, "drafts": [], "batch_sha256": "a" * 64},
    )
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", database)

    with pytest.raises(ValueError, match="news_review_accept_drafts_reviewer_required"):
        news_review._handle_review_accept_drafts(
            _args(dry_run=False, only="event-prefix", reviewer=""), object(), object()
        )

    assert database_calls == []


def test_accept_drafts_records_the_model_that_actually_authored_the_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    draft = {
        "should_push": "should_hold",
        "dimensions": {
            "factual_fidelity": "pass",
            "taxonomy_subject_codes": "pass",
            "taxonomy_event_family": "pass",
            "taxonomy_change_state": "pass",
            "taxonomy_source_authority": "pass",
            "taxonomy_assertion_status": "pass",
        },
        "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        "taxonomy": {
            "subject_codes": [],
            "event_family": "other",
            "change_state": "unknown",
            "assertion_status": "unknown",
        },
        "confidence": 0.8,
    }
    draft_file = tmp_path / "drafts.json"
    draft_file.write_text(
        json.dumps(
            {
                "schema_id": DRAFT_SCHEMA,
                "drafter": {
                    "drafter_id": "tracefold.news.review_drafter_v6",
                    "model": "openai/qwen3.8-27b:thinking",
                },
                "drafts": [
                    {
                        "task_id": "evt.1",
                        "task_version": "1" * 64,
                        "event_id": "2" * 64,
                        "source_authority": "unknown",
                        "draft": draft,
                    }
                ],
                "batch_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    persisted: dict[str, str] = {}

    class _Connection:
        pass

    class _Desk:
        def __init__(self, _conn: Any) -> None:
            pass

        def submit(
            self,
            _task: Any,
            submission: Any,
            *,
            principal: Any,
            idempotency_key: str,
        ) -> dict[str, Any]:
            assert idempotency_key
            persisted["draft_author"] = submission.taxonomy_review.draft_author
            persisted["reviewer"] = principal.subject
            return {"review_id": "review-1"}

    @contextmanager
    def fake_postgres_connection(_settings: Any, **_kwargs: Any):
        yield _Connection()

    @contextmanager
    def fake_transaction(_conn: Any):
        yield

    monkeypatch.setattr(news_review, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.review.desk.ReviewDesk", _Desk)
    monkeypatch.setattr("tracefold.platform.postgres.client.transaction", fake_transaction)

    args = build_parser().parse_args(
        [
            "news",
            "review",
            "accept-drafts",
            "--file",
            str(draft_file),
            "--only",
            "evt.1",
            "--reviewer",
            "owner_authorized_codex",
        ]
    )
    code, result = news_review._handle_review(args)

    assert code == 0 and result["data"]["submitted"] == 1
    assert persisted == {
        "draft_author": "tracefold.news.review_drafter_v6@openai/qwen3.8-27b:thinking",
        "reviewer": "owner_authorized_codex",
    }
