from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from tests.support import evidence

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / "tests" / "fixtures" / "issue_335_evidence_v2_v3_shadow.json"
pytestmark = pytest.mark.contract


def _test_functions(source: str) -> set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


def _source_at(commit: str, path: str) -> str:
    return subprocess.run(
        ("git", "show", f"{commit}:{path}"),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def test_v2_detector_contracts_are_preserved_or_explicitly_replaced_in_v3() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    v2_commit = receipt["window"]["v2"]["commit_sha"]
    v2_tests = _test_functions(_source_at(v2_commit, "tests/contract/test_evidence_v2_contract.py"))
    v3_tests = _test_functions((ROOT / "tests/contract/test_evidence_v3_contract.py").read_text(encoding="utf-8"))
    replacements = receipt["replaced_detector_contracts"]

    assert receipt["window"]["v2"]["overall"] == "success"
    assert receipt["window"]["v2"]["python_selected"] == receipt["window"]["v2"]["python_passed"]
    assert set(v2_tests) - set(v3_tests) == set(replacements)
    assert set(replacements.values()) <= v3_tests
    assert len(v3_tests) > len(v2_tests)


def test_v2_v3_shadow_receipt_keeps_each_critical_real_seam_in_its_v3_owner() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    v2_commit = receipt["window"]["v2"]["commit_sha"]
    classes = set()
    for seam in receipt["critical_seams"]:
        classes.add(seam["class"])
        v2_path, v2_name = seam["v2_test"].split("::", 1)
        v3_path, v3_name = seam["v3_test"].split("::", 1)
        assert v2_name in _test_functions(_source_at(v2_commit, v2_path))
        assert v3_name in _test_functions((ROOT / v3_path).read_text(encoding="utf-8"))
        assert (
            evidence.primary_lane_owner(v3_path, {"integration"} if "/integration/" in v3_path else set())
            == seam["v3_owner"]
        )
        assert seam["fault"]

    assert classes == {"architecture", "news-decision", "trading-capital", "migration"}
