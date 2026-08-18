# tests/test_docs_generated.py
"""Verify docs/generated/ files are regeneration-clean."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.postgres_test_utils import prepare_postgres_database
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATED = REPO_ROOT / "docs" / "generated"
AUTO_GENERATED = {
    "db-schema.md",
    "cli-help.md",
}
EXPECTED = {"README.md"} | AUTO_GENERATED
HEADER_MARKER = "AUTO-GENERATED"


def test_generated_directory_present() -> None:
    assert GENERATED.is_dir(), "docs/generated/ missing"


def test_expected_generated_files() -> None:
    actual = {p.name for p in GENERATED.iterdir() if p.is_file()}
    expected = EXPECTED | {"openapi.json"}
    assert actual == expected, f"unexpected docs/generated/ contents: {actual ^ expected}"


def test_generated_files_have_header_marker() -> None:
    for name in AUTO_GENERATED:
        path = GENERATED / name
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert HEADER_MARKER in first_line, f"{name} missing AUTO-GENERATED header"


def test_make_docs_generated_clean_diff() -> None:
    prepare_postgres_database()
    before = _generated_snapshot()
    env = os.environ.copy()
    env["GMGN_TEST_POSTGRES_DSN"] = _test_postgres_dsn()
    proc = subprocess.run(
        ["make", "docs-generated"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"make docs-generated failed:\n{proc.stderr}"
    after = _generated_snapshot()
    assert after == before, "make docs-generated changed docs/generated files:\n" + "\n".join(
        _snapshot_diff(before, after)
    )


def _generated_snapshot() -> dict[str, bytes]:
    return {
        str(path.relative_to(GENERATED)): path.read_bytes() for path in sorted(GENERATED.rglob("*")) if path.is_file()
    }


def _snapshot_diff(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    before_keys = set(before)
    after_keys = set(after)
    lines = [f"added: {path}" for path in sorted(after_keys - before_keys)]
    lines.extend(f"removed: {path}" for path in sorted(before_keys - after_keys))
    lines.extend(f"changed: {path}" for path in sorted(before_keys & after_keys) if before[path] != after[path])
    return lines
