"""The public app and News imports work in the same process, in either order."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "imports",
    [
        "import tracefold.news.program.lm; import fastapi",
        "import tracefold.app.http.responses; import tracefold.news.program.lm",
    ],
)
def test_dspy_and_fastapi_import_order_is_safe(imports: str) -> None:
    result = subprocess.run([sys.executable, "-c", imports], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
