"""Issue #162 refactor baseline remains an exact behavior/runtime contract."""

from typing import Any

import pytest

from tests.support import refactor_baseline
from tests.support.refactor_baseline import assert_matches_baseline


def test_issue_162_refactor_baseline_has_not_drifted() -> None:
    assert_matches_baseline()


def _with_drift(monkeypatch: pytest.MonkeyPatch, declared: dict[str, tuple[str, str]]) -> None:
    monkeypatch.setattr(refactor_baseline, "INTENTIONAL_DRIFT", declared)


def _declared() -> dict[str, tuple[str, str]]:
    return dict(refactor_baseline.INTENTIONAL_DRIFT)


# The allowlist is only worth having if it fails. Against a baseline frozen at one revision a leaf that has
# drifted once can never drift back, so a name-only exemption would silently cover every later change to the
# same leaf. These three cases pin that it does not.


def test_an_undeclared_leaf_still_fails_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = _declared()
    dropped = declared.pop("program_learning.program_sha256")
    assert dropped
    _with_drift(monkeypatch, declared)
    with pytest.raises(AssertionError, match="drifted on leaves nobody declared"):
        assert_matches_baseline()


def test_a_declared_leaf_that_moves_past_its_declared_value_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = _declared()
    reason, _ = declared["generated_artifacts_sha256.docs/generated/openapi.json"]
    declared["generated_artifacts_sha256.docs/generated/openapi.json"] = (reason, "f" * 64)
    _with_drift(monkeypatch, declared)
    with pytest.raises(AssertionError, match="moved past their declared values"):
        assert_matches_baseline()


def test_a_stale_declaration_for_an_unchanged_leaf_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = _declared()
    declared["news_delivery.card_v10_sha256"] = ("a_reason_whose_cause_is_gone", "0" * 64)
    _with_drift(monkeypatch, declared)
    with pytest.raises(AssertionError, match="now match the frozen baseline"):
        assert_matches_baseline()


def test_every_declared_leaf_names_a_reason_and_an_exact_value() -> None:
    """A declared exemption covers one exact value — never a prefix, never an ellipsis.

    #162 PR8-B added the first non-hash leaves (a migration head, an epoch id, a program version, a
    factory id), so the shape check is now conditional: anything hash-shaped must still be written in
    full, because a prefix would let the leaf keep drifting inside the part nobody wrote down.
    """

    for path, value in refactor_baseline.INTENTIONAL_DRIFT.items():
        reason, expected = value
        assert reason and not reason.startswith("<"), path
        assert expected and "…" not in expected and "..." not in expected, path
        if set(expected) <= set("0123456789abcdef"):
            assert len(expected) == 64, path


def test_the_baseline_identity_itself_is_never_regenerated_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """The frozen revision is the reference point; moving it is what this guard exists to prevent."""

    original: Any = refactor_baseline.BASELINE_REVISION
    monkeypatch.setattr(refactor_baseline, "BASELINE_REVISION", "0" * 40)
    with pytest.raises(AssertionError, match="not the frozen one"):
        assert_matches_baseline()
    monkeypatch.setattr(refactor_baseline, "BASELINE_REVISION", original)
    assert_matches_baseline()
