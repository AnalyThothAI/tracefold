"""news.policy (NewsPolicySettings): the decide() knobs, incl. the policy v3 novelty caps (issue #61)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.platform.config.settings import NewsPolicySettings


def test_policy_defaults_match_the_live_decide_policy() -> None:
    from tracefold.news.triage_rules import DEFAULT_POLICY, DecidePolicy

    settings = NewsPolicySettings()
    assert DecidePolicy(**settings.model_dump()) == DEFAULT_POLICY


def test_flood_ceiling_follows_a_raised_soft_cap_unless_set_explicitly() -> None:
    # A config that only raises theme_cap_4h stays valid: the ceiling follows it.
    assert NewsPolicySettings(theme_cap_4h=24).distinct_hard_cap_4h == 24
    assert NewsPolicySettings(theme_cap_4h=2).distinct_hard_cap_4h == 18
    assert NewsPolicySettings(theme_cap_4h=3, distinct_hard_cap_4h=10).distinct_hard_cap_4h == 10
    with pytest.raises(ValidationError, match="news_policy_distinct_hard_cap_invalid"):
        NewsPolicySettings(theme_cap_4h=8, distinct_hard_cap_4h=6)
    with pytest.raises(ValidationError, match="news_policy_distinct_asset_cap_invalid"):
        NewsPolicySettings(distinct_asset_cap_2h=0)


def test_similarity_max_is_a_ratio() -> None:
    # 0 switches the content judgment off (pre-v5 count cap); 1 releases all but an exact repeat.
    assert NewsPolicySettings(similarity_max=0.0).similarity_max == 0.0
    assert NewsPolicySettings(similarity_max=1.0).similarity_max == 1.0
    with pytest.raises(ValidationError, match="news_policy_similarity_max_invalid"):
        NewsPolicySettings(similarity_max=1.5)
