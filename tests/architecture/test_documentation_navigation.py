"""The handbook has live links and one reachable current source handbook, without external services."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_links_check_anchors_references_and_percent_encoded_paths(tmp_path: Path) -> None:
    checker = _script("check_mandatory_docs_links")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target name.md").write_text("# Target\n## Same\n## Same\n## 中文标题\n")
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Test\n[one](docs/target%20name.md#same-1)\n[two][ref]\n"
        "[ref]: docs/target%20name.md#中文标题\n"
        "```md\n[example](absent.md)\n```\n"
        "Inline regex `[A-Z][a-z]` is not a reference link.\n"
    )
    assert checker.missing_links(tmp_path) == ()
    readme.write_text(readme.read_text() + "\n[broken](docs/target%20name.md#missing)\n[broken][undefined]\n")
    errors = checker.missing_links(tmp_path)
    assert any("missing Markdown anchor" in error for error in errors)
    assert any("undefined reference" in error for error in errors)


def test_context_and_nested_module_docs_are_checked(tmp_path: Path) -> None:
    checker = _script("check_mandatory_docs_links")
    (tmp_path / "CONTEXT.md").write_text("[missing](missing.md)\n")
    modules = tmp_path / "docs" / "modules"
    modules.mkdir(parents=True)
    (modules / "example.md").write_text("[missing](old-layout.py)\n")
    assert len(checker.missing_links(tmp_path)) == 2


def test_current_module_guides_have_an_explicit_handbook_entry() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for guide in (ROOT / "docs" / "modules").glob("*.md"):
        assert f"(modules/{guide.name})" in index


def test_every_current_handbook_page_is_reachable_from_the_front_door() -> None:
    checker = _script("check_mandatory_docs_links")
    queue = [ROOT / "README.md"]
    seen: set[Path] = set()
    while queue:
        source = queue.pop().resolve()
        if source in seen or not source.is_file() or source.suffix != ".md":
            continue
        seen.add(source)
        targets, _ = checker.local_targets(source.read_text(encoding="utf-8"))
        for target in targets:
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc:
                continue
            path = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
            if path.is_file() and path.suffix == ".md":
                queue.append(path)
    expected = {path.resolve() for path in (ROOT / "docs").rglob("*.md")}
    assert expected <= seen, sorted(str(path.relative_to(ROOT)) for path in expected - seen)
