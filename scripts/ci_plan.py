#!/usr/bin/env python3
"""Build and verify Tracefold's conservative, fail-closed CI impact plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from scripts.verification_topology import (
        FRONTEND_LANES,
        PYTHON_LANES,
        REQUIRED_LANES,
        impact_policy_sha256,
        is_declared_test_module,
        module_primary_lane_owner,
    )
else:
    from verification_topology import (  # type: ignore[import-not-found]
        FRONTEND_LANES,
        PYTHON_LANES,
        REQUIRED_LANES,
        impact_policy_sha256,
        is_declared_test_module,
        module_primary_lane_owner,
    )

SCHEMA_VERSION = "tracefold_ci_plan_v1"
POLICY_VERSION = "tracefold_ci_impact_policy_v1"
RESOURCE_LANES = frozenset({"postgres-behavior", "migration", "runtime-process", "browser"})
JOB_LANES: dict[str, frozenset[str]] = {
    "quality-static": frozenset({"quality-static"}),
    "python-hermetic": frozenset({"python-hermetic"}),
    "trust-root": frozenset({"trust-root"}),
    "postgres-behavior": frozenset({"postgres-behavior"}),
    "migration": frozenset({"migration"}),
    "runtime-process": frozenset({"runtime-process"}),
    "frontend": FRONTEND_LANES,
}

_FULL_EVENTS = frozenset({"push", "workflow_dispatch", "release"})
_CORE_SPECS = frozenset(
    {
        "docs/ARCHITECTURE.md",
        "docs/CONTRACTS.md",
        "docs/DEVELOPMENT.md",
        "docs/FRONTEND.md",
        "docs/OPERATIONS.md",
        "docs/SECURITY.md",
    }
)
_GOVERNANCE_PATHS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        ".pre-commit-config.yaml",
        "web/package.json",
        "web/package-lock.json",
        "scripts/ci_plan.py",
        "scripts/verification_topology.py",
        "scripts/check_mandatory_docs_links.py",
        "scripts/agent_task_dry_run.py",
        "scripts/require_main_ci.py",
        "scripts/sync_agent_router.py",
        "docs/agents/shared-router.md",
        "docs/agents/worktrees.md",
    }
)
_HEX_SHA = re.compile(r"^[0-9a-f]{40}$")


def policy_sha256() -> str:
    """Bind plans to the exact code-owned planner implementation."""

    return impact_policy_sha256(Path(__file__).resolve().parents[1])


def build_plan(
    *,
    event: str,
    changed_paths: Sequence[str],
    added_paths: Sequence[str] = (),
    tested_sha: str,
    base_sha: str | None,
    planner_error: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic plan; uncertainty expands to the full plan."""

    normalized_paths = tuple(sorted({_normalize_path(path) for path in changed_paths if path.strip()}))
    normalized_added_paths = tuple(sorted({_normalize_path(path) for path in added_paths if path.strip()}))
    lane_reasons: dict[str, set[str]] = defaultdict(set)
    full_reasons: set[str] = set()
    if event in _FULL_EVENTS:
        full_reasons.add(f"event_requires_full:{event}")
    elif event != "pull_request":
        full_reasons.add(f"unknown_event:{event}")
    if planner_error:
        full_reasons.add(f"planner_error:{planner_error}")
    if not normalized_paths:
        full_reasons.add("empty_or_unavailable_change_set")
    if not set(normalized_added_paths) <= set(normalized_paths):
        full_reasons.add("added_path_not_in_change_set")

    lane_reasons["quality-static"].add("all_changes_require_quality")
    for path in normalized_paths:
        lanes, reason, requires_full = _classify_path(path, added=path in normalized_added_paths)
        if requires_full:
            full_reasons.add(reason)
        for lane in lanes:
            lane_reasons[lane].add(reason)

    full = bool(full_reasons)
    if full:
        for lane in REQUIRED_LANES:
            lane_reasons[lane].update(full_reasons)

    lanes_payload: dict[str, dict[str, str]] = {}
    for lane in REQUIRED_LANES:
        reasons = sorted(lane_reasons.get(lane, ()))
        if reasons:
            lanes_payload[lane] = {"status": "required", "reason": ";".join(reasons)}
        else:
            lanes_payload[lane] = {
                "status": "not_required",
                "reason": "no_changed_surface_requires_lane",
            }

    jobs = {
        job: any(lanes_payload[lane]["status"] == "required" for lane in owned_lanes)
        for job, owned_lanes in JOB_LANES.items()
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "policy_sha256": policy_sha256(),
        "event": event,
        "tested_sha": tested_sha,
        "base_sha": base_sha,
        "changed_paths": list(normalized_paths),
        "added_paths": list(normalized_added_paths),
        "full": full,
        "full_reasons": sorted(full_reasons),
        "lanes": lanes_payload,
        "jobs": jobs,
    }
    payload["plan_sha256"] = _payload_sha256(payload)
    verify_plan(payload)
    return payload


def verify_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("ci_plan_schema_invalid")
    if plan.get("policy_version") != POLICY_VERSION or plan.get("policy_sha256") != policy_sha256():
        raise ValueError("ci_plan_policy_mismatch")
    if not _HEX_SHA.fullmatch(str(plan.get("tested_sha", ""))):
        raise ValueError("ci_plan_tested_sha_invalid")
    base_sha = plan.get("base_sha")
    if base_sha is not None and not _HEX_SHA.fullmatch(str(base_sha)):
        raise ValueError("ci_plan_base_sha_invalid")
    changed_paths = plan.get("changed_paths")
    added_paths = plan.get("added_paths")
    if (
        not isinstance(changed_paths, list)
        or changed_paths != sorted(set(changed_paths))
        or not all(isinstance(path, str) and path for path in changed_paths)
    ):
        raise ValueError("ci_plan_changed_paths_invalid")
    if (
        not isinstance(added_paths, list)
        or added_paths != sorted(set(added_paths))
        or not all(isinstance(path, str) and path for path in added_paths)
        or not set(added_paths) <= set(changed_paths)
    ):
        raise ValueError("ci_plan_added_paths_invalid")
    lanes = plan.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != set(REQUIRED_LANES):
        raise ValueError("ci_plan_lanes_invalid")
    for lane, decision in lanes.items():
        if not isinstance(decision, dict) or decision.get("status") not in {"required", "not_required"}:
            raise ValueError(f"ci_plan_lane_status_invalid:{lane}")
        if not isinstance(decision.get("reason"), str) or not decision["reason"]:
            raise ValueError(f"ci_plan_lane_reason_missing:{lane}")
    if plan.get("full") is True and any(decision["status"] != "required" for decision in lanes.values()):
        raise ValueError("ci_plan_full_missing_lane")
    expected_jobs = {
        job: any(lanes[lane]["status"] == "required" for lane in owned_lanes) for job, owned_lanes in JOB_LANES.items()
    }
    if plan.get("jobs") != expected_jobs:
        raise ValueError("ci_plan_jobs_mismatch")
    if plan.get("plan_sha256") != _payload_sha256(plan):
        raise ValueError("ci_plan_sha256_mismatch")


def validate_gate(
    plan: Mapping[str, Any],
    *,
    job_results: Mapping[str, str],
    plan_result: str,
    aggregate_result: str,
) -> None:
    verify_plan(plan)
    if plan_result != "success":
        raise ValueError(f"ci_plan_job_not_success:{plan_result}")
    if set(job_results) != set(JOB_LANES):
        raise ValueError("ci_job_results_incomplete")
    jobs = plan["jobs"]
    for job in JOB_LANES:
        result = job_results[job]
        if jobs[job] and result != "success":
            raise ValueError(f"ci_required_job_not_success:{job}:{result}")
        if not jobs[job] and result != "skipped":
            raise ValueError(f"ci_not_required_job_not_skipped:{job}:{result}")
    if aggregate_result != "success":
        raise ValueError(f"ci_evidence_aggregate_not_success:{aggregate_result}")


def full_plan_receipt(plan: Mapping[str, Any]) -> str:
    verify_plan(plan)
    return (
        f"tracefold_ci_plan schema={SCHEMA_VERSION} full={str(plan['full']).lower()} "
        f"tested_sha={plan['tested_sha']} plan_sha256={plan['plan_sha256']}"
    )


def discover_changed_paths(root: Path, *, base_sha: str, head_sha: str) -> tuple[str, ...]:
    return discover_changes(root, base_sha=base_sha, head_sha=head_sha)[0]


def discover_changes(root: Path, *, base_sha: str, head_sha: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = subprocess.run(
        ("git", "diff", "--no-renames", "--name-status", "--diff-filter=ACDMRTUXB", f"{base_sha}...{head_sha}"),
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    changes: list[str] = []
    added: list[str] = []
    for line in result.stdout.splitlines():
        status, separator, path = line.partition("\t")
        if not separator or status not in {"A", "C", "D", "M", "R", "T", "U", "X", "B"} or not path:
            raise ValueError("ci_plan_git_diff_status_invalid")
        changes.append(path)
        if status == "A":
            added.append(path)
    return tuple(sorted(changes)), tuple(sorted(added))


def _classify_path(path: str, *, added: bool = False) -> tuple[frozenset[str], str, bool]:
    if path in _GOVERNANCE_PATHS or path in _CORE_SPECS or path.startswith("docs/agents/"):
        return frozenset(), f"governance_or_core_spec:{path}", True
    if path.startswith((".github/", "tests/support/", "web/tests/support/", "tests/browser/")):
        return frozenset(), f"verification_trust_root:{path}", True
    if path.startswith("web/") and (
        PurePosixPath(path).name.startswith(("eslint.config.", "playwright.", "vite.config."))
        or PurePosixPath(path).name in {"tsconfig.json", ".prettierrc.json", ".prettierignore"}
    ):
        return frozenset(), f"frontend_toolchain:{path}", True
    if path in {"Dockerfile", "compose.yaml", "alembic.ini"} or path.startswith("docker/"):
        return frozenset(), f"runtime_root:{path}", True
    if path.startswith(("src/tracefold/app/", "src/tracefold/trading/", "src/tracefold/integrations/")):
        return frozenset(), f"wiring_capital_or_runtime:{path}", True
    if path.startswith(("src/tracefold/platform/config/", "src/tracefold/platform/observability/")):
        return frozenset(), f"config_security_or_runtime:{path}", True
    if path.startswith("tests/") and path.endswith(".py"):
        if not is_declared_test_module(path):
            return frozenset(), f"unclassified_test_surface:{path}", True
        if added:
            return frozenset(), f"new_test_module:{path}", True
        owner = module_primary_lane_owner(path)
        if owner == "trust-root":
            return frozenset(), f"verification_trust_root:{path}", True
        if owner == "frontend-python":
            return frozenset({"quality-static", *FRONTEND_LANES}), f"test_owner:{owner}:{path}", False
        return frozenset({"quality-static", owner}), f"test_owner:{owner}:{path}", False
    if path.startswith("src/tracefold/platform/postgres/"):
        return (
            frozenset({"quality-static", "python-hermetic", "postgres-behavior", "migration", "runtime-process"}),
            f"postgresql_surface:{path}",
            False,
        )
    if path.startswith("src/tracefold/news/"):
        return (
            frozenset({"quality-static", "python-hermetic", "postgres-behavior", "runtime-process"}),
            f"python_domain_surface:{path}",
            False,
        )
    if path.startswith("web/"):
        return frozenset({"quality-static", *FRONTEND_LANES}), f"frontend_surface:{path}", False
    if path.startswith("web/tests/"):
        return frozenset({"quality-static", *FRONTEND_LANES}), f"frontend_test_surface:{path}", False
    if path.startswith("tests/"):
        return frozenset(), f"unclassified_test_surface:{path}", True
    if path.endswith(".md") and (path == "README.md" or path.startswith(("docs/", "notebooks/"))):
        return frozenset({"quality-static"}), f"ordinary_docs:{path}", False
    return frozenset(), f"unknown_surface:{path}", True


def task_surface_for_path(path: str) -> str:
    """Return the one root-router task surface for a normalized tracked path."""

    normalized = _normalize_path(path)
    if normalized.startswith(("src/tracefold/app/", "src/tracefold/trading/", "src/tracefold/integrations/")):
        return "deploy/capital"
    if normalized.startswith("src/tracefold/platform/postgres/"):
        return "PostgreSQL"
    if normalized.startswith("web/"):
        return "frontend"
    _, _, requires_full = _classify_path(normalized)
    if requires_full or normalized.startswith((".github/", "scripts/")):
        return "CI/evidence"
    if normalized.startswith("tests/"):
        return "test module"
    if normalized.startswith("src/") and normalized.endswith(".py"):
        return "pure Python"
    if normalized.endswith(".md") and normalized.startswith(("docs/", "notebooks/", "README")):
        return "docs-only"
    raise ValueError(f"agent_router_dry_run_surface_unknown:{normalized}")


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")
    if not normalized or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        return f"__invalid_path__/{hashlib.sha256(path.encode()).hexdigest()}"
    return normalized


def _payload_sha256(plan: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ci_plan_payload_invalid")
    verify_plan(payload)
    return payload


def _parse_job_results(values: Sequence[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        job, separator, result = value.partition("=")
        if not separator or job in results:
            raise ValueError(f"ci_job_result_invalid:{value}")
        results[job] = result
    return results


def _write_github_outputs(path: Path, plan: Mapping[str, Any]) -> None:
    lines = [f"plan_sha256={plan['plan_sha256']}", f"full={str(plan['full']).lower()}"]
    lines.extend(f"{job.replace('-', '_')}={str(required).lower()}" for job, required in plan["jobs"].items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--event", required=True)
    plan_parser.add_argument("--base-sha")
    plan_parser.add_argument("--head-sha", required=True)
    plan_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--github-output", type=Path)
    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--plan", type=Path, required=True)
    gate_parser.add_argument("--job-result", action="append", default=[])
    gate_parser.add_argument("--plan-result", required=True)
    gate_parser.add_argument("--aggregate-result", required=True)
    gate_parser.add_argument("--summary", type=Path)
    options = parser.parse_args(arguments)

    try:
        if options.command == "plan":
            changed_paths: tuple[str, ...] = ()
            added_paths: tuple[str, ...] = ()
            planner_error = None
            if options.event == "pull_request":
                try:
                    if not options.base_sha:
                        raise ValueError("base_sha_missing")
                    changed_paths, added_paths = discover_changes(
                        options.root,
                        base_sha=options.base_sha,
                        head_sha=options.head_sha,
                    )
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    planner_error = type(exc).__name__
            plan = build_plan(
                event=options.event,
                changed_paths=changed_paths,
                added_paths=added_paths,
                tested_sha=options.head_sha,
                base_sha=options.base_sha,
                planner_error=planner_error,
            )
            _write_json(options.output, plan)
            if options.github_output:
                _write_github_outputs(options.github_output, plan)
            print(full_plan_receipt(plan))
            return 0

        plan = _read_plan(options.plan)
        validate_gate(
            plan,
            job_results=_parse_job_results(options.job_result),
            plan_result=options.plan_result,
            aggregate_result=options.aggregate_result,
        )
        receipt = full_plan_receipt(plan)
        if options.summary:
            options.summary.write_text(f"## Tracefold CI plan\n\n`{receipt}`\n", encoding="utf-8")
        print(receipt)
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ci plan refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FRONTEND_LANES",
    "JOB_LANES",
    "PYTHON_LANES",
    "REQUIRED_LANES",
    "RESOURCE_LANES",
    "build_plan",
    "discover_changed_paths",
    "discover_changes",
    "full_plan_receipt",
    "main",
    "policy_sha256",
    "task_surface_for_path",
    "validate_gate",
    "verify_plan",
]
