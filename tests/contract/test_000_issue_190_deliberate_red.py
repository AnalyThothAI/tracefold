from __future__ import annotations

import pytest


def test_issue_190_deliberate_required_gate_failure() -> None:
    pytest.exit("issue_190_deliberate_required_gate_failure", returncode=1)
