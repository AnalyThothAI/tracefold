"""The `notebooks/` research workspace (#274): where a notebook lives, what it declares, what it commits.

Tracked files only, deliberately. An untracked notebook is a draft on the operator's disk: it does
not fail `make check` here, for the same reason it does not fail `make deploy-image` over in
`tests/deploy/test_onboarding_surface.py`.

The rule these checks enforce is in `notebooks/README.md`. The one worth restating: a channel-A or
channel-B output cannot be recomputed from the repository — a live read describes a moment that has
passed, and a frozen run directory is operator-owned and never repository content — so committing
one publishes a number nobody can check. A channel-C output is the opposite: it is recomputable by
anyone with the repository, which is exactly why it is the evidence and has to be committed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "notebooks"
DECLARATION_KEYS = {"channel", "purpose", "window", "identity", "safety"}
CHANNELS = {"A", "B", "C"}
COMMITTED_SNAPSHOT = "C"
WRITE_CAPABLE_ROLES = ("tracefold_workers", "tracefold_owner")
NETWORKED_CALL_SITES = ("urlopen", "urllib.request", "httpx", "requests.", "aiohttp", "psycopg")
DECLARATION_BLOCK_RE = re.compile(r"^```yaml\n(?P<body>.*?)^```$", re.DOTALL | re.MULTILINE)


def _tracked_notebooks() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.ipynb"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [ROOT / name for name in listed.split("\0") if name]


def _cells(path: Path) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["cells"])


def _source(cell: dict[str, Any]) -> str:
    source = cell["source"]
    return source if isinstance(source, str) else "".join(source)


def _declaration(path: Path) -> dict[str, Any]:
    cells = _cells(path)
    assert cells and cells[0]["cell_type"] == "markdown", f"{path.name}: first cell is not markdown"
    block = DECLARATION_BLOCK_RE.search(_source(cells[0]))
    assert block is not None, f"{path.name}: first cell carries no fenced yaml declaration"
    declared = yaml.safe_load(block.group("body"))
    assert isinstance(declared, dict), f"{path.name}: the declaration is not a mapping"
    return declared


def test_every_tracked_notebook_lives_flat_in_the_research_workspace() -> None:
    misplaced = [path.relative_to(ROOT).as_posix() for path in _tracked_notebooks() if path.parent != WORKSPACE]

    assert misplaced == []


def test_every_notebook_declares_purpose_window_identity_safety_and_one_data_channel() -> None:
    undeclared: list[str] = []
    for path in _tracked_notebooks():
        declared = _declaration(path)
        if set(declared) != DECLARATION_KEYS:
            undeclared.append(f"{path.name}: keys={sorted(declared)}")
            continue
        if declared["channel"] not in CHANNELS:
            undeclared.append(f"{path.name}: channel={declared['channel']!r}")
        undeclared.extend(
            f"{path.name}: {key} is empty" for key in sorted(DECLARATION_KEYS) if not str(declared[key]).strip()
        )

    assert undeclared == []


def test_a_live_or_frozen_artifact_notebook_carries_no_committed_output() -> None:
    committed: list[str] = []
    for path in _tracked_notebooks():
        if _declaration(path)["channel"] == COMMITTED_SNAPSHOT:
            continue
        committed.extend(
            f"{path.name}:cell{index}"
            for index, cell in enumerate(_cells(path))
            if cell.get("outputs") or cell.get("execution_count") is not None
        )

    assert committed == []


def test_a_committed_snapshot_notebook_keeps_the_outputs_that_are_its_evidence() -> None:
    silent = [
        path.name
        for path in _tracked_notebooks()
        if _declaration(path)["channel"] == COMMITTED_SNAPSHOT and not any(cell.get("outputs") for cell in _cells(path))
    ]

    assert silent == []


def test_a_committed_snapshot_notebook_was_run_in_order_from_a_fresh_kernel() -> None:
    """Committed outputs are evidence only if one pass produced all of them.

    `Restart Kernel and Run All Cells` — and `jupyter execute` — leave the code cells numbered 1..n
    in document order. A gap or an out-of-order number means one cell's output came from a kernel
    state the cell above it never saw, which is precisely the reading a committed number invites and
    cannot support.
    """
    out_of_order: list[str] = []
    for path in _tracked_notebooks():
        if _declaration(path)["channel"] != COMMITTED_SNAPSHOT:
            continue
        counts = [cell.get("execution_count") for cell in _cells(path) if cell["cell_type"] == "code"]
        if counts != list(range(1, len(counts) + 1)):
            out_of_order.append(f"{path.name}: execution_count={counts}")

    assert out_of_order == []


def test_a_committed_snapshot_notebook_reaches_nothing_outside_the_repository() -> None:
    """Channel C is the one channel allowed to commit output, because its input is committed too.

    A provider or database call in a channel-C cell would quietly break that: the committed numbers
    would depend on a moment nobody can replay, which is the exact defect the channel-A and
    channel-B strip rule exists to prevent — arriving through the door left open for evidence.
    """
    reaching: list[str] = []
    for path in _tracked_notebooks():
        if _declaration(path)["channel"] != COMMITTED_SNAPSHOT:
            continue
        for index, cell in enumerate(_cells(path)):
            if cell["cell_type"] != "code":
                continue
            source = _source(cell)
            reaching.extend(f"{path.name}:cell{index}:{site}" for site in NETWORKED_CALL_SITES if site in source)

    assert reaching == []


def test_no_notebook_commits_the_wall_clock_of_the_run_that_produced_it() -> None:
    """`metadata.execution` is per-cell wall-clock stamped by nbclient, and it changes every run.

    Committed, it makes the channel-C reproduction unreadable: `git diff` is never empty, so a
    timestamp change and an evidence change look alike, and in the primary checkout the dirty tree
    blocks `make deploy-image`. `notebooks/README.md` prescribes the reproduction that leaves it
    off; this is what keeps a plain `jupyter execute --inplace` from putting it back.
    """
    stamped: list[str] = []
    for path in _tracked_notebooks():
        stamped.extend(
            f"{path.name}:cell{index}"
            for index, cell in enumerate(_cells(path))
            if "execution" in cell.get("metadata", {})
        )

    assert stamped == []


def test_no_notebook_code_cell_names_a_write_capable_database_role() -> None:
    named: list[str] = []
    for path in _tracked_notebooks():
        for index, cell in enumerate(_cells(path)):
            if cell["cell_type"] != "code":
                continue
            source = _source(cell)
            named.extend(f"{path.name}:cell{index}:{role}" for role in WRITE_CAPABLE_ROLES if role in source)

    assert named == []
