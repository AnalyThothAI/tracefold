"""news.policy (NewsPolicySettings): the decide() knobs, incl. the policy v3 novelty caps (issue #61)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.platform.config.settings import NewsPolicySettings


def test_policy_defaults_match_the_live_decide_policy() -> None:
    from tracefold.news.triage_rules import DEFAULT_POLICY, DecidePolicy

    settings = NewsPolicySettings()
    assert DecidePolicy(**settings.model_dump()) == DEFAULT_POLICY


def test_theme_hard_cap_follows_a_raised_soft_cap_unless_set_explicitly() -> None:
    # A config that only raises theme_cap_4h stays valid: the hard cap follows it.
    assert NewsPolicySettings(theme_cap_4h=8).theme_hard_cap_4h == 8
    assert NewsPolicySettings(theme_cap_4h=2).theme_hard_cap_4h == 6
    assert NewsPolicySettings(theme_cap_4h=3, theme_hard_cap_4h=10).theme_hard_cap_4h == 10
    with pytest.raises(ValidationError, match="news_policy_theme_hard_cap_invalid"):
        NewsPolicySettings(theme_cap_4h=8, theme_hard_cap_4h=6)
    with pytest.raises(ValidationError, match="news_policy_asset_hard_cap_invalid"):
        NewsPolicySettings(asset_hard_cap_2h=0)
    with pytest.raises(ValidationError, match="news_policy_novel_min_magnitude_invalid"):
        NewsPolicySettings(novel_min_magnitude=4)
