"""Immutable evidence preparation values and deterministic, bounded selection.

This module does no I/O. Offsets are Unicode codepoints in a named persisted
text field; a selected excerpt is always a contiguous slice of that field.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifact_identity import canonical_sha
from .events.tokens import comparison_tokens
from .models import MarketAsset

EVIDENCE_INPUT_VERSION: Final = "news_evidence_input_v2"
SELECTOR_VERSION: Final = "local_focus_sentences_v2"
TEXT_VERSION: Final = "provider_plaintext_v1"
CURRENT_CHARS: Final = 6000
RELATED_CHARS: Final = 2400
RELATED_MAX: Final = 4
BACKGROUND_WINDOW_MS: Final = 30 * 86_400_000
RELATION_MAX: Final = 8
ENTITY_MAX: Final = 24
SIMILAR_MAX: Final = 32
CANDIDATE_MAX: Final = 64
CURRENT_SPANS: Final = 12
RELATED_SPANS: Final = 24
MEMBER_CANDIDATES: Final = 16
MEMBER_MATERIALS: Final = 4


class Exact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisibleEvidenceSpan(Exact):
    ref_id: str
    text: str
    source: str
    url: str
    reported_published_at_ms: int | None
    available_at_ms: int | None
    coverage_status: str


class EvidenceSpan(Exact):
    ref_id: str
    material_kind: Literal["current", "related"]
    source_item_id: str = ""
    source_artifact_id: str = ""
    content_sha256: str
    extraction_version: str
    text_space: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    text: str
    source: str = ""
    url: str = ""
    reported_published_at_ms: int | None = None
    available_at_ms: int | None = None
    selection_reason: str
    coverage_status: str

    @model_validator(mode="after")
    def _span_length_matches_text(self) -> EvidenceSpan:
        if self.span_end - self.span_start != len(self.text):
            raise ValueError("news_evidence_span_length_mismatch")
        return self

    def visible(self) -> VisibleEvidenceSpan:
        return VisibleEvidenceSpan.model_validate(
            self.model_dump(
                include={
                    "ref_id",
                    "text",
                    "source",
                    "url",
                    "reported_published_at_ms",
                    "available_at_ms",
                    "coverage_status",
                }
            )
        )


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


class PreparedEvidence(Exact):
    input_version: Literal["news_evidence_input_v2"] = EVIDENCE_INPUT_VERSION
    selector_version: str = SELECTOR_VERSION
    cutoff_at_ms: int
    current_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=CURRENT_SPANS)
    related_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=RELATED_SPANS)
    query: EvidenceQuery | None = None
    candidate_count: int = 0
    selected_count: int = 0
    exclusions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    member_candidate_count: int = 0
    member_selected_count: int = 0
    elapsed_ms: int = 0
    token_measurement: str = "utf8_bytes_upper_bound_not_tokenizer"

    @model_validator(mode="after")
    def _budgets_and_refs(self) -> PreparedEvidence:
        for spans, chars, kind in (
            (self.current_evidence, CURRENT_CHARS, "current"),
            (self.related_evidence, RELATED_CHARS, "related"),
        ):
            if sum(len(s.text) for s in spans) > chars or any(s.material_kind != kind for s in spans):
                raise ValueError("news_evidence_budget_or_kind_invalid")
        refs = [s.ref_id for s in (*self.current_evidence, *self.related_evidence)]
        if len(refs) != len(set(refs)):
            raise ValueError("news_evidence_duplicate_ref")
        return self


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
_QUALIFICATION = re.compile(
    r"\b(?:non-binding|binding|pending|approval|approved|regulatory|agreement|subject to|"
    r"if|unless|conditional|expects?|claims?|according to|effective|completed|not yet)\b"
    r"|尚未|批准|前提|条件|声称|据称|协议|生效|完成",
    re.I,
)


def evidence_terms(text: str) -> frozenset[str]:
    separated = re.sub(
        r"([a-zA-Z0-9])([\u4e00-\u9fff])|([\u4e00-\u9fff])([a-zA-Z0-9])",
        lambda m: (m[1] or m[3]) + " " + (m[2] or m[4]),
        text,
    )
    return comparison_tokens(separated.lower()) - _GENERIC


def _sentences(text: str) -> list[tuple[int, int]]:
    # Decimal points belong to their number; offsets always address the original text.
    boundaries = [m.end() for m in re.finditer(r"[。！？\n]+|[.!?](?!\d)(?:\s+|$)", text)]
    points = [0, *boundaries]
    if points[-1] != len(text):
        points.append(len(text))
    return [(a, b) for a, b in pairwise(points) if text[a:b].strip()]


def _ranges(
    text: str, budget: int, *, focus: str = "", max_spans: int = 6, seen: set[str] | None = None
) -> list[tuple[int, int]]:
    if not text or budget <= 0 or max_spans <= 0:
        return []
    sentences = _sentences(text)
    focus_terms = evidence_terms(focus)
    overlaps = [len(evidence_terms(text[a:b]) & focus_terms) for a, b in sentences]
    anchors = {i for i, score in enumerate(overlaps) if score > 0} or {0}
    best_focus = max(range(len(sentences)), key=lambda i: (overlaps[i], -i), default=0)
    ranked = []
    for i, (a, b) in enumerate(sentences):
        nearby = any(abs(i - anchor) <= 2 for anchor in anchors)
        qualified = bool(_QUALIFICATION.search(text[a:b]))
        # Conditions must be near the focus or carry a concrete event cue; arbitrary
        # negations in unrelated boilerplate do not get global priority.
        relevant_condition = qualified and (nearby or bool(evidence_terms(text[a:b]) & _EVENT_CUES))
        rank = 0 if i == best_focus else 1 if relevant_condition else 2 if overlaps[i] else 3 if nearby else 4
        ranked.append((rank, -overlaps[i], i, a, b))
    chosen: list[tuple[int, int]] = []
    keys = set(seen or ())
    used = 0
    for _, _, _, a, b in sorted(ranked):
        key = text[a:b].strip()
        if key in keys or used + b - a > budget:
            continue
        tentative = sorted([*chosen, (a, b)])
        groups = sum(i == 0 or tentative[i - 1][1] != lo for i, (lo, _) in enumerate(tentative))
        if groups > max_spans:
            continue
        chosen = tentative
        used += b - a
        keys.add(key)
    if not chosen and not seen:
        # A single oversize sentence is explicitly truncated, never joined across a gap.
        start = sentences[best_focus][0] if sentences else 0
        chosen = [(start, min(len(text), start + budget))]
    merged: list[tuple[int, int]] = []
    for a, b in chosen:
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if seen is not None:
        for a, b in chosen:
            seen.add(text[a:b].strip())
    return merged


def select_item(
    card: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    kind: Literal["current", "related"],
    cutoff: int,
    budget: int,
    prefix: str,
    reason: str,
    max_spans: int = 6,
    seen: set[str] | None = None,
) -> tuple[EvidenceSpan, ...]:
    available = item.get("provider_params_available_at_ms")
    text = str(item.get("evidence_text") or "") if available is not None and int(available) <= cutoff else ""
    status = "complete"
    space = "news_items.evidence_text"
    version = TEXT_VERSION
    if not text:
        text = (
            str(card["fact_text"])
            if card.get("fact_text")
            else "\n".join(str(card.get(key) or "") for key in ("leader_title", "raw_first_line", "leader_description"))
        )
        space, version, status = (
            "frozen_member.fact_text" if card.get("fact_text") else "frozen_card.title_first_line_description",
            "snapshot_v3",
            "legacy_excerpt_only",
        )
        available = None
    source_text = text
    regions = [(0, len(text))]
    if (
        str(card.get("focus_fact_method") or "") == "explicit_numbered"
        or re.search(r"(?m)^\s*\d{1,2}[.)、:：]\s*", text)
    ) and space == "news_items.evidence_text":
        # Match the exact admitted fact, and include only the shared preamble.
        focus = str(card.get("fact_text") or card.get("leader_title") or "")
        start = text.find(focus)
        first = re.search(r"(?m)^\s*\d{1,2}[.)、:：]\s*", text)
        if start < 0 or first is None:
            source_text = focus
            regions = [(0, len(source_text))]
            space, version, status = "frozen_card.title_description", "snapshot_v3", "relation_unproven"
        else:
            regions = [(0, first.start()), (start, start + len(focus))]
    total = sum(end - start for start, end in regions)
    if total > budget:
        status = "selection_truncated"
    spans: list[EvidenceSpan] = []
    remaining = budget
    # The numbered fact has priority over its shared lead.
    for start, end in reversed(regions):
        if remaining <= 0 or len(spans) >= max_spans:
            break
        for lo, hi in _ranges(
            source_text[start:end],
            remaining,
            focus=str(card.get("fact_text") or card.get("leader_title") or ""),
            max_spans=max_spans - len(spans),
            seen=seen,
        ):
            spans.append(
                EvidenceSpan(
                    ref_id=f"{prefix}{len(spans) + 1}",
                    material_kind=kind,
                    source_item_id=str(item.get("item_id") or card.get("leader_item_id") or ""),
                    source_artifact_id=str(item.get("source_artifact_id") or ""),
                    content_sha256=text_sha(source_text),
                    extraction_version=version,
                    text_space=space,
                    span_start=start + lo,
                    span_end=start + hi,
                    text=source_text[start + lo : start + hi],
                    source=str(item.get("reporting_origin") or ""),
                    url=str(item.get("canonical_url") or ""),
                    available_at_ms=available,
                    reported_published_at_ms=item.get("published_at_ms"),
                    selection_reason=reason,
                    coverage_status=status,
                )
            )
            remaining -= hi - lo
    return tuple(spans)


def frozen_members(card: Mapping[str, Any]) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    leader_id = str(card.get("leader_item_id") or "")
    members = sorted(
        card.get("evidence_members") or (),
        key=lambda row: (int(row.get("joined_at_ms") or 0), str(row["item_id"]), str(row["fact_id"])),
    )
    leader = next(
        (dict(row) for row in members if row["item_id"] == leader_id and row["fact_id"] == card.get("focus_fact_id")),
        {"item_id": leader_id, "fact_id": card.get("focus_fact_id", ""), "fact_text": card.get("leader_title", "")},
    )
    ordered = [leader, *(dict(row) for row in members if row["item_id"] != leader_id)]
    return ordered[:MEMBER_CANDIDATES], (("member_candidates_truncated",) if len(ordered) > MEMBER_CANDIDATES else ())


def select_members(
    members: Sequence[Mapping[str, Any]], metadata: Sequence[Mapping[str, Any]], *, cutoff: int
) -> list[dict[str, Any]]:
    by_id = {row["item_id"]: row for row in metadata}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for member in members:
        row = {**member, **by_id.get(member["item_id"], {})}
        available = row.get("provider_params_available_at_ms")
        digest = row.get("evidence_text_sha256") if available is not None and int(available) <= cutoff else None
        digest = None if digest == text_sha("") else digest
        key = ("body", str(digest)) if digest else ("fact", str(member.get("fact_text") or ""))
        if key not in seen:
            seen.add(key)
            result.append(row)
    # Leader always first; available complete material precedes excerpt-only supplements.
    return (
        result[:1]
        + sorted(
            result[1:],
            key=lambda r: (
                not (
                    r.get("evidence_text_sha256")
                    and r.get("provider_params_available_at_ms") is not None
                    and int(r["provider_params_available_at_ms"]) <= cutoff
                )
            ),
        )[: MEMBER_MATERIALS - 1]
    )


def assemble_evidence(
    card: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    query: EvidenceQuery,
    candidates: Sequence[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]] = (),
    exclusions: Sequence[str] = (),
    elapsed_ms: int = 0,
) -> PreparedEvidence:
    current: list[EvidenceSpan] = []
    seen: set[str] = set()
    selected_members = [item, *members]
    missing: set[str] = set()
    for index, row in enumerate(selected_members):
        member_card = (
            {**card, "fact_text": row.get("fact_text")}
            if index == 0
            else {**row, "leader_title": row.get("fact_text", "")}
        )
        remaining = CURRENT_CHARS - sum(len(s.text) for s in current)
        # Reserve a bounded share for supplements while preserving the leader focus first.
        budget = min(remaining, CURRENT_CHARS - 1000 * len(members)) if index == 0 else remaining
        spans = select_item(
            member_card,
            row,
            kind="current",
            cutoff=query.cutoff_at_ms,
            budget=budget,
            prefix="c",
            reason="current_focus" if index == 0 else "frozen_member",
            max_spans=min(6, CURRENT_SPANS - len(current)),
            seen=seen,
        )
        current.extend([s.model_copy(update={"ref_id": f"c{len(current) + i + 1}"}) for i, s in enumerate(spans)])
        if not spans and index:
            missing.add("member_duplicate_or_budget_omitted")
        conflict_at = row.get("provider_params_conflict_at_ms")
        if conflict_at is not None and int(conflict_at) <= query.cutoff_at_ms:
            missing.add("provider_payload_conflict")
    related: list[EvidenceSpan] = []
    origins: set[tuple[str, str]] = set()
    excluded = list(exclusions)
    selected = 0
    for row in candidates:
        remaining = RELATED_CHARS - sum(len(s.text) for s in related)
        if selected >= RELATED_MAX or remaining <= 0:
            break
        origin = str(row.get("source_artifact_id") or row.get("canonical_url") or row["item_id"])
        key = (origin, str(row.get("comparison_fingerprint") or row.get("leader_title") or ""))
        if key in origins:
            excluded.append("duplicate_origin_fact")
            continue
        origins.add(key)
        spans = select_item(
            row,
            row,
            kind="related",
            cutoff=query.cutoff_at_ms,
            budget=remaining,
            prefix=f"r{selected + 1}.",
            reason=str(row.get("retrieval_reason") or "text_similarity"),
            max_spans=min(6, RELATED_SPANS - len(related)),
            seen=seen,
        )
        if spans:
            related.extend(spans)
            selected += 1
    missing.update(s.coverage_status for s in current if s.coverage_status != "complete")
    return PreparedEvidence(
        cutoff_at_ms=query.cutoff_at_ms,
        current_evidence=tuple(current),
        related_evidence=tuple(related),
        query=query,
        candidate_count=len(candidates),
        selected_count=selected,
        exclusions=tuple(excluded),
        missing=tuple(sorted(missing)),
        elapsed_ms=elapsed_ms,
        member_selected_count=len(selected_members),
    )


EVIDENCE_SELECTION_SHA256: Final = canonical_sha(
    {
        "input": EVIDENCE_INPUT_VERSION,
        "selector": SELECTOR_VERSION,
        "text": TEXT_VERSION,
        "current_chars": CURRENT_CHARS,
        "related_chars": RELATED_CHARS,
        "related_max": RELATED_MAX,
        "window_ms": BACKGROUND_WINDOW_MS,
        "channel_caps": [RELATION_MAX, ENTITY_MAX, SIMILAR_MAX],
        "candidate_max": CANDIDATE_MAX,
        "member_caps": [MEMBER_CANDIDATES, MEMBER_MATERIALS],
        "span_caps": [CURRENT_SPANS, RELATED_SPANS],
    }
)


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
