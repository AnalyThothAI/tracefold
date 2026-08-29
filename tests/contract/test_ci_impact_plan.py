from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

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
        ("tests/news/test_news_v3_pure.py", {"quality-static", "python-hermetic"}),
        ("tests/integration/test_news_v3_pipeline.py", {"quality-static", "postgres-behavior"}),
        ("tests/integration/test_trading_migration.py", {"quality-static", "migration"}),
        ("tests/contract/test_hook_installer.py", {"quality-static", "runtime-process"}),
        ("tests/contract/test_openapi_codegen.py", {"quality-static", *ci_plan.FRONTEND_LANES}),
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


def test_new_test_module_fails_closed_but_a_tracked_domain_test_stays_risk_scoped() -> None:
    new_test = "tests/news/test_new_policy.py"
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("src/tracefold/news/events/facts.py", "tests/news/test_news_v3_pure.py"),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )
    new_test_plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=(new_test,),
        added_paths=(new_test,),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )

    assert plan["full"] is False
    assert _required(plan) == {
        "quality-static",
        "python-hermetic",
        "postgres-behavior",
        "runtime-process",
    }
    assert new_test_plan["full"] is True
    assert "new_test_module:tests/news/test_new_policy.py" in new_test_plan["full_reasons"]


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


def test_plan_verification_survives_the_canonical_sorted_json_round_trip() -> None:
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md",),
        tested_sha="1" * 40,
        base_sha="0" * 40,
    )
    reloaded = json.loads(json.dumps(plan, sort_keys=True))

    ci_plan.verify_plan(reloaded)


def test_git_change_discovery_keeps_both_sides_of_a_cross_surface_rename(tmp_path: Path) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ("git", *arguments),
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "ci-plan@example.invalid")
    git("config", "user.name", "CI Plan Test")
    source = tmp_path / "src" / "tracefold" / "trading" / "capital.py"
    source.parent.mkdir(parents=True)
    source.write_text("CAPITAL_AUTHORITY = True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "add capital authority")
    base_sha = git("rev-parse", "HEAD")
    target = tmp_path / "docs" / "capital.md"
    target.parent.mkdir()
    git("mv", source.relative_to(tmp_path).as_posix(), target.relative_to(tmp_path).as_posix())
    git("commit", "-qam", "move capital authority")
    head_sha = git("rev-parse", "HEAD")

    changed_paths = ci_plan.discover_changed_paths(tmp_path, base_sha=base_sha, head_sha=head_sha)
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=changed_paths,
        tested_sha=head_sha,
        base_sha=base_sha,
    )

    assert changed_paths == ("docs/capital.md", "src/tracefold/trading/capital.py")
    assert plan["full"] is True

    new_test = tmp_path / "tests" / "news" / "test_new_policy.py"
    new_test.parent.mkdir(parents=True)
    new_test.write_text("def test_new_policy(): assert True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "add a new test module")
    new_head_sha = git("rev-parse", "HEAD")
    all_changes, added_paths = ci_plan.discover_changes(
        tmp_path,
        base_sha=base_sha,
        head_sha=new_head_sha,
    )
    new_plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=all_changes,
        added_paths=added_paths,
        tested_sha=new_head_sha,
        base_sha=base_sha,
    )

    assert added_paths == ("tests/news/test_new_policy.py",)
    assert new_plan["full"] is True
    assert "new_test_module:tests/news/test_new_policy.py" in new_plan["full_reasons"]


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
