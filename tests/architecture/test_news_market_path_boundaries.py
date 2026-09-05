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

import io
import re
import token
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Every module a market observation passes through between the provider frame and the reader.
MARKET_PATH = (
    "tracefold/news/opennews.py",
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


def _scannable(relative: str) -> str:
    """The module's code, plus any string literal that contains SQL. Comments and prose removed."""

    text = (ROOT / relative).read_text(encoding="utf-8")
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
