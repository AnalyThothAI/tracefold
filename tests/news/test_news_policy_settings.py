"""news.policy settings: semantic thresholds and duplicate evidence, never reader quotas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.platform.config.settings import NewsPolicySettings, NewsPushSettings


def test_policy_defaults_match_the_live_decide_policy() -> None:
    from tracefold.news.triage_rules import DEFAULT_POLICY, DecidePolicy

    settings = NewsPolicySettings()
    assert DecidePolicy(**settings.model_dump()) == DEFAULT_POLICY


@pytest.mark.parametrize(
    "key",
    [
        "theme_cap_4h",
        "storyline_throttle",
        "hourly_cap_enabled",
        "distinct_hard_cap_4h",
        "distinct_asset_cap_2h",
        "similarity_all_pushes",
    ],
)
def test_retired_policy_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        NewsPolicySettings.model_validate({key: True})


def test_retired_delivery_quota_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NewsPushSettings.model_validate({"hourly_cap": 20})


def test_similarity_max_is_a_ratio() -> None:
    # 0 switches duplicate similarity off; it never restores a count cap.
    assert NewsPolicySettings(similarity_max=0.0).similarity_max == 0.0
    assert NewsPolicySettings(similarity_max=1.0).similarity_max == 1.0
    with pytest.raises(ValidationError, match="news_policy_similarity_max_invalid"):
        NewsPolicySettings(similarity_max=1.5)
