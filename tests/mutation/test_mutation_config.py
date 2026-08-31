"""Guards the reasoning `mutation-survivors.toml`'s `annotation-union` rule rests on.

That rule accepts a surviving `|` mutation wherever every `|` on the line sits inside an annotation
deferred by `from __future__ import annotations`. The premise is checked per site rather than
asserted. The first test falsifies the detector in both directions on sources written here; the
rest hold the batch's own premises.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from scripts.mutation_survivors import evaluated_bit_or_lines

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MUTATION_CONFIG = REPO_ROOT / "mutation.toml"
SURVIVOR_TRIAGE = REPO_ROOT / "mutation-survivors.toml"


def _config() -> dict:
    return tomllib.loads(MUTATION_CONFIG.read_text(encoding="utf-8"))["cosmic-ray"]


def test_the_detector_separates_an_evaluated_bitwise_or_from_a_union_annotation() -> None:
    """A detector that always returned the empty set would pass every other test in this module."""

    deferred_unions = (
        "from __future__ import annotations\ndef f(a: int | None) -> str | None: ...\nx: int | None = None\n"
    )
    assert evaluated_bit_or_lines(deferred_unions) == set()

    runtime_alias = "from __future__ import annotations\nAlias = int | str\n"
    assert evaluated_bit_or_lines(runtime_alias) == {2}

    real_operator = "from __future__ import annotations\ndef f(a: int) -> int:\n    return a | 3\n"
    assert evaluated_bit_or_lines(real_operator) == {3}


def test_a_wrapped_runtime_alias_is_evaluated_on_every_line_it_spans() -> None:
    """Cosmic Ray anchors on the `|` token; `ast` anchors on the leftmost operand.

    They agree until the expression wraps, and then the disagreement fails open — the classifier
    would look up the token's row, find no evaluated `|` recorded there, and exempt eleven mutants
    of a union that really is built at import. Covering the whole span makes the two anchors agree
    for any formatting. `ruff format` reflowing the `ExecutionQuoteAuditV1` alias is exactly how
    this would have arrived.
    """

    wrapped = "from __future__ import annotations\nAlias = (\n    int\n    | str\n)\n"
    assert evaluated_bit_or_lines(wrapped) == {3, 4}


@pytest.mark.parametrize("module_path", _config()["module-path"])
def test_every_mutated_module_defers_its_annotations(module_path: str) -> None:
    """Without this import the annotation unions would be evaluated, and the rule would be unsound."""

    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    assert "from __future__ import annotations" in source


def test_the_batch_excludes_no_operator() -> None:
    """Survivors are classified after the fact, where the reason is checked; none are skipped before."""

    assert "filters" not in _config()


def test_the_batch_mutates_exactly_the_current_price_kernel() -> None:
    """Adding a module changes the bound the workflow's shard count was sized against."""

    assert _config()["module-path"] == ["tracefold/trading/market_context.py"]


@pytest.mark.parametrize(
    "test_file",
    ["tests/trading/test_market_context.py"],
)
def test_the_command_runs_the_tests_that_actually_constrain_the_mutated_modules(test_file: str) -> None:
    """The batch is only worth its runtime if the constraining tests are in it.

    The command must carry the focused suite that pins the selected bar and basis-point arithmetic.
    """

    assert test_file in _config()["test-command"]


def test_the_matrix_covers_every_shard_the_workflow_declares() -> None:
    """Parsed, not substring-matched, and cross-checked rather than asserted twice.

    The first version compared raw text, which false-passes in both directions that matter:
    `"timeout-minutes: 30" in "timeout-minutes: 300"` is true, so a tenfold cap increase reads as
    green, and the shard list and `SHARDS` were two independent literals for one invariant — a
    five-entry matrix beside `SHARDS: "6"` satisfied both while silently dropping a sixth of the
    population from the score.
    """

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "mutation.yml").read_text(encoding="utf-8"))
    mutate = workflow["jobs"]["mutate"]
    declared = int(workflow["env"]["SHARDS"])
    assert list(mutate["strategy"]["matrix"]["shard"]) == list(range(declared))
    assert mutate["strategy"]["fail-fast"] is False
    assert mutate["timeout-minutes"] == 30


def test_every_triage_rule_names_a_kind_the_classifier_can_check() -> None:
    """A rule whose premise nothing verifies is prose, and prose does not gate."""

    document = tomllib.loads(SURVIVOR_TRIAGE.read_text(encoding="utf-8"))
    assert document.get("rule"), "the triage file must classify the annotation survivors by rule"
    for rule in document["rule"]:
        assert rule["kind"] == "annotation-union"
        assert rule["reason"].strip()
