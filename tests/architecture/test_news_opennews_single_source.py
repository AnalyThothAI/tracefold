from __future__ import annotations

import inspect
from pathlib import Path

from tracefold import news
from tracefold.news import NewsAcquisition, NewsSourceDefinition

ROOT = Path(__file__).resolve().parents[2]


def test_retired_rss_surface_is_absent() -> None:
    assert not (ROOT / "src/tracefold/integrations/news_feeds").exists()
    assert "feedparser" not in (ROOT / "pyproject.toml").read_text()
    assert 'name = "feedparser"' not in (ROOT / "uv.lock").read_text()
    assert not {
        "NewsFeedExpectedError",
        "NewsFeedFetch",
        "NewsFeedReader",
        "default_sources",
    } & set(news.__all__)


def test_news_source_and_acquisition_are_opennews_only() -> None:
    assert set(NewsSourceDefinition.model_fields) == {
        "source_id",
        "name",
        "tier",
        "lang",
        "source_kind",
        "enabled",
    }
    parameters = inspect.signature(NewsAcquisition).parameters
    assert "opennews_source" in parameters
    assert "sources" not in parameters
