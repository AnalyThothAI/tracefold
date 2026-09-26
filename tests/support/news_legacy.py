"""Builders for the legacy `news_judgment_v3` verdict rows an Event judged before #706 still carries.

Nothing in production writes a verdict any more; these build the stored documents a test seeds so the read
side, the ReviewDesk and the retention jobs can be exercised against the history they must keep reading. The
documents and digests are the ones the retired Program wrote: a `news_judgment_v3` verdict beside a
`news_editorial_v4` editorial envelope carrying a `news_taxonomy_v1` label.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TriageVerdict
from tracefold.news.taxonomy import IPTC_CODEBOOK_SHA256, IPTC_SUBJECT_CODES

LEGACY_TRIAGE_POLICY_VERSION = "news_triage_policy_v17"
LEGACY_PROGRAM_VERSION = "news_semantic_program_v13"
LEGACY_JUDGMENT_CONTRACT_VERSION = "news_judgment_v3"
LEGACY_EDITORIAL_CONTRACT_VERSION = "news_editorial_v4"


def legacy_taxonomy(**overrides: Any) -> dict[str, Any]:
    """One stored `news_taxonomy_v1` label: the four model-owned axes plus the codebook identity."""

    values: dict[str, Any] = {
        "subject_codes": [],
        "event_family": "other",
        "change_state": "unknown",
        "assertion_status": "unknown",
        **overrides,
    }
    present = set(values["subject_codes"])
    values["subject_codes"] = [code for code in IPTC_SUBJECT_CODES if code in present]
    return {**values, "taxonomy_version": "news_taxonomy_v1", "codebook_sha256": IPTC_CODEBOOK_SHA256}


@dataclass(frozen=True, slots=True)
class LegacyEditorial:
    document: dict[str, Any]

    @property
    def editorial_sha256(self) -> str:
        return str(self.document["editorial_sha256"])


@dataclass(frozen=True, slots=True)
class LegacyJudgment:
    verdict: TriageVerdict
    editorial: LegacyEditorial
    verdict_sha256: str
    scored_judgment_sha256: str
    judgment_contract_version: str = LEGACY_JUDGMENT_CONTRACT_VERSION


def legacy_editorial(
    *,
    source_authority: str = "unknown",
    taxonomy: dict[str, Any] | None = None,
    taxonomy_error_code: str | None = None,
) -> LegacyEditorial:
    """One stored `news_editorial_v4` envelope; naming an error code stores the taxonomy-unavailable shape."""

    label = None if taxonomy_error_code is not None else (taxonomy or legacy_taxonomy())
    payload = {
        "editorial_contract_version": LEGACY_EDITORIAL_CONTRACT_VERSION,
        "editorial_origin": "model",
        "source_authority": source_authority,
        "taxonomy": label,
        "taxonomy_status": "available" if label is not None else "unavailable",
        "taxonomy_error_code": None if label is not None else taxonomy_error_code,
    }
    return LegacyEditorial({**payload, "editorial_sha256": canonical_sha(payload)})


def legacy_judgment(
    verdict: dict[str, Any] | TriageVerdict,
    *,
    source_authority: str = "unknown",
    taxonomy: dict[str, Any] | None = None,
    taxonomy_error_code: str | None = None,
) -> LegacyJudgment:
    """One legacy model judgment with the digests its writer computed over the stored documents."""

    typed = verdict if isinstance(verdict, TriageVerdict) else TriageVerdict.model_validate(verdict)
    editorial = legacy_editorial(
        source_authority=source_authority, taxonomy=taxonomy, taxonomy_error_code=taxonomy_error_code
    )
    verdict_document = typed.model_dump(mode="json")
    verdict_sha256 = canonical_sha(verdict_document)
    payload = {
        "judgment_contract_version": LEGACY_JUDGMENT_CONTRACT_VERSION,
        "verdict": verdict_document,
        "editorial": editorial.document,
        "verdict_sha256": verdict_sha256,
    }
    return LegacyJudgment(
        verdict=typed,
        editorial=editorial,
        verdict_sha256=verdict_sha256,
        scored_judgment_sha256=canonical_sha(payload),
    )


@dataclass(frozen=True, slots=True)
class LegacyDecision:
    """The persisted decision projection a legacy verdict row carries beside its judgment."""

    final: str
    override_rule: str | None
    throttled_by: str | None
    rule_baseline: str
    watchlist_hits: tuple[str, ...] = ()
    seen_similarity: float | None = None
    seen_against: int = -1
    seen_scope: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "final": self.final,
            "override_rule": self.override_rule,
            "throttled_by": self.throttled_by,
            "rule_baseline": self.rule_baseline,
            "watchlist_hits": list(self.watchlist_hits),
            "seen_similarity": self.seen_similarity,
            "seen_against": self.seen_against,
            "seen_scope": self.seen_scope,
        }


@dataclass(frozen=True, slots=True)
class LegacyDegradedJudgment:
    """One code-owned degraded presentation, stored when the retired Program had no model answer."""

    verdict: TriageVerdict
    decision: LegacyDecision
    error_code: str
    judgment_contract_version: str = LEGACY_JUDGMENT_CONTRACT_VERSION

    @property
    def judgment_atom(self) -> dict[str, Any]:
        return {
            "judgment_contract_version": self.judgment_contract_version,
            "origin": "degraded",
            "verdict": self.verdict.model_dump(mode="json"),
            "decision": self.decision.as_dict(),
            "error_code": self.error_code,
        }

    @property
    def judgment_sha256(self) -> str:
        return canonical_sha(self.judgment_atom)


def legacy_degraded_judgment(
    *,
    title: str,
    error_code: str,
    final: str = "drop",
    override_rule: str = "degraded_no_objective_guard",
    watchlist_hits: tuple[str, ...] = (),
) -> LegacyDegradedJudgment:
    """The degraded row the retired runtime stored: no model observation, only an objective action."""

    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction="neutral",
        scope="macro",
        confidence=0.0,
        headline_zh=" ".join(title.split())[:60] or "模型不可用（规则兜底）",
        why_zh="",
    )
    decision = LegacyDecision(
        final=final, override_rule=override_rule, throttled_by=None, rule_baseline=final, watchlist_hits=watchlist_hits
    )
    return LegacyDegradedJudgment(verdict=verdict, decision=decision, error_code=error_code)
