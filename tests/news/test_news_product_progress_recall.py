"""Fixed product-recall cases for Issue #173.

Every case runs through the public semantic/Policy seam: a typed
``ScoredJudgment`` a contract-following EventSemantics would emit, then the
unchanged policy-v10 ``decide()``. The suite therefore pins the *contract*, not
one model's wording — a Program that reads the RulePacks correctly passes it,
and a candidate that quietly re-learns "product is background" fails it.

Policy v10 is deliberately untouched by #173. Product recall arrives by making
the typed semantics accurate enough to satisfy the eligibility predicate that
already existed, never by adding a second action authority.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.models import TriageAsset, TriageVerdict
from tracefold.news.semantic_contract import (
    TRADE_CHANNEL_ORDER,
    ScoredJudgment,
    TradeChannel,
    TradeRelevanceV1,
)
from tracefold.news.triage_rules import DecisionResult, GateFacts, decide, realtime_eligible

_CANDIDATE = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="candidate",
)
# A grounded tag would fire `watchlist_objective_guard` and prove nothing about the semantics under test, so
# every case here is an ungrounded candidate: the only thing that can push it is the trade-relevance path.
_GROUNDED = GateFacts(
    grounded_assets=("HYPE",),
    watchlist_symbols=frozenset(),
    admission="candidate",
)

_PUSH = DecisionResult("push", "trade_relevance_realtime", None, "drop", ())
_DROP_BACKGROUND = DecisionResult("drop", "reader_value_background", None, "drop", ())
_DROP_NONE = DecisionResult("drop", "reader_value_none", None, "drop", ())


def _verdict(**overrides: Any) -> TriageVerdict:
    values: dict[str, Any] = {
        "novelty": "new_fact",
        "event_type": "product",
        "assets": [TriageAsset(role="primary", symbol="HYPE", market_type="crypto")],
        "direction": "neutral",
        "scope": "single_name",
        "magnitude": 2,
        "actionable": False,
        "confidence": 0.9,
        "decision": "drop",
        "audience": "crypto",
        "headline_zh": "固定产品召回案例",
        "title_zh": "",
        "why_zh": "",
    }
    values.update(overrides)
    return TriageVerdict.model_validate(values)


def _judgment(*, relevance: TradeRelevanceV1, **verdict_overrides: Any) -> ScoredJudgment:
    return scored_judgment(_verdict(**verdict_overrides), relevance=relevance)


# --- A. concrete product state change: must_push -------------------------------------------------------


def _state_change(**overrides: Any) -> TradeRelevanceV1:
    values: dict[str, Any] = {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "unscheduled",
        "development_delta": "state_change",
        "channels": ["product_progress"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    }
    values.update(overrides)
    return trade_relevance(**values)


MUST_PUSH: tuple[tuple[str, TradeRelevanceV1, dict[str, Any]], ...] = (
    (
        # 77ebe56d: shipped as noise/m0/none. A paid, irreversible step toward deploying one named market.
        "hyperliquid_eqmsft_ticker_auction",
        _state_change(tradability="second_order", channels=["exchange_access", "product_progress"]),
        {"magnitude": 2, "direction": "neutral"},
    ),
    (
        "exchange_opens_new_spot_market",
        _state_change(channels=["exchange_access", "product_progress"]),
        {"event_type": "listing", "direction": "bullish"},
    ),
    (
        "protocol_mainnet_capability_live",
        _state_change(),
        {"direction": "bullish"},
    ),
    (
        "issuer_changes_own_pricing",
        _state_change(surprise="material_vs_expectation", channels=["product_progress", "earnings_cashflow"]),
        {"direction": "bullish"},
    ),
    (
        "announced_product_cancelled_or_recalled",
        _state_change(),
        {"direction": "bearish"},
    ),
)


@pytest.mark.parametrize(("name", "relevance", "verdict"), MUST_PUSH, ids=[case[0] for case in MUST_PUSH])
def test_a_confirmed_product_state_change_reaches_the_reader(
    name: str, relevance: TradeRelevanceV1, verdict: dict[str, Any]
) -> None:
    assert decide(_judgment(relevance=relevance, **verdict), _CANDIDATE, None) == _PUSH


def test_a_product_state_change_pushes_on_an_ungrounded_event_with_no_watchlist_help() -> None:
    """The whole point of #173: no deterministic guard is available, so the semantics must carry it alone."""

    result = decide(_judgment(relevance=_state_change()), _CANDIDATE, None)
    assert result.final == "push"
    assert result.rule_baseline == "drop"
    assert result.watchlist_hits == ()


# --- B. high-quality adoption progress: should_push ----------------------------------------------------


def _adoption(**overrides: Any) -> TradeRelevanceV1:
    return _state_change(tradability="second_order", **overrides)


SHOULD_PUSH: tuple[tuple[str, dict[str, Any]], ...] = (
    # 97281ae7: shipped as product/m1/none with contextual/color_only.
    ("active_perp_traders_all_time_high", {"magnitude": 2, "direction": "neutral"}),
    ("official_paying_users_cross_threshold", {"magnitude": 2, "direction": "bullish"}),
)


@pytest.mark.parametrize(("name", "verdict"), SHOULD_PUSH, ids=[case[0] for case in SHOULD_PUSH])
def test_first_party_active_adoption_reaches_the_reader(name: str, verdict: dict[str, Any]) -> None:
    assert decide(_judgment(relevance=_adoption(), **verdict), _GROUNDED, None) == _PUSH


# --- C/D. routine milestone and no product fact: must_hold ---------------------------------------------


MUST_HOLD: tuple[tuple[str, TradeRelevanceV1, dict[str, Any], DecisionResult], ...] = (
    (
        # a8ffa0eb: a cumulative account total in a marketing post stays exactly where it is.
        "tron_400m_cumulative_accounts",
        trade_relevance(
            tradability="contextual",
            surprise="in_line",
            development_delta="color_only",
            channels=[],
            affected_markets=[],
            reader_value="background",
        ),
        {"magnitude": 1, "direction": "bullish"},
        _DROP_BACKGROUND,
    ),
    (
        # 138d2689: a prediction-market quote is not a product fact.
        "polymarket_starship_odds",
        trade_relevance(
            impact_breadth="none",
            tradability="none",
            surprise="unknown",
            development_delta="color_only",
            channels=[],
            affected_markets=[],
            reader_value="none",
        ),
        {"event_type": "rumor", "magnitude": 0},
        _DROP_NONE,
    ),
    (
        "roadmap_or_testnet_teaser",
        trade_relevance(
            tradability="contextual",
            surprise="unknown",
            development_delta="scheduled",
            channels=[],
            affected_markets=[],
            reader_value="none",
        ),
        {"magnitude": 1},
        _DROP_NONE,
    ),
    (
        "partnership_recap_without_shipped_capability",
        trade_relevance(
            tradability="contextual",
            surprise="in_line",
            development_delta="color_only",
            channels=[],
            affected_markets=[],
            reader_value="background",
        ),
        {"event_type": "partnership", "magnitude": 1},
        _DROP_BACKGROUND,
    ),
    (
        "marketing_competition_or_airdrop",
        trade_relevance(
            impact_breadth="none",
            tradability="none",
            surprise="unknown",
            development_delta="color_only",
            channels=[],
            affected_markets=[],
            reader_value="none",
        ),
        {"event_type": "noise", "magnitude": 0},
        _DROP_NONE,
    ),
)


@pytest.mark.parametrize(("name", "relevance", "verdict", "expected"), MUST_HOLD, ids=[case[0] for case in MUST_HOLD])
def test_a_milestone_or_non_product_fact_is_still_withheld(
    name: str, relevance: TradeRelevanceV1, verdict: dict[str, Any], expected: DecisionResult
) -> None:
    assert decide(_judgment(relevance=relevance, **verdict), _GROUNDED, None) == expected


def test_a_provider_common_word_tag_cannot_manufacture_product_progress() -> None:
    """`PERP`, `SPOT`, `ERA` and friends are ordinary English words the provider tags as A-grade coins.

    A tag is Gate evidence; it is not a product action, so it must never be what lifts an event into
    `product_progress`. The Gate facts below carry the tag and the semantics carry no product state change:
    the card still has to be withheld.
    """

    tagged = GateFacts(grounded_assets=("PERP",), watchlist_symbols=frozenset(), admission="candidate")
    relevance = trade_relevance(
        impact_breadth="none",
        tradability="none",
        surprise="unknown",
        development_delta="color_only",
        channels=[],
        affected_markets=[],
        reader_value="none",
    )
    assert decide(_judgment(relevance=relevance, magnitude=0, event_type="noise"), tagged, None) == _DROP_NONE


# --- stability ------------------------------------------------------------------------------------------


def test_two_paraphrases_of_one_product_fact_take_the_same_action() -> None:
    """The corpus had `ChatGPT launches Apple Messages integration` at m1/drop and m2/push 104 minutes apart."""

    first = _judgment(relevance=_state_change(), headline_zh="A 公司在 B 平台上线集成功能")
    second = _judgment(relevance=_state_change(), headline_zh="B 平台现已支持 A 公司的集成功能")
    left, right = decide(first, _CANDIDATE, None), decide(second, _CANDIDATE, None)
    assert left == right == _PUSH
    assert first.verdict.magnitude == second.verdict.magnitude
    assert first.editorial.relevance == second.editorial.relevance


def test_an_unknown_price_implication_does_not_withhold_a_product_state_change() -> None:
    """A ticker auction has no knowable direction. Under the old prior that alone demoted it to m1."""

    for direction in ("neutral", "unclear"):
        judgment = _judgment(relevance=_state_change(tradability="second_order"), direction=direction)
        assert decide(judgment, _CANDIDATE, None) == _PUSH


def test_a_product_progression_is_not_a_restatement_of_its_own_announcement() -> None:
    """`progression` carries `restates=-1`, so the restatement drop cannot reach a shipped follow-up."""

    judgment = _judgment(relevance=_state_change(), novelty="progression", restates=-1)
    assert decide(judgment, _CANDIDATE, None) == _PUSH


def test_the_same_launch_from_another_outlet_is_still_a_restatement() -> None:
    from tracefold.news.triage_rules import storyline_status

    status = storyline_status(
        "asset:HYPE",
        told=[{"dir": "bullish", "headline_zh": "某协议主网能力上线", "grounded_assets": ["HYPE"]}],
    )
    judgment = _judgment(relevance=_state_change(), direction="bullish", novelty="restatement", restates=0)
    assert decide(judgment, _CANDIDATE, status).final == "drop"
    assert decide(judgment, _CANDIDATE, status).override_rule == "restatement"


# --- contract -------------------------------------------------------------------------------------------


def test_product_progress_is_the_only_new_channel_and_the_two_declarations_agree() -> None:
    """A Literal member missing from the canonical order would silently stop being sorted, so the same set
    of channels could hash two ways. Nothing else checks that the two declarations stay in step."""

    literal = get_args(TradeChannel)
    assert literal == TRADE_CHANNEL_ORDER
    assert "product_progress" in literal
    previous = (
        "rates",
        "liquidity",
        "risk_premium",
        "energy_supply",
        "commodity_supply",
        "commodity_demand",
        "regulation",
        "exchange_access",
        "earnings_cashflow",
        "positioning_flow",
        "security_incident",
    )
    assert set(literal) - set(previous) == {"product_progress"}
    assert set(previous) - set(literal) == set()


def test_a_pre_173_relevance_payload_still_reads_and_an_unknown_channel_still_fails_closed() -> None:
    old = TradeRelevanceV1.model_validate(
        {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unknown",
            "development_delta": "material_detail",
            "channels": ["regulation", "earnings_cashflow"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        }
    )
    assert old.channels == ("regulation", "earnings_cashflow")
    with pytest.raises(ValueError):
        TradeRelevanceV1.model_validate(
            {
                "impact_breadth": "single_instrument",
                "tradability": "direct",
                "surprise": "unknown",
                "development_delta": "state_change",
                "channels": ["product_progres"],  # one character short of the real code
                "affected_markets": ["single_asset"],
                "reader_value": "realtime",
            }
        )


def test_channels_stay_in_canonical_order_however_the_model_emits_them() -> None:
    relevance = TradeRelevanceV1.model_validate(
        {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["earnings_cashflow", "product_progress", "exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        }
    )
    assert relevance.channels == ("exchange_access", "product_progress", "earnings_cashflow")


def test_product_progress_alone_satisfies_the_unchanged_eligibility_predicate() -> None:
    """#173 changes what the model can say, never what the policy accepts."""

    assert realtime_eligible(_verdict(magnitude=2), _state_change()) is True
    assert realtime_eligible(_verdict(magnitude=1), _state_change()) is False


def test_without_a_true_channel_the_contract_cannot_even_express_realtime() -> None:
    """This is the structural trap #173 exists to remove, pinned so it cannot come back by another route.

    ``TradeRelevanceV1`` rejects an empty surface unless ``reader_value`` is already background/none. So a
    product event with no truthful channel to name could not be marked realtime and then be vetoed by
    ``realtime_eligible`` — it was pushed down to background *at emission*, one step earlier than the policy.
    Adding a channel the model can honestly use is therefore the only fix that reaches this case; loosening
    the policy never could.
    """

    empty_surface: dict[str, Any] = {
        "impact_breadth": "single_instrument",
        "tradability": "contextual",
        "surprise": "unknown",
        "development_delta": "state_change",
        "channels": [],
        "affected_markets": [],
    }
    with pytest.raises(ValueError, match="news_trade_relevance_empty_surface_invalid"):
        TradeRelevanceV1.model_validate({**empty_surface, "reader_value": "realtime"})
    withheld = TradeRelevanceV1.model_validate({**empty_surface, "reader_value": "background"})
    assert realtime_eligible(_verdict(magnitude=2), withheld) is False
