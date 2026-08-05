from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from tracefold.macro.dependencies import (
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.projection import MacroModuleClaim, MacroProjectionService
from tracefold.news import story_store
from tracefold.news.projection import NewsProjectionSnapshot
from tracefold.news.repository import NewsRepository


def test_macro_publish_rejects_lost_claim_before_serving_write() -> None:
    calls: list[str] = []

    class _Macro:
        @staticmethod
        def dataset_projection_states(*, dataset_ids: tuple[str, ...]) -> list[dict[str, object]]:
            del dataset_ids
            return []

        @staticmethod
        def upsert_module_current(**_kwargs: Any) -> int:
            calls.append("write")
            return 1

    class _Frontiers:
        @staticmethod
        def complete(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("cas")
            return False

    repos = SimpleNamespace(
        macro=_Macro(),
        projection_frontiers=_Frontiers(),
        transaction=nullcontext,
    )

    class _Database:
        @staticmethod
        def worker_session(*_args: Any, **_kwargs: Any) -> Any:
            return nullcontext(repos)

    service = MacroProjectionService(db=_Database())
    claim = MacroModuleClaim(
        module_id="rates_fed",
        runtime_id=str(uuid4()),
        input_fingerprint=module_input_fingerprint("rates_fed", []),
        projection_version=module_projection_version("rates_fed"),
        deadline_at_ms=1,
    )

    result = service.publish_module(
        claim,
        {"module_id": "rates_fed", "module_payload": {}},
        now_ms=2,
    )

    assert result == {
        "projection_status": "stale_snapshot",
        "module_id": "rates_fed",
        "rows_written": 0,
    }
    assert calls == ["cas"]


def test_news_story_owner_invariant_uses_only_published_snapshot(monkeypatch: Any) -> None:
    conn = _NewsConnection()
    repository = NewsRepository(conn)
    monkeypatch.setattr(repository, "refresh_brief_selection", lambda *, now_ms: 0)
    monkeypatch.setattr(
        story_store,
        "load_story_projection",
        lambda _repository, *, now_ms: {
            "input_fingerprint": "snapshot-fingerprint",
            "rows": [],
        },
    )
    for helper in (
        "_publish_items",
        "_upsert_stories",
        "_replace_memberships",
        "_delete_absent_stories",
        "_replace_facets",
    ):
        monkeypatch.setattr(story_store, helper, lambda *_args, **_kwargs: 0)

    snapshot = NewsProjectionSnapshot(
        input_fingerprint="snapshot-fingerprint",
        cutoff_ms=0,
        scoring_epoch_ms=0,
        current_input_fingerprint=None,
        rows=(
            {"item_id": "snapshot-b", "published_at_ms": 2},
            {"item_id": "snapshot-a", "published_at_ms": 1},
        ),
    )
    result = story_store.publish_story_projection(
        repository,
        snapshot=snapshot,
        projection={"stories": [], "memberships": [], "item_updates": []},
        now_ms=3,
    )

    assert result["projection_status"] == "rebuilt"
    assert conn.invariant_params == {
        "item_ids": ["snapshot-a", "snapshot-b"],
    }


def test_news_story_load_captures_the_publish_fence_before_moving_facts() -> None:
    calls: list[str] = []

    class _LoadCursor:
        def __init__(
            self,
            *,
            row: dict[str, Any] | None = None,
            rows: list[dict[str, Any]] | None = None,
        ) -> None:
            self._row = row
            self._rows = rows or []

        def fetchone(self) -> dict[str, Any] | None:
            return self._row

        def fetchall(self) -> list[dict[str, Any]]:
            return self._rows

    class _LoadConnection:
        @staticmethod
        def execute(sql: str, _params: object = None) -> _LoadCursor:
            if "SELECT input_fingerprint FROM news_projection_summary" in sql:
                calls.append("summary")
                return _LoadCursor(row={"input_fingerprint": "published-fingerprint"})
            if "SELECT count(*) AS item_count" in sql:
                calls.append("bounds")
                return _LoadCursor(row={"item_count": 1, "minimum_input_bytes": 100})
            if "SELECT item.item_id" in sql:
                calls.append("facts")
                return _LoadCursor(
                    rows=[
                        {
                            "item_id": "item-1",
                            "source_id": "news-opennews",
                            "canonical_url": "https://example.test/item-1",
                            "reporting_origin": "Reuters",
                            "title": "Central bank holds rates steady",
                            "description": "Policy makers held rates steady.",
                            "published_at_ms": 1,
                            "content_fingerprint": "content-fingerprint",
                            "tier": 1,
                        }
                    ]
                )
            raise AssertionError(sql)

    snapshot = story_store.load_story_projection(
        NewsRepository(_LoadConnection()),
        now_ms=2_000_000_000_000,
    )

    assert calls == ["summary", "bounds", "facts"]
    assert snapshot["current_input_fingerprint"] == "published-fingerprint"


def test_news_story_publish_does_not_require_a_quiet_ingest_window(monkeypatch: Any) -> None:
    conn = _NewsConnection(summary_fingerprint="previous-fingerprint")
    repository = NewsRepository(conn)
    monkeypatch.setattr(repository, "refresh_brief_selection", lambda *, now_ms: 0)

    def moving_window_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("publication must not re-read the moving input window")

    monkeypatch.setattr(story_store, "load_story_projection", moving_window_read)
    for helper in (
        "_publish_items",
        "_upsert_stories",
        "_replace_memberships",
        "_delete_absent_stories",
        "_replace_facets",
    ):
        monkeypatch.setattr(story_store, helper, lambda *_args, **_kwargs: 0)

    snapshot = NewsProjectionSnapshot(
        input_fingerprint="snapshot-fingerprint",
        cutoff_ms=0,
        scoring_epoch_ms=0,
        current_input_fingerprint="previous-fingerprint",
        rows=({"item_id": "snapshot", "published_at_ms": 1},),
    )

    result = story_store.publish_story_projection(
        repository,
        snapshot=snapshot,
        projection={"stories": [], "memberships": [], "item_updates": []},
        now_ms=3,
    )

    assert result["projection_status"] == "rebuilt"


def test_news_story_publish_rejects_a_superseded_snapshot(monkeypatch: Any) -> None:
    conn = _NewsConnection(summary_fingerprint="newer-fingerprint")
    repository = NewsRepository(conn)

    def unexpected_write(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("a superseded snapshot must not write")

    for helper in (
        "_publish_items",
        "_upsert_stories",
        "_replace_memberships",
        "_delete_absent_stories",
        "_replace_facets",
    ):
        monkeypatch.setattr(story_store, helper, unexpected_write)

    snapshot = NewsProjectionSnapshot(
        input_fingerprint="snapshot-fingerprint",
        cutoff_ms=0,
        scoring_epoch_ms=0,
        current_input_fingerprint="previous-fingerprint",
        rows=({"item_id": "snapshot", "published_at_ms": 1},),
    )

    result = story_store.publish_story_projection(
        repository,
        snapshot=snapshot,
        projection={"stories": [], "memberships": [], "item_updates": []},
        now_ms=3,
    )

    assert result == {
        "projection_status": "superseded_snapshot",
        "items": 1,
        "stories": 0,
        "rows_written": 0,
    }


def test_news_story_invariant_failure_is_not_hidden_as_a_moving_snapshot(monkeypatch: Any) -> None:
    conn = _NewsConnection(invariant_total=1)
    repository = NewsRepository(conn)
    monkeypatch.setattr(repository, "refresh_brief_selection", lambda *, now_ms: 0)
    for helper in (
        "_publish_items",
        "_upsert_stories",
        "_replace_memberships",
        "_delete_absent_stories",
        "_replace_facets",
    ):
        monkeypatch.setattr(story_store, helper, lambda *_args, **_kwargs: 0)
    snapshot = NewsProjectionSnapshot(
        input_fingerprint="snapshot-fingerprint",
        cutoff_ms=0,
        scoring_epoch_ms=0,
        current_input_fingerprint=None,
        rows=({"item_id": "snapshot", "published_at_ms": 1},),
    )

    with pytest.raises(RuntimeError, match="news_story_invariant_failed"):
        story_store.publish_story_projection(
            repository,
            snapshot=snapshot,
            projection={"stories": [], "memberships": [], "item_updates": []},
            now_ms=3,
        )


class _Cursor:
    def __init__(self, row: dict[str, Any] | None = None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _NewsConnection:
    def __init__(
        self,
        *,
        invariant_total: int = 0,
        summary_fingerprint: str | None = None,
    ) -> None:
        self.invariant_params: object = None
        self.invariant_total = int(invariant_total)
        self.summary_fingerprint = summary_fingerprint

    def execute(self, sql: str, params: object = None) -> _Cursor:
        if "WITH current_owners AS" in sql:
            self.invariant_params = params
            return _Cursor(
                {
                    "invalid_owner_count": self.invariant_total,
                    "invalid_story_aggregate_count": 0,
                }
            )
        if "SELECT input_fingerprint FROM news_projection_summary" in sql:
            if self.summary_fingerprint is None:
                return _Cursor()
            return _Cursor({"input_fingerprint": self.summary_fingerprint})
        if "UPDATE news_projection_summary" in sql:
            return _Cursor(rowcount=1)
        return _Cursor()
