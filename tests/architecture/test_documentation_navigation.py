"""The handbook has live links and a deterministic tracked-file map, without external services."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_map_is_current_and_complete() -> None:
    generator = _script("regen_repository_map")
    paths = generator.tracked_paths(ROOT)
    assert paths
    actual = (ROOT / generator.OUTPUT).read_text(encoding="utf-8")
    assert actual == generator.render(ROOT, paths)
    assert actual.count("| [`") == len(paths)


def test_inventory_parses_python_without_importing_it(tmp_path: Path) -> None:
    generator = _script("regen_repository_map")
    source = tmp_path / "unsafe_to_import.py"
    source.write_text(
        '"""A module with a non-importable body."""\nraise RuntimeError("must not execute")\ndef entry(): pass\n'
    )
    first = generator.render(tmp_path, (Path(source.name),))
    assert "non-importable body" in first and "entry" in first
    assert first == generator.render(tmp_path, (Path(source.name),))


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


def test_context_and_historical_research_are_not_exempt(tmp_path: Path) -> None:
    checker = _script("check_mandatory_docs_links")
    (tmp_path / "CONTEXT.md").write_text("[missing](missing.md)\n")
    research = tmp_path / "docs" / "research"
    research.mkdir(parents=True)
    (research / "historical.md").write_text("[missing](old-layout.py)\n")
    assert len(checker.missing_links(tmp_path)) == 2


def test_current_module_guides_have_an_explicit_handbook_entry() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for guide in (ROOT / "docs" / "modules").glob("*.md"):
        assert f"(modules/{guide.name})" in index
