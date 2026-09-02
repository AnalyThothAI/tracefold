"""news.policy settings: semantic thresholds and duplicate evidence, never reader quotas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import NewsPolicySettings, NewsPushSettings, NewsSettings


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


def test_retired_gate_low_signal_section_is_rejected() -> None:
    """#504 D7: the `news.gate` low-signal switch was never switched on and produced zero admissions in the retained
    history, so the whole `news.gate` section is gone; a config that still carries it fails at startup."""

    with pytest.raises(ValidationError):
        NewsSettings.model_validate({"gate": {}})
    assert not hasattr(NewsSettings(), "gate")


def test_retired_delivery_quota_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NewsPushSettings.model_validate({"hourly_cap": 20})


def test_storyline_budget_knobs_are_non_negative_and_zero_disables() -> None:
    """#504: the per-storyline budget is exposed through `news.policy`; either knob at 0 switches it off."""

    settings = NewsPolicySettings()
    assert settings.storyline_budget_window_s == 3600 and settings.storyline_budget_max == 2
    assert NewsPolicySettings(storyline_budget_max=0).storyline_budget_max == 0
    assert NewsPolicySettings(storyline_budget_window_s=0).storyline_budget_window_s == 0
    with pytest.raises(ValidationError, match="news_policy_storyline_budget_max_invalid"):
        NewsPolicySettings(storyline_budget_max=-1)
    with pytest.raises(ValidationError, match="news_policy_storyline_budget_window_s_invalid"):
        NewsPolicySettings(storyline_budget_window_s=-1)


def test_similarity_max_is_a_ratio() -> None:
    # 0 switches duplicate similarity off; it never restores a count cap.
    assert NewsPolicySettings(similarity_max=0.0).similarity_max == 0.0
    assert NewsPolicySettings(similarity_max=1.0).similarity_max == 1.0
    with pytest.raises(ValidationError, match="news_policy_similarity_max_invalid"):
        NewsPolicySettings(similarity_max=1.5)
