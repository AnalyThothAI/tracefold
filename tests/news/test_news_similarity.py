"""The one measurement policy v5 rests on (#81): how much of this card the reader has already read."""

from __future__ import annotations

from tracefold.news.similarity import character_bigrams, max_similarity, similarity


def test_identical_and_unrelated_headlines_sit_at_the_ends() -> None:
    assert similarity("怀俄明州稳定币迁移至 CCIP", "怀俄明州稳定币迁移至 CCIP") == 1.0
    assert similarity("俄军向基辅发射导弹", "美联储会议纪要公布") == 0.0


def test_whitespace_and_short_text_never_score() -> None:
    assert similarity("比 特 币", "比特币") == 1.0
    assert character_bigrams(" 币 ") == frozenset()
    assert similarity("币", "币") == 0.0  # nothing to bigram: not evidence of a repeat
    assert similarity("", "任何标题") == 0.0


def test_a_paraphrase_of_the_same_fact_scores_above_the_shipped_threshold() -> None:
    """The pair from issue #81's first example: one Reuters wire and one 金十 line, five minutes apart, both
    delivered under policy v4. It is the case the shipped 0.25 threshold has to catch."""

    score = similarity(
        "和记黄埔就巴拿马运河港口争端于8月20日启动国际仲裁",
        "长江和记就巴拿马港口启动国际仲裁，索偿超 15 亿美元",
    )
    assert 0.25 <= score < 0.30


def test_two_different_contracts_in_one_template_stay_below_it() -> None:
    """The opposite failure: an exchange's delisting notices share almost all their wording and are not repeats.
    They must stay releasable, which is why the threshold is 0.25 and not 0.6."""

    assert similarity("Bybit 将下架 HFTUSDT 永续合约", "Bybit 将下架 VINEUSDT 永续合约") < 0.62


def test_max_similarity_names_which_card_it_matched() -> None:
    ledger = ["美联储纪要显示官员分歧", "怀俄明州稳定币转用 Chainlink CCIP", "油价连涨四日"]
    score, index = max_similarity("怀俄明州稳定币迁移至 Chainlink CCIP", ledger)
    assert index == 1 and score > 0.6
    assert max_similarity("完全无关的一条新闻", []) == (0.0, -1)
    assert max_similarity("", ledger) == (0.0, -1)
