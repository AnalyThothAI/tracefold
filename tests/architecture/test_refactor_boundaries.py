"""Long-lived semantic boundaries retained after the Issue #162 migration."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tracefold"


def _production_files() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts and "alembic/versions" not in path.as_posix()
    )


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


def test_package_initializers_are_declarative() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("__init__.py")):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for index, node in enumerate(tree.body):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                violations.append(f"{relative}:{type(node).__name__}:{node.lineno}")
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
                violations.append(f"{relative}:non_declarative_assignment:{node.lineno}")
                continue
            if targets[0].id == "__all__":
                try:
                    exported = ast.literal_eval(value)
                except (ValueError, TypeError):
                    exported = None
                if not isinstance(exported, (list, tuple)) or not all(isinstance(item, str) for item in exported):
                    violations.append(f"{relative}:non_literal_all:{node.lineno}")
                continue
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError):
                if not all(isinstance(child, TYPE_EXPRESSION_NODES) for child in ast.walk(value)):
                    violations.append(f"{relative}:non_literal_assignment:{node.lineno}")
    assert violations == []


def test_workers_root_owns_lifecycle_but_not_capability_construction() -> None:
    root = SRC / "app" / "workers" / "root.py"
    forbidden = (
        "dspy",
        "tracefold.app.learning_runtime",
        "tracefold.app.llm",
        "tracefold.integrations",
        "tracefold.news",
        "tracefold.trading",
    )
    imports = _import_targets(root, ast.parse(root.read_text(encoding="utf-8"), filename=str(root)))
    assert sorted(imported for imported in imports if imported.startswith(forbidden)) == []


# #202 §12. The online plane may observe, review and monitor a canary; it may not optimize. This is the
# only test here that measures what a *process* loads rather than what a file names, because that is the
# claim: an import three hops down a wiring chain reaches the optimizer exactly as surely as a direct one.
WORKER_FORBIDDEN_AUTHORITY_MODULES = (
    "tracefold.news.learning.dataset",
    "tracefold.news.learning.evaluation_history",
    "tracefold.news.learning.ledger",
    "tracefold.news.release.candidate",
    "tracefold.news.learning.optimizer",
    "tracefold.news.learning.baseline",
    "tracefold.news.learning.evaluate",
    "tracefold.news.learning.judge",
    "tracefold.news.review.drafter",
    "tracefold.news.review.desk",
)


def _forbidden_worker_authorities(modules: set[str]) -> list[str]:
    return sorted(
        module
        for module in modules
        if any(
            module == authority or module.startswith(f"{authority}.")
            for authority in WORKER_FORBIDDEN_AUTHORITY_MODULES
        )
    )


def _module_file(module: str) -> Path | None:
    """The first-party file a dotted module name refers to, or None for a third-party one."""

    if not module.startswith("tracefold."):
        return None
    relative = Path(*module.split(".")[1:])
    for candidate in (SRC / relative.with_suffix(".py"), SRC / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _every_import(path: Path) -> set[str]:
    """Every module this file names, at any depth — including inside a function body.

    `ast.walk`, not `tree.body`, and that is the whole point: `app/workers/wiring/news.py` already imports
    `learning.canary` from inside `_wire_news_pipeline`, so a boundary that only reads top-level imports
    would miss exactly the pattern this codebase actually uses to defer a heavy import.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                modules.add(str(node.module or ""))
                continue
            base = package[: len(package) - node.level + 1]
            modules.add(".".join([*base, *(str(node.module).split(".") if node.module else [])]))
    return modules


def _worker_import_closure() -> set[str]:
    """Everything the online worker can reach, following first-party imports transitively."""

    roots = [SRC / "app" / "workers" / "root.py", *sorted((SRC / "app" / "workers" / "wiring").glob("*.py"))]
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        for module in _every_import(current):
            if module in seen:
                continue
            seen.add(module)
            child = _module_file(module)
            if child is not None:
                stack.append(child)
    return seen


def test_online_worker_cannot_reach_offline_learning_or_release_authorities() -> None:
    """Two measurements of one claim, because each catches what the other cannot.

    The static half follows every import at any depth, transitively — a lazy import inside
    `_wire_news_pipeline` reaches the optimizer exactly as surely as a top-level one, and this codebase
    already defers heavy imports that way. The process half below then catches what a source read cannot:
    a dynamic import, or a module pulled in by something the walk resolved as third-party.

    What the online route legitimately reaches is `dspy` — it *runs* a DSPy Program, and dspy's own package
    pulls `dspy.teleprompt`, which pulls `gepa`. That is the optimizer library, not this repository's
    optimizer, and nothing in the worker can start a run with it. `learning.canary` and `learning.contracts`
    are in and belong in: arming, tripping and validating an image-carried candidate is release control,
    and it is online by nature.
    """

    reachable = _worker_import_closure()
    assert _forbidden_worker_authorities(reachable) == []


def test_online_worker_process_does_not_load_offline_learning_or_release_authorities() -> None:
    """The dynamic half: a fresh interpreter, because a test session has already loaded half the repo.

    Weaker than the static walk above — it sees only module-level imports — and kept beside it because it
    is the only one that would notice an `importlib` call or a plugin hook, which no source read resolves.
    """

    probe = (
        "import json, sys\n"
        "import tracefold.app.workers.root\n"
        "from tracefold.app.workers import wiring\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.startswith(('tracefold.news.learning', 'tracefold.news.review', 'tracefold.news.release')))))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))

    assert _forbidden_worker_authorities(loaded) == []


def test_importing_worker_wiring_does_not_start_the_worker_runtime() -> None:
    """Cold composition commands must not inherit the online Worker's heavy import graph."""

    probe = (
        "import json, sys\n"
        "from tracefold.app.workers import run_workers\n"
        "import tracefold.app.workers.wiring.news_to_trading\n"
        "print(json.dumps({'callable': callable(run_workers), **{name: name in sys.modules for name in "
        "('dspy', 'tracefold.app.workers.root')}}))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "callable": True,
        "dspy": False,
        "tracefold.app.workers.root": False,
    }


BUSINESS_WRITE_SQL_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>(?:news|trading)_[a-z0-9_]*)",
    re.IGNORECASE,
)


def test_app_composes_capabilities_but_never_writes_their_facts() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{table}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] == "app"
        for table in BUSINESS_WRITE_SQL_RE.findall(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def _top_level_import_targets(path: Path, tree: ast.Module) -> set[str]:
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
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        graph[module] = {imported for imported in _top_level_import_targets(path, tree) if imported.startswith(prefix)}
    return {module: {target for target in targets if target in graph} for module, targets in graph.items()}


def _import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        order[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph[node]):
            if target not in order:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], order[target])
        if low[node] != order[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or node in graph[node]:
            cycles.append(sorted(component))

    for module in sorted(graph):
        if module not in order:
            visit(module)
    return cycles


def test_owning_packages_have_acyclic_internal_import_graphs() -> None:
    assert {package: _import_cycles(_internal_import_graph(package)) for package in ("app", "news", "trading")} == {
        "app": [],
        "news": [],
        "trading": [],
    }


CROSS_CONTEXT_BOUNDARY_MODULES = (
    "app/workers/wiring/news_to_trading.py",
    "news/pipeline/runtime.py",
    "news/storage/trade_projection.py",
    "trading/capital_lane.py",
)


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_any_mapping(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and _terminal_name(node.value) in {"dict", "Mapping"}
        and any(_terminal_name(child) == "Any" for child in ast.walk(node.slice))
    )


def test_cross_context_boundaries_carry_named_typed_rows() -> None:
    violations = [
        f"{relative}:{node.lineno}:{ast.unparse(node)}"
        for relative in CROSS_CONTEXT_BOUNDARY_MODULES
        for node in ast.walk(ast.parse((SRC / relative).read_text(encoding="utf-8"), filename=relative))
        if _is_any_mapping(node)
    ]
    assert violations == []


CROSS_CONTEXT_TYPED_PARAMETERS = {
    "app/cli/commands/trading_evidence.py": {"_fetch_blind_market_health": {"capture"}},
    "trading/evidence_research.py": {
        "build_future_capture_collection_health": {"collector", "workers", "market"},
        "summarize_blind_market_health": {"probes"},
    },
    "trading/evidence_verification.py": {
        "fixed_window_verification_checks": {"serve", "sources"},
        "release_verification_checks": {"observations", "window_sources"},
        "rollback_verification_checks": {"serve"},
    },
}


def test_evidence_cross_context_facts_have_named_annotations() -> None:
    violations: list[str] = []
    for relative, functions in CROSS_CONTEXT_TYPED_PARAMETERS.items():
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"), filename=relative)
        by_name = {
            node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name, parameter_names in functions.items():
            function = by_name[function_name]
            parameters = {argument.arg: argument for argument in (*function.args.args, *function.args.kwonlyargs)}
            for parameter_name in parameter_names:
                annotation = parameters[parameter_name].annotation
                if annotation is None or _is_any_mapping(annotation):
                    violations.append(f"{relative}:{function.lineno}:{function_name}:{parameter_name}")
    assert violations == []


def test_news_to_trading_mapper_matches_the_projection_version() -> None:
    from tracefold.app.workers.wiring.news_to_trading import MAPPED_NEWS_PROJECTION_VERSION
    from tracefold.news.storage.trade_projection import NEWS_TRADE_PROJECTION_VERSION

    assert MAPPED_NEWS_PROJECTION_VERSION == NEWS_TRADE_PROJECTION_VERSION


APP_DATABASE_METHODS = frozenset({"worker_session", "run_news", "run_business", "run_control", "heavy_business"})


def test_business_packages_never_reach_for_an_app_database_method() -> None:
    violations = [
        f"{path.relative_to(SRC).as_posix()}:{node.lineno}:{node.attr}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] in ("news", "trading")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.Attribute) and node.attr in APP_DATABASE_METHODS
    ]
    assert violations == []


def test_build_time_python_probes_resolve_real_modules() -> None:
    probed = re.findall(r"from (tracefold\.[\w.]+) import", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert probed
    assert [module for module in probed if not _module_exists(module)] == []
