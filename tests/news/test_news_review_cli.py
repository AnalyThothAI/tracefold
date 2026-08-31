from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from tracefold.app.cli.commands import news_review
from tracefold.news.review.drafter import DRAFT_SCHEMA


def _args(*, dry_run: bool) -> Namespace:
    return Namespace(
        file="drafts.json",
        min_confidence=0.0,
        only="",
        exclude="",
        reviewer="operator",
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
