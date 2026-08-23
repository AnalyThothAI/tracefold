"""Issue #162 ratchets: stable seams, declarative package roots, and shrinking hotspots."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
BASELINE = ROOT / "docs" / "generated" / "refactor-baseline-9441ce99.json"
MAX_NEW_MODULE_LINES = 800
MAX_NEW_FUNCTION_LINES = 100

# Existing oversized modules may shrink or disappear, never grow. A rename without a split receives
# no exception: the target module is new and must fit the default budget.
GRANDFATHERED_MODULE_LINES = {
    "app/cli/commands/news.py": 1581,
    "app/http/schemas_news.py": 853,
    "news/agents/program_baseline.py": 1059,
    "news/agents/program_compiler.py": 804,
    "news/agents/program_compiler_launcher.py": 1334,
    "news/agents/program_compiler_proxy.py": 1139,
    "news/agents/program_compiler_proxy_sidecar.py": 151,
    "news/agents/program_compiler_runner.py": 255,
    "news/agents/program_compiler_sandbox.py": 825,
    "news/agents/program_compiler_security.py": 1307,
    "news/agents/program_compiler_source.py": 47,
    "news/agents/program_compiler_trusted.py": 408,
    "news/agents/program_metric.py": 1120,
    "news/agents/semantic_program.py": 3645,
    "news/candidate_evaluator.py": 3785,
    "news/consumers.py": 2075,
    "news/price_repository.py": 1048,
    "news/repository.py": 2588,
    "news/review.py": 2456,
    "news/semantic_contract.py": 902,
    "platform/config/settings.py": 889,
    "trading/pipeline.py": 1396,
    "trading/repository.py": 715,
}

# Function debt is identified by exact source path and qualified name. A structural PR that purely
# moves one of these functions must explicitly move its entry while preserving (or lowering) the limit;
# unrelated functions with the same generic name cannot inherit an exception.
GRANDFATHERED_FUNCTION_LINES = {
    ("app/cli/commands/news.py", "_handle_review_accept_drafts"): 105,
    ("app/cli/commands/news.py", "_handle_learning"): 697,
    ("app/cli/commands/news.py", "_handle_learning_baseline"): 121,
    ("app/cli/commands/news.py", "_handle_learning_draft_reviews"): 125,
    ("app/cli/commands/trading.py", "handle_trading"): 130,
    ("app/cli/parser.py", "build_parser"): 314,
    ("app/worker_database.py", "WorkerDatabase._run_executor"): 111,
    ("app/workers/root.py", "run_workers"): 262,
    ("app/workers/wiring/news.py", "_wire_news_pipeline"): 172,
    ("news/agents/program_baseline.py", "run_baseline"): 239,
    ("news/agents/program_baseline.py", "_build_report"): 124,
    ("news/agents/program_compiler.py", "ProgramCompiler.compile"): 163,
    ("news/agents/program_compiler_launcher.py", "ProgramCompilerLauncher.launch"): 398,
    ("news/agents/program_compiler_launcher.py", "_docker_container_boundary_payload"): 138,
    ("news/agents/program_compiler_runner.py", "_run"): 137,
    ("news/agents/program_compiler_security.py", "validate_compile_receipt_chain_v3"): 338,
    ("news/agents/program_compiler_trusted.py", "build_eligible_demo_bank"): 107,
    ("news/agents/program_metric.py", "accepted_review_metric"): 367,
    ("news/agents/semantic_program.py", "DspyNewsSemanticProgram._run_route"): 136,
    ("news/agents/semantic_program.py", "DspyNewsSemanticProgram._call_predictor"): 304,
    ("news/candidate_evaluator.py", "CandidateEvaluator.evaluate"): 198,
    ("news/candidate_evaluator.py", "CandidateEvaluator._validate_candidate_static"): 115,
    ("news/candidate_evaluator.py", "CandidateEvaluator._accepted_cases"): 139,
    ("news/candidate_evaluator.py", "CandidateEvaluator._run_sequential"): 184,
    ("news/candidate_evaluator.py", "CandidateEvaluator._run_shadow"): 138,
    ("news/candidate_evaluator.py", "CandidateEvaluator._collect_canary_observations"): 140,
    ("news/candidate_evaluator.py", "CandidateEvaluator._persist_program_call"): 212,
    ("news/candidate_evaluator.py", "CandidateEvaluator._evaluate_evidence"): 311,
    ("news/candidate_evaluator.py", "_observed_production_output"): 142,
    ("news/consumers.py", "TriageConsumer.handle"): 419,
    ("news/consumers.py", "TriageConsumer._judge_telemetry"): 126,
    ("news/eval/replay.py", "replay_hits"): 133,
    ("news/events.py", "admit_item"): 261,
    ("news/price_loops.py", "EventReactionLoop.turn"): 104,
    ("news/query_specs.py", "news_query_specs"): 145,
    ("news/repository.py", "NewsRepository.insert_event"): 103,
    ("news/repository.py", "NewsRepository.append_evidence_snapshot"): 178,
    ("news/repository.py", "NewsRepository.status_snapshot"): 116,
    ("news/review.py", "ReviewDesk._proposals"): 101,
    ("news/review.py", "ReviewDesk._coverage"): 142,
    ("news/review.py", "ReviewDesk._submit_external"): 132,
    ("news/timeline.py", "event_timeline"): 194,
    ("news/triage_rules.py", "decide"): 103,
    ("platform/postgres/runtime_roles.py", "runtime_role_contract"): 154,
    ("trading/pipeline.py", "CandidateRunner._freeze"): 107,
    ("trading/pipeline.py", "CandidateRunner._advance"): 144,
    ("trading/pipeline.py", "CandidateRunner._place"): 128,
    ("trading/pipeline.py", "ReconcileRunner._manage_open"): 122,
}

# These two aliases predate the declarative-root guard. They contain type expressions only and may
# disappear, but no additional package-root assignment receives an exception.
LEGACY_INIT_TYPE_ALIASES = {
    ("app/cli/commands/__init__.py", "CommandPayload"),
    ("app/cli/commands/__init__.py", "CommandResult"),
}
TYPE_EXPRESSION_NODES = (
    ast.Attribute,
    ast.BinOp,
    ast.BitOr,
    ast.Constant,
    ast.Load,
    ast.Name,
    ast.Subscript,
    ast.Tuple,
)

# News still has absolute self-imports and imports through its public root. Later ownership PRs remove
# these exact edges; no new module or root symbol may join the debt. Relative submodule imports are the
# target style and do not appear here. Trading starts at zero.
LEGACY_INTERNAL_ABSOLUTE_IMPORTS = {
    ("news/agents/program_compiler_proxy_sidecar.py", "tracefold.news.agents.program_compiler_proxy"),
    ("news/agents/program_compiler_proxy_sidecar.py", "tracefold.news.agents.program_compiler_source"),
    ("news/agents/program_compiler_proxy_sidecar.py", "tracefold.news.artifact_identity"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler_proxy"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler_sandbox"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler_security"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler_source"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_compiler_trusted"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.program_judge"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.agents.semantic_program"),
    ("news/agents/program_compiler_runner.py", "tracefold.news.artifact_identity"),
    ("news/agents/programs/candidates.py", "tracefold.news.candidate_evaluator"),
    ("news/canary.py", "tracefold.news.SemanticJudge"),
    ("news/candidate_evaluator.py", "tracefold.news.EditorialEnvelope"),
    ("news/candidate_evaluator.py", "tracefold.news.ScoredJudgment"),
    ("news/candidate_evaluator.py", "tracefold.news.SemanticJudge"),
    ("news/candidate_evaluator.py", "tracefold.news.SemanticJudgeError"),
    ("news/candidate_evaluator.py", "tracefold.news.TriageContext"),
    ("news/consumers.py", "tracefold.news.EditorialEnvelope"),
    ("news/consumers.py", "tracefold.news.ProgramTrace"),
    ("news/consumers.py", "tracefold.news.ProgramUsage"),
    ("news/consumers.py", "tracefold.news.ScoredJudgment"),
    ("news/consumers.py", "tracefold.news.SemanticJudge"),
    ("news/consumers.py", "tracefold.news.SemanticJudgeError"),
    ("news/consumers.py", "tracefold.news.TOLD_SOURCE_MAX"),
    ("news/consumers.py", "tracefold.news.TOLD_WINDOW_MS"),
    ("news/consumers.py", "tracefold.news.TriageContext"),
    ("news/eval/replay.py", "tracefold.news.events"),
    ("news/eval/replay.py", "tracefold.news.exact_atom_identity"),
    ("news/eval/replay.py", "tracefold.news.gate"),
    ("news/eval/replay.py", "tracefold.news.minhash"),
    ("news/eval/replay.py", "tracefold.news.models"),
    ("news/eval/replay.py", "tracefold.news.opennews"),
    ("news/eval/replay.py", "tracefold.news.storyline"),
    ("news/eval/replay.py", "tracefold.news.titles"),
    ("news/eval/replay.py", "tracefold.news.tokens"),
    ("news/query_specs.py", "tracefold.news.pricing"),
    ("news/recording_replay.py", "tracefold.news.SemanticJudgment"),
    ("news/recording_replay.py", "tracefold.news.TriageContext"),
}


def _production_files() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts and "alembic/versions" not in path.as_posix()
    )


class _FunctionSpans(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.spans: list[tuple[str, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        end = node.end_lineno or node.lineno
        self.spans.append((".".join((*self.stack, node.name)), end - node.lineno + 1))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def _declared_exports(path: Path) -> list[str] | None:
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                return sorted(str(item) for item in ast.literal_eval(node.value)) if node.value is not None else []
    return None


def _package_path(module: str) -> Path:
    parts = module.split(".")[1:]
    return SRC.joinpath(*parts, "__init__.py") if parts else SRC / "__init__.py"


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(("tracefold", *parts))


def _resolved_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    importer_parts = _module_name(path).split(".")
    package_parts = importer_parts if path.name == "__init__.py" else importer_parts[:-1]
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        return ""
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join((*package_parts[:keep], *suffix))


def _module_exists(module: str) -> bool:
    if not module.startswith("tracefold."):
        return False
    relative = module.split(".")[1:]
    return SRC.joinpath(*relative).with_suffix(".py").exists() or SRC.joinpath(*relative, "__init__.py").exists()


def _import_targets(path: Path, tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_from_module(path, node)
            if resolved:
                imports.add(resolved)
                imports.update(f"{resolved}.{alias.name}" for alias in node.names if alias.name != "*")
    return imports


def test_relative_import_targets_are_absolute_before_guard_checks() -> None:
    root = SRC / "app" / "workers" / "root.py"
    tree = ast.parse("from ... import news\nfrom ...integrations import RabbitMQBus\nfrom .. import learning_runtime\n")
    assert {
        "tracefold.news",
        "tracefold.integrations",
        "tracefold.app.learning_runtime",
    } <= _import_targets(root, tree)
    root_reexport = ast.parse("from . import SemanticJudge\n").body[0]
    assert isinstance(root_reexport, ast.ImportFrom)
    assert _resolved_from_module(SRC / "news" / "probe.py", root_reexport) == "tracefold.news"
    assert not _module_exists("tracefold.news.SemanticJudge")


def test_production_module_and_function_sizes_only_ratchet_down() -> None:
    violations: list[str] = []
    seen_modules: set[str] = set()
    seen_functions: set[tuple[str, str]] = set()
    for path in _production_files():
        relative = path.relative_to(SRC).as_posix()
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if relative in GRANDFATHERED_MODULE_LINES:
            seen_modules.add(relative)
            recorded_lines = GRANDFATHERED_MODULE_LINES[relative]
            if lines != recorded_lines:
                violations.append(f"{relative}:stale_module_ledger:{lines}!={recorded_lines}")
        elif lines > MAX_NEW_MODULE_LINES:
            violations.append(f"{relative}:module:{lines}>{MAX_NEW_MODULE_LINES}")

        spans = _FunctionSpans()
        spans.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for qualified_name, function_lines in spans.spans:
            function_key = (relative, qualified_name)
            if function_key in GRANDFATHERED_FUNCTION_LINES:
                seen_functions.add(function_key)
                recorded_lines = GRANDFATHERED_FUNCTION_LINES[function_key]
                if function_lines != recorded_lines:
                    violations.append(
                        f"{relative}:{qualified_name}:stale_function_ledger:{function_lines}!={recorded_lines}"
                    )
            elif function_lines > MAX_NEW_FUNCTION_LINES:
                violations.append(f"{relative}:{qualified_name}:{function_lines}>{MAX_NEW_FUNCTION_LINES}")
    violations.extend(
        f"missing_module_debt:{relative}" for relative in GRANDFATHERED_MODULE_LINES.keys() - seen_modules
    )
    violations.extend(
        f"missing_function_debt:{relative}:{qualified_name}"
        for relative, qualified_name in GRANDFATHERED_FUNCTION_LINES.keys() - seen_functions
    )
    assert violations == []


def test_business_modules_do_not_add_package_root_back_imports() -> None:
    actual: set[tuple[str, str]] = set()
    for package in ("news", "trading"):
        for path in sorted((SRC / package).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    resolved = _resolved_from_module(path, node)
                    package_root = f"tracefold.{package}"
                    if node.level == 0 and resolved == package_root:
                        actual.update(
                            (path.relative_to(SRC).as_posix(), f"{package_root}.{alias.name}") for alias in node.names
                        )
                    elif node.level == 0 and resolved.startswith(f"{package_root}."):
                        actual.add((path.relative_to(SRC).as_posix(), resolved))
                    elif node.level == 0 and resolved == "tracefold":
                        actual.update(
                            (path.relative_to(SRC).as_posix(), package_root)
                            for alias in node.names
                            if alias.name == package
                        )
                    elif node.level > 0 and resolved == package_root:
                        actual.update(
                            (path.relative_to(SRC).as_posix(), f"{package_root}.{alias.name}")
                            for alias in node.names
                            if not _module_exists(f"{package_root}.{alias.name}")
                        )
                elif isinstance(node, ast.Import):
                    actual.update(
                        (path.relative_to(SRC).as_posix(), alias.name)
                        for alias in node.names
                        if alias.name == f"tracefold.{package}" or alias.name.startswith(f"tracefold.{package}.")
                    )
    assert actual == LEGACY_INTERNAL_ABSOLUTE_IMPORTS


def test_package_exports_remain_at_the_frozen_public_seam() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    expected = baseline["historical_structure"]["package_exports"]
    actual = {module: _declared_exports(_package_path(module)) for module in expected}
    assert actual == expected
    for module in ("tracefold.news", "tracefold.trading"):
        tree = ast.parse(_package_path(module).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.FunctionDef) and node.name == "__getattr__" for node in tree.body)


def test_init_modules_are_declarative_once_the_workers_root_exists() -> None:
    target_root_exists = (SRC / "app" / "workers" / "root.py").exists()
    violations: list[str] = []
    for path in sorted(SRC.rglob("__init__.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative == "app/workers/__init__.py" and not target_root_exists:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for index, node in enumerate(tree.body):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
                    violations.append(f"{relative}:non_declarative_assignment:{node.lineno}")
                    continue
                target = targets[0].id
                if target == "__all__":
                    try:
                        exported = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        exported = None
                    if not isinstance(exported, (list, tuple)) or not all(isinstance(item, str) for item in exported):
                        violations.append(f"{relative}:non_literal_all:{node.lineno}")
                    continue
                if (relative, target) in LEGACY_INIT_TYPE_ALIASES and all(
                    isinstance(child, TYPE_EXPRESSION_NODES) for child in ast.walk(value)
                ):
                    continue
                try:
                    ast.literal_eval(value)
                except (ValueError, TypeError):
                    violations.append(f"{relative}:non_literal_assignment:{node.lineno}")
                continue
            violations.append(f"{relative}:{type(node).__name__}:{node.lineno}")
    assert violations == []


def test_target_workers_root_owns_lifecycle_but_not_capability_construction() -> None:
    root = SRC / "app" / "workers" / "root.py"
    if not root.exists():
        return  # PR 2 activates this target guard when the lifecycle module lands.
    forbidden = (
        "dspy",
        "tracefold.app.learning_runtime",
        "tracefold.app.llm",
        "tracefold.integrations",
        "tracefold.news",
        "tracefold.trading",
    )
    tree = ast.parse(root.read_text(encoding="utf-8"), filename=str(root))
    imports = _import_targets(root, tree)
    assert sorted(imported for imported in imports if imported.startswith(forbidden)) == []
