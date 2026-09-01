"""The one measurement policy v5 rests on (#81): how much of this card the reader has already read."""

from __future__ import annotations

from tracefold.news.similarity import (
    character_bigrams,
    max_similarity,
    similarity,
    trigram_similarity,
    word_trigrams,
)


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


def test_word_trigrams_are_pg_trgm_show_trgm() -> None:
    """Vectors taken from `SELECT show_trgm(...)` on pg_trgm 1.6, PostgreSQL 18, en_US.utf8. Multibyte trigrams
    are hashed in pg_trgm's output, so the ASCII subset is compared exactly and the total is compared by count."""

    assert word_trigrams("cat") == {"  c", " ca", "cat", "at "}
    assert word_trigrams("a") == {"  a", " a "}
    assert word_trigrams("") == frozenset()
    assert word_trigrams(" -- ") == frozenset()

    trigrams = word_trigrams("据axios 特朗普正在考虑 num_22 Trump")
    assert len(trigrams) == 28
    assert sorted(t for t in trigrams if t.isascii()) == [
        "  2", "  n", "  t", " 22", " nu", " tr", "22 ", "axi", "ios", "mp ", "num", "os ", "rum", "tru",
        "um ", "ump", "xio",
    ]  # fmt: skip
    # `_` is not alphanumeric, so "num_22" is two words; "Trump" is lower-cased before padding.
    assert len(word_trigrams("特朗普正在考虑")) == 8
    assert len(word_trigrams("据axios")) == 7


def test_trigram_similarity_matches_pg_trgm_similarity() -> None:
    """`SELECT similarity(a, b)` on the same server for the same pairs."""

    assert (
        trigram_similarity(
            "fact sheet president donald j trump announces historic oil agreement",
            "this deal secures stable low cost oil for americans",
        )
        == 0.125
    )
    assert abs(trigram_similarity("特朗普正在考虑对伊朗进行有限打击", "特朗普考虑对伊朗有限打击") - 0.42857143) < 1e-6
    assert trigram_similarity("据axios 特朗普", "特朗普 据axios") == 1.0
    assert trigram_similarity("", "anything") == 0.0
    assert trigram_similarity("Trump", "trump") == 1.0


def test_word_trigrams_separate_a_shared_template_from_a_shared_fact_better_than_bigrams() -> None:
    """The told selector ranks on `comparison_title`, English 87% of the time. On 22k random English title pairs
    from the 2026-09-01 ledger, character bigrams put 4.6% above 0.25 and word trigrams 0.10%; the labelled
    duplicate pairs keep a median of 0.19-0.27 on either. These two pairs are the shape of that difference."""

    outlet_a = "trump says us is refilling the strategic petroleum reserve"
    outlet_b = "trump wants to fill up the strategic petroleum reserve"
    unrelated = "the treasury will sell num_50000000000 of num_10 year notes on thursday"
    assert trigram_similarity(outlet_a, outlet_b) > 0.25 > trigram_similarity(outlet_a, unrelated)
    assert similarity(outlet_a, unrelated) > trigram_similarity(outlet_a, unrelated)
