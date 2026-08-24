"""Release-only Node codegen check for the committed OpenAPI TypeScript client."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "docs" / "generated" / "openapi.json"
OPENAPI_TS_PATH = ROOT / "web" / "src" / "lib" / "types" / "openapi.ts"

pytestmark = [pytest.mark.contract, pytest.mark.external_codegen]


def test_openapi_ts_matches_committed_artefact(tmp_path: Path) -> None:
    fresh_path = tmp_path / "openapi.ts"
    result = subprocess.run(
        ["npx", "openapi-typescript", str(OPENAPI_PATH), "-o", str(fresh_path)],
        cwd=ROOT / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "openapi-typescript invocation failed; run `cd web && npm ci`.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert fresh_path.read_text(encoding="utf-8") == OPENAPI_TS_PATH.read_text(encoding="utf-8"), (
        "Frontend OpenAPI types drifted; run `make regen-contract`."
    )
