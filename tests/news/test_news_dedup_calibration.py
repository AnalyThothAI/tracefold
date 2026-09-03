"""The 2026-09-01 duplicate-push calibration set (#491), frozen so every retrieval change is measured the same way.

910 first/sent deliveries of one 24 h production window (38 cards an hour, peak 85), the 143 pairs a human
read as the same event pushed twice, and the groups of *different* facts that share a template. Each pair
records the first link that failed when the later card's verdict trace was replayed: the 4 h recent window,
the 128-row cap, the told selection, or the model calling a restatement a progression.

Two questions are asked of every candidate retrieval design, and only those two:

- recall: for how many of the 143 pairs does the earlier card reach the 16 rows the Program is shown? The
  production selector of the audit day (the reader history and told selector #491 retired) reached 46;
- collateral: does the mechanical layer, `decide()` over the 4 h recent ledger, still release every card in the
  different-fact groups? Widening that ledger to 24 h withheld three countries' PMI prints as one story, so the
  recent set is asserted unchanged and the groups are asserted released.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.support.news_judgment import scored_judgment
from tracefold.news.models import base_symbol
from tracefold.news.program.contracts import ScoredJudgment
from tracefold.news.reader_history import (
    RECENT_HISTORY_MAX,
    RECENT_HISTORY_WINDOW_MS,
    SIMILAR_HISTORY_WINDOW_MS,
    SIMILAR_TITLE_MAX,
    build_reader_history,
)
from tracefold.news.told_context import TOLD_MAX, ToldLedgerSnapshot
from tracefold.news.triage_rules import DEFAULT_POLICY, GateFacts, decide, storyline_status

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_dedup_calibration_v1.json.gz"

# Recall the audit-day production selector reached on this set, and the floor the current design is held to.
# The current design measured 105 when it landed; the floor leaves room for ranking refinements without
# letting a regression to the old shape (46) or to a per-storyline-key cap (57-83) pass.
AUDIT_DAY_RECALL = 46
RECALL_FLOOR = 100


@lru_cache(maxsize=1)
def _fixture() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _cards() -> list[dict[str, Any]]:
    return sorted(_fixture()["cards"], key=lambda card: (card["delivered_at_ms"], card["event_id"]))


def _symbols(card: dict[str, Any]) -> set[str]:
    symbols = {base_symbol(str(value)) for value in card["grounded_assets"] if value}
    symbols.update(base_symbol(str(asset["symbol"])) for asset in card["assets"] if asset.get("symbol"))
    return symbols


def _receipt(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": card["event_id"],
        "at_ms": card["delivered_at_ms"],
        "storyline_key": card["storyline_key"],
        "comparison_title": card["comparison_title"],
        "comparison_fingerprint": card["comparison_fingerprint"],
        "dedupe_family": card["dedupe_family"],
        "grounded_assets": list(card["grounded_assets"]),
        "assets": [asset["symbol"] for asset in card["assets"]],
        "canonical_assets": sorted(_symbols(card)),
        "magnitude": card["magnitude"],
        "direction": card["direction"],
        "headline_zh": card["headline_zh"],
        "why_zh": card["why_zh"],
    }


def _history_for(later: dict[str, Any], receipts: list[dict[str, Any]]):
    now_ms = int(later["verdict_at_ms"])
    return build_reader_history(
        [row for row in receipts if row["at_ms"] < now_ms and row["event_id"] != later["event_id"]],
        now_ms=now_ms,
        dedupe_family=later["dedupe_family"],
        comparison_fingerprint=later["comparison_fingerprint"],
        canonical_assets=sorted(_symbols(later)),
        comparison_title=later["comparison_title"],
    )


def _shown(later: dict[str, Any], history) -> ToldLedgerSnapshot:
    return ToldLedgerSnapshot.select(
        [row.as_told_row() for row in history.told_source_rows],
        now_ms=int(later["verdict_at_ms"]),
        storyline_key=later["storyline_key"],
        symbols=tuple(later["grounded_assets"]),
        comparison_title=later["comparison_title"],
        exclude_event_id=later["event_id"],
    )


def test_the_frozen_set_is_the_audit_it_claims_to_be() -> None:
    doc = _fixture()
    cards = _cards()
    by_id = {card["event_id"] for card in cards}

    assert doc["source"]["issue"] == 491
    assert len(cards) == 910 and len(by_id) == 910
    assert len(doc["duplicate_pairs"]) == 143
    assert Counter(pair["lost_at"] for pair in doc["duplicate_pairs"]) == {
        "recent_window": 61,
        "recent_cap": 16,
        "told_selection": 31,
        "model_progression": 35,
    }
    for pair in doc["duplicate_pairs"]:
        assert pair["earlier"] in by_id and pair["later"] in by_id
    assert all(len(ids) >= 2 for ids in doc["different_fact_groups"].values())


def test_the_recent_policy_ledger_is_the_unchanged_4h_128_newest_first_slice() -> None:
    """Widening what the Program sees must not widen what `decide()` measures against (the #491 counterfactual)."""

    cards = _cards()
    receipts = [_receipt(card) for card in cards]
    later = cards[-1]
    now_ms = int(later["verdict_at_ms"])
    history = _history_for(later, receipts)

    expected = sorted(
        (row for row in receipts if now_ms - RECENT_HISTORY_WINDOW_MS <= row["at_ms"] < now_ms),
        key=lambda row: (-row["at_ms"], row["event_id"]),
    )[:RECENT_HISTORY_MAX]
    assert [row.event_id for row in history.recent_seen_rows] == [row["event_id"] for row in expected]
    assert len(history.recent_seen_rows) == RECENT_HISTORY_MAX
    assert len(history.similar_told_rows) <= SIMILAR_TITLE_MAX
    assert all(now_ms - row.at_ms <= SIMILAR_HISTORY_WINDOW_MS for row in history.similar_told_rows)
    assert {row.reason for row in history.similar_told_rows} == {"title_similarity"}
    assert not {row.event_id for row in history.similar_told_rows} & {row.event_id for row in history.recent_seen_rows}


def test_recall_of_the_earlier_card_into_the_shown_rows_clears_the_floor() -> None:
    doc = _fixture()
    cards = _cards()
    by_id = {card["event_id"]: card for card in cards}
    receipts = [_receipt(card) for card in cards]

    recalled: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for pair in doc["duplicate_pairs"]:
        later = by_id[pair["later"]]
        shown = _shown(later, _history_for(later, receipts))
        assert len(shown.entries) <= TOLD_MAX
        total[pair["lost_at"]] += 1
        recalled[pair["lost_at"]] += pair["earlier"] in {entry.event_id for entry in shown.entries}

    assert sum(recalled.values()) >= RECALL_FLOOR > AUDIT_DAY_RECALL, dict(recalled)
    # The band exists for the pairs the 4 h window could not see; those are where the gain has to be.
    assert recalled["recent_window"] >= 40, dict(recalled)
    assert recalled["recent_cap"] >= 12, dict(recalled)
    # Pairs the audit-day Program was already shown must, with few exceptions, still be shown.
    assert recalled["model_progression"] >= 30, dict(recalled)


def test_the_twelve_hour_venezuela_repeat_is_shown_to_the_program() -> None:
    """The extreme case of the audit: the same Chinese headline, word for word, 12.8 h apart under
    the `none` key, from two different English wires. No link in the audit-day chain ever saw the first card."""

    cards = _cards()
    by_id = {card["event_id"]: card for card in cards}
    receipts = [_receipt(card) for card in cards]
    pair = next(
        pair
        for pair in _fixture()["duplicate_pairs"]
        if pair["gap_min"] > 12 * 60 and by_id[pair["earlier"]]["headline_zh"] == by_id[pair["later"]]["headline_zh"]
    )
    later = by_id[pair["later"]]
    history = _history_for(later, receipts)

    assert pair["earlier"] in {row.event_id for row in history.similar_told_rows}
    assert pair["earlier"] in {entry.event_id for entry in _shown(later, history).entries}


def _judgment(card: dict[str, Any]) -> ScoredJudgment:
    return scored_judgment(
        {
            "novelty": "new_fact",
            "assets": [{"symbol": asset["symbol"], "role": asset["role"]} for asset in card["assets"]],
            "direction": card["direction"],
            "scope": "macro",
            "magnitude": max(2, card["magnitude"]),
            "confidence": 0.8,
            "headline_zh": card["headline_zh"],
            "why_zh": card["why_zh"],
        }
    )


def test_different_facts_in_one_template_are_never_withheld_by_the_mechanical_layer() -> None:
    """Three countries' PMI prints, a crude forecast raised beside a products forecast cut, six Robinhood Chain
    metrics: `decide()` measured against the 4 h recent ledger keeps releasing every later card of each group.
    The Program may be shown the earlier ones; that is what the novelty examples in the seed are for."""

    doc = _fixture()
    cards = _cards()
    by_id = {card["event_id"]: card for card in cards}
    receipts = [_receipt(card) for card in cards]
    facts = GateFacts(grounded_assets=(), watchlist_symbols=frozenset(), admission="candidate")

    checked = 0
    for group, event_ids in doc["different_fact_groups"].items():
        members = sorted((by_id[event_id] for event_id in event_ids), key=lambda card: card["delivered_at_ms"])
        for later in members[1:]:
            history = _history_for(later, receipts)
            seen = [row.as_told_row() for row in history.recent_seen_rows]
            status = storyline_status(later["storyline_key"], told=[], seen=seen)
            decision = decide(_judgment(later), facts, status, policy=DEFAULT_POLICY)
            assert decision.final == "push", (group, later["headline_zh"], decision)
            checked += 1
    assert checked >= 10
