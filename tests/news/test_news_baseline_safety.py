"""#150: the baseline reads. It has no authority to write, deliver, propose, accept or promote anything.

This is a structural guarantee rather than a review convention. The measurement plane runs the same metric
the optimizer maximizes and, in `runtime_live`, the same Program the Workers run — so the one thing keeping
it from becoming an unattended release path is that it never acquires the means to write.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import tracefold.news.learning.baseline as baseline_module
from tests.support.baseline_calibration import load_calibration_corpus
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.learning.baseline import build_baseline_cases, run_baseline

# Anything that would turn a measurement into an action. `submit` appends an acceptance row, `promote`/
# `accept` move a candidate through the release gate, `deliver`/`publish` reach the reader.
_WRITE_VERBS = (
    "submit",
    "accept",
    "promote",
    "deploy",
    "rollback",
    "deliver",
    "publish",
    "propose",
    "insert",
    "update",
    "delete",
    "commit",
    "write_program_candidate_artifact",
    "apply_trusted_program_patch",
    "apply_program_patch_v2",
)


def _module_tree(module: object) -> ast.Module:
    return ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(str(node.module or ""))
    return names


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _cli_handler() -> ast.FunctionDef:
    import tracefold.app.cli.commands.news_learning_baseline as cli_module

    tree = _module_tree(cli_module)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_learning_baseline"
    )
    return handler


def test_the_harness_imports_no_repository_review_plane_or_database_driver() -> None:
    imported = _imports(_module_tree(baseline_module))
    assert not any("repositor" in name for name in imported), imported
    assert not any("psycopg" in name for name in imported), imported
    assert not any(name.endswith(".review") or name == "review" for name in imported), imported
    assert not any("deliver" in name for name in imported), imported
    # The compiler is the one module with proposal authority, and the architecture harness allows exactly one
    # importer of it. A baseline that reached for it would both break that rule and gain a way to act.
    assert not any("program_compiler" in name for name in imported), imported


def test_the_harness_calls_nothing_that_could_write() -> None:
    called = _called_names(_module_tree(baseline_module))
    assert sorted(verb for verb in _WRITE_VERBS if verb in called) == []


def test_the_cli_opens_the_database_only_as_serve() -> None:
    handler = _cli_handler()
    roles = [
        keyword.value.value
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "postgres_connection"
        for keyword in node.keywords
        if keyword.arg == "role" and isinstance(keyword.value, ast.Constant)
    ]
    assert roles == ["serve"], "the baseline reads the corpus and nothing else"


def test_the_cli_handler_performs_no_write_action() -> None:
    called = _called_names(_cli_handler())
    assert sorted(verb for verb in _WRITE_VERBS if verb in called) == []


def test_the_database_connection_closes_before_any_model_call() -> None:
    """A `runtime_live` run can take minutes of provider latency. Holding a `serve` connection open across it
    would pin a pool slot the API needs, so the corpus read is its own bounded `with` block."""

    handler = _cli_handler()
    with_blocks = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "postgres_connection"
            for item in node.items
        )
    ]
    assert len(with_blocks) == 1
    assert "run_baseline" not in _called_names(with_blocks[0])
    assert "run_baseline" in _called_names(handler)


def test_a_recorded_run_needs_no_connection_no_provider_and_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strongest form of the guarantee: score the whole frozen corpus with the network unavailable."""

    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the recorded baseline opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    corpus = load_calibration_corpus()
    report = run_baseline(
        build_baseline_cases(corpus["episodes"], action_source="recorded"),
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    assert report.population["answered_n"] == len(corpus["episodes"])
    assert report.route == {}
    assert report.semantic_judge == {}
