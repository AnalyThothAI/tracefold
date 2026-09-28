"""Receipt recall ranks actual delivered copy before applying the model budget."""

from __future__ import annotations

from typing import Any

from tests.support.news_update_semantic import draft, material
from tracefold.news.storage.event_updates import receipt_queries, select_receipts
from tracefold.news.updates.contracts import Extraction, FrozenInput
from tracefold.news.updates.identity import digest
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000
ETHENA = "Ethena expands USDe backing strategy into bStocks and equity perpetuals on Binance"


def head(text: str = ETHENA):
    evidence = material(text)
    update = assemble_update(
        FrozenInput(event_id="current", revision=1, lineage_id="current", evidence=(evidence,)),
        Extraction(claims=(draft(evidence),)),
        None,
        adopted_at_ms=STAMP,
    )
    assert update is not None
    return update


def receipt(intent: str, event: str, text: str, *, title: str = "", at: int = STAMP - 1000, refs=()) -> dict[str, Any]:
    return {
        "intent_id": intent,
        "event_id": event,
        "kind": "update",
        "body": text,
        "payload_sha256": digest(text),
        "settled_at_ms": at,
        "receipt": {},
        "history_context": {"comparison_title": title},
        "claim_refs": list(refs),
    }


def test_relevant_recent_receipt_is_not_crowded_out_by_older_asset_band() -> None:
    update = head()
    rows = [receipt(f"noise-{i}", f"other-{i}", "币安钱包转账", title="Binance wallet transfer") for i in range(65)]
    rows.append(receipt("ethena", "previous", "Ethena扩展USDe策略至股票永续", title=ETHENA))
    chosen = select_receipts(update, receipt_queries(update, ETHENA), rows)
    assert len(chosen) == 16
    assert chosen[0].intent_id == "ethena"
    assert chosen[0].body == "Ethena扩展USDe策略至股票永续"


def test_newest_incremental_card_does_not_replace_an_earlier_receipt() -> None:
    update = head("Agency approved the project.")
    rows = [
        receipt("later-B", "other", "生效日期为下月", title="The effective date is next month", at=STAMP - 1),
        receipt("earlier-A", "other", "机构批准该项目", title="Agency approved the project.", at=STAMP - 2000),
    ]
    chosen = select_receipts(update, receipt_queries(update, ""), rows)
    assert [row.intent_id for row in chosen] == ["earlier-A", "later-B"]
    assert chosen[0].body == "机构批准该项目"


def test_same_event_does_not_spend_the_entire_budget_ahead_of_relevant_copy() -> None:
    update = head()
    rows = [receipt(f"own-{i}", update.event_id, "无关消息", title="Unrelated background") for i in range(20)]
    relevant = receipt("related", "another", "Ethena扩展策略", title=ETHENA, at=STAMP - 3000)
    chosen = select_receipts(update, receipt_queries(update, ""), [*rows, relevant, relevant])
    assert len(chosen) == 16
    assert [row.intent_id for row in chosen].count("related") == 1
    assert chosen[0].intent_id == "related"


def test_explicit_claim_reference_is_priority_but_never_substitutes_actual_body() -> None:
    update = head()
    rows = [
        receipt("same-title", "other", "旧卡实际只说了规模", title=ETHENA),
        receipt("linked", "other", "实际收到的另一段文字", refs=[update.claims[0].ref], at=STAMP - 4000),
    ]
    chosen = select_receipts(update, receipt_queries(update, ""), rows, limit=1)
    assert chosen[0].intent_id == "linked"
    assert chosen[0].body == "实际收到的另一段文字"


def test_later_claim_and_original_quote_are_queries_even_when_leader_is_unrelated() -> None:
    update = head("项目获批，批准方仍未披露。")
    queries = receipt_queries(update, "Original English leader")
    assert update.claims[0].statement in queries
    assert update.claims[0].citations[0].quote in queries
    assert receipt_queries(update, "Original English leader", [update.claims[0].ref]) == ("Original English leader",)
