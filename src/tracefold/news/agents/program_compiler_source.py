"""Canonical source-tree identities for the trusted compiler boundary.

Hashing the complete News Python execution closure is intentionally broader
than a hand-maintained import list. It prevents a newly introduced indirect
dependency from silently escaping the source identity checked independently by
the host, optimizer runner and proxy sidecar.
"""

from __future__ import annotations

from pathlib import Path

from ..artifact_identity import canonical_sha

_NEWS_ROOT = Path(__file__).resolve().parents[1]
_TRACEFOLD_ROOT = _NEWS_ROOT.parent
_CLI_NEWS = _TRACEFOLD_ROOT / "app" / "cli" / "commands" / "news.py"


def compiler_source_sha256(*, tracefold_root: Path | None = None) -> str:
    return _source_root("tracefold.news.compiler_source.v2", tracefold_root=tracefold_root)


def proxy_source_sha256(*, tracefold_root: Path | None = None) -> str:
    return _source_root("tracefold.news.compiler_proxy_source.v2", tracefold_root=tracefold_root)


def _source_root(schema: str, *, tracefold_root: Path | None) -> str:
    root = (tracefold_root or _TRACEFOLD_ROOT).resolve(strict=True)
    news_root = root / "news"
    cli_news = root / "app" / "cli" / "commands" / "news.py"
    cli_parser = root / "app" / "cli" / "parser.py"
    if not news_root.is_dir() or not cli_news.is_file() or not cli_parser.is_file():
        raise ValueError("news_program_compile_source_tree_invalid")
    paths = [path for path in news_root.rglob("*.py") if "__pycache__" not in path.parts]
    paths.extend((root / "__init__.py", cli_news, cli_parser))
    payload: dict[str, str] = {}
    for path in sorted(set(paths)):
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or root not in resolved.parents:
            raise ValueError("news_program_compile_source_file_invalid")
        name = resolved.relative_to(root).as_posix()
        payload[name] = canonical_sha({"source": resolved.read_text(encoding="utf-8").replace("\r\n", "\n")})
    return canonical_sha({"schema": schema, "files": payload})


__all__ = ["compiler_source_sha256", "proxy_source_sha256"]
