"""Current loader rejects retired wallet quorum settings."""

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import Settings


def test_the_retired_second_window_quorum_is_a_startup_error_not_a_silent_default():
    """#649 PR-3 §2: one window, one quorum, and a config that still names the other one fails.

    The loader forbids unknown keys, which is what makes the deletion visible: a deployment whose
    `~/.tracefold/config.yaml` still holds `net_buy_fast_n: 3` refuses to start rather than running
    with a key nothing reads.
    """

    base = {"news": {"chain_tape": {"rules": {"net_buy_slow_n": 5}}}}
    assert Settings.model_validate(base).news.chain_tape.rules.net_buy_slow_n == 5
    with pytest.raises(ValidationError, match="net_buy_fast_n"):
        Settings.model_validate({"news": {"chain_tape": {"rules": {"net_buy_fast_n": 3, "net_buy_slow_n": 5}}}})
