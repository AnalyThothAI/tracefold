#!/usr/bin/env python3
"""Execute one root-router task decision and emit a reviewable JSON receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__:
    from scripts import ci_plan
else:
    import ci_plan  # type: ignore[import-not-found]

_BEGIN = "<!-- BEGIN SHARED AGENT ROUTER -->"
_END = "<!-- END SHARED AGENT ROUTER -->"
_SURFACES = frozenset({"docs-only", "pure Python", "PostgreSQL", "frontend"})


def _shared_block(source: str) -> str:
    try:
        return source.split(_BEGIN, 1)[1].split(_END, 1)[0]
    except IndexError as exc:
        raise ValueError("agent_router_shared_block_missing") from exc


def _task_rows(shared: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in shared.splitlines():
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        if len(cells) != 5 or cells[0] not in _SURFACES:
            continue
        if cells[0] in rows:
            raise ValueError(f"agent_router_duplicate_surface:{cells[0]}")
        rows[cells[0]] = {
            "must_read": cells[1],
            "bootstrap": cells[2],
            "development_tests": cells[3],
            "completion_plan": cells[4],
        }
    if set(rows) != set(_SURFACES):
        raise ValueError("agent_router_acceptance_surfaces_incomplete")
    return rows


def _surface_for_path(changed_path: str) -> str:
    if changed_path.startswith("web/"):
        return "frontend"
    if changed_path.startswith("src/tracefold/platform/postgres/"):
        return "PostgreSQL"
    if changed_path.startswith("src/") and changed_path.endswith(".py"):
        return "pure Python"
    if changed_path.endswith(".md") and changed_path.startswith(("docs/", "notebooks/", "README")):
        return "docs-only"
    raise ValueError(f"agent_router_dry_run_surface_unknown:{changed_path}")


def build_receipt(
    root: Path,
    *,
    changed_path: str,
    tested_sha: str,
    base_sha: str,
) -> dict[str, Any]:
    shared_source = (root / "docs" / "agents" / "shared-router.md").read_text(encoding="utf-8")
    canonical = _shared_block(shared_source)
    conflicts: list[str] = []
    for router_name in ("AGENTS.md", "CLAUDE.md"):
        router = (root / router_name).read_text(encoding="utf-8")
        if _shared_block(router) != canonical:
            conflicts.append(f"shared_block_drift:{router_name}")
        appendix = router.split(_END, 1)[1]
        if "| Task surface |" in appendix:
            conflicts.append(f"duplicate_task_matrix:{router_name}")
    surface = _surface_for_path(changed_path)
    route = _task_rows(canonical)[surface]
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(changed_path,),
        tested_sha=tested_sha,
        base_sha=base_sha,
    )
    required_lanes = [lane for lane in ci_plan.REQUIRED_LANES if plan["lanes"][lane]["status"] == "required"]
    return {
        "schema_version": "tracefold_agent_task_dry_run_v1",
        "surface": surface,
        "changed_path": changed_path,
        **route,
        "required_lanes": required_lanes,
        "required_jobs": [job for job, required in plan["jobs"].items() if required],
        "full": plan["full"],
        "plan_sha256": plan["plan_sha256"],
        "tested_sha": tested_sha,
        "base_sha": base_sha,
        "router_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "router_conflicts": conflicts,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-path", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args(arguments)
    try:
        receipt = build_receipt(
            options.root,
            changed_path=options.changed_path,
            tested_sha=options.tested_sha,
            base_sha=options.base_sha,
        )
        if receipt["router_conflicts"]:
            raise ValueError("agent_router_conflict")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"agent task dry-run refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_receipt", "main"]
