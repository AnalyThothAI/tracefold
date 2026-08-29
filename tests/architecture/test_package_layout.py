"""One package root, and no way back to the legacy nested one.

#373 moved the production package out of the old `src/` parent to the repository root. The hard-cut
rule is that nothing keeps the old path alive — no symlink, no forwarding package, no second Hatch
include, no source directory on `PYTHONPATH`, no `sitecustomize`. A rename is easy to half-finish:
one stale build include, one `Path("src") / ...` in a script, one Docker `COPY` of the old parent is
enough to make the working tree and the shipped distribution disagree, and the working tree wins in
every ordinary test run.

This module names the forbidden path only by assembling it from parts, so the guard can scan every
tracked file including itself and needs no self-exemption.

`docs/research/` is out of scope here, and deliberately. It is the dated evidence corpus: its
mentions of the old path are `file:line` citations and pinned-commit GitHub permalinks describing
trees as they were on the audit date, and `scripts/check_mandatory_docs_links.py` already exempts
that directory from the live-link contract for the same reason. Rewriting the directory component
of a citation whose line ranges — and often whose file — belong to an older tree would not make it
correct, only harder to recognise as historical.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "tracefold"
ARCHIVAL_EVIDENCE = "docs/research/"
LEGACY_PARENT = "src"
LEGACY_PACKAGE_PATH = f"{LEGACY_PARENT}/{PACKAGE}"
LEGACY_IMPORT_PATH_ENV = re.compile(r"PYTHONPATH[\"'\]:= ]*[^\n]*\b" + LEGACY_PARENT + r"\b")


def _tracked_files() -> tuple[Path, ...]:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return tuple(Path(name) for name in listing.split("\0") if name)


def _readable_files_outside_the_archive() -> tuple[tuple[Path, str], ...]:
    readable: list[tuple[Path, str]] = []
    for path in _tracked_files():
        if path.as_posix().startswith(ARCHIVAL_EVIDENCE) or not (ROOT / path).is_file():
            continue
        try:
            readable.append((path, (ROOT / path).read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return tuple(readable)


def test_the_repository_root_holds_exactly_one_production_package() -> None:
    assert (ROOT / PACKAGE / "__init__.py").is_file()
    assert not (ROOT / LEGACY_PARENT).exists()


def test_no_current_path_still_names_the_legacy_package_root() -> None:
    offenders = [
        f"{path.as_posix()}:{number}"
        for path, content in _readable_files_outside_the_archive()
        for number, line in enumerate(content.splitlines(), start=1)
        if LEGACY_PACKAGE_PATH in line
    ]

    assert offenders == []


def test_nothing_resurrects_the_legacy_root_through_the_import_system() -> None:
    """Symlinks, `sitecustomize`, and a source directory on `PYTHONPATH` undo the cut most cheaply."""

    tracked = _tracked_files()

    assert [path.as_posix() for path in tracked if (ROOT / path).is_symlink()] == []
    assert [path.as_posix() for path in tracked if path.name == "sitecustomize.py"] == []
    assert [
        f"{path.as_posix()}:{number}"
        for path, content in _readable_files_outside_the_archive()
        for number, line in enumerate(content.splitlines(), start=1)
        if LEGACY_IMPORT_PATH_ENV.search(line)
    ] == []


def test_the_build_backend_ships_exactly_the_flat_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["packages"] == [PACKAGE]
    assert "mypy_path" not in pyproject["tool"]["mypy"]


def test_alembic_reads_the_migration_tree_from_the_flat_package() -> None:
    """`%(here)s`, not a bare relative path: Alembic resolves those against the caller's directory."""

    alembic_ini = (ROOT / "alembic.ini").read_text(encoding="utf-8")

    assert f"script_location = %(here)s/{PACKAGE}/platform/postgres/alembic" in alembic_ini
    assert "prepend_sys_path = %(here)s" in alembic_ini
    assert (ROOT / PACKAGE / "platform" / "postgres" / "alembic" / "env.py").is_file()


def test_the_image_copies_the_flat_package_and_its_web_assets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert f"COPY {PACKAGE} ./{PACKAGE}" in dockerfile
    assert f"/app/{PACKAGE}/web/dist" in dockerfile
    assert not re.search(r"^COPY\s+" + LEGACY_PARENT + r"\b", dockerfile, flags=re.MULTILINE)


def test_the_image_proves_its_own_import_from_outside_the_application_root() -> None:
    """`/app` is a package root under the flat layout, so a probe that runs there proves nothing."""

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    probe = re.search(r"^RUN cd / \\\n(?:.*\\\n)*.*$", dockerfile, flags=re.MULTILINE)

    assert probe is not None, "the image never imports itself from outside /app"
    body = probe.group(0)
    assert f"{PACKAGE} --help" in body
    assert f'Path("/app/{PACKAGE}")' in body
    assert '"web" / "dist" / "index.html"' in body
