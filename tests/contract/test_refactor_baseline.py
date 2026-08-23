"""Issue #162 refactor baseline remains an exact behavior/runtime contract."""

from tests.support.refactor_baseline import assert_matches_baseline


def test_issue_162_refactor_baseline_has_not_drifted() -> None:
    assert_matches_baseline()
