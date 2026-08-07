from __future__ import annotations

from tracefold.news.presentation import normalize_news_display_text, normalize_news_display_title


def test_news_display_text_normalizes_provider_markup_and_entities() -> None:
    assert normalize_news_display_text("Reuters<br/>Bitcoin &amp; Ether <b>rise</b>") == "Reuters Bitcoin & Ether rise"


def test_news_display_text_is_deterministic_plain_text_without_mutating_source() -> None:
    source = "  Fed\u0000  says <script>alert('x')</script> rates unchanged  "

    first = normalize_news_display_text(source)

    assert first == "Fed says alert('x') rates unchanged"
    assert normalize_news_display_text(source) == first
    assert source == "  Fed\u0000  says <script>alert('x')</script> rates unchanged  "


def test_news_display_text_preserves_non_markup_angle_brackets() -> None:
    assert normalize_news_display_text("Inflation 5 < 10 and BTC > ETH") == "Inflation 5 < 10 and BTC > ETH"


def test_news_display_text_normalizes_unicode_and_whitespace() -> None:
    assert normalize_news_display_text("Cafe\u0301\n\tmarkets") == "Café markets"


def test_news_display_text_removes_bare_urls() -> None:
    assert normalize_news_display_text("Update https://example.com/a?b=1") == "Update"


def test_news_display_title_uses_domain_for_url_only_provider_title() -> None:
    assert normalize_news_display_title("https://Example.com/a?b=1") == "example.com"


def test_news_display_title_has_safe_nonempty_fallback() -> None:
    assert normalize_news_display_title("<br/>") == "未命名新闻"
