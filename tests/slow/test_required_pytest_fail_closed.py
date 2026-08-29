from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.require_test_reports import ReportError, require_junit

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.slow


def _run_required_pytest(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    module = tmp_path / "test_fault.py"
    report = tmp_path / "junit.xml"
    module.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "_hypothesis_pytestplugin",
            str(module),
            "--maxfail=0",
            "--override-ini=xfail_strict=true",
            f"--junitxml={report}",
            "-q",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRACEFOLD_HYPOTHESIS_PROFILE": "ci",
            "TRACEFOLD_TEST_RESOURCES_REQUIRED": "1",
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return result, report


def test_required_pytest_accepts_one_plain_pass(tmp_path: Path) -> None:
    result, report = _run_required_pytest(tmp_path, "def test_plain_pass():\n    pass\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert require_junit(report) == 1


@pytest.mark.parametrize(
    ("source", "runner_can_succeed"),
    [
        ("", False),
        (
            'import pytest\n\n@pytest.mark.skip(reason="fault injection")\ndef test_skip():\n    pass\n',
            True,
        ),
        (
            'import pytest\n\n@pytest.mark.xfail(reason="fault injection")\ndef test_xfail():\n    assert False\n',
            True,
        ),
        (
            'import pytest\n\n@pytest.mark.xfail(reason="fault injection")\ndef test_xpass():\n    pass\n',
            False,
        ),
        ("def test_failure():\n    assert False\n", False),
        ('raise RuntimeError("collection fault")\n', False),
    ],
)
def test_required_pytest_never_authorizes_pseudo_green(tmp_path: Path, source: str, runner_can_succeed: bool) -> None:
    result, report = _run_required_pytest(tmp_path, source)

    assert (result.returncode == 0) is runner_can_succeed, result.stdout + result.stderr
    with pytest.raises(ReportError):
        require_junit(report)
