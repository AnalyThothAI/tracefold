"""Fixed product-recall cases for Issue #173.

Every case runs through the public semantic/Policy seam: a typed ``ScoredJudgment`` a
contract-following EventSemantics would emit, then the current ``decide()``. The suite therefore pins
the *contract*, not one model's wording — a Program that reads the RulePacks correctly passes it, and
a candidate that quietly re-learns "product is background" fails it.

#173 bought product recall by adding a `product_progress` channel to the seven-code trade-relevance
contract, so that a first-party product reaching a verifiable new state had a surface it could name
and could therefore be marked realtime at all. Policy v16 deletes that contract and asks the model one
question instead: what kind of new thing does this text state. The cases below are unchanged and the
answer is now direct — a shipped capability is a `state_change` and reaches the reader, a cumulative
vanity total is a `recap`, a roadmap is a `schedule` and a competition is a `promotion`, and none of
those three is a fact the reader is woken for.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest

from tests.support.news_judgment import scored_judgment
from tracefold.news.models import FACT_KINDS, Decision, FactKind, TriageAsset, TriageVerdict
from tracefold.news.program.contracts import ScoredJudgment
from tracefold.news.triage_rules import (
    DROP_FACT_KINDS,
    PUSH_FACT_KINDS,
    DecisionResult,
    GateFacts,
    decide,
)

_CANDIDATE = GateFacts(
    grounded_assets=(),
    watchlist_symbols=frozenset(),
    admission="candidate",
)
# A grounded tag would fire `watchlist_objective_guard` and prove nothing about the semantics under test, so
# every case here is an ungrounded candidate: the only thing that can push it is the stated fact kind.
_GROUNDED = GateFacts(
    grounded_assets=("HYPE",),
    watchlist_symbols=frozenset(),
    admission="candidate",
)


def _result(final: Decision, kind: str) -> DecisionResult:
    return DecisionResult(final, f"fact_kind_{kind}", None, "drop", ())


_PUSH_STATE_CHANGE = _result("push", "state_change")


def _verdict(**overrides: Any) -> TriageVerdict:
    values: dict[str, Any] = {
        "novelty": "new_fact",
        "assets": [TriageAsset(role="primary", symbol="HYPE", market_type="crypto")],
        "direction": "neutral",
        "scope": "single_name",
        "fact_kind": "state_change",
        "evidence_ref": "c1",
        "confidence": 0.9,
        "headline_zh": "固定产品召回案例",
        "why_zh": "",
    }
    values.update(overrides)
    return TriageVerdict.model_validate(values)


def _judgment(**verdict_overrides: Any) -> ScoredJudgment:
    return scored_judgment(_verdict(**verdict_overrides))


# --- A. concrete product state change: must_push -------------------------------------------------------


MUST_PUSH: tuple[tuple[str, dict[str, Any]], ...] = (
    # 77ebe56d: shipped as noise/m0/none. A paid, irreversible step toward deploying one named market.
    ("hyperliquid_eqmsft_ticker_auction", {"direction": "neutral"}),
    ("exchange_opens_new_spot_market", {"direction": "bullish"}),
    ("protocol_mainnet_capability_live", {"direction": "bullish"}),
    ("issuer_changes_own_pricing", {"direction": "bullish"}),
    ("announced_product_cancelled_or_recalled", {"direction": "bearish"}),
)


@pytest.mark.parametrize(("name", "verdict"), MUST_PUSH, ids=[case[0] for case in MUST_PUSH])
def test_a_confirmed_product_state_change_reaches_the_reader(name: str, verdict: dict[str, Any]) -> None:
    assert decide(_judgment(**verdict), _CANDIDATE, None) == _PUSH_STATE_CHANGE


def test_a_product_state_change_pushes_on_an_ungrounded_event_with_no_watchlist_help() -> None:
    """The whole point of #173: no deterministic guard is available, so the semantics must carry it alone."""

    result = decide(_judgment(), _CANDIDATE, None)
    assert result.final == "push"
    assert result.rule_baseline == "drop"
    assert result.watchlist_hits == ()


# --- B. high-quality adoption progress: should_push ----------------------------------------------------


SHOULD_PUSH: tuple[tuple[str, str, dict[str, Any]], ...] = (
    # 97281ae7: shipped as product/m1/none with contextual/color_only. The highest of a named period is
    # the fact, and it is a different kind from the launch that made it possible.
    ("active_perp_traders_all_time_high", "period_record", {"direction": "neutral"}),
    ("official_paying_users_cross_threshold", "new_quantity", {"direction": "bullish"}),
)


@pytest.mark.parametrize(("name", "kind", "verdict"), SHOULD_PUSH, ids=[case[0] for case in SHOULD_PUSH])
def test_first_party_active_adoption_reaches_the_reader(name: str, kind: str, verdict: dict[str, Any]) -> None:
    assert decide(_judgment(fact_kind=kind, **verdict), _GROUNDED, None) == _result("push", kind)


# --- C/D. routine milestone and no product fact: must_hold ---------------------------------------------


MUST_HOLD: tuple[tuple[str, str, dict[str, Any]], ...] = (
    # a8ffa0eb: a cumulative account total in a marketing post stays exactly where it is.
    ("tron_400m_cumulative_accounts", "recap", {"direction": "bullish"}),
    # 138d2689: a prediction-market quote is not a product fact.
    ("polymarket_starship_odds", "statement", {}),
    ("roadmap_or_testnet_teaser", "schedule", {}),
    ("partnership_recap_without_shipped_capability", "recap", {}),
    ("marketing_competition_or_airdrop", "promotion", {}),
)


@pytest.mark.parametrize(("name", "kind", "verdict"), MUST_HOLD, ids=[case[0] for case in MUST_HOLD])
def test_a_milestone_or_non_product_fact_is_still_withheld(name: str, kind: str, verdict: dict[str, Any]) -> None:
    assert decide(_judgment(fact_kind=kind, **verdict), _GROUNDED, None) == _result("drop", kind)


def test_a_provider_common_word_tag_cannot_manufacture_product_progress() -> None:
    """`PERP`, `SPOT`, `ERA` and friends are ordinary English words the provider tags as A-grade coins.

    A tag is Gate evidence; it is not a product action, so it must never be what lifts an event into a
    product state change. The Gate facts below carry the tag and the text states no new fact about the
    world: the card still has to be withheld.
    """

    tagged = GateFacts(grounded_assets=("PERP",), watchlist_symbols=frozenset(), admission="candidate")
    assert decide(_judgment(fact_kind="promotion"), tagged, None) == _result("drop", "promotion")


# --- stability ------------------------------------------------------------------------------------------


def test_two_paraphrases_of_one_product_fact_take_the_same_action() -> None:
    """The corpus had `ChatGPT launches Apple Messages integration` at m1/drop and m2/push 104 minutes apart."""

    first = _judgment(headline_zh="A 公司在 B 平台上线集成功能")
    second = _judgment(headline_zh="B 平台现已支持 A 公司的集成功能")
    left, right = decide(first, _CANDIDATE, None), decide(second, _CANDIDATE, None)
    assert left == right == _PUSH_STATE_CHANGE
    assert first.verdict.fact_kind == second.verdict.fact_kind


def test_an_unknown_price_implication_does_not_withhold_a_product_state_change() -> None:
    """A ticker auction has no knowable direction. Under the old prior that alone demoted it to m1."""

    for direction in ("neutral", "unclear"):
        assert decide(_judgment(direction=direction), _CANDIDATE, None) == _PUSH_STATE_CHANGE


def test_a_product_progression_is_not_a_restatement_of_its_own_announcement() -> None:
    """`progression` carries `restates=-1`, so the restatement drop cannot reach a shipped follow-up."""

    assert decide(_judgment(novelty="progression", restates=-1), _CANDIDATE, None) == _PUSH_STATE_CHANGE


def test_the_same_launch_from_another_outlet_is_still_a_restatement() -> None:
    from tracefold.news.triage_rules import storyline_status

    status = storyline_status(
        "asset:HYPE",
        told=[{"direction": "bullish", "headline_zh": "某协议主网能力上线", "symbols": ["HYPE"]}],
    )
    judgment = _judgment(direction="bullish", novelty="restatement", restates=0)
    assert decide(judgment, _CANDIDATE, status).final == "drop"
    assert decide(judgment, _CANDIDATE, status).override_rule == "restatement"


# --- contract -------------------------------------------------------------------------------------------


def test_the_fact_kind_vocabulary_is_closed_and_its_two_declarations_agree() -> None:
    """A Literal member missing from `FACT_KINDS` would leave `FACT_KIND_RULES` without a rule name for a
    kind the model can emit, and `decide()` would raise on a valid verdict. Nothing else checks that the
    tuple and the type stay in step, or that every kind is on exactly one side of the push/drop split."""

    assert get_args(FactKind) == FACT_KINDS
    assert sorted(PUSH_FACT_KINDS | DROP_FACT_KINDS) == sorted(FACT_KINDS)
    assert not PUSH_FACT_KINDS & DROP_FACT_KINDS
    assert "state_change" in PUSH_FACT_KINDS and "promotion" in DROP_FACT_KINDS


def test_an_unknown_fact_kind_fails_closed() -> None:
    """The kind is a closed vocabulary on the verdict too: a near-miss is a refusal, never a tenth kind."""

    with pytest.raises(ValueError):
        _verdict(fact_kind="product_progress")
