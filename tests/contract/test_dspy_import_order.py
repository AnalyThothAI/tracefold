"""The public app and News imports support DSPy's first LiteLLM call."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "imports",
    [
        "import tracefold.news.updates.dspy_backend; import fastapi",
        "import tracefold.app.http.responses; import tracefold.news.updates.dspy_backend",
    ],
)
def test_dspy_and_fastapi_import_order_is_safe(imports: str) -> None:
    first_model_call = (
        "; from dspy.clients._litellm import get_litellm"
        "; assert callable(get_litellm(feature='import test').acompletion)"
    )
    result = subprocess.run(
        [sys.executable, "-c", imports + first_model_call],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
