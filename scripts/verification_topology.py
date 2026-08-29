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
OWNERSHIP_RULES = (
    "external_codegen=>frontend-python",
    "verification self-test modules=>trust-root",
    "tests/integration/*_migration.py|tests/integration/test_postgres_schema_runtime.py=>migration",
    "tests/deploy|e2e|golden|slow and RabbitMQ integration modules=>runtime-process",
    "integration=>postgres-behavior",
    "default=>python-hermetic",
)
IMPACT_POLICY_FILES = ("scripts/ci_plan.py", "scripts/verification_topology.py")


def primary_lane_owner(path: str, markers: set[str]) -> str:
    """Return the one Phase-1 owner for a deterministic Python test item."""

    if "external_codegen" in markers:
        return "frontend-python"
    if path in TRUST_ROOT_MODULES:
        return "trust-root"
    if path.startswith("tests/integration/") and (
        path.endswith("_migration.py") or path == "tests/integration/test_postgres_schema_runtime.py"
    ):
        return "migration"
    if (
        path.startswith(("tests/deploy/", "tests/e2e/", "tests/golden/", "tests/slow/"))
        or markers & {"deploy", "e2e", "golden", "slow"}
        or path
        in {
            "tests/integration/test_cli_resources.py",
            "tests/integration/test_news_bus_rabbitmq.py",
        }
    ):
        return "runtime-process"
    if "integration" in markers:
        return "postgres-behavior"
    return "python-hermetic"


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
    "FRONTEND_LANES",
    "IMPACT_POLICY_FILES",
    "OWNERSHIP_RULES",
    "PYTHON_LANES",
    "REQUIRED_LANES",
    "TRUST_ROOT_MODULES",
    "impact_policy_sha256",
    "primary_lane_owner",
]
