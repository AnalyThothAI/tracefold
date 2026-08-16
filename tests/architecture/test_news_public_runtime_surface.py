from __future__ import annotations

import ast
import inspect
from pathlib import Path

from tracefold import news
from tracefold.news import NewsAcquisition, NewsSourceDefinition

ROOT = Path(__file__).resolve().parents[2]

PUBLIC_NEWS_INTERFACE = {
    "EventCategory",
    "NewsAcquisition",
    "NewsBriefCandidate",
    "NewsBriefPublisher",
    "NewsBriefSource",
    "NewsBriefStory",
    "NewsBriefStoryLine",
    "NewsBriefSynthesisResult",
    "NewsFeedEntry",
    "NewsFeedExpectedError",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsSourceDefinition",
    "NewsStoryProjection",
    "NewsStoryFactSnapshot",
    "NewsStoryProjectionWorker",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "PublicInsightsCategory",
    "PublicInsightsThreatLevel",
    "ThreatLevel",
    "build_story_projection",
}


def test_news_root_is_three_deep_capabilities_plus_public_models() -> None:
    assert set(news.__all__) == PUBLIC_NEWS_INTERFACE

    leaked_implementation = {
        "NewsInterface",
        "NewsProjectionService",
        "NewsProjectionSnapshot",
        "NewsRepository",
        "NewsStoryPush",
        "brief_system_prompt",
        "classify_by_keyword",
        "cluster_texts",
        "compute_news_story_projection",
        "importance_score",
        "opennews_source",
        "parse_brief_synthesis",
        "parse_opennews_message",
        "parse_opennews_rest_response",
        "rebuild_all_news_for_maintenance",
        "selection_fingerprint",
    }
    assert leaked_implementation.isdisjoint(news.__dict__)


def test_public_news_acquisition_owns_rss_and_opennews_inputs() -> None:
    assert (ROOT / "src/tracefold/integrations/news_feeds").is_dir()
    assert set(NewsSourceDefinition.model_fields) == {
        "source_id",
        "name",
        "tier",
        "lang",
        "source_kind",
        "enabled",
        "feed_url",
        "memberships",
        "refresh_interval_seconds",
    }

    parameters = inspect.signature(NewsAcquisition).parameters
    assert {
        "rss_sources",
        "rss_feed_reader",
        "rss_feed_parser",
        "opennews_source",
        "opennews_strategy_ids",
        "opennews_ws_client",
    } <= set(parameters)
    assert "opennews_rest_client" not in parameters


def test_opennews_production_has_no_subscription_or_rest_recovery_path() -> None:
    integration_root = ROOT / "src/tracefold/integrations/opennews"
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            integration_root / "__init__.py",
            integration_root / "client.py",
            ROOT / "src/tracefold/app/workers.py",
            ROOT / "src/tracefold/news/runtime.py",
        )
    )

    assert "news.subscribe" not in production_text
    assert "OpenNewsRestClient" not in production_text
    assert "opennews_rest_client" not in production_text


def test_news_root_has_no_compatibility_getattr() -> None:
    assert "__getattr__" not in news.__dict__


def test_news_has_no_shallow_interface_or_duplicate_story_rebuild() -> None:
    news_root = ROOT / "src/tracefold/news"

    assert not (news_root / "interface.py").exists()
    assert "def rebuild_stories(" not in (news_root / "repository.py").read_text(encoding="utf-8")


def test_story_v2_has_one_jaccard_identity_and_no_retired_kernel() -> None:
    news_root = ROOT / "src/tracefold/news"
    identity_sources = "\n".join(
        (news_root / name).read_text(encoding="utf-8").lower()
        for name in ("identity.py", "projection.py", "story_projection.py")
    )

    assert "jaccard" in identity_sources
    assert all(
        marker not in identity_sources for marker in ("cosine", "fnv", "cluster_texts", "union_find", "unionfind")
    )


def test_story_v2_private_decision_seams_do_not_leak_to_callers() -> None:
    news_root = ROOT / "src/tracefold/news"
    violations: list[str] = []
    for path in sorted(news_root.rglob("*.py")):
        if path.name == "story_projection.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not str(node.module or "").endswith("story_projection"):
                continue
            violations.extend(
                f"{path.relative_to(ROOT)}:{alias.name}" for alias in node.names if alias.name.startswith("_")
            )
    assert violations == []


def test_semantic_qualification_cannot_become_a_production_authority() -> None:
    production_root = ROOT / "src" / "tracefold"
    forbidden_import_roots = {"sentence_transformers", "sklearn", "torch", "transformers"}
    import_violations: list[str] = []
    marker_violations: list[str] = []
    builder_definitions: list[str] = []
    for path in sorted(production_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [str(node.module or "")]
            else:
                imported = []
            import_violations.extend(
                f"{path.relative_to(ROOT)}:{name}" for name in imported if name.split(".")[0] in forbidden_import_roots
            )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_story_projection":
                builder_definitions.append(str(path.relative_to(ROOT)))
        marker_violations.extend(
            f"{path.relative_to(ROOT)}:{marker}"
            for marker in ("news_story_semantic_qualification", "semantic_ablation", "linear_verifier")
            if marker in source
        )

    assert import_violations == []
    assert marker_violations == []
    assert builder_definitions == ["src/tracefold/news/story_projection.py"]


def test_story_projection_has_no_push_interface_or_candidate_writer() -> None:
    from tracefold.news.repository import NewsRepository

    assert "push_enabled" not in inspect.signature(news.NewsStoryProjection).parameters
    assert "push_enabled" not in inspect.signature(NewsRepository.publish_story_projection).parameters
    repository_source = (ROOT / "src/tracefold/news/repository.py").read_text(encoding="utf-8")
    story_store_source = (ROOT / "src/tracefold/news/story_store.py").read_text(encoding="utf-8")
    assert "insert_story_push_candidates" not in repository_source
    assert "insert_story_push_candidates" not in story_store_source


def test_opennews_item_writer_does_not_acquire_story_publication_lock() -> None:
    from tracefold.news.repository import NewsRepository

    source = inspect.getsource(NewsRepository.record_opennews_events)

    assert "lock_story_inputs" not in source


def test_news_brief_is_not_registered_as_a_generic_queue() -> None:
    queue_health = (ROOT / "src/tracefold/app/queue_health.py").read_text(encoding="utf-8")
    queue_ops = (ROOT / "src/tracefold/app/cli/commands/queue_ops.py").read_text(encoding="utf-8")

    assert "news_brief" not in queue_health
    assert "news_brief" not in queue_ops


def test_push_delivery_accepts_only_the_current_frozen_schema() -> None:
    adapter = (ROOT / "src/tracefold/integrations/news_push.py").read_text(encoding="utf-8")
    push_module = (ROOT / "src/tracefold/news/push.py").read_text(encoding="utf-8")

    assert 'source.get("schema_version") != PUSH_PAYLOAD_SCHEMA_VERSION' in adapter
    assert "legacy_delivery_payload" not in adapter
    assert "def prepare(" not in push_module
    assert "def deliver(" not in push_module
