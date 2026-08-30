"""Bounded, release-neutral taxonomy shadow execution."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha
from ..program.artifact import render_model_evidence_json
from ..program.contracts import TriageContext
from ..program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallReceipt,
    LMDelegateProgramError,
    RecordedLM,
    RuntimeModelIdentity,
    TerminalDisposition,
    program_json_adapter,
)
from ..taxonomy import (
    IPTC_CODEBOOK_SHA256,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    source_authority_from_evidence,
)

TAXONOMY_SHADOW_SCHEMA: Final = "tracefold.news.taxonomy_shadow_observation.v2"
TAXONOMY_SHADOW_MAX_CALLS: Final = 2
TAXONOMY_SHADOW_MAX_TOKENS: Final = 800
TAXONOMY_SHADOW_MODEL_BINDING: Final = "taxonomy-shadow-v2"
TAXONOMY_SHADOW_INSTRUCTION: Final = """Classify one bounded ordinary News Event under news_taxonomy_v1.
Return only the typed taxonomy. Choose at most three allowed IPTC subject qcodes. event_family describes what
happened, never source format, rumor status, actor type, noise, or delivery value. filing is a source container;
classify its underlying financial/product/corporate/regulatory event. change_state distinguishes announced,
scheduled, effective, reported, updated, delayed, cancelled, recalled, and unknown. assertion_status is confirmed
only when bounded evidence directly establishes the fact; otherwise claimed, rumor, conflicted, or unknown. Use
other/unknown as honest abstentions. Do not output source_authority; code derives it only from the exact structured
reporting source, never strategy/provenance routing IDs. Use no tools, retrieval, external knowledge, confidence,
delivery recommendation, or trading recommendation."""


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaxonomyShadowSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Production-bounded EventSemantics evidence JSON")
    taxonomy: ModelTaxonomyV1 = dspy.OutputField(desc="The four model-owned news_taxonomy_v1 axes")


class TaxonomyShadowAttemptV1(_ExactModel):
    attempt: int = Field(ge=1, le=TAXONOMY_SHADOW_MAX_CALLS)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invocation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_disposition: TerminalDisposition
    error_code: str | None = None
    recording_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recording: dict[str, Any]

    @classmethod
    def from_receipt(cls, receipt: LMCallReceipt) -> TaxonomyShadowAttemptV1:
        if (
            receipt.predictor != "taxonomy_shadow"
            or receipt.route != "shadow"
            or receipt.terminal_disposition is None
            or receipt.recording is None
        ):
            raise ValueError("news_taxonomy_shadow_recording_missing")
        return cls(
            attempt=receipt.attempt,
            request_sha256=receipt.request_sha256,
            invocation_sha256=receipt.invocation_sha256,
            terminal_disposition=receipt.terminal_disposition,
            error_code=receipt.error_code,
            recording_sha256=canonical_sha(receipt.recording),
            recording=receipt.recording,
        )

    @model_validator(mode="after")
    def recording_is_addressed(self) -> TaxonomyShadowAttemptV1:
        if self.recording_sha256 != canonical_sha(self.recording):
            raise ValueError("news_taxonomy_shadow_recording_identity_mismatch")
        response = self.recording.get("response")
        error = self.recording.get("error")
        if self.terminal_disposition in {"provider_success", "adapter_parse_error"}:
            if response is None or error is not None:
                raise ValueError("news_taxonomy_shadow_recording_terminal_mismatch")
        elif self.terminal_disposition == "provider_error":
            if error is None or response is not None:
                raise ValueError("news_taxonomy_shadow_recording_terminal_mismatch")
        else:
            raise ValueError("news_taxonomy_shadow_attempt_terminal_unsupported")
        return self


TaxonomyShadowOutcome = Literal["success", "schema_invalid", "provider_failure", "budget_deadline_failure"]


def _is_budget_deadline_failure(code: str, exc: Exception | None = None) -> bool:
    return isinstance(exc, dspy.LMTimeoutError) or any(token in code for token in ("budget", "deadline", "timeout"))


def _is_truncated_attempt(attempt: TaxonomyShadowAttemptV1) -> bool:
    response = attempt.recording.get("response")
    return (
        attempt.terminal_disposition == "provider_success"
        and attempt.error_code == "news_program_lm_output_truncated"
        and isinstance(response, Mapping)
        and response.get("truncated") is True
    )


class TaxonomyShadowObservationV2(_ExactModel):
    schema_id: Literal["tracefold.news.taxonomy_shadow_observation.v2"] = TAXONOMY_SHADOW_SCHEMA
    release_authority: Literal[False] = False
    event_id: str
    evidence_version: int = Field(ge=0)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    shadow_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identity: RuntimeModelIdentity
    model_binding: str = Field(min_length=1)
    outcome: TaxonomyShadowOutcome
    error_code: str | None = None
    attempts: tuple[TaxonomyShadowAttemptV1, ...] = Field(max_length=TAXONOMY_SHADOW_MAX_CALLS)
    taxonomy: NewsTaxonomyV1 | None = None

    @property
    def observation_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def recordings_are_exactly_replayable(self) -> TaxonomyShadowObservationV2:
        if tuple(attempt.attempt for attempt in self.attempts) != tuple(range(1, len(self.attempts) + 1)):
            raise ValueError("news_taxonomy_shadow_attempt_order_invalid")
        recordings = {attempt.request_sha256: attempt.recording for attempt in self.attempts}
        if len(recordings) != len(self.attempts):
            raise ValueError("news_taxonomy_shadow_request_identity_duplicate")
        RecordedLM(
            recordings,
            model=self.model_identity.model,
            runtime_identity=self.model_identity,
            model_binding=self.model_binding,
        )
        for attempt in self.attempts:
            expected_invocation = canonical_sha(
                {
                    "program_version": TAXONOMY_SHADOW_SCHEMA,
                    "program_sha256": self.shadow_program_sha256,
                    "context_sha256": self.context_sha256,
                    "predictor": "taxonomy_shadow",
                    "route": "shadow",
                    "attempt": attempt.attempt,
                    "model_binding": self.model_binding,
                    "runtime_binding_sha256": self.model_identity.binding_sha256,
                    "request_sha256": attempt.request_sha256,
                }
            )
            if attempt.invocation_sha256 != expected_invocation:
                raise ValueError("news_taxonomy_shadow_invocation_identity_mismatch")
        dispositions = tuple(attempt.terminal_disposition for attempt in self.attempts)
        prefix = dispositions[:-1]
        if any(disposition != "adapter_parse_error" for disposition in prefix):
            raise ValueError("news_taxonomy_shadow_attempt_transition_invalid")
        if self.outcome == "success":
            if (
                self.taxonomy is None
                or self.error_code is not None
                or not dispositions
                or dispositions[-1] != "provider_success"
            ):
                raise ValueError("news_taxonomy_shadow_success_terminal_invalid")
        elif self.taxonomy is not None or not self.error_code:
            raise ValueError("news_taxonomy_shadow_failure_terminal_invalid")
        if self.outcome == "schema_invalid" and (
            not dispositions or any(disposition != "adapter_parse_error" for disposition in dispositions)
        ):
            raise ValueError("news_taxonomy_shadow_schema_terminal_invalid")
        if self.outcome in {"provider_failure", "budget_deadline_failure"}:
            error_code = self.error_code or ""
            budget_failure = _is_budget_deadline_failure(error_code)
            if self.outcome == "provider_failure" and budget_failure:
                raise ValueError("news_taxonomy_shadow_provider_terminal_invalid")
            if self.outcome == "budget_deadline_failure" and not budget_failure:
                raise ValueError("news_taxonomy_shadow_budget_terminal_invalid")
            if (
                dispositions
                and dispositions[-1] != "provider_error"
                and not (self.outcome == "provider_failure" and _is_truncated_attempt(self.attempts[-1]))
            ):
                raise ValueError("news_taxonomy_shadow_failure_attempt_invalid")
            if not dispositions and self.outcome != "budget_deadline_failure":
                raise ValueError("news_taxonomy_shadow_failure_attempt_missing")
        return self


class TaxonomyShadowPopulationV1(_ExactModel):
    eligible_case_n: int = Field(ge=0)
    observation_n: int = Field(ge=0)
    success_n: int = Field(ge=0)
    schema_invalid_n: int = Field(ge=0)
    provider_failure_n: int = Field(ge=0)
    budget_deadline_failure_n: int = Field(ge=0)
    missing_observation_n: int = Field(ge=0)
    invalid_observation_n: int = Field(ge=0)
    physical_attempt_n: int = Field(ge=0)
    recorded_attempt_n: int = Field(ge=0)
    schema_invalid_attempt_n: int = Field(ge=0)
    provider_failure_attempt_n: int = Field(ge=0)

    @classmethod
    def issue(
        cls,
        observations: Sequence[TaxonomyShadowObservationV2],
        *,
        eligible_case_n: int,
        missing_observation_n: int = 0,
        invalid_observation_n: int = 0,
    ) -> TaxonomyShadowPopulationV1:
        attempts = [attempt for observation in observations for attempt in observation.attempts]
        outcomes = [observation.outcome for observation in observations]
        return cls(
            eligible_case_n=eligible_case_n,
            observation_n=len(observations),
            success_n=outcomes.count("success"),
            schema_invalid_n=outcomes.count("schema_invalid"),
            provider_failure_n=outcomes.count("provider_failure"),
            budget_deadline_failure_n=outcomes.count("budget_deadline_failure"),
            missing_observation_n=missing_observation_n,
            invalid_observation_n=invalid_observation_n,
            physical_attempt_n=len(attempts),
            recorded_attempt_n=len(attempts),
            schema_invalid_attempt_n=sum(a.terminal_disposition == "adapter_parse_error" for a in attempts),
            provider_failure_attempt_n=sum(a.terminal_disposition == "provider_error" for a in attempts),
        )

    @model_validator(mode="after")
    def population_reconciles(self) -> TaxonomyShadowPopulationV1:
        if self.observation_n != (
            self.success_n + self.schema_invalid_n + self.provider_failure_n + self.budget_deadline_failure_n
        ):
            raise ValueError("news_taxonomy_shadow_outcome_population_mismatch")
        if self.eligible_case_n != self.observation_n + self.missing_observation_n + self.invalid_observation_n:
            raise ValueError("news_taxonomy_shadow_eligible_population_mismatch")
        if self.recorded_attempt_n > self.physical_attempt_n:
            raise ValueError("news_taxonomy_shadow_recorded_population_invalid")
        return self

    @property
    def complete(self) -> bool:
        return (
            self.missing_observation_n == 0
            and self.invalid_observation_n == 0
            and self.recorded_attempt_n == self.physical_attempt_n
        )


class TaxonomyShadowProgramV2(dspy.Module):  # type: ignore[misc]
    """One bounded offline Predictor; never composed into the production route."""

    def __init__(self, *, lm: AuditedConfiguredLM, max_tokens: int = TAXONOMY_SHADOW_MAX_TOKENS) -> None:
        super().__init__()
        if lm.predictor != "taxonomy_shadow" or lm.route != "shadow" or lm.ledger is None:
            raise dspy.LMConfigurationError("news_taxonomy_shadow_audited_lm_required")
        self.lm = lm
        self.model_identity = lm.runtime_identity
        self.model_binding = lm.model_binding
        self.max_tokens = int(max_tokens)
        self.classify = dspy.Predict(
            TaxonomyShadowSignature.with_instructions(TAXONOMY_SHADOW_INSTRUCTION),
            max_tokens=self.max_tokens,
        )
        self.shadow_program_sha256 = canonical_sha(
            {
                "schema": TAXONOMY_SHADOW_SCHEMA,
                "instruction": TAXONOMY_SHADOW_INSTRUCTION,
                "signature": TaxonomyShadowSignature.dump_state(),
                "output_schema": ModelTaxonomyV1.model_json_schema(),
                "codebook_sha256": IPTC_CODEBOOK_SHA256,
                "model_identity": self.model_identity.model_dump(mode="json"),
                "model_binding": self.model_binding,
                "dspy": importlib.metadata.version("dspy"),
                "adapter": "tracefold.news.program.lm.program_json_adapter",
                "max_tokens": self.max_tokens,
            }
        )

    @property
    def model_binding_sha256(self) -> str:
        return canonical_sha(
            {
                "model_identity": self.model_identity.model_dump(mode="json"),
                "model_binding": self.model_binding,
            }
        )

    def forward(self, context: TriageContext | Mapping[str, Any]) -> TaxonomyShadowObservationV2:
        typed = context if isinstance(context, TriageContext) else TriageContext.model_validate(context)
        evidence_json = render_model_evidence_json(typed.event_semantics_payload(), predictor="event_semantics")
        context_sha256 = canonical_sha(typed.event_semantics_payload())
        ledger = self.lm.ledger
        if ledger is None:  # Constructor rejects this; keep the type boundary explicit.
            raise dspy.LMConfigurationError("news_taxonomy_shadow_audited_lm_required")
        start_index = len(ledger.receipts)
        prediction: Any = None
        outcome: TaxonomyShadowOutcome = "success"
        error_code: str | None = None
        try:
            with (
                ledger.scope(
                    LMCallContext(
                        program_version=TAXONOMY_SHADOW_SCHEMA,
                        program_sha256=self.shadow_program_sha256,
                        context_sha256=context_sha256,
                    )
                ),
                dspy.context(lm=self.lm, adapter=program_json_adapter()),
            ):
                prediction = self.classify(evidence_json=evidence_json)
        except LMDelegateProgramError as exc:
            raise exc.original from None
        except Exception as exc:
            receipts = ledger.receipts[start_index:]
            adapter_failure = bool(receipts and receipts[-1].terminal_disposition == "adapter_parse_error")
            if not isinstance(exc, dspy.LMError) and not adapter_failure:
                raise
            code = str(getattr(exc, "code", "") or "")
            if adapter_failure:
                outcome = "schema_invalid"
                error_code = "news_taxonomy_shadow_schema_invalid"
            elif _is_budget_deadline_failure(code, exc):
                outcome = "budget_deadline_failure"
                error_code = code or "news_taxonomy_shadow_budget_deadline_failure"
            else:
                outcome = "provider_failure"
                error_code = code or "news_taxonomy_shadow_provider_failure"
        receipts = ledger.receipts[start_index:]
        attempts = tuple(TaxonomyShadowAttemptV1.from_receipt(receipt) for receipt in receipts)
        if len(attempts) > TAXONOMY_SHADOW_MAX_CALLS:
            raise ValueError("news_taxonomy_shadow_call_budget_exhausted")
        taxonomy = None
        if outcome == "success":
            if prediction is None:
                raise RuntimeError("news_taxonomy_shadow_prediction_missing")
            labels = (
                prediction.taxonomy
                if isinstance(prediction.taxonomy, ModelTaxonomyV1)
                else ModelTaxonomyV1.model_validate(prediction.taxonomy)
            )
            taxonomy = NewsTaxonomyV1.issue(
                labels,
                source_authority=source_authority_from_evidence(typed.evidence),
            )
        return TaxonomyShadowObservationV2(
            event_id=typed.evidence.event_id,
            evidence_version=typed.evidence.evidence_version,
            evidence_sha256=typed.evidence.evidence_sha256,
            context_sha256=context_sha256,
            shadow_program_sha256=self.shadow_program_sha256,
            model_identity=self.model_identity,
            model_binding=self.model_binding,
            outcome=outcome,
            error_code=error_code,
            attempts=attempts,
            taxonomy=taxonomy,
        )


__all__ = [
    "TAXONOMY_SHADOW_INSTRUCTION",
    "TAXONOMY_SHADOW_MAX_CALLS",
    "TAXONOMY_SHADOW_MAX_TOKENS",
    "TAXONOMY_SHADOW_MODEL_BINDING",
    "TAXONOMY_SHADOW_SCHEMA",
    "TaxonomyShadowAttemptV1",
    "TaxonomyShadowObservationV2",
    "TaxonomyShadowPopulationV1",
    "TaxonomyShadowProgramV2",
    "TaxonomyShadowSignature",
]
