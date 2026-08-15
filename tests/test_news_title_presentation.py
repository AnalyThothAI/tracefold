from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.news.title_presentation import (
    NewsItemTitlePresentation,
    TitleTranslationError,
)
from tracefold.platform.resource import ResourceAdmissionTimeout

_ITEM_ID = "news_item_0123456789abcdef0123456789abcdef"
_FINGERPRINT = "a" * 64


class _Provider:
    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.titles: list[str] = []

    async def translate(self, title: str) -> str:
        self.titles.append(title)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        return None


class _Database:
    def __init__(
        self,
        *,
        fail_settlement: bool = False,
        original_title: str = "Bitcoin rises",
    ) -> None:
        self.fail_settlement = fail_settlement
        self.original_title = original_title
        self.resolution: tuple[Any, ...] | None = None

    async def run_business(
        self,
        operation_name: str,
        _function: Any,
        /,
        *args: Any,
        **_kwargs: Any,
    ) -> object:
        if operation_name == "news_title_presentation_peek":
            return {
                "item_id": _ITEM_ID,
                "source_title_fingerprint": _FINGERPRINT,
                "original_title": self.original_title,
            }
        if operation_name == "news_title_presentation_fence":
            return True
        if operation_name == "news_title_presentation_resolve":
            if self.fail_settlement:
                raise ResourceAdmissionTimeout("settlement_unavailable")
            self.resolution = args
            return True
        raise AssertionError(operation_name)


def test_deepl_failure_falls_through_to_deepseek_for_same_item() -> None:
    deepl = _Provider(TitleTranslationError("news_title_presentation_deepl_key_rejected"))
    deepseek = _Provider("比特币上涨")
    database = _Database()
    presentation = NewsItemTitlePresentation(
        db=database,
        deepl=deepl,
        deepseek=deepseek,
        clock_ms=lambda: 100,
    )

    assert asyncio.run(presentation.turn()) is True

    assert deepl.titles == ["Bitcoin rises"]
    assert deepseek.titles == ["Bitcoin rises"]
    assert database.resolution is not None
    assert database.resolution[2:7] == (
        "resolving",
        "比特币上涨",
        "translated",
        "deepseek",
        None,
    )


def test_invalid_deepl_output_falls_through_to_deepseek() -> None:
    deepl = _Provider("still English")
    deepseek = _Provider("比特币上涨")
    database = _Database()
    presentation = NewsItemTitlePresentation(
        db=database,
        deepl=deepl,
        deepseek=deepseek,
        clock_ms=lambda: 100,
    )

    assert asyncio.run(presentation.turn()) is True
    assert deepseek.titles == ["Bitcoin rises"]
    assert database.resolution is not None
    assert database.resolution[5] == "deepseek"


def test_minority_han_text_does_not_bypass_translation() -> None:
    deepl = _Provider("中文标题")
    database = _Database(original_title="中AB")
    presentation = NewsItemTitlePresentation(
        db=database,
        deepl=deepl,
        deepseek=None,
        clock_ms=lambda: 100,
    )

    assert asyncio.run(presentation.turn()) is True
    assert deepl.titles == ["中AB"]
    assert database.resolution is not None
    assert database.resolution[5] == "deepl"


def test_external_success_followed_by_settlement_failure_is_process_fatal() -> None:
    deepl = _Provider("比特币上涨")
    presentation = NewsItemTitlePresentation(
        db=_Database(fail_settlement=True),
        deepl=deepl,
        deepseek=None,
        clock_ms=lambda: 100,
    )

    with pytest.raises(
        RuntimeError,
        match="news_title_presentation_settlement_unavailable",
    ):
        asyncio.run(presentation.turn())

    assert deepl.titles == ["Bitcoin rises"]
