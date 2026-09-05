"""The live market path answers over stored facts alone (#553 PR-1).

A market observation is stored at admission with its typed fact and read back from that fact. The
whole point of the cut is that none of the editorial plane is in the way: no verdict, no reader
history, no model judgment, no Event. This is the code search that proves it, kept as a test so the
next change to any of these modules has to keep proving it.

It scans *code*, with comments and string literals tokenized away, and it scans text rather than
imports. An editorial name reintroduced as a raw SQL string or a column in a `SELECT` would be
invisible to an import graph -- and the OI ledger's whole defect was a `JOIN news_events` that no
import declared. Prose keeps the right to name what was removed, because the record of why a
mechanism is gone is worth more than the absence of the word.
"""

from __future__ import annotations

import ast
import io
import re
import token
import tokenize
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[2]

# Every module a market observation passes through between the provider frame and the reader.
MARKET_PATH = (
    "tracefold/news/opennews.py",
    "tracefold/news/pipeline/admission.py",
    "tracefold/news/source_contracts.py",
    "tracefold/news/oi_signals.py",
    "tracefold/news/oi_contracts.py",
    "tracefold/news/liquidations.py",
    "tracefold/news/smart_money.py",
    "tracefold/news/market_contracts.py",
    "tracefold/news/storage/market.py",
    "tracefold/app/http/routes/market.py",
    "tracefold/app/http/schemas/market.py",
    "tracefold/app/workers/wiring/news_to_trading.py",
    "tracefold/news/storage/trade_projection.py",
)

# What the market path may not name in code. Each one is a mechanism the cut removed, not a spelling
# preference: a verdict is an editorial decision, reader history is the delivered-card ledger the OI
# lane was CAS-gated on, a judgment atom is the model's contract, and `news_events` is the table a
# market fact had to reach through before a recovery frame could be read at all.
FORBIDDEN = (
    r"\bnews_events\b",
    r"\bnews_verdicts\b",
    r"\bnews_deliveries\b",
    r"\bnews_event_members\b",
    r"\bnews_event_evidence_snapshots\b",
    r"\bnews_event_bands\b",
    r"\breader_history\b",
    r"\bTriageVerdict\b",
    r"\bScoredJudgment\b",
    r"\bSemanticJudge\b",
    r"\bevaluate_oi\b",
    r"\bjudgment_atom\b",
    r"\block_storyline\b",
    r"\bstoryline_key\b",
    r"\blearning_epoch\b",
)
# SQL lives in string literals, so the literals cannot simply be dropped. These two are scanned as
# code: a table name inside a statement is exactly the reintroduction this test exists to catch.
_SQL_MARKERS = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b")


# `admission.py` is the one module on the path that hosts *both* branches: the editorial one still
# builds a storyline key and an Event, and must. Scanning the whole file would either fail on the
# editorial code or force the forbid-list to be weakened for everyone. The market lane's own
# functions are named instead, so the scan over them is stricter than the file-wide one, not looser.
MARKET_OWNED_FUNCTIONS: Final[dict[str, tuple[str, ...]]] = {
    "tracefold/news/pipeline/admission.py": (
        "_prepare_market",
        "_related_address",
        "admit_market_item",
        "_write_market_fact",
    ),
}


def _market_owned_source(relative: str, names: tuple[str, ...]) -> str:
    """The exact source of the named top-level functions, and nothing else in the module."""

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    found = {
        node.name: ast.unparse(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in names
    }
    missing = sorted(set(names) - set(found))
    assert missing == [], f"{relative}: market-owned functions renamed or removed: {missing}"
    return "\n".join(found[name] for name in names)


def _scannable(relative: str) -> str:
    """The module's code, plus any string literal that contains SQL. Comments and prose removed."""

    names = MARKET_OWNED_FUNCTIONS.get(relative)
    # `ast.unparse` has already dropped the comments; docstrings are still string literals and are
    # filtered by the same rule as every other module's prose.
    text = _market_owned_source(relative, names) if names is not None else (ROOT / relative).read_text(encoding="utf-8")
    kept: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == token.COMMENT:
            continue
        if tok.type == token.STRING and not _SQL_MARKERS.search(tok.string):
            continue
        kept.append(tok.string)
    return "\n".join(kept)


def _offenders(patterns: tuple[str, ...]) -> list[str]:
    return [
        f"{relative}: {pattern}"
        for relative in MARKET_PATH
        for pattern in patterns
        if re.search(pattern, _scannable(relative))
    ]


def test_the_live_market_path_names_no_verdict_history_model_or_event_mechanism() -> None:
    assert _offenders(FORBIDDEN) == []


def test_the_admission_scan_reads_the_market_functions_and_not_the_editorial_ones() -> None:
    """Not vacuous: the module it narrows genuinely contains the names, in the branch it excludes.

    `admission.py` hosts both lanes. If the narrowing ever silently matched nothing -- a rename, a
    move -- this scan would pass by reading an empty string, so the two halves are asserted directly.
    """

    scanned = _scannable("tracefold/news/pipeline/admission.py")
    whole = (ROOT / "tracefold/news/pipeline/admission.py").read_text(encoding="utf-8")

    assert "admit_market_item" in scanned and "_write_market_fact" in scanned
    assert "storyline_key" in whole, "the editorial branch still builds one, and must"
    assert "storyline_key" not in scanned
    assert len(scanned) < len(whole)


def test_the_scan_would_still_catch_a_reintroduced_name() -> None:
    """A negative control: the filter above removes prose, not code, and not SQL."""

    source = "\n".join(
        [
            '"""A docstring naming news_verdicts is prose."""',
            "# A comment naming reader_history is prose.",
            'QUERY = "SELECT 1 FROM news_events"',
        ]
    )
    scanned = "\n".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type != token.COMMENT and (tok.type != token.STRING or _SQL_MARKERS.search(tok.string))
    )
    assert "news_verdicts" not in scanned
    assert "reader_history" not in scanned
    assert "news_events" in scanned


def test_the_market_path_manifest_covers_every_module_the_lane_owns() -> None:
    """A module added to the lane without a line here would be scanned by nothing."""

    for relative in MARKET_PATH:
        assert (ROOT / relative).is_file(), relative
    owned = {
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "tracefold" / "news", ROOT / "tracefold" / "app")
        for path in root.rglob("*market*.py")
        if "__pycache__" not in path.parts
    }
    # `market_review/` is the Quote and instrument plane, a different lane with its own owner.
    unreviewed = {name for name in owned if "market_review" not in name} - set(MARKET_PATH)
    assert unreviewed == set()
