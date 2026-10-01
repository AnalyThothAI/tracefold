"""The public app and News imports support DSPy's first LiteLLM call."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.contract

ADAPTER_IMPORTS = (
    "import tracefold.news.adapters.extraction"
    "; import tracefold.news.adapters.semantic_judgments"
    "; import tracefold.news.adapters.reader_judge"
    "; import tracefold.news.adapters.card_copy"
)


@pytest.mark.parametrize(
    "imports",
    [
        ADAPTER_IMPORTS + "; import fastapi",
        "import tracefold.app.http.responses; " + ADAPTER_IMPORTS,
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
