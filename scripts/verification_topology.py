"""Single code-owned verification lane topology shared by planning and evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

PYTHON_LANES = (
    "python-hermetic",
    "postgres-behavior",
    "migration",
    "runtime-process",
    "frontend-python",
    "trust-root",
)
FRONTEND_LANES = frozenset(
    {
        "frontend-python",
        "frontend-typecheck",
        "frontend-lint",
        "frontend-architecture",
        "frontend-unit",
        "frontend-format",
        "frontend-build",
        "browser",
    }
)
REQUIRED_LANES = (
    "quality-static",
    *PYTHON_LANES,
    "frontend-typecheck",
    "frontend-lint",
    "frontend-architecture",
    "frontend-unit",
    "frontend-format",
    "frontend-build",
    "browser",
)
TRUST_ROOT_MODULES = frozenset(
    {
        "tests/architecture/test_docs_surface.py",
        "tests/architecture/test_test_resource_declarations.py",
        "tests/contract/test_ci_impact_plan.py",
        "tests/contract/test_evidence_v3_contract.py",
        "tests/contract/test_evidence_v2_v3_shadow_contract.py",
        "tests/contract/test_test_profile.py",
        "tests/contract/test_test_resources_contract.py",
        "tests/contract/test_verification_gate_contract.py",
        "tests/deploy/test_main_ci_gate.py",
        "tests/slow/test_frontend_harness_fail_closed.py",
    }
)
FRONTEND_PYTHON_MODULES = frozenset({"tests/contract/test_openapi_codegen.py"})
RUNTIME_PROCESS_MODULES = frozenset(
    {
        "tests/contract/test_hook_installer.py",
        "tests/integration/test_cli_resources.py",
        "tests/integration/test_nautilus_config.py",
        "tests/integration/test_news_bus_rabbitmq.py",
        "tests/integration/test_news_status_scale.py",
        "tests/integration/test_news_v3_price_scale.py",
        "tests/integration/test_workers_runtime_v2.py",
        "tests/test_workers_probe.py",
    }
)
OWNERSHIP_RULES = (
    "verification self-test module path=>trust-root",
    "OpenAPI codegen module path=>frontend-python",
    "tests/integration/*_migration.py|tests/integration/test_postgres_schema_runtime.py=>migration",
    "runtime module paths and tests/deploy|e2e|golden|slow=>runtime-process",
    "tests/integration module path=>postgres-behavior",
    "default=>python-hermetic",
)
IMPACT_POLICY_FILES = ("scripts/ci_plan.py", "scripts/verification_topology.py")
DECLARED_TEST_ROOTS = frozenset(
    {"architecture", "contract", "deploy", "e2e", "golden", "integration", "news", "slow", "trading"}
)


def is_declared_test_module(path: str) -> bool:
    parts = Path(path).parts
    if len(parts) == 2:
        return parts[0] == "tests" and parts[1].startswith("test_") and parts[1].endswith(".py")
    return (
        len(parts) == 3
        and parts[0] == "tests"
        and parts[1] in DECLARED_TEST_ROOTS
        and parts[2].startswith("test_")
        and parts[2].endswith(".py")
    )


def module_primary_lane_owner(path: str) -> str:
    """Return the stable primary owner for one deterministic Python test module."""

    if path in TRUST_ROOT_MODULES:
        return "trust-root"
    if path in FRONTEND_PYTHON_MODULES:
        return "frontend-python"
    if path.startswith("tests/integration/") and (
        path.endswith("_migration.py") or path == "tests/integration/test_postgres_schema_runtime.py"
    ):
        return "migration"
    if (
        path.startswith(("tests/deploy/", "tests/e2e/", "tests/golden/", "tests/slow/"))
        or path in RUNTIME_PROCESS_MODULES
    ):
        return "runtime-process"
    if path.startswith("tests/integration/"):
        return "postgres-behavior"
    return "python-hermetic"


def primary_lane_owner(path: str, markers: set[str]) -> str:
    """Return the path-owned lane; markers cannot silently move an item."""

    del markers
    return module_primary_lane_owner(path)


def impact_policy_sha256(root: Path) -> str:
    """Hash every file that defines path planning or lane ownership."""

    digest = hashlib.sha256()
    for relative_path in IMPACT_POLICY_FILES:
        path = root / relative_path
        if not path.is_file():
            return "not-applicable"
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "DECLARED_TEST_ROOTS",
    "FRONTEND_LANES",
    "FRONTEND_PYTHON_MODULES",
    "IMPACT_POLICY_FILES",
    "OWNERSHIP_RULES",
    "PYTHON_LANES",
    "REQUIRED_LANES",
    "RUNTIME_PROCESS_MODULES",
    "TRUST_ROOT_MODULES",
    "impact_policy_sha256",
    "is_declared_test_module",
    "module_primary_lane_owner",
    "primary_lane_owner",
]
