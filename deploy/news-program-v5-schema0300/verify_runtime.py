"""Fail-closed identity and schema checks for the independent rollback image."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tracefold.news.agents.semantic_program import (
    PROGRAM_FACTORY_ID,
    PROGRAM_LEARNING_EPOCH,
    PROGRAM_VERSION,
    load_stable_program_artifact,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.platform.config.settings import NewsPolicySettings
from tracefold.platform.postgres.postgres_migrations import latest_migration_version

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "deploy/news-program-v5-schema0300/profile.json").read_text(encoding="utf-8"))


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def verify() -> None:
    artifact = load_stable_program_artifact()
    registry_path = ROOT / "src/tracefold/news/agents/programs/registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    program_root = registry_path.parent

    _require(PROFILE["learning_epoch"] == PROGRAM_LEARNING_EPOCH, "news_rollback_epoch_mismatch")
    _require(PROFILE["factory_id"] == PROGRAM_FACTORY_ID, "news_rollback_factory_mismatch")
    _require(PROFILE["program_version"] == PROGRAM_VERSION, "news_rollback_program_version_mismatch")
    _require(PROFILE["policy_version"] == TRIAGE_POLICY_VERSION, "news_rollback_policy_mismatch")
    _require(artifact.program_sha256 == PROFILE["program_sha256"], "news_rollback_program_sha_mismatch")
    _require(
        registry == {"images": [PROFILE["program_sha256"]], "stable": PROFILE["program_sha256"]},
        "news_rollback_registry_not_single_stable",
    )
    _require((program_root / PROFILE["program_sha256"]).is_dir(), "news_rollback_program_missing")
    _require(latest_migration_version() == PROFILE["migration_head"], "news_rollback_schema_head_mismatch")

    # The post-v6 operator config is a strict subset of v9's knobs.  Parsing it
    # must restore the reviewed v9 defaults rather than require compatibility
    # keys in the operator-owned file.
    policy = NewsPolicySettings.model_validate(
        {
            "restatement_drop": True,
            "similarity_max": 0.25,
            "listing_exempt_from_duplicate": True,
            "stale_source_max_age_s": 43_200,
        }
    )
    _require(policy.escalate_magnitude == 3, "news_rollback_v9_defaults_missing")

    repository = (ROOT / "src/tracefold/news/repository.py").read_text(encoding="utf-8")
    events = (ROOT / "src/tracefold/news/events.py").read_text(encoding="utf-8")
    query_specs = (ROOT / "src/tracefold/news/query_specs.py").read_text(encoding="utf-8")
    candidate = (ROOT / "src/tracefold/news/candidate_evaluator.py").read_text(encoding="utf-8")
    review = (ROOT / "src/tracefold/news/review.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "src/tracefold/platform/postgres/alembic/versions" / "20260823_0300_trade_relevance_program_v6.py"
    ).read_text(encoding="utf-8")

    for source in (repository, events, query_specs):
        _require(
            re.search(r"(?<![A-Za-z0-9_])e\.priority\b", source) is None,
            "news_rollback_legacy_event_column_query",
        )
    _require("admission, priority," not in repository, "news_rollback_legacy_event_column_insert")
    _require("SET admission = %s, priority = %s" not in repository, "news_rollback_legacy_event_column_update")
    _require(repository.count("queue_priority AS priority") >= 2, "news_rollback_public_priority_projection_missing")
    _require('card["priority"] = card["queue_priority"]' in repository, "news_rollback_v2_evidence_adapter_missing")
    _require(
        'event["priority"] = event["queue_priority"]' in candidate, "news_rollback_learning_evidence_adapter_missing"
    )
    _require('row.get("priority") or row.get("queue_priority")' in review, "news_rollback_review_view_adapter_missing")
    _require("RENAME COLUMN priority TO queue_priority" in migration, "news_rollback_migration_column_contract_missing")
    _require(
        "policy_version <> 'news_triage_policy_v10'" in migration, "news_rollback_v9_null_triplet_contract_missing"
    )


if __name__ == "__main__":
    verify()
    print(json.dumps(PROFILE, sort_keys=True, separators=(",", ":")))
