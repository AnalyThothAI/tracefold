from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_docker_build_context_excludes_python_generated_files_at_every_depth() -> None:
    """Keep stale local bytecode and tool caches out of recursive COPY inputs."""

    patterns = {
        line
        for raw_line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    }
    required_recursive_patterns = {
        "**/__pycache__",
        "**/*.py[cod]",
        "**/.pytest_cache",
        "**/.ruff_cache",
        "**/.mypy_cache",
        "**/.hypothesis",
    }

    assert required_recursive_patterns <= patterns
