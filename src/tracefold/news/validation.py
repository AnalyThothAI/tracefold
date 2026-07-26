from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from tracefold.news.models import (
    BriefEvidenceBundle,
    GlobalBriefDraft,
    StoryAnalysisDraft,
    StoryAnalysisEvidence,
)

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)*\s*"
    r"(?:%|percent(?:age points?)?|basis points?|个?基点|亿|万|兆|bp|bps)?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:19|20)\d{2}"
    r"(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])\d{1,2}月\d{1,2}日(?![A-Za-z0-9_])"
)
_LATIN_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9.&'-]*(?:\s+[A-Z][A-Za-z0-9.&'-]*){0,3}\b")
_KNOWN_ENTITY_TERMS = (
    "美国",
    "中国",
    "俄罗斯",
    "乌克兰",
    "以色列",
    "伊朗",
    "日本",
    "欧盟",
    "英国",
    "美联储",
    "欧洲央行",
    "日本央行",
    "中国人民银行",
    "联合国",
    "北约",
    "欧佩克",
)
_ENTITY_GROUNDING_ALIASES: dict[str, tuple[str, ...]] = {
    "美国": ("us", "united states", "u.s.", "america"),
    "us": ("美国", "united states", "u.s.", "america"),
    "中国": ("china", "prc", "people's republic of china"),
    "china": ("中国", "prc", "people's republic of china"),
    "俄罗斯": ("russia", "russian federation"),
    "乌克兰": ("ukraine",),
    "以色列": ("israel",),
    "伊朗": ("iran",),
    "日本": ("japan",),
    "欧盟": ("eu", "european union"),
    "英国": ("uk", "united kingdom", "britain"),
    "美联储": ("fed", "federal reserve", "fomc"),
    "fed": ("美联储", "federal reserve", "fomc"),
    "欧洲央行": ("ecb", "european central bank"),
    "ecb": ("欧洲央行", "european central bank"),
    "日本央行": ("boj", "bank of japan"),
    "boj": ("日本央行", "bank of japan"),
    "中国人民银行": ("pboc", "people's bank of china"),
    "pboc": ("中国人民银行", "people's bank of china"),
    "联合国": ("un", "united nations"),
    "北约": ("nato",),
    "欧佩克": ("opec", "opec+"),
    "opec": ("欧佩克", "opec+"),
}
_INJECTION_MARKERS = (
    "ignore previous",
    "system prompt",
    "developer message",
    "忽略此前",
    "系统提示",
)


def validate_brief_publication(
    payload: Mapping[str, Any],
    evidence: BriefEvidenceBundle,
) -> tuple[GlobalBriefDraft | None, tuple[str, ...]]:
    errors: list[str] = []
    try:
        draft = GlobalBriefDraft.model_validate(payload)
    except Exception as exc:
        return None, (f"schema:{_bounded(exc)}",)
    expected = [str(row["story_id"]) for row in evidence.stories]
    actual = [item.story_id for item in draft.items]
    if actual != expected:
        errors.append("selected_story_coverage_or_order_mismatch")
    evidence_by_story = {
        str(row["story_id"]): _evidence_by_ref(_sequence(row.get("evidence_articles"))) for row in evidence.stories
    }
    story_rows = {str(row["story_id"]): row for row in evidence.stories}
    for item in draft.items:
        evidence_rows = evidence_by_story.get(item.story_id, {})
        allowed = set(evidence_rows)
        story = story_rows.get(item.story_id, {})
        source_text = _evidence_text((story,))
        cited_refs: set[str] = set()
        for index, fact in enumerate(item.what_happened):
            refs = set(fact.evidence_references)
            cited_refs.update(refs)
            if not refs or not refs <= allowed:
                errors.append(f"evidence_ref_integrity:{item.story_id}:{index}")
            if not _has_chinese(fact.text):
                errors.append(f"locale_zh_cn:{item.story_id}:fact:{index}")
            cited_text = _evidence_text(tuple(evidence_rows[ref] for ref in sorted(refs) if ref in evidence_rows))
            errors.extend(
                _ground_claim(
                    fact.text,
                    cited_text,
                    f"{item.story_id}:fact:{index}",
                )
            )
        rendered_text = " ".join(
            (
                item.why_it_matters,
                *(scenario.condition for scenario in item.transmission_scenarios),
                *(scenario.mechanism for scenario in item.transmission_scenarios),
                *(scenario.possible_effect for scenario in item.transmission_scenarios),
                *item.uncertainties,
                *item.watchpoints,
            )
        )
        if rendered_text and not _has_chinese(rendered_text):
            errors.append(f"locale_zh_cn:{item.story_id}")
        errors.extend(
            _ground_claim(
                rendered_text,
                source_text,
                f"{item.story_id}:interpretation",
            )
        )
        posture = str(story.get("evidence_posture") or "")
        if posture in {"contested", "corrected"} and not item.uncertainties:
            errors.append(f"evidence_posture_not_preserved:{item.story_id}")
        required_refs = _required_conflict_or_correction_refs(story)
        if not required_refs <= cited_refs:
            errors.append(f"conflict_or_correction_refs_missing:{item.story_id}")
    global_text = " ".join(
        (
            draft.headline,
            draft.executive_summary,
            *draft.narratives,
            *draft.global_watchpoints,
        )
    )
    if not _has_chinese(global_text):
        errors.append("locale_zh_cn:brief")
    errors.extend(_ground_claim(global_text, _evidence_text(evidence.stories), "brief"))
    errors.extend(_prompt_injection_errors(draft.model_dump_json()))
    return (draft if not errors else None), tuple(dict.fromkeys(errors))


def validate_story_publication(
    payload: Mapping[str, Any],
    evidence: StoryAnalysisEvidence,
) -> tuple[StoryAnalysisDraft | None, tuple[str, ...]]:
    errors: list[str] = []
    try:
        draft = StoryAnalysisDraft.model_validate(payload)
    except Exception as exc:
        return None, (f"schema:{_bounded(exc)}",)
    evidence_rows = _evidence_by_ref(evidence.articles)
    allowed = set(evidence_rows)
    source_text = _evidence_text(evidence.articles)
    cited_refs: set[str] = set()
    for index, fact in enumerate(draft.what_happened):
        refs = set(fact.evidence_references)
        cited_refs.update(refs)
        if not refs or not refs <= allowed:
            errors.append(f"evidence_ref_integrity:{index}")
        if not _has_chinese(fact.text):
            errors.append(f"locale_zh_cn:fact:{index}")
        cited_text = _evidence_text(tuple(evidence_rows[ref] for ref in sorted(refs) if ref in evidence_rows))
        errors.extend(
            _ground_claim(
                fact.text,
                cited_text,
                f"{evidence.story_id}:fact:{index}",
            )
        )
    rendered_text = " ".join(
        (
            draft.why_it_matters,
            draft.political_impact,
            draft.economic_market_impact,
            *draft.disagreements_unknowns,
            *(scenario.condition for scenario in draft.transmission_scenarios),
            *(scenario.mechanism for scenario in draft.transmission_scenarios),
            *(scenario.possible_effect for scenario in draft.transmission_scenarios),
            draft.next_checkpoint,
        )
    )
    if not _has_chinese(rendered_text):
        errors.append("locale_zh_cn")
    errors.extend(
        _ground_claim(
            rendered_text,
            source_text,
            f"{evidence.story_id}:interpretation",
        )
    )
    if evidence.evidence_posture in {"contested", "corrected"} and not draft.disagreements_unknowns:
        errors.append(f"evidence_posture_not_preserved:{evidence.story_id}")
    required_refs = _required_conflict_or_correction_refs(
        {
            "evidence_factors": evidence.evidence_factors,
            "evidence_articles": evidence.articles,
        }
    )
    if not required_refs <= cited_refs:
        errors.append(f"conflict_or_correction_refs_missing:{evidence.story_id}")
    errors.extend(_prompt_injection_errors(draft.model_dump_json()))
    return (draft if not errors else None), tuple(dict.fromkeys(errors))


def validate_publication(
    *,
    publication_kind: Literal["brief", "story_analysis"],
    payload: Mapping[str, Any],
    evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
) -> tuple[GlobalBriefDraft | StoryAnalysisDraft | None, tuple[str, ...]]:
    if publication_kind == "brief":
        if not isinstance(evidence, BriefEvidenceBundle):
            return None, ("evidence_kind_mismatch",)
        return validate_brief_publication(payload, evidence)
    if not isinstance(evidence, StoryAnalysisEvidence):
        return None, ("evidence_kind_mismatch",)
    return validate_story_publication(payload, evidence)


def _ground_claim(text: str, evidence_text: str, target: str) -> list[str]:
    errors = _ground_numbers(text, evidence_text, target)
    errors.extend(_ground_dates(text, evidence_text, target))
    errors.extend(_ground_entities(text, evidence_text, target))
    return errors


def _ground_numbers(text: str, evidence_text: str, target: str) -> list[str]:
    evidence_numbers = {_normalize_number(value) for value in _numbers(evidence_text)}
    unsupported = sorted(value for value in set(_numbers(text)) if _normalize_number(value) not in evidence_numbers)
    return [f"unsupported_number:{target}:{value}" for value in unsupported]


def _ground_dates(text: str, evidence_text: str, target: str) -> list[str]:
    evidence_dates = {_normalize_date(value) for value in _DATE_RE.findall(evidence_text)}
    unsupported = sorted(value for value in set(_DATE_RE.findall(text)) if _normalize_date(value) not in evidence_dates)
    return [f"unsupported_date:{target}:{value}" for value in unsupported]


def _ground_entities(text: str, evidence_text: str, target: str) -> list[str]:
    evidence_lower = evidence_text.casefold()
    entities = {value.strip() for value in _LATIN_ENTITY_RE.findall(text) if len(value.strip()) >= 2}
    entities.update(term for term in _KNOWN_ENTITY_TERMS if term in text)
    unsupported = sorted(value for value in entities if not _entity_supported(value, evidence_lower))
    return [f"unsupported_entity:{target}:{value}" for value in unsupported]


def _entity_supported(value: str, evidence_lower: str) -> bool:
    normalized = value.casefold()
    if normalized in evidence_lower:
        return True
    aliases = _ENTITY_GROUNDING_ALIASES.get(normalized)
    if aliases is None:
        aliases = _ENTITY_GROUNDING_ALIASES.get(value, ())
    return any(alias.casefold() in evidence_lower for alias in aliases)


def _normalize_number(value: str) -> str:
    normalized = re.sub(r"\s+", "", value.casefold()).replace(",", "")
    normalized = re.sub(r"(?:basispoints?|个?基点|bps?)$", "bp", normalized)
    normalized = re.sub(r"percent$", "%", normalized)
    normalized = re.sub(r"percentagepoints?$", "percentagepoint", normalized)
    return normalized


def _normalize_date(value: str) -> str:
    return "-".join(str(int(part)) for part in re.findall(r"\d+", value))


def _numbers(value: str) -> list[str]:
    without_dates = _DATE_RE.sub(" ", value)
    return _NUMBER_RE.findall(without_dates)


def _required_conflict_or_correction_refs(story: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    factors = story.get("evidence_factors")
    if isinstance(factors, Mapping):
        conflicts = factors.get("material_conflicts")
        for conflict in _sequence(conflicts):
            if not isinstance(conflict, Mapping):
                continue
            refs.update(
                str(conflict.get(field) or "")
                for field in ("left_revision_id", "right_revision_id")
                if str(conflict.get(field) or "")
            )
    for article in _sequence(story.get("evidence_articles")):
        if (
            isinstance(article, Mapping)
            and str(article.get("development_relation")) == "correction"
            and article.get("evidence_ref")
        ):
            refs.add(str(article["evidence_ref"]))
    return refs


def _evidence_text(rows: Sequence[object]) -> str:
    values: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        values.extend((str(row.get("title") or ""), str(row.get("snippet") or "")))
        values.append(str(row.get("event_core") or ""))
        content_snapshot = row.get("content_snapshot")
        if isinstance(content_snapshot, Mapping):
            values.append(str(content_snapshot.get("extracted_text") or ""))
        nested = row.get("evidence_articles")
        if isinstance(nested, Sequence):
            values.append(_evidence_text(nested))
    return " ".join(values)


def _evidence_by_ref(rows: Sequence[object]) -> dict[str, Mapping[str, Any]]:
    return {str(row["evidence_ref"]): row for row in rows if isinstance(row, Mapping) and row.get("evidence_ref")}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _prompt_injection_errors(text: str) -> list[str]:
    lowered = text.lower()
    return ["prompt_injection_echo"] if any(marker in lowered for marker in _INJECTION_MARKERS) else []


def _has_chinese(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _bounded(value: object) -> str:
    return " ".join(str(value).split())[:800]


__all__ = ["validate_publication"]
