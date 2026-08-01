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
from tracefold.news.projection import NewsProjectionService, NewsProjectionSnapshot
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


def test_news_story_changed_snapshot_rolls_back_invariant_failure(monkeypatch: Any) -> None:
    conn = _NewsConnection(invariant_total=1)
    repository = NewsRepository(conn)
    monkeypatch.setattr(repository, "refresh_brief_selection", lambda *, now_ms: 0)
    inputs = iter(
        (
            {"input_fingerprint": "snapshot-fingerprint", "rows": []},
            {"input_fingerprint": "new-fingerprint", "rows": [{"item_id": "new"}]},
        )
    )
    monkeypatch.setattr(story_store, "load_story_projection", lambda _repository, *, now_ms: next(inputs))
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

    with pytest.raises(story_store._StorySnapshotLost) as lost:
        story_store.publish_story_projection(
            repository,
            snapshot=snapshot,
            projection={"stories": [], "memberships": [], "item_updates": []},
            now_ms=3,
        )

    assert lost.value.items == 1


def test_news_projection_service_maps_snapshot_loss_to_retry() -> None:
    class _News:
        @staticmethod
        def publish_story_projection(**_kwargs: Any) -> dict[str, Any]:
            raise story_store._StorySnapshotLost(items=4)

    repos = SimpleNamespace(news=_News(), transaction=nullcontext)

    class _Database:
        @staticmethod
        def worker_session(*_args: Any, **_kwargs: Any) -> Any:
            return nullcontext(repos)

    service = NewsProjectionService(db=_Database())
    snapshot = NewsProjectionSnapshot(
        input_fingerprint="snapshot-fingerprint",
        cutoff_ms=0,
        scoring_epoch_ms=0,
        current_input_fingerprint=None,
        rows=(),
    )

    assert service.publish(snapshot, {}, now_ms=3) == {
        "projection_status": "stale_snapshot",
        "items": 4,
        "stories": 0,
        "rows_written": 0,
    }


class _Cursor:
    def __init__(self, row: dict[str, Any] | None = None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

class _NewsConnection:
    def __init__(self, *, invariant_total: int = 0) -> None:
        self.invariant_params: object = None
        self.invariant_total = int(invariant_total)

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
            return _Cursor()
        if "UPDATE news_projection_summary" in sql:
            return _Cursor(rowcount=1)
        return _Cursor()
