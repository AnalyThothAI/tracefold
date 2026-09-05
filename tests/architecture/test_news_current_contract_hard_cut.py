"""#369/#398: current News surfaces contain no retired contract path."""

from __future__ import annotations

import ast
import gzip
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

_ALL_SURFACES = (
    ROOT / "tracefold",
    ROOT / "web" / "src",
    ROOT / "web" / "tests",
    ROOT / "docs" / "generated",
    ROOT / "tests",
)
_NEWS_SURFACES = (
    ROOT / "tracefold" / "news",
    ROOT / "tracefold" / "app" / "http",
    ROOT / "tracefold" / "app" / "cli",
    ROOT / "web" / "src",
    ROOT / "web" / "tests",
    ROOT / "docs" / "generated",
    ROOT / "tests",
)
_TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".sql", ".ts", ".tsx"})

_RETIRED_VERSION_IDENTITIES = frozenset(
    {f"news_semantic_program_v{version}" for version in range(1, 8)}
    | {f"news_program_v{version}" for version in range(1, 8)}
    | {f"news_triage_policy_v{version}" for version in range(1, 11)}
    | {f"news_delivery_card_v{version}" for version in range(1, 11)}
    | {f"news_review_v{version}" for version in range(1, 6)}
    | {
        "news_liquidation_fact_v1",
        "news_liquidation_policy_v1",
        "news_oi_signal_v1",
        "news_reader_history_v1",
        "news_reader_history_v2",
        "told_context_selector_v1",
        "told_context_selector_v2",
        "told_context_selector_v3",
    }
)

_RETIRED_EVERYWHERE = (
    frozenset(
        {
            "event_type",
            "event_type_zh",
            "funnel_parsed_24h",
            "legacy_label",
            "legacy_reconstructed",
            "legacy_event_type",
            "LegacyTaxonomyProjectionV1",
            "project_legacy_event_type",
            "model_decision",
            "model_decision_zh",
            "display_title",
            "news_editorial_v1",
            "novelty_defaulted",
            "provider_cost_usd",
            "source_contract_unverified",
            "news_triage_model_unconfigured",
            "news_triage_output_invalid",
            "news_triage_output_truncated",
            "news_triage_timeout",
            "unclear_push_event_types",
        }
    )
    | _RETIRED_VERSION_IDENTITIES
)
_RETIRED_NEWS_ONLY = frozenset({"actionable"})
_RETIRED_WITH_DERIVATIVES = frozenset({"model_decision", "novelty_defaulted"})
_CURRENT_LEDGER_CONTRACT_FILES = (
    ROOT / "tracefold" / "news" / "reader_history.py",
    ROOT / "tracefold" / "news" / "told_context.py",
    ROOT / "tracefold" / "news" / "pipeline" / "delivery.py",
    ROOT / "tracefold" / "news" / "pipeline" / "triage_audit.py",
    ROOT / "tracefold" / "news" / "program" / "contracts.py",
)

# Historical bytes, migration inputs, and negative contract assertions stay inspectable, but each exception is
# one file plus one exact token. Ordinary fixtures never belong here.
_EXACT_ALLOWLIST: dict[str, frozenset[str]] = {
    "tracefold/platform/postgres/alembic/current_schema_20260831_0340.sql": frozenset(
        {
            "actionable",
            "display_title",
            "event_type",
            "event_type_zh",
            "legacy_event_type",
            "legacy_label",
            "legacy_reconstructed",
            "model_decision",
            "novelty_defaulted",
            "project_legacy_event_type",
            "provider_cost_usd",
            "unclear_push_event_types",
        }
    ),
    # #458 restates the whole `news_verdicts` judgment CHECK to change its OI branch, so the
    # forbidden-key list inside it names the same retired tokens the baseline dump does.
    "tracefold/platform/postgres/alembic/versions/20260901_0344_news_oi_push_cut.py": frozenset(
        {
            "actionable",
            "display_title",
            "event_type",
            "event_type_zh",
            "legacy_event_type",
            "legacy_label",
            "model_decision",
            "novelty_defaulted",
            "project_legacy_event_type",
            "provider_cost_usd",
            "unclear_push_event_types",
        }
    ),
    # #501 restates the same CHECK again to open its program-version literal to v9.
    "tracefold/platform/postgres/alembic/versions/20260902_0351_news_program_v9_judgment_check.py": frozenset(
        {
            "actionable",
            "display_title",
            "event_type",
            "event_type_zh",
            "legacy_event_type",
            "legacy_label",
            "model_decision",
            "novelty_defaulted",
            "project_legacy_event_type",
            "provider_cost_usd",
            "unclear_push_event_types",
        }
    ),
    # #504 restates the same CHECK once more to open its policy-version literal to v12.
    "tracefold/platform/postgres/alembic/versions/20260903_0352_news_policy_v12_judgment_check.py": frozenset(
        {
            "actionable",
            "display_title",
            "event_type",
            "event_type_zh",
            "legacy_event_type",
            "legacy_label",
            "model_decision",
            "novelty_defaulted",
            "project_legacy_event_type",
            "provider_cost_usd",
            "unclear_push_event_types",
        }
    ),
    # #523 restates it a fourth time to open the same literal to v13.
    "tracefold/platform/postgres/alembic/versions/20260903_0358_news_policy_v13_judgment_check.py": frozenset(
        {
            "actionable",
            "display_title",
            "event_type",
            "event_type_zh",
            "legacy_event_type",
            "legacy_label",
            "model_decision",
            "novelty_defaulted",
            "project_legacy_event_type",
            "provider_cost_usd",
            "unclear_push_event_types",
        }
    ),
    "tests/fixtures/news_baseline_calibration_v1.json.gz": frozenset(
        {
            "actionable",
            "decision",
            "event_type",
            "family",
            "news_triage_policy_v8",
            "title_zh",
            "unclear_push_event_types",
        }
    ),
    "tests/contract/test_news_http_contract.py": frozenset(
        {
            "actionable",
            "event_type",
            "event_type_zh",
            "funnel_parsed_24h",
            "legacy_reconstructed",
            "model_decision",
            "model_decision_zh",
            "novelty_defaulted",
        }
    ),
    "tests/contract/test_openapi_drift.py": frozenset(
        {
            "actionable",
            "event_type",
            "event_type_zh",
            "legacy_label",
            "model_decision",
            "model_decision_zh",
        }
    ),
    "tests/integration/test_news_candidate_evaluator.py": frozenset({"news_semantic_program_v1"}),
    "tests/integration/test_postgres_schema_runtime.py": frozenset({"family", "model_decision", "novelty_defaulted"}),
}

_TEST_SHAPE_ALLOWLIST: dict[str, frozenset[str]] = {}


def _text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def _files(roots: tuple[Path, ...]) -> set[Path]:
    return {
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix in _TEXT_SUFFIXES and path.resolve() != THIS_FILE
    }


def _is_test_source(path: Path) -> bool:
    return path.is_relative_to(ROOT / "tests") or path.is_relative_to(ROOT / "web" / "tests")


def _contains_token(text: str, token: str, *, test_source: bool = False) -> bool:
    if test_source and token == "actionable":
        return re.search(r"""["']actionable["']|\bactionable\s*[:=]|\.actionable\b""", text) is not None
    if token in _RETIRED_WITH_DERIVATIVES:
        return token in text
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text) is not None


def _literal_strings(node: ast.Dict | ast.Set | ast.List | ast.Tuple) -> set[str]:
    values = node.keys if isinstance(node, ast.Dict) else node.elts
    return {value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str)}


def _retired_test_shape_tokens(path: Path) -> set[str]:
    if path.suffix != ".py":
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    retired: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = _literal_strings(node)
            if "headline_zh" in keys:
                retired.update(keys & {"actionable", "decision", "event_type", "title_zh"})
            if {"title", "family"} <= keys or {"family", "decision", "limit"} <= keys:
                retired.add("family")
            if {"family", "decision", "limit"} <= keys:
                retired.add("decision")
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            values = _literal_strings(node)
            if "family" in values and {"event_id", "leader_title"} <= values:
                retired.add("family")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"INSERT\s+INTO\s+news_events\s*\([^)]*\bfamily\b", node.value, flags=re.IGNORECASE):
                retired.add("family")
    return retired


def _table_columns(schema: str, table: str) -> set[str]:
    section = schema.split(f"## `{table}`\n", 1)[1].split("\n## `", 1)[0]
    return set(re.findall(r"^\| `([^`]+)` \|", section, flags=re.MULTILINE))


def test_retired_news_contract_tokens_exist_only_at_exact_historical_boundaries() -> None:
    all_files = _files(_ALL_SURFACES)
    news_files = _files(_NEWS_SURFACES)
    scans = {token: all_files for token in _RETIRED_EVERYWHERE} | {token: news_files for token in _RETIRED_NEWS_ONLY}
    found_allowed_tokens: dict[str, set[str]] = {path: set() for path in _EXACT_ALLOWLIST}
    offenders: list[str] = []

    for token, paths in scans.items():
        for path in sorted(paths):
            text = _text(path)
            if not _contains_token(text, token, test_source=_is_test_source(path)):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if token in _EXACT_ALLOWLIST.get(relative, ()):
                found_allowed_tokens[relative].add(token)
            else:
                offenders.append(f"{relative}: {token}")

    archive_scan_tokens = _RETIRED_EVERYWHERE | _RETIRED_NEWS_ONLY
    for relative, allowed in _EXACT_ALLOWLIST.items():
        text = _text(ROOT / relative)
        offenders.extend(
            f"{relative}: {token}"
            for token in archive_scan_tokens
            if _contains_token(
                text,
                token,
                test_source=relative.startswith(("tests/", "web/tests/")),
            )
            and token not in allowed
        )
        for token in allowed:
            if _contains_token(text, token, test_source=relative.startswith(("tests/", "web/tests/"))):
                found_allowed_tokens[relative].add(token)

    assert offenders == []
    assert {path: frozenset(tokens) for path, tokens in found_allowed_tokens.items()} == _EXACT_ALLOWLIST


def test_runtime_does_not_know_a_retired_queue_name() -> None:
    """#407: the application declares the current topology and reports everything else.

    A retired name in runtime code is not documentation — it is either a queue this image would
    recreate or, worse, one it would delete on somebody's behalf. Both names stay readable in the
    operations history and in the migration test that proves they are only ever reported.
    """

    offenders = [
        f"{path.relative_to(ROOT).as_posix()}: {token}"
        for path in (ROOT / "tracefold").rglob("*.py")
        if not path.is_relative_to(ROOT / "tracefold" / "platform" / "postgres" / "alembic")
        for token in ("news.retry", "news.deep", "RETIRED_QUEUES", "REMOVED_RETRY_LANE")
        if _contains_token(_text(path), token)
    ]

    assert offenders == []


def test_ordinary_feed_reads_only_the_current_review_projection() -> None:
    feed = _text(ROOT / "tracefold" / "news" / "storage" / "feed.py")

    assert "news_review_records_v1" in feed
    assert "news_reviews" not in feed


def test_ordinary_news_reads_current_events_without_a_compatibility_projection() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "tracefold" / "news", ROOT / "tracefold" / "app" / "http")
        for path in root.rglob("*.py")
        if "news_current_events_v1" in _text(path)
    ]

    assert offenders == []


def test_live_surfaces_have_no_archive_compatibility_contract() -> None:
    tokens = (
        "current_contract_archive_only",
        "news_current_events_v1",
        "news_current_event_archive_guard",
        "news_event_archive_only",
        '"archive_only"',
    )
    roots = (
        ROOT / "tracefold" / "news",
        ROOT / "tracefold" / "app" / "http",
        ROOT / "web" / "src",
        ROOT / "docs" / "generated",
    )
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}: {token}"
        for path in sorted(_files(roots))
        for token in tokens
        if token in _text(path)
    ]

    assert offenders == []


def test_ordinary_tests_do_not_build_retired_news_shapes() -> None:
    found_allowed: dict[str, set[str]] = {path: set() for path in _TEST_SHAPE_ALLOWLIST}
    offenders: list[str] = []
    for path in sorted(_files((ROOT / "tests",))):
        relative = path.relative_to(ROOT).as_posix()
        for token in sorted(_retired_test_shape_tokens(path)):
            if token in _TEST_SHAPE_ALLOWLIST.get(relative, ()):
                found_allowed[relative].add(token)
            else:
                offenders.append(f"{relative}: {token}")

    assert offenders == []
    assert {path: frozenset(tokens) for path, tokens in found_allowed.items()} == _TEST_SHAPE_ALLOWLIST


def test_current_python_verdict_and_event_identity_are_exact() -> None:
    from tracefold.news.models import EVENT_IDENTITY_VERSION, TriageVerdict
    from tracefold.news.program.contracts import FrozenEventEvidence

    assert set(TriageVerdict.model_fields) == {
        "novelty",
        "restates",
        "assets",
        "direction",
        "scope",
        "magnitude",
        "confidence",
        "audience",
        "headline_zh",
        "why_zh",
    }
    assert TriageVerdict.model_config["extra"] == "forbid"
    assert EVENT_IDENTITY_VERSION == "news_event_identity_v6"
    assert "dedupe_family" in FrozenEventEvidence.model_fields
    assert "family" not in FrozenEventEvidence.model_fields
    assert FrozenEventEvidence.model_config["extra"] == "forbid"


def test_web_uses_timeline_title_only_at_the_timeline_owner() -> None:
    users = {
        path.relative_to(ROOT).as_posix()
        for path in _files((ROOT / "web" / "src",))
        if path.name != "openapi.ts" and _contains_token(path.read_text(encoding="utf-8"), "title_zh")
    }
    assert users == {"web/src/features/news/ui/detail/NewsTimeline.tsx"}


def test_current_ledger_contracts_have_no_short_or_pre_rename_field_aliases() -> None:
    aliases = ("family", "type", "sym", "m", "dir")
    offenders = [
        f"{path.relative_to(ROOT)}: {alias}"
        for path in _CURRENT_LEDGER_CONTRACT_FILES
        for alias in aliases
        if re.search(rf"[\"']{alias}[\"']", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_generated_news_api_has_only_current_typed_shapes() -> None:
    document = json.loads((ROOT / "docs" / "generated" / "openapi.json").read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    verdict = schemas["NewsVerdictData"]
    editorial = schemas["NewsModelEditorialData"]
    presentation = schemas["NewsPresentationVerdictData"]
    triage = schemas["NewsTriageSummaryData"]
    filters = schemas["NewsFeedFiltersData"]
    event = schemas["NewsEventData"]
    evidence = schemas["NewsEvidenceSnapshotData"]
    review = schemas["NewsAcceptedReviewData"]

    assert verdict["additionalProperties"] is False
    assert {"editorial", "trace", "prompt_version", "model_decision"}.isdisjoint(verdict["properties"])
    assert verdict["properties"]["verdict"] == {"$ref": "#/components/schemas/NewsPresentationVerdictData"}
    assert "verdict" in verdict["required"]
    assert presentation["additionalProperties"] is False
    assert set(presentation["properties"]) == {
        "novelty",
        "restates",
        "assets",
        "direction",
        "scope",
        "magnitude",
        "confidence",
        "audience",
        "headline_zh",
        "why_zh",
    }
    assert editorial["additionalProperties"] is False
    assert set(editorial["properties"]) == {"taxonomy", "relevance"}
    assert {
        "event_type",
        "event_type_zh",
        "actionable",
        "model_decision",
        "model_decision_zh",
        "title_zh",
    }.isdisjoint(triage["properties"])
    assert {"family", "decision", "channel"}.isdisjoint(filters["properties"])
    assert {"final_decision", "event_kind"}.issubset(filters["properties"])
    assert {"family", "dedupe_family"}.isdisjoint(event["properties"])
    assert evidence["properties"]["provenance"]["const"] == "observed"
    assert "snapshot" not in evidence["properties"]
    assert "legacy_label" not in review["properties"]
    assert {"payload", "dimensions", "novelty"}.isdisjoint(review["properties"])


def test_generated_news_schema_has_only_current_identity_columns() -> None:
    schema = (ROOT / "docs" / "generated" / "db-schema.md").read_text(encoding="utf-8")

    for table in ("news_events", "news_event_bands"):
        columns = _table_columns(schema, table)
        assert "dedupe_family" in columns
        assert "family" not in columns
    assert "current_contract_archive_only" not in _table_columns(schema, "news_events")
    assert "current_contract_archive_only" not in _table_columns(schema, "news_reviews")
    verdict_columns = _table_columns(schema, "news_verdicts")
    assert {"judgment_contract_version", "judgment_origin", "scored_judgment_sha256"}.issubset(verdict_columns)
    assert "model_decision" not in verdict_columns
