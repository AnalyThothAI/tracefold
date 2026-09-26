"""Provider evidence text, related-Event candidate queries, and archived execution evidence views.

This module does no I/O. The bounded span selector that packed evidence for the retired three-Predictor
Program was deleted with it (#706); archived executions it produced stay readable through
`execution_evidence_views`.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from .events.tokens import comparison_tokens
from .models import MarketAsset

BACKGROUND_WINDOW_MS: Final = 30 * 86_400_000
RELATED_MAX: Final = 4
RELATION_MAX: Final = 8
ENTITY_MAX: Final = 24
SIMILAR_MAX: Final = 32
CANDIDATE_MAX: Final = 64


class Exact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceQuery(Exact):
    event_id: str
    focus_fact_id: str
    source_artifact_id: str = ""
    canonical_url: str = ""
    title: str
    assets: tuple[MarketAsset, ...] = ()
    terms: tuple[str, ...] = ()
    cutoff_at_ms: int
    window_ms: int = BACKGROUND_WINDOW_MS


def normalized_provider_text(params: Mapping[str, Any]) -> str:
    value = params.get("text")
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<br\s*/?>|</(?:p|div)>", "\n", value, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()).strip()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query_for(
    card: Mapping[str, Any], item: Mapping[str, Any], *, cutoff: int, assets: Sequence[MarketAsset] = ()
) -> EvidenceQuery:
    title = str(card.get("leader_title") or "")
    terms = tuple(sorted(evidence_terms(title) | {a.symbol.lower() for a in assets}))[:32]
    return EvidenceQuery(
        event_id=str(card.get("event_id") or ""),
        focus_fact_id=str(card.get("focus_fact_id") or ""),
        title=title,
        source_artifact_id=str(item.get("source_artifact_id") or ""),
        canonical_url=str(item.get("canonical_url") or ""),
        assets=tuple(assets),
        terms=terms,
        cutoff_at_ms=cutoff,
    )


# Retrieval uses the existing tokenizer without changing Event/fact identity.
_GENERIC = frozenset(
    {
        "announce",
        "report",
        "company",
        "news",
        "update",
        "today",
        "inc",
        "corp",
        "发布",
        "宣布",
        "公司",
        "报道",
        "消息",
        "今日",
        "表示",
    }
)
_EVENT_CUES = frozenset(
    {
        "acquire",
        "acquisition",
        "agreement",
        "merger",
        "approve",
        "approval",
        "regulatory",
        "buyback",
        "sell",
        "sale",
        "split",
        "dividend",
        "file",
        "lawsuit",
        "investment",
        "funding",
        "invest",
        "earnings",
        "revenue",
        "profit",
        "production",
        "outflow",
        "inflow",
        "withdrawal",
        "deposit",
        "closure",
        "halt",
        "resume",
        "sanction",
        "tariff",
        "inflation",
        "cpi",
        "jobs",
        "settlement",
        "contract",
        "deal",
        "stake",
        "partnership",
        "投资",
        "融资",
        "营收",
        "利润",
        "关税",
        "通胀",
        "资金",
        "流入",
        "流出",
        "存款",
        "撤资",
        "launch",
        "list",
        "delist",
        "increase",
        "decrease",
        "rate",
        "收购",
        "批准",
        "协议",
        "回购",
        "拆股",
        "上市",
        "利率",
        "合作",
        "监管",
        "诉讼",
    }
)


def evidence_terms(text: str) -> frozenset[str]:
    separated = re.sub(
        r"([a-zA-Z0-9])([\u4e00-\u9fff])|([\u4e00-\u9fff])([a-zA-Z0-9])",
        lambda m: (m[1] or m[3]) + " " + (m[2] or m[4]),
        text,
    )
    return comparison_tokens(separated.lower()) - _GENERIC


def execution_evidence_views(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read frozen executions only; failed and superseded calls remain inspectable."""
    views = []
    for verdict in verdicts:
        trace = dict(verdict.get("trace") or {})
        for execution in trace.get("program_executions") or ():
            context = dict(execution.get("context") or {})
            prepared = context.get("prepared_evidence")
            if not isinstance(prepared, Mapping):
                continue
            refs: list[str] = []
            card_output_seen = False
            for call in dict(execution.get("trace") or {}).get("calls") or ():
                if call.get("predictor") == "reader_card" and isinstance(call.get("validated_output"), Mapping):
                    card_output_seen = True
                    value = call["validated_output"]
                    refs.extend(dict(value.get("card") or value).get("source_refs") or ())
            views.append(
                {
                    "execution_index": execution["execution_index"],
                    "status": execution["status"],
                    "selected": execution["execution_index"] == trace.get("program_execution_index"),
                    "focus_fact_id": dict(context.get("evidence") or {}).get("focus_fact_id", ""),
                    "input_version": prepared["input_version"],
                    "cutoff_at_ms": prepared["cutoff_at_ms"],
                    "current_evidence": prepared["current_evidence"],
                    "related_evidence": prepared["related_evidence"],
                    "missing": prepared.get("missing", []),
                    "exclusions": prepared.get("exclusions", []),
                    # Archive-only fields; new executions never emit a webpage receipt.
                    **(
                        {
                            "document_status": prepared["document_status"],
                            "document_receipt": prepared.get("document_receipt", {}),
                        }
                        if "document_status" in prepared
                        else {}
                    ),
                    "candidate_count": prepared["candidate_count"],
                    "selected_count": prepared["selected_count"],
                    "declared_source_refs": list(dict.fromkeys(refs)),
                    "reference_issues": ["empty_source_refs"] if card_output_seen and not refs else [],
                    "elapsed_ms": prepared.get("elapsed_ms", 0),
                }
            )
    return views


def _relevant_candidate(row: Mapping[str, Any], query: EvidenceQuery) -> bool:
    assets = [MarketAsset.of(a) for a in row.get("assets") or ()]
    assets.extend(
        MarketAsset.of({"symbol": symbol, "market_type": row.get("asset_class")})
        for symbol in row.get("grounded_assets") or ()
    )
    # Evaluate all known types before considering unknown tags. A provider's unknown
    # tag cannot erase an explicit equity/crypto contradiction for the same symbol.
    if any(
        a.symbol == b.symbol and a.market_type != b.market_type and "unknown" not in (a.market_type, b.market_type)
        for a in query.assets
        for b in assets
    ):
        return False
    terms = evidence_terms(str(row.get("leader_title") or row.get("comparison_title") or ""))
    shared = terms & evidence_terms(query.title)
    symbols = {a.symbol.lower() for a in (*query.assets, *assets)}
    specific = shared - symbols
    # A shared issuer/coin or generic announcement is insufficient. Preserve a
    # concrete event cue together with a shared subject/asset clue, including CJK.
    subject_clues = {term for term in shared - _EVENT_CUES if not term.isdigit()}
    asset_overlap = {a.symbol for a in query.assets} & {a.symbol for a in assets}
    return bool(specific & _EVENT_CUES) and bool(subject_clues or asset_overlap)


def shortlist(rows: Sequence[Mapping[str, Any]], *, query: EvidenceQuery) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(rows, key=lambda r: (r["priority"], -r["score"], -r["created_at_ms"], r["event_id"])):
        if not _relevant_candidate(row, query):
            continue
        origin = str(row.get("source_artifact_id") or row.get("canonical_url") or row["item_id"])
        key = (origin, str(row["comparison_fingerprint"]))
        if key not in seen:
            seen.add(key)
            result.append(dict(row))
        if len(result) == RELATED_MAX:
            break
    return result
