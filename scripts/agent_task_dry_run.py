#!/usr/bin/env python3
"""Execute one root-router task decision and emit a reviewable JSON receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__:
    from scripts import ci_plan
else:
    import ci_plan  # type: ignore[import-not-found]

_BEGIN = "<!-- BEGIN SHARED AGENT ROUTER -->"
_END = "<!-- END SHARED AGENT ROUTER -->"
_SURFACES = frozenset({"docs-only", "pure Python", "PostgreSQL", "frontend", "CI/evidence", "deploy/capital"})


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


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _identity_errors(root: Path, *, changed_path: str, tested_sha: str, base_sha: str) -> list[str]:
    errors: list[str] = []
    head = _git(root, "rev-parse", "HEAD")
    if tested_sha != head:
        errors.append("agent_router_tested_sha_not_head")
    expected_base = _git(root, "merge-base", "origin/main", "HEAD")
    if base_sha != expected_base:
        errors.append("agent_router_base_not_origin_main_merge_base")
    try:
        tracked = _git(root, "ls-files", "--error-unmatch", "--", changed_path)
    except subprocess.CalledProcessError:
        tracked = ""
    if tracked != changed_path or not (root / changed_path).is_file():
        errors.append("agent_router_path_not_tracked")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        errors.append("agent_router_worktree_dirty")
    return errors


def _plan_route_errors(surface: str, plan: dict[str, Any]) -> list[str]:
    required_jobs = {job for job, required in plan["jobs"].items() if required}
    expected_jobs = {
        "docs-only": {"quality-static"},
        "pure Python": {"quality-static", "python-hermetic", "postgres-behavior", "runtime-process"},
        "PostgreSQL": {
            "quality-static",
            "python-hermetic",
            "postgres-behavior",
            "migration",
            "runtime-process",
        },
        "frontend": {"quality-static", "frontend"},
        "CI/evidence": set(ci_plan.JOB_LANES),
        "deploy/capital": set(ci_plan.JOB_LANES),
    }[surface]
    expects_full = surface in {"CI/evidence", "deploy/capital"}
    if bool(plan["full"]) != expects_full or required_jobs != expected_jobs:
        return [f"route_plan_mismatch:{surface}"]
    return []


def build_receipt(
    root: Path,
    *,
    changed_path: str,
    tested_sha: str,
    base_sha: str,
) -> dict[str, Any]:
    identity_errors = _identity_errors(
        root,
        changed_path=changed_path,
        tested_sha=tested_sha,
        base_sha=base_sha,
    )
    if identity_errors:
        raise ValueError(";".join(identity_errors))
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
    surface = ci_plan.task_surface_for_path(changed_path)
    route = _task_rows(canonical)[surface]
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(changed_path,),
        tested_sha=tested_sha,
        base_sha=base_sha,
    )
    conflicts.extend(_plan_route_errors(surface, plan))
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
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"agent task dry-run refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_receipt", "main"]
