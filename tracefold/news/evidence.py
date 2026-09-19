"""Immutable evidence preparation values and deterministic, bounded selection.

This module does no I/O. Offsets are Unicode codepoints in a named persisted
text field; a selected excerpt is always a contiguous slice of that field.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifact_identity import canonical_sha
from .models import MarketAsset

EVIDENCE_INPUT_VERSION: Final = "news_evidence_input_v1"
SELECTOR_VERSION: Final = "focus_sentences_v1"
TEXT_VERSION: Final = "provider_plaintext_v1"
CURRENT_CHARS: Final = 6000
RELATED_CHARS: Final = 2400
RELATED_MAX: Final = 4
BACKGROUND_WINDOW_MS: Final = 30 * 86_400_000
RELATION_MAX: Final = 8
ENTITY_MAX: Final = 24
SIMILAR_MAX: Final = 32
CANDIDATE_MAX: Final = 64
DOCUMENT_TIMEOUT_SECONDS: Final = 2.0
DOCUMENT_MAX_BYTES: Final = 2 * 1024 * 1024
DOCUMENT_MAX_CHARS: Final = 100_000
DOCUMENT_REDIRECT_MAX: Final = 2
DOCUMENT_CONCURRENCY: Final = 2
DOCUMENT_CACHE_MS: Final = 3_600_000


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
    document_id: str = ""
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


class DocumentResult(Exact):
    status: str
    requested_url: str = ""
    final_url: str = ""
    normalized_url: str = ""
    document_id: str = ""
    response_sha256: str = ""
    extracted_text_sha256: str = ""
    extractor_version: str = ""
    extracted_text: str = ""
    reported_published_at_ms: int | None = None
    observed_at_ms: int = 0
    available_at_ms: int = 0
    content_type: str = ""
    elapsed_ms: int = 0
    physical_requests: int = 0
    retry_after: str | None = None


class NewsDocumentReader(Protocol):
    async def read(self, url: str) -> DocumentResult: ...
    async def close(self) -> None: ...


class PreparedEvidence(Exact):
    input_version: Literal["news_evidence_input_v1"] = EVIDENCE_INPUT_VERSION
    selector_version: str = SELECTOR_VERSION
    cutoff_at_ms: int
    current_evidence: tuple[EvidenceSpan, ...] = ()
    related_evidence: tuple[EvidenceSpan, ...] = ()
    query: EvidenceQuery | None = None
    candidate_count: int = 0
    selected_count: int = 0
    exclusions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    document_status: str = "not_attempted"
    document_receipt: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0
    token_measurement: str = "utf8_bytes_upper_bound_not_tokenizer"


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
    terms = tuple(dict.fromkeys(re.findall(r"[a-zA-Z]{4,}|[\u4e00-\u9fff]{2,8}", title.lower())))[:16]
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


def _ranges(text: str, budget: int) -> list[tuple[int, int]]:
    if len(text) <= budget:
        return [(0, len(text))] if text else []
    # Keep complete sentences/paragraphs when possible, including tail conditions.
    # Unselected middles are explicit truncation, never a synthetic continuous quote.
    sentences = [(m.start(), m.end()) for m in re.finditer(r"[^。！？.!?\n]+[。！？.!?\n]*", text)]
    selected: list[tuple[int, int]] = []
    used = 0
    for start, end in [*sentences[:1], *reversed(sentences[1:])]:
        if used + end - start <= budget:
            selected.append((start, end))
            used += end - start
    if not selected:
        half = budget // 2
        selected = [(0, half), (len(text) - (budget - half), len(text))]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(selected):
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged if len(merged) <= 6 else [merged[0], *merged[-5:]]


def select_item(
    card: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    kind: Literal["current", "related"],
    cutoff: int,
    budget: int,
    prefix: str,
    reason: str,
) -> tuple[EvidenceSpan, ...]:
    available = item.get("provider_params_available_at_ms")
    text = str(item.get("evidence_text") or "") if available is not None and int(available) <= cutoff else ""
    status = "complete"
    space = "news_items.evidence_text"
    version = TEXT_VERSION
    if not text:
        text = "\n".join(str(card.get(key) or "") for key in ("leader_title", "raw_first_line", "leader_description"))
        space, version, status = "frozen_card.title_first_line_description", "snapshot_v3", "legacy_excerpt_only"
        available = None
    source_text = text
    regions = [(0, len(text))]
    if str(card.get("focus_fact_method") or "") == "explicit_numbered" and space == "news_items.evidence_text":
        # Match the exact admitted fact, and include only the shared preamble.
        focus = str(card.get("leader_title") or "")
        start = text.find(focus)
        first = re.search(r"(?m)^\s*\d{1,2}[.)、:：]\s*", text)
        if start < 0 or first is None:
            source_text = focus + "\n" + str(card.get("leader_description") or "")
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
        if remaining <= 0:
            break
        for lo, hi in _ranges(source_text[start:end], remaining):
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


def assemble_evidence(
    card: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    query: EvidenceQuery,
    candidates: Sequence[Mapping[str, Any]],
    document: DocumentResult | None = None,
    document_status: str = "not_attempted",
    elapsed_ms: int = 0,
) -> PreparedEvidence:
    current = select_item(
        card, item, kind="current", cutoff=query.cutoff_at_ms, budget=CURRENT_CHARS, prefix="c", reason="current_focus"
    )
    related: list[EvidenceSpan] = []
    exclusions: list[str] = []
    origins: set[tuple[str, str]] = set()
    selected = 0
    remaining = RELATED_CHARS
    for row in candidates:
        if selected >= RELATED_MAX or remaining <= 0:
            break
        reason = str(row.get("retrieval_reason") or "text_similarity")
        origin = str(row.get("source_artifact_id") or row.get("canonical_url") or row["item_id"])
        # Same-origin *same fact* dedup; preserve distinct states/facts from a common page.
        key = (origin, str(row.get("comparison_fingerprint") or row.get("leader_title") or ""))
        if key in origins:
            exclusions.append("duplicate_origin_fact")
            continue
        origins.add(key)
        spans = select_item(
            row,
            row,
            kind="related",
            cutoff=query.cutoff_at_ms,
            budget=min(remaining, RELATED_CHARS // RELATED_MAX),
            prefix=f"r{selected + 1}.",
            reason=reason,
        )
        related.extend(spans)
        remaining -= sum(len(span.text) for span in spans)
        selected += 1
    if document is not None and document.status == "success" and document.available_at_ms > query.cutoff_at_ms:
        exclusions.append("document_after_cutoff")
    elif document is not None and document.status == "success":
        # Exact current URL is necessary but cannot prove a new page version is this fact.
        # Require specific current words before exposing it, and keep numbered digests isolated.
        matches = sum(term in document.extracted_text.lower() for term in query.terms)
        remaining_current = CURRENT_CHARS - sum(len(span.text) for span in current)
        if matches >= 2 and card.get("focus_fact_method") != "explicit_numbered" and remaining_current > 0:
            extra = tuple(
                EvidenceSpan(
                    ref_id=f"d{i + 1}",
                    material_kind="current",
                    document_id=document.document_id,
                    content_sha256=document.extracted_text_sha256,
                    extraction_version=document.extractor_version,
                    text_space="news_evidence_documents.extracted_text",
                    span_start=lo,
                    span_end=hi,
                    text=document.extracted_text[lo:hi],
                    url=document.final_url,
                    source="current_source_page",
                    reported_published_at_ms=document.reported_published_at_ms,
                    available_at_ms=document.available_at_ms,
                    selection_reason="canonical_url_text_overlap_candidate",
                    coverage_status="selection_truncated"
                    if len(document.extracted_text) > remaining_current
                    else "complete",
                )
                for i, (lo, hi) in enumerate(_ranges(document.extracted_text, remaining_current))
            )
            current += extra
        else:
            exclusions.append("document_relation_unproven" if matches < 2 else "document_not_selected")
    missing = tuple(sorted({s.coverage_status for s in current if s.coverage_status != "complete"}))
    conflict_at = item.get("provider_params_conflict_at_ms")
    if conflict_at is not None and int(conflict_at) <= query.cutoff_at_ms:
        missing += ("provider_payload_conflict",)
    receipt = {} if document is None else document.model_dump(exclude={"extracted_text"})
    return PreparedEvidence(
        cutoff_at_ms=query.cutoff_at_ms,
        current_evidence=current,
        related_evidence=tuple(related),
        query=query,
        candidate_count=len(candidates),
        selected_count=selected,
        exclusions=tuple(exclusions),
        missing=missing,
        document_status=document_status,
        document_receipt=receipt,
        elapsed_ms=elapsed_ms,
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
            for call in dict(execution.get("trace") or {}).get("calls") or ():
                if call.get("predictor") == "reader_card" and isinstance(call.get("validated_output"), Mapping):
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
                    "document_status": prepared["document_status"],
                    "document_receipt": prepared.get("document_receipt", {}),
                    "candidate_count": prepared["candidate_count"],
                    "selected_count": prepared["selected_count"],
                    "declared_source_refs": list(dict.fromkeys(refs)),
                    "elapsed_ms": prepared.get("elapsed_ms", 0),
                }
            )
    return views


def shortlist(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(rows, key=lambda r: (r["priority"], -r["score"], -r["created_at_ms"], r["event_id"])):
        origin = str(row.get("source_artifact_id") or row.get("canonical_url") or row["item_id"])
        key = (origin, str(row["comparison_fingerprint"]))
        if key not in seen:
            seen.add(key)
            result.append(dict(row))
        if len(result) == RELATED_MAX:
            break
    return result


def document_identity(url: str, response_sha: str, extractor: str) -> str:
    return canonical_sha([url, response_sha, extractor])
