"""Require every surviving mutant to be classified in `mutation-survivors.toml`, and no entry to be stale.

A mutation score on its own invites the wrong reaction: the number moves, someone tunes the batch
until it looks better, and nothing is learned. What makes the lane useful is the rule that a
survivor must be *named* — either it is a real gap and gets a test, or it is equivalent and gets a
reason someone can disagree with.

So this fails in both directions. An unclassified survivor fails because the lane found something
nobody has looked at. An accepted entry or rule that matches no survivor also fails, because a test
now kills that mutant and the exemption describes a repo that no longer exists — exemptions that
outlive their reason are how a gate quietly stops gating.

Classifications come in two forms. A `[[accepted]]` entry names one site and is written by hand. A
`[[rule]]` covers a whole mechanical category, and is only honoured where its premise is *checked
against the source* rather than asserted in prose: `annotation-union` accepts a mutated `|` only on
a line where every `|` sits inside an annotation the interpreter never evaluates. That distinction
is load-bearing and not theoretical — the `ExecutionQuoteAuditV1` alias in `quote_authority.py`
builds a runtime type alias whose `|` is evaluated, and the batch kills its mutants. A rule
written as "BitOr is always an annotation" would have swallowed those.
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import sys
import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRIAGE = REPO_ROOT / "mutation-survivors.toml"
_REQUIRED_ENTRY_KEYS = ("module", "function", "line", "operator", "occurrence", "reason")
_REQUIRED_RULE_KEYS = ("kind", "operator", "reason")


class Mutant(NamedTuple):
    """The identity of one mutation site, as both the session and the triage file spell it.

    `occurrence` is what makes this an identity rather than a bucket. One line can hold several
    applications of the same operator — `ts_event_ns < 0 or ts_init_ns < 0 or now_ns < 0` is three
    `NumberReplacer` sites on one line — and they do not share a reason: two of those bounds are
    masked by neighbouring clauses and the third sits behind an unreachable guard. Keying on
    `(module, line, operator)` alone would let one entry's reason silently stand in for another's.
    """

    module: str
    function: str
    line: int
    operator: str
    occurrence: int

    def describe(self) -> str:
        return f"{self.module}:{self.line} {self.function}() {self.operator} #{self.occurrence}"


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Every node reachable from an annotation, by identity.

    The `AnnAssign` branch is defensive rather than load-bearing, and it is worth saying which is
    which. Cosmic Ray's `operator_is_pipe_in_assignment_annotation` refuses to mutate a `|` whose
    parent is an annotated assignment, so a variable annotation emits no mutants at all: measured in
    the batch, `market_context.py:63` — `best: Bar | None = None` — produces zero, while the
    function signatures at 55, 72 and 85 produce eleven each. Only signatures and runtime aliases
    reach the classifier today. The branch stays because the classifier's answer should not depend
    on a filter inside the tool, but nothing in the current population exercises it.
    """

    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.extend(
                argument.annotation
                for argument in ast.walk(node.args)
                if isinstance(argument, ast.arg) and argument.annotation
            )
            if node.returns is not None:
                annotations.append(node.returns)
    return {id(inner) for annotation in annotations for inner in ast.walk(annotation)}


def evaluated_bit_or_lines(source: str) -> set[int]:
    """Lines holding a `|` the interpreter actually evaluates, i.e. one outside every annotation.

    Every line the expression spans, not just where it starts. The two sides of this comparison come
    from different tools and anchor differently: Cosmic Ray records the row of the `|` *token*, while
    `ast` reports `lineno` for the leftmost operand. They agree on a single-line union and diverge
    the moment one wraps — `Alias = (\\n    int\\n    | str\\n)` is token row 3 against `lineno` 2 —
    and the divergence fails open, quietly exempting a `|` that really is evaluated. Taking the
    whole `lineno..end_lineno` span removes the disagreement, and errs toward calling a line
    evaluated, which is the direction that refuses an exemption rather than granting one.

    Only sound for a module with `from __future__ import annotations`; callers check that separately.
    """

    tree = ast.parse(source)
    inside_annotation = _annotation_nodes(tree)
    spanned: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)):
            continue
        if id(node) in inside_annotation:
            continue
        spanned.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return spanned


@cache
def _module_facts(module: str) -> tuple[bool, frozenset[int]]:
    source = (REPO_ROOT / module).read_text(encoding="utf-8")
    return "from __future__ import annotations" in source, frozenset(evaluated_bit_or_lines(source))


def _is_unevaluated_annotation_union(mutant: Mutant) -> bool:
    """The premise behind the `annotation-union` rule, checked against the source it is claimed of."""

    deferred, evaluated = _module_facts(mutant.module)
    return deferred and mutant.line not in evaluated


_RULE_PREMISES = {"annotation-union": _is_unevaluated_annotation_union}


_SPEC_COLUMNS = "s.module_path, s.definition_name, s.start_pos_row, s.operator_name, s.occurrence"


def _as_mutants(rows: list[tuple[Any, ...]]) -> list[Mutant]:
    return [
        Mutant(module, function or "<module>", int(line), operator, int(occurrence))
        for module, function, line, operator, occurrence in rows
    ]


def _by_outcome(session: Path) -> dict[str, list[Mutant]]:
    """Every mutant that reached an outcome, grouped by what that outcome was.

    Identities rather than counts, because the shard databases each hold the *whole* population —
    `mutation_shard.py` marks the other shards' jobs SKIPPED, it does not delete their specs — so
    summing per-session `count(*)` reports six times the mutants that exist. The union of
    identities is the only figure that survives being sharded.
    """

    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            f"SELECT coalesce(r.test_outcome, r.worker_outcome), {_SPEC_COLUMNS} "
            "FROM mutation_specs s JOIN work_results r ON r.job_id = s.job_id"
        ).fetchall()
    grouped: dict[str, list[Mutant]] = {}
    for outcome, *spec in rows:
        grouped.setdefault(str(outcome), []).extend(_as_mutants([tuple(spec)]))
    return grouped


def _population(session: Path) -> set[Mutant]:
    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        rows = connection.execute(f"SELECT {_SPEC_COLUMNS.replace('s.', '')} FROM mutation_specs").fetchall()
    return set(_as_mutants(rows))


def _triage(path: Path) -> tuple[list[Mutant], list[dict[str, Any]]]:
    if not path.is_file():
        return [], []
    document = tomllib.loads(path.read_text(encoding="utf-8"))

    accepted: list[Mutant] = []
    for index, entry in enumerate(document.get("accepted", [])):
        # `is None` rather than falsiness: `occurrence = 0` is the first site on a line, not a blank.
        missing = [key for key in _REQUIRED_ENTRY_KEYS if entry.get(key) is None or entry.get(key) == ""]
        if missing:
            raise SystemExit(f"{path.name}: [[accepted]] #{index} is missing {', '.join(missing)}")
        accepted.append(
            Mutant(
                entry["module"],
                entry["function"],
                int(entry["line"]),
                entry["operator"],
                int(entry["occurrence"]),
            )
        )

    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(document.get("rule", [])):
        missing = [key for key in _REQUIRED_RULE_KEYS if not rule.get(key)]
        if missing:
            raise SystemExit(f"{path.name}: [[rule]] #{index} is missing {', '.join(missing)}")
        if rule["kind"] not in _RULE_PREMISES:
            raise SystemExit(f"{path.name}: [[rule]] #{index} has unknown kind {rule['kind']!r}")
        rules.append(rule)
    return accepted, rules


def _matched_by_rule(mutant: Mutant, rules: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for rule in rules:
        if re.fullmatch(rule["operator"], mutant.operator) and _RULE_PREMISES[rule["kind"]](mutant):
            return rule
    return None


def _report(label: str, mutants: Iterable[Mutant]) -> None:
    listed = sorted(mutants)
    if not listed:
        return
    sys.stderr.write(f"\n{label} ({len(listed)}):\n")
    for mutant in listed:
        sys.stderr.write(f"  {mutant.describe()}\n")


def _report_stale(stale: Iterable[Mutant], survivors: Iterable[Mutant], triage_name: str) -> None:
    """Say *why* an entry no longer matches, because the two reasons need opposite responses.

    "No longer surviving" is the message for "a test now kills this, delete the exemption". It is
    also what a moved line produces, and that wants the opposite response — the classification is
    still correct and only its anchor is stale. The identity is keyed on a line number, so an edit
    anywhere above a mutated module shifts every entry below it; that has happened three times while
    this lane was being built. Matching the orphan against a survivor with the same operator and
    occurrence tells the two apart without loosening what the gate accepts.
    """

    listed = sorted(stale)
    if not listed:
        return
    moved = {(m.module, m.operator, m.occurrence): m for m in survivors}
    sys.stderr.write(f"\nlisted in {triage_name} but no longer surviving ({len(listed)}):\n")
    for mutant in listed:
        elsewhere = moved.get((mutant.module, mutant.operator, mutant.occurrence))
        if elsewhere is not None:
            sys.stderr.write(
                f"  {mutant.describe()}\n"
                f"      still survives at line {elsewhere.line} — the line moved, so update the anchor\n"
                f"      rather than deleting a classification that is still true.\n"
            )
        else:
            sys.stderr.write(
                f"  {mutant.describe()}\n      nothing with this identity survives; a test now kills it.\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+", help="cosmic-ray session databases, one per shard")
    parser.add_argument("--triage", type=Path, default=DEFAULT_TRIAGE, help="checked-in survivor classification")
    args = parser.parse_args(argv)

    missing = [str(session) for session in args.sessions if not session.is_file()]
    if missing:
        sys.stderr.write(f"no mutation session at {', '.join(missing)}\n")
        return 1

    population: set[Mutant] = set()
    outcomes: dict[str, set[Mutant]] = {}
    for session in args.sessions:
        population |= _population(session)
        for outcome, mutants in _by_outcome(session).items():
            outcomes.setdefault(outcome, set()).update(mutants)

    survivors = sorted(outcomes.get("SURVIVED", set()))
    killed = outcomes.get("KILLED", set())
    scored = len(killed) + len(survivors)
    if not scored:
        sys.stdout.write("no mutants were scored\n")
        return 1

    accepted, rules = _triage(args.triage)
    by_rule = [mutant for mutant in survivors if _matched_by_rule(mutant, rules)]
    matched = set(by_rule)
    real = [mutant for mutant in survivors if mutant not in matched]

    sys.stdout.write(
        f"mutation score {len(killed) / scored:.1%} — {len(killed)} killed, {len(survivors)} survived "
        f"({len(by_rule)} by rule, {len(real)} to classify), "
        f"{len(population)} generated across {len(args.sessions)} shard(s)\n"
    )

    # Anything that is neither killed nor survived never produced a verdict — a mutant that could
    # not be launched, timed out at the harness level, or raised before the tests. It is not in the
    # score's denominator, so it has to be named or a batch that mostly failed to run reads as a
    # batch that mostly passed.
    unresolved = {
        outcome: len(mutants) for outcome, mutants in outcomes.items() if outcome not in {"KILLED", "SURVIVED"}
    }
    if unresolved:
        named = ", ".join(f"{count} {outcome}" for outcome, count in sorted(unresolved.items()))
        sys.stdout.write(f"outcomes outside the score: {named}\n")

    # A shard's own database holds the whole population with the other shards' jobs marked SKIPPED,
    # so completeness is about the *union*: did every mutant reach a verdict in some session? Only
    # then is "listed but no longer surviving" a statement about the tests rather than about which
    # slice happened to run — which is what makes `make mutation TRACEFOLD_MUTATION_SHARDS=6` usable
    # locally instead of reporting three quarters of a correct triage file as stale.
    verdicts = killed | set(survivors)
    complete = verdicts >= population
    unclassified = sorted(set(real) - set(accepted))
    # Against every survivor, not just the hand-classified ones: an entry written beside a rule that
    # also matches its mutant is still describing something that survived, and calling it stale would
    # push the maintainer to delete a correct classification.
    stale = sorted(set(accepted) - set(survivors)) if complete else []
    unused = [rule for rule in rules if not any(_matched_by_rule(mutant, [rule]) for mutant in survivors)]

    _report("SURVIVED and unclassified", unclassified)
    _report_stale(stale, survivors, args.triage.name)
    for rule in unused:
        sys.stderr.write(f"\n[[rule]] {rule['kind']} / {rule['operator']} matched no survivor; drop it\n")
    if not complete:
        sys.stdout.write(
            f"partial population ({len(verdicts)} of {len(population)} reached a verdict); "
            "checking only for unclassified survivors\n"
        )
    if unclassified or stale or unused:
        sys.stderr.write(
            f"\nEvery survivor needs an entry in {args.triage.name} giving a reason, or a test that kills it.\n"
        )
        return 1

    sys.stdout.write(f"every survivor is classified in {args.triage.name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
