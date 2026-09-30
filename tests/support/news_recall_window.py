"""Receipt windows for lexical recall: document frequency is only meaningful over a realistic 48 h window.

Both the pure selector (`select_for_claim` without SQL routes) and the PostgreSQL route read the same receipts
here, so the tests can compare them term for term.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from tracefold.news.notifications.recall import WORD_PATTERN
from tracefold.news.updates.identity import identity

GOLD_RECALL = Path(__file__).resolve().parents[1] / "fixtures/news/issue_750_gold_recall.json"
# The 48 h sent window read-only on 2026-09-29 14:13 UTC: 1,321 receipts, and the document frequency of the
# gold claim's generic terms in it. The frozen gold receipts are part of that window; filler stands in for
# the rest so the gold claim's terms are as common as they were in production.
WINDOW_RECEIPTS = 1_321
PRODUCTION_DF = {"prices": 0.064, "week": 0.030, "gold": 0.020, "low": 0.011}


def gold_fixture() -> dict[str, Any]:
    return json.loads(GOLD_RECALL.read_text("utf-8"))


def _words(text: str) -> set[str]:
    return {match.lower() for match in re.findall(WORD_PATTERN, text)}


def gold_window_filler(fixture: dict[str, Any]) -> list[tuple[str, str, int]]:
    """`(intent_id, body, settled_at_ms)` for the rest of the gold claim's window, without claims.

    Each generic term goes into as many filler bodies as the production share requires beyond the frozen
    receipts that already carry it; every other filler word occurs once.
    """

    carried = Counter(
        term
        for row in fixture["candidates"]
        for term in _words(" ".join((row["body"], *(claim["statement"] for claim in row["claims"]))))
        if term in PRODUCTION_DF
    )
    bodies = [
        f"{term} filler{term}{index}"
        for term, share in PRODUCTION_DF.items()
        for index in range(round(share * WINDOW_RECEIPTS) - carried[term])
    ]
    bodies += [f"filler{index}" for index in range(WINDOW_RECEIPTS - len(fixture["candidates"]) - len(bodies))]
    settled = int(fixture["as_of_ms"]) - 3_600_000
    return [(identity("intent", "gold-filler", index), body, settled - index) for index, body in enumerate(bodies)]


# A House committee probe into prediction markets (production, 2026-09-29): the claim shares "market" and
# "trading" (and 市场, 交易) with unrelated receipts, and rare terms only with the receipt about the probe.
PROBE_STATEMENT = (
    "The House Oversight Committee expanded its prediction market insider trading probe to Hyperliquid, "
    "Crypto.com, and PredictIt"
)
PROBE_STATEMENT_ZH = "美国众议院监督委员会将预测市场内幕交易调查扩大至Hyperliquid"
PROBE_RECEIPT = "probe"
PROBE_NOISE = ("kyiv", "credits", "iran", "premarket", "kyiv-zh", "iran-zh")


def probe_window(as_of_ms: int) -> list[tuple[str, str, str, int]]:
    """`(key, intent_id, text, settled_at_ms)`; `text` is the body plus the English statement it carried."""

    texts = {
        PROBE_RECEIPT: "众议院监督委员会将预测市场内幕交易调查扩大至Hyperliquid、Crypto.com与PredictIt "
        "House Oversight Committee widens its insider trading probe to Hyperliquid",
        "kyiv": "Russian drone hits a Kyiv market as trading halts",
        "credits": "Trump administration ends the fuel-economy credit trading market",
        "iran": "Iran stock market index falls in thin trading",
        "premarket": "Arm and Intel rise in premarket trading as the chip market rallies",
        "kyiv-zh": "基辅市场遭无人机袭击，交易暂停",
        "iran-zh": "伊朗股票市场指数在清淡交易中下跌",
    }
    # The rest of the window, as common as in production: English statements full of function words, and
    # "market", "trading", 市场 and 交易 in a large share of receipts.
    filler = {
        f"filler-{index}": (
            "The market and its index closed flat",
            "Trading in the bond and its futures was thin",
            "市场 交易 平淡",
            "The fund said its outlook and the rate path held",
        )[index % 4]
        + f" {index}"
        for index in range(200)
    }
    return [
        (key, identity("intent", "probe-window", key), text, as_of_ms - 3_600_000 - number)
        for number, (key, text) in enumerate({**texts, **filler}.items())
    ]
