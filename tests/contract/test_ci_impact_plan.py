from __future__ import annotations

import copy

import pytest

from scripts import ci_plan


def _required(plan: dict[str, object]) -> set[str]:
    lanes = plan["lanes"]
    assert isinstance(lanes, dict)
    return {name for name, decision in lanes.items() if decision["status"] == "required"}


def test_docs_only_plan_starts_no_external_resource_or_frontend_job() -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md", "docs/SETUP.md"),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )

    assert plan["full"] is False
    assert _required(plan) == {"quality-static"}
    assert plan["jobs"] == {
        "quality-static": True,
        "python-hermetic": False,
        "trust-root": False,
        "postgres-behavior": False,
        "migration": False,
        "runtime-process": False,
        "frontend": False,
    }
    for lane in ci_plan.RESOURCE_LANES | ci_plan.FRONTEND_LANES:
        assert plan["lanes"][lane]["status"] == "not_required"
        assert plan["lanes"][lane]["reason"]
    ci_plan.verify_plan(plan)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "src/tracefold/news/domain/event.py",
            {"quality-static", "python-hermetic", "postgres-behavior", "runtime-process"},
        ),
        (
            "src/tracefold/platform/postgres/database.py",
            {"quality-static", "python-hermetic", "postgres-behavior", "migration", "runtime-process"},
        ),
        ("web/src/routes/News.tsx", {"quality-static", *ci_plan.FRONTEND_LANES}),
    ],
)
def test_known_task_surface_selects_its_conservative_owner_lanes(path: str, expected: set[str]) -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(path,),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )

    assert plan["full"] is False
    assert _required(plan) == expected
    ci_plan.verify_plan(plan)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "Makefile",
        "uv.lock",
        "pyproject.toml",
        "tests/support/evidence.py",
        "web/tests/support/evidenceReporter.ts",
        "scripts/ci_plan.py",
        "scripts/require_main_ci.py",
        "tests/deploy/test_main_ci_gate.py",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/agents/shared-router.md",
        "docs/agents/worktrees.md",
        "docs/DEVELOPMENT.md",
        "src/tracefold/app/runtime.py",
        "src/tracefold/trading/domain.py",
        "unknown/new_surface.py",
        "tests/new_surface/test_unclassified.py",
    ],
)
def test_governance_capital_runtime_and_unknown_paths_fail_closed_to_full(path: str) -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(path,),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )

    assert plan["full"] is True
    assert _required(plan) == set(ci_plan.REQUIRED_LANES)
    assert plan["full_reasons"]
    ci_plan.verify_plan(plan)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "release"])
def test_main_release_and_manual_events_always_use_full_plan(event: str) -> None:
    plan = ci_plan.build_plan(
        event=event,
        changed_paths=("README.md",),
        tested_sha="1" * 40,
        base_sha=None,
    )

    assert plan["full"] is True
    assert _required(plan) == set(ci_plan.REQUIRED_LANES)
    ci_plan.verify_plan(plan)


def test_planner_error_fails_closed_to_full() -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(),
        tested_sha="1" * 40,
        base_sha="0" * 40,
        planner_error="git_diff_failed",
    )

    assert plan["full"] is True
    assert "planner_error:git_diff_failed" in plan["full_reasons"]
    ci_plan.verify_plan(plan)


def test_plan_digest_rejects_tampering() -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md",),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )
    tampered = copy.deepcopy(plan)
    tampered["lanes"]["runtime-process"]["reason"] = "tampered"

    with pytest.raises(ValueError, match="ci_plan_sha256_mismatch"):
        ci_plan.verify_plan(tampered)


def test_gate_requires_planned_jobs_and_rejects_unplanned_execution() -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md",),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )
    results = {job: ("success" if required else "skipped") for job, required in plan["jobs"].items()}

    ci_plan.validate_gate(plan, job_results=results, plan_result="success", aggregate_result="success")

    with pytest.raises(ValueError, match="ci_required_job_not_success:quality-static:skipped"):
        ci_plan.validate_gate(
            plan,
            job_results={**results, "quality-static": "skipped"},
            plan_result="success",
            aggregate_result="success",
        )
    with pytest.raises(ValueError, match="ci_not_required_job_not_skipped:runtime-process:success"):
        ci_plan.validate_gate(
            plan,
            job_results={**results, "runtime-process": "success"},
            plan_result="success",
            aggregate_result="success",
        )
    with pytest.raises(ValueError, match="ci_plan_job_not_success:failure"):
        ci_plan.validate_gate(
            plan,
            job_results=results,
            plan_result="failure",
            aggregate_result="success",
        )
