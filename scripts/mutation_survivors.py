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
    """Every node reachable from an annotation, by identity."""

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

    Only sound for a module with `from __future__ import annotations`; callers check that separately.
    """

    tree = ast.parse(source)
    inside_annotation = _annotation_nodes(tree)
    return {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and id(node) not in inside_annotation
    }


@cache
def _module_facts(module: str) -> tuple[bool, frozenset[int]]:
    source = (REPO_ROOT / module).read_text(encoding="utf-8")
    return "from __future__ import annotations" in source, frozenset(evaluated_bit_or_lines(source))


def _is_unevaluated_annotation_union(mutant: Mutant) -> bool:
    """The premise behind the `annotation-union` rule, checked against the source it is claimed of."""

    deferred, evaluated = _module_facts(mutant.module)
    return deferred and mutant.line not in evaluated


_RULE_PREMISES = {"annotation-union": _is_unevaluated_annotation_union}


def _survivors(session: Path) -> list[Mutant]:
    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT s.module_path, s.definition_name, s.start_pos_row, s.operator_name, s.occurrence "
            "FROM mutation_specs s JOIN work_results r ON r.job_id = s.job_id "
            "WHERE r.test_outcome = 'SURVIVED'"
        ).fetchall()
    return [
        Mutant(module, function or "<module>", int(line), operator, int(occurrence))
        for module, function, line, operator, occurrence in rows
    ]


def _outcomes(session: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        total = connection.execute("SELECT count(*) FROM mutation_specs").fetchone()[0]
        rows = connection.execute(
            "SELECT coalesce(test_outcome, worker_outcome), count(*) FROM work_results GROUP BY 1"
        ).fetchall()
    counts = {str(outcome): int(count) for outcome, count in rows}
    counts["generated"] = int(total)
    return counts


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+", help="cosmic-ray session databases, one per shard")
    parser.add_argument("--triage", type=Path, default=DEFAULT_TRIAGE, help="checked-in survivor classification")
    parser.add_argument("--report-only", action="store_true", help="print the score without failing")
    args = parser.parse_args(argv)

    missing = [str(session) for session in args.sessions if not session.is_file()]
    if missing:
        sys.stderr.write(f"no mutation session at {', '.join(missing)}\n")
        return 1

    counts: dict[str, int] = {}
    survivors: list[Mutant] = []
    for session in args.sessions:
        survivors.extend(_survivors(session))
        for outcome, count in _outcomes(session).items():
            counts[outcome] = counts.get(outcome, 0) + count

    killed = counts.get("KILLED", 0)
    scored = killed + len(survivors)
    accepted, rules = _triage(args.triage)
    # Partition the survivor list itself rather than a set of it. Several mutants can share one
    # identity — same module, function, line and operator at different occurrences — and a report
    # whose parts do not sum to its total is one nobody can check.
    by_rule = [mutant for mutant in survivors if _matched_by_rule(mutant, rules)]
    matched = set(by_rule)
    real = [mutant for mutant in survivors if mutant not in matched]

    sys.stdout.write(
        f"mutation score {killed / scored:.1%} — {killed} killed, {len(survivors)} survived "
        f"({len(by_rule)} by rule, {len(real)} to classify at {len(set(real))} sites), "
        f"{counts.get('generated', 0)} generated "
        f"across {len(args.sessions)} shard(s)\n"
        if scored
        else "no mutants were scored\n"
    )
    if args.report_only:
        return 0

    unclassified = sorted(set(real) - set(accepted))
    stale = sorted(set(accepted) - set(real))
    unused = [rule for rule in rules if not any(_matched_by_rule(mutant, [rule]) for mutant in survivors)]

    _report("SURVIVED and unclassified", unclassified)
    _report(f"listed in {args.triage.name} but no longer surviving", stale)
    for rule in unused:
        sys.stderr.write(f"\n[[rule]] {rule['kind']} / {rule['operator']} matched no survivor; drop it\n")
    if unclassified or stale or unused:
        sys.stderr.write(
            f"\nEvery survivor needs an entry in {args.triage.name} giving a reason, or a test that kills it.\n"
        )
        return 1

    sys.stdout.write(f"every survivor is classified in {args.triage.name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
