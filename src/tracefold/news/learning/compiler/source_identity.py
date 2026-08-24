"""Canonical source-tree identities for the trusted compiler boundary.

Hashing the complete News Python execution closure is intentionally broader
than a hand-maintained import list. It prevents a newly introduced indirect
dependency from silently escaping the source identity checked independently by
the host, optimizer runner and proxy sidecar.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from ...artifact_identity import canonical_sha

# Resolved from the package, not by counting `__file__` parents: this module has moved once already
# (`news/agents/` -> `news/learning/compiler/`), and a depth-coupled root silently became the wrong
# directory rather than failing at import. `test_the_compile_source_seal_is_computable_from_the_package`
# is what keeps that honest.
_NEWS_ROOT = Path(str(importlib.resources.files("tracefold.news")))
_TRACEFOLD_ROOT = _NEWS_ROOT.parent

# The dependency identity the host expects to find inside the compiler image. It lives here, with the
# host/container attestation that consumes it, rather than in the Program artifact: a wheel has no
# `uv.lock`, the Program's running behavior does not depend on this file, and the only party that reads
# it is `_verify_image_payload_before_secrets`, which compares it against the lock copied out of the
# image before any secret is staged. A drift test keeps it equal to the source lock.
COMPILER_DEPENDENCY_LOCK_SHA256 = "defdd610578ecd1f1f667f5eaf0ebf0b94ae866b16fd5cdd41ba3fc793ab4b37"


def compiler_source_sha256(*, tracefold_root: Path | None = None) -> str:
    return _source_root("tracefold.news.compiler_source.v2", tracefold_root=tracefold_root)


def proxy_source_sha256(*, tracefold_root: Path | None = None) -> str:
    return _source_root("tracefold.news.compiler_proxy_source.v2", tracefold_root=tracefold_root)


def _source_root(schema: str, *, tracefold_root: Path | None) -> str:
    root = (tracefold_root or _TRACEFOLD_ROOT).resolve(strict=True)
    news_root = root / "news"
    cli_commands = root / "app" / "cli" / "commands"
    cli_news = sorted(cli_commands.glob("news_*.py"))
    cli_parser = root / "app" / "cli" / "parser.py"
    if not news_root.is_dir() or not cli_news or not cli_parser.is_file():
        raise ValueError("news_program_compile_source_tree_invalid")
    paths = [path for path in news_root.rglob("*.py") if "__pycache__" not in path.parts]
    paths.extend((root / "__init__.py", cli_parser, *cli_news))
    payload: dict[str, str] = {}
    for path in sorted(set(paths)):
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or root not in resolved.parents:
            raise ValueError("news_program_compile_source_file_invalid")
        name = resolved.relative_to(root).as_posix()
        payload[name] = canonical_sha({"source": resolved.read_text(encoding="utf-8").replace("\r\n", "\n")})
    return canonical_sha({"schema": schema, "files": payload})


__all__ = ["COMPILER_DEPENDENCY_LOCK_SHA256", "compiler_source_sha256", "proxy_source_sha256"]
