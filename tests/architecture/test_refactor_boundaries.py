"""Issue #162 ratchets: stable seams, declarative package roots, and shrinking hotspots."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
BASELINE = ROOT / "docs" / "generated" / "refactor-baseline-9441ce99.json"
MAX_NEW_MODULE_LINES = 800
MAX_NEW_FUNCTION_LINES = 100

# Existing oversized modules may shrink or disappear, never grow. A rename without a split receives
# no exception: the target module is new and must fit the default budget.
GRANDFATHERED_MODULE_LINES = {
    "news/learning/baseline.py": 1059,
    "news/learning/compiler/root.py": 804,
    "news/learning/compiler/launcher.py": 1332,
    "news/learning/compiler/proxy.py": 1139,
    "news/learning/compiler/proxy_sidecar.py": 151,
    "news/learning/compiler/runner.py": 255,
    "news/learning/compiler/sandbox.py": 825,
    "news/learning/compiler/security.py": 1307,
    # `source_identity.py` left the ledger in PR8-A: at 52 lines it is not oversized debt, and pinning
    # it exactly meant a 5-line bug fix read as a ratchet violation. The 800-line default now covers it.
    "news/learning/compiler/trusted.py": 408,
    "news/learning/metric.py": 1120,
    "news/program/graph.py": 3645,
    "news/learning/evaluator.py": 3673,
    # PR4 moved the 823-line TriageConsumer atomically; PR7-B3 split `handle` into named phases and
    # moved the route's typed vocabulary to `triage_route.py`. Still over budget, still only shrinking.
    "news/pipeline/triage.py": 937,
    "news/learning/review.py": 2456,
    # #173 added the `product_progress` TradeChannel and its rationale: +2 enum lines, +4 comment lines.
    "news/program/contracts.py": 908,
}

# Function debt is identified by exact source path and qualified name. A structural PR that purely
# moves one of these functions must explicitly move its entry while preserving (or lowering) the limit;
# unrelated functions with the same generic name cannot inherit an exception.
GRANDFATHERED_FUNCTION_LINES = {
    ("app/cli/commands/news_review.py", "_handle_review_accept_drafts"): 105,
    ("app/cli/commands/news_learning.py", "_handle_learning"): 696,
    ("app/cli/commands/news_learning_baseline.py", "_handle_learning_draft_reviews"): 125,
    ("app/cli/commands/trading.py", "handle_trading"): 130,
    ("app/cli/parser.py", "build_parser"): 314,
    ("app/worker_database.py", "WorkerDatabase._run_executor"): 111,
    ("app/workers/root.py", "run_workers"): 262,
    ("news/learning/baseline.py", "run_baseline"): 239,
    ("news/learning/baseline.py", "_build_report"): 124,
    ("news/learning/compiler/root.py", "ProgramCompiler.compile"): 163,
    ("news/learning/compiler/launcher.py", "ProgramCompilerLauncher.launch"): 398,
    ("news/learning/compiler/launcher.py", "_docker_container_boundary_payload"): 138,
    ("news/learning/compiler/runner.py", "_run"): 137,
    ("news/learning/compiler/security.py", "validate_compile_receipt_chain_v3"): 338,
    ("news/learning/compiler/trusted.py", "build_eligible_demo_bank"): 107,
    ("news/learning/metric.py", "accepted_review_metric"): 367,
    ("news/program/graph.py", "DspyNewsSemanticProgram._run_route"): 136,
    ("news/program/graph.py", "DspyNewsSemanticProgram._call_predictor"): 304,
    ("news/learning/evaluator.py", "CandidateEvaluator.evaluate"): 198,
    ("news/learning/evaluator.py", "CandidateEvaluator._validate_candidate_static"): 115,
    ("news/learning/evaluator.py", "CandidateEvaluator._accepted_cases"): 139,
    ("news/learning/evaluator.py", "CandidateEvaluator._run_sequential"): 184,
    ("news/learning/evaluator.py", "CandidateEvaluator._run_shadow"): 138,
    ("news/learning/evaluator.py", "CandidateEvaluator._collect_canary_observations"): 140,
    ("news/learning/evaluator.py", "CandidateEvaluator._persist_program_call"): 212,
    ("news/learning/evaluator.py", "CandidateEvaluator._evaluate_evidence"): 311,
    ("news/learning/evaluator.py", "_observed_production_output"): 142,
    # PR7-B3: 419 -> 81. `handle` now owns the broker sequence and the one stale re-ask loop; every
    # phase it names is its own function under the 100-line default.
    ("news/pipeline/triage.py", "TriageConsumer.handle"): 83,
    ("news/pipeline/triage.py", "TriageConsumer._judge_telemetry"): 126,
    ("news/eval/replay.py", "replay_hits"): 133,
    ("news/pipeline/admission.py", "admit_item"): 250,
    ("news/market_review/loops.py", "EventReactionLoop.turn"): 104,
    ("news/query_specs.py", "news_query_specs"): 145,
    ("news/storage/events.py", "EventStorage.insert_event"): 103,
    ("news/storage/events.py", "EventStorage.append_evidence_snapshot"): 178,
    ("news/storage/feed.py", "FeedStorage.status_snapshot"): 116,
    ("news/learning/review.py", "ReviewDesk._proposals"): 101,
    ("news/learning/review.py", "ReviewDesk._coverage"): 142,
    ("news/learning/review.py", "ReviewDesk._submit_external"): 132,
    ("news/timeline.py", "event_timeline"): 194,
    ("news/triage_rules.py", "decide"): 103,
    ("platform/postgres/runtime_roles.py", "runtime_role_contract"): 154,
    ("trading/pipeline/candidate.py", "CandidateRunner._freeze"): 103,
    ("trading/pipeline/candidate.py", "CandidateRunner._advance"): 144,
    ("trading/pipeline/candidate.py", "CandidateRunner._place"): 128,
    ("trading/pipeline/reconcile.py", "ReconcileRunner._manage_open"): 122,
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

# The cold compiler subprocess still uses absolute module identities as part of its explicit launch
# protocol. Runtime News and Trading otherwise use relative owner imports and start at zero package-root
# back-import debt.
LEGACY_INTERNAL_ABSOLUTE_IMPORTS = {
    ("news/learning/compiler/proxy_sidecar.py", "tracefold.news.learning.compiler.proxy"),
    ("news/learning/compiler/proxy_sidecar.py", "tracefold.news.learning.compiler.source_identity"),
    ("news/learning/compiler/proxy_sidecar.py", "tracefold.news.artifact_identity"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.root"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.proxy"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.sandbox"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.security"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.source_identity"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.compiler.trusted"),
    ("news/learning/compiler/runner.py", "tracefold.news.learning.judge"),
    ("news/learning/compiler/runner.py", "tracefold.news.program.graph"),
    ("news/learning/compiler/runner.py", "tracefold.news.artifact_identity"),
    ("news/program/resources/candidates.py", "tracefold.news.learning.contracts"),
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


def test_news_pipeline_split_has_no_compatibility_aliases() -> None:
    assert not (SRC / "news" / "consumers.py").exists()
    assert not (SRC / "news" / "events.py").exists()
    assert {
        "admission.py",
        "delivery.py",
        "maintenance.py",
        "receiver.py",
        "recovery.py",
        "root.py",
        "runtime.py",
        "triage.py",
        "triage_audit.py",
    } <= {path.name for path in (SRC / "news" / "pipeline").glob("*.py")}


def test_news_event_storage_and_market_review_splits_have_no_compatibility_aliases() -> None:
    retired = {
        "exact_atom_identity.py",
        "facts.py",
        "gate.py",
        "identity.py",
        "instruments.py",
        "instruments_repository.py",
        "minhash.py",
        "price_loops.py",
        "price_repository.py",
        "pricing.py",
        "repository.py",
        "storyline.py",
        "titles.py",
        "tokens.py",
    }
    assert sorted(path.name for path in (SRC / "news").iterdir() if path.name in retired) == []
    assert {
        "facts.py",
        "gate.py",
        "identity.py",
        "javascript_text.py",
        "minhash.py",
        "storyline.py",
        "titles.py",
        "tokens.py",
    } == {path.name for path in (SRC / "news" / "events").glob("*.py") if path.name != "__init__.py"}
    assert {"events.py", "decisions.py", "feed.py", "trade_projection.py"} <= {
        path.name for path in (SRC / "news" / "storage").glob("*.py")
    }
    assert {"instruments.py", "pricing.py", "loops.py", "storage.py"} <= {
        path.name for path in (SRC / "news" / "market_review").glob("*.py")
    }


def test_trading_ownership_split_has_no_compatibility_aliases() -> None:
    retired = {
        "blacklist.py",
        "candidates.py",
        "decision_program.py",
        "models.py",
        "order.py",
        "paper.py",
        "pipeline.py",
        "policy.py",
        "regime.py",
        "repository.py",
    }
    assert sorted(path.name for path in (SRC / "trading").iterdir() if path.name in retired) == []
    assert {"candidate.py", "reconcile.py", "root.py", "runtime.py"} == {
        path.name for path in (SRC / "trading" / "pipeline").glob("*.py") if path.name != "__init__.py"
    }
    assert {"cases.py", "control.py", "orders.py", "queries.py", "root.py"} <= {
        path.name for path in (SRC / "trading" / "storage").glob("*.py")
    }


def test_postgres_modules_have_owner_names_without_compatibility_aliases() -> None:
    postgres = SRC / "platform" / "postgres"
    assert sorted(path.name for path in postgres.glob("postgres_*.py")) == []
    assert {"audit.py", "client.py", "migrations.py"} <= {path.name for path in postgres.glob("*.py")}

    retired_imports = tuple(f"postgres_{suffix}" for suffix in ("audit", "client", "migrations"))
    # `deploy/news-program-v5-schema0301` is copied into a pinned pre-refactor source tree and must
    # continue importing that historical module name; it is an independent rollback image, not a
    # compatibility path in the current runtime.
    code_roots = (SRC, ROOT / "tests", ROOT / "scripts")
    files = [path for root in code_roots for path in root.rglob("*") if path.is_file()]
    violations = [
        path.relative_to(ROOT).as_posix()
        for path in files
        if path.suffix in {"", ".py"}
        and path != BASELINE
        and any(retired in path.read_text(encoding="utf-8") for retired in retired_imports)
    ]

    # One Makefile command executes only inside the pinned pre-refactor rollback image. The other
    # inspects an arbitrary deployable image and selects its new or pinned-old module name without
    # adding a compatibility module to the current source. Host/source probes use only the new path.
    makefile_lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    historical_image_probes = [
        line
        for line in makefile_lines
        if any(retired in line for retired in retired_imports)
        and (
            "PROGRAM_FACTORY_ID" in line or ("image_head=$$(docker run" in line and "importlib.util.find_spec" in line)
        )
    ]
    assert len(historical_image_probes) == 2
    violations.extend(
        f"Makefile:{line_number}"
        for line_number, line in enumerate(makefile_lines, start=1)
        if any(retired in line for retired in retired_imports) and line not in historical_image_probes
    )
    assert violations == []


def test_news_domain_modules_do_not_reach_through_repository_connections() -> None:
    violations = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((SRC / "news").rglob("*.py"))
        if "repos.conn" in path.read_text(encoding="utf-8")
    ]
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


def test_package_exports_remain_at_the_intentional_public_seam() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    expected = baseline["historical_structure"]["package_exports"]
    expected["tracefold.news"] = [
        "EditorialEnvelope",
        "NewsFeedEntry",
        "OpenNewsEvent",
        "OpenNewsExpectedError",
        "OpenNewsHistoryError",
        "OpenNewsStrategyHistory",
        "ProgramTrace",
        "ProgramUsage",
        "ReaderCardSemanticView",
        "ReaderReceipt",
        "ScoredJudgment",
        "SemanticJudge",
        "SemanticJudgeError",
        "SemanticJudgment",
        "TradeRelevanceV1",
        "TriageContext",
        "TriageVerdict",
    ]
    expected["tracefold.trading"] = ["Bar", "ExecutionReceipt", "InstrumentRef", "PreparedOrder", "TradingMode"]
    # #162 PR8-B renamed the package the Program lives in. The frozen baseline is a record of revision
    # 9441ce99 and is never regenerated, so the rename is declared here: the *export surface* it names
    # (an empty `__all__`) still has to hold, at the new path.
    renamed = {"tracefold.news.agents": "tracefold.news.program"}
    actual = {module: _declared_exports(_package_path(renamed.get(module, module))) for module in expected}
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


# ---------------------------------------------------------------------------- PR7-B4 guards
# `tracefold.app` decides how capabilities are assembled and run; it never decides what a business fact
# means. Reads are legitimate at the composition seam — the News -> Trading projection is one — but a
# write is an ownership claim, and business writes belong to the owning package's storage.
APP_ROOT = SRC / "app"
BUSINESS_WRITE_SQL_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>(?:news|trading)_[a-z0-9_]*)",
    re.IGNORECASE,
)

# The exact modules that may import DSPy. The old rule allowed the whole `workers/wiring` directory,
# which meant News or Market Review wiring could have started importing it without anything failing.
DSPY_WIRING_OWNERS = {"app/workers/wiring/trading.py"}

# `Any` at a seam is debt with a reason: a repository session is an App aggregate the business packages
# may not name, and a provider payload is whatever the venue sent. The counts are exact so the number
# can only fall — a new `Any` on one of these boundaries has to be argued for here.
ANY_DEBT_LEDGER = {
    # `Callable[[Any], T]`: the repository session the port hands the caller back is App-owned.
    "news/pipeline/runtime.py": 4,
    "trading/pipeline/runtime.py": 4,
    "app/workers/wiring/database.py": 10,
    # `verdict` and `grounded_assets` are jsonb documents; `conn` is the psycopg connection.
    "news/storage/trade_projection.py": 5,
    "app/workers/wiring/news_to_trading.py": 2,
    "app/workers/wiring/news.py": 1,
    # Operator settings and the venue adapter factories: pydantic models and provider callables the
    # composition root only forwards. Real debt, and the reason it is written down rather than waived.
    "app/workers/wiring/market_review.py": 10,
    "app/workers/wiring/trading.py": 4,
    "app/workers/task_contract.py": 0,
    "app/workers/wiring/components.py": 0,
}

# Where one context's facts become another's inputs. A bare dictionary here is exactly the untyped
# pass-through PR7-B2 removed, so it may not come back.
CROSS_CONTEXT_BOUNDARY_MODULES = (
    "app/workers/wiring/news_to_trading.py",
    "news/pipeline/runtime.py",
    "news/storage/trade_projection.py",
    "trading/pipeline/runtime.py",
)
BARE_DICT_MARKERS = ("dict[str, Any]", "Mapping[str, Any]")


def _module_name_of(path: Path) -> str:
    return _module_name(path)


def _top_level_import_targets(path: Path, tree: ast.Module) -> set[str]:
    """Imports that execute (or are declared) at module scope.

    A deferred import inside a function body is the documented way this repository breaks an import
    cycle, so it is not one. A `TYPE_CHECKING` block is still a declared design dependency and counts.
    """

    statements: list[ast.stmt] = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.If):
            statements.extend(node.body)
    imports: set[str] = set()
    for node in statements:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_from_module(path, node)
            if not resolved or not _module_exists(resolved):
                continue
            imports.add(resolved)
            imports.update(
                candidate
                for alias in node.names
                if alias.name != "*"
                if _module_exists(candidate := f"{resolved}.{alias.name}")
            )
    return imports


def _internal_import_graph(package: str) -> dict[str, set[str]]:
    prefix = f"tracefold.{package}."
    graph: dict[str, set[str]] = {}
    for path in _production_files():
        if path.relative_to(SRC).parts[0] != package:
            continue
        module = _module_name_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        graph[module] = {imported for imported in _top_level_import_targets(path, tree) if imported.startswith(prefix)}
    return {module: {target for target in targets if target in graph} for module, targets in graph.items()}


def _import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module, plus any self-import."""

    order: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    counter = 0

    def visit(root: str) -> None:
        nonlocal counter
        work: list[tuple[str, list[str]]] = [(root, sorted(graph[root]))]
        order[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                target = pending.pop()
                if target not in order:
                    order[target] = low[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, sorted(graph[target])))
                elif target in on_stack:
                    low[node] = min(low[node], order[target])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == order[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.append(sorted(component))

    for module in sorted(graph):
        if module not in order:
            visit(module)
    cycles.extend([module] for module in sorted(graph) if module in graph[module])
    return cycles


def test_app_composes_capabilities_but_never_writes_their_facts() -> None:
    """#162 PR7-B4: business SQL writes live in the owning package's storage, never at the seam."""

    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{table}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] == "app"
        for table in BUSINESS_WRITE_SQL_RE.findall(path.read_text(encoding="utf-8"))
    ]
    assert violations == []
    assert BUSINESS_WRITE_SQL_RE.findall("INSERT INTO news_learning_artifacts (a) VALUES (1)") == [
        "news_learning_artifacts"
    ]
    assert BUSINESS_WRITE_SQL_RE.findall("UPDATE workers_runtime SET x = 1") == []


def test_the_three_owning_packages_have_acyclic_internal_import_graphs() -> None:
    """A cycle inside a package means its modules cannot be read, tested or moved one at a time."""

    cycles = {package: _import_cycles(_internal_import_graph(package)) for package in ("app", "news", "trading")}
    assert cycles == {"app": [], "news": [], "trading": []}
    # The detector must actually detect: a two-node loop is a cycle, a diamond is not.
    assert _import_cycles({"a": {"b"}, "b": {"a"}}) == [["a", "b"]]
    assert _import_cycles({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}) == []


def test_dspy_wiring_is_owned_by_named_modules_not_a_directory() -> None:
    """Only the Trading decision wiring builds an LM here; News wiring goes through `learning_runtime`."""

    wiring = SRC / "app" / "workers" / "wiring"
    importers = {
        path.relative_to(SRC).as_posix()
        for path in sorted(wiring.rglob("*.py"))
        if "dspy" in {imported.split(".")[0] for imported in _import_targets(path, ast.parse(path.read_text("utf-8")))}
    }
    assert importers == DSPY_WIRING_OWNERS


def test_seam_any_debt_only_shrinks() -> None:
    """An exact ledger, not a threshold: a new `Any` on a declared boundary has to be argued for here."""

    actual = {
        relative: sum(
            1
            for node in ast.walk(ast.parse((SRC / relative).read_text(encoding="utf-8")))
            if isinstance(node, ast.Name) and node.id == "Any"
        )
        for relative in ANY_DEBT_LEDGER
    }
    assert actual == ANY_DEBT_LEDGER


def test_the_cross_context_boundary_carries_no_bare_dictionaries() -> None:
    """PR7-B2 replaced `dict[str, Any]` pass-through with named row contracts; it may not return."""

    violations = [
        f"{relative}:{marker}"
        for relative in CROSS_CONTEXT_BOUNDARY_MODULES
        for marker in BARE_DICT_MARKERS
        if marker in (SRC / relative).read_text(encoding="utf-8")
    ]
    assert violations == []


def test_the_news_to_trading_mapper_names_the_projection_version_it_was_written_against() -> None:
    """A News projection that changes what a field *means* keeps its keys; only the version moves."""

    from tracefold.app.workers.wiring.news_to_trading import MAPPED_NEWS_PROJECTION_VERSION
    from tracefold.news.storage.trade_projection import NEWS_TRADE_PROJECTION_VERSION

    assert MAPPED_NEWS_PROJECTION_VERSION == NEWS_TRADE_PROJECTION_VERSION


# The error codes `tracefold.app.*` may still suppress. Exact, so the list can only be shortened: PR7-B4
# restored `arg-type`, `union-attr`, `assignment` and `warn_return_any` by fixing the 28 errors they hid,
# and what remains is untyped-third-party debt (`attr-defined`, `index`, `operator`, `no-untyped-call`).
APP_MYPY_SUPPRESSED_CODES = frozenset({"attr-defined", "index", "operator", "no-untyped-call"})
# Strictness flags a composition seam may not turn off again. `tracefold.integrations.*` keeps its own
# looser policy on purpose — a provider payload is whatever the venue sent — and is deliberately not here.
APP_MYPY_REQUIRED_FLAGS = ("warn_return_any", "strict_equality", "no_implicit_optional")


def _mypy_override(module: str) -> dict[str, object]:
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = config["tool"]["mypy"]["overrides"]
    matched = [
        override
        for override in overrides
        if override["module"] == module or (isinstance(override["module"], list) and module in override["module"])
    ]
    assert len(matched) == 1, f"expected exactly one mypy override for {module}"
    return dict(matched[0])


def test_app_type_suppression_only_shrinks() -> None:
    """#162 PR7-B4: the seam's typing debt is a ledger, not a preference."""

    override = _mypy_override("tracefold.app.*")
    assert frozenset(override.get("disable_error_code", ())) == APP_MYPY_SUPPRESSED_CODES
    assert [flag for flag in APP_MYPY_REQUIRED_FLAGS if flag in override] == []
    # The looser third-party policy still exists, and still belongs to the adapter layer only.
    integrations = _mypy_override("tracefold.integrations.*")
    assert integrations.get("warn_return_any") is False
    assert frozenset(integrations.get("disable_error_code", ())) > APP_MYPY_SUPPRESSED_CODES


# The App-owned database methods a business package may never reach for. Checked as attribute *access*
# rather than source text, so the sentence in `news/pipeline/runtime.py` that explains the rule — and any
# future comment quoting it — is prose, not a violation.
APP_DATABASE_METHODS = frozenset({"worker_session", "run_news", "run_business", "run_control", "heavy_business"})


def test_business_packages_never_reach_for_an_app_database_method() -> None:
    """#162 PR7-B1's rule, made executable: capabilities depend on their port, not on `WorkerDatabase`.

    Before PR7-B there was no import edge and a hard dependency anyway — `db: Any`, then
    `db.heavy_business()`. Nothing but this guard would notice that coming back.
    """

    violations = [
        f"{path.relative_to(SRC).as_posix()}:{node.lineno}:{node.attr}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] in ("news", "trading")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.Attribute) and node.attr in APP_DATABASE_METHODS
    ]
    assert violations == []
    # The detector reads attribute access, not the words: a docstring naming the method is not a call.
    quoted = ast.parse('"""Do not call heavy_business() here."""\nx = 1\n')
    assert [n for n in ast.walk(quoted) if isinstance(n, ast.Attribute)] == []
    reached = ast.parse("db.heavy_business()\n")
    assert [n.attr for n in ast.walk(reached) if isinstance(n, ast.Attribute)] == ["heavy_business"]


def test_build_time_python_probes_name_modules_that_exist() -> None:
    """The `Dockerfile` smoke check is not covered by any source grep, and #162 PR8-B proved it.

    Moving the Program left `Dockerfile` importing `tracefold.news.agents.semantic_program`. `make check`
    was green, the whole test suite was green, and `make up` failed at image build — safely, because the
    probe is a build stage, but only after a full rebuild. A module name embedded in a non-Python file is
    still a dependency; this resolves every one of them against the tree.

    `Makefile` is deliberately excluded: its image probes run `python` *inside an arbitrary image*, which
    may be a pinned pre-refactor rollback build, so they name historical modules on purpose.
    """

    import re as _re

    probed = _re.findall(r"from (tracefold\.[\w.]+) import", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert probed, "expected at least one build-time import probe in the Dockerfile"
    assert [module for module in probed if not _module_exists(module)] == []
