"""Frozen manifest contracts for the learning plane.

Separated from `evaluator.py` so a caller that only needs to *read* a candidate manifest — the Workers
composition root, which validates image-carried candidates at startup — does not import the evaluator,
and through it the compiler and DSPy. The online process paid ~4 s of import for four Pydantic models.

Nothing here evaluates, scores, compiles or persists; it is the shape a candidate has, plus the hashes
that make that shape content-addressed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha, reject_nonfinite_json
from ..program.artifact import ProgramStrategyArtifactV1, ProgramStrategyPatchV1, validate_program_instruction
from ..triage_rules import DecidePolicy

# v3 (#456): the development gate adds taxonomy target/control split floors and an independently reviewed
# 50-cluster calibration receipt. The profile is inside `TRUSTED_ROOT_SHA`; this readable name prevents an
# older corpus from being mistaken for one that met those gates.
LEARNING_PROFILE_ID: Literal["news_learning_release_v3"] = "news_learning_release_v3"
LEARNING_PROGRAM_VERSION = "news_semantic_program_v8"
PROMPT_CANDIDATE_SCHEMA: Literal["news_prompt_candidate_v2"] = "news_prompt_candidate_v2"
MODEL_EXECUTION_IDENTITY_SCHEMA: Literal["tracefold.news.model_execution_identity.v1"] = (
    "tracefold.news.model_execution_identity.v1"
)
# v3 (#150): the projection now carries `policy_values`/`policy_sha256`/`policy_source`, and the metric
# fails closed without them. A dataset sealed under v2 is content-addressed by its `dataset_sha` and so
# cannot be regenerated without changing identity — left at v2 it would still validate here and then
# raise inside every single metric call, which a compile run reports as 100% provider failure with zero
# provider calls actually made. This is the one field whose job is to detect exactly that change.
# v4 (#199): `accepted_review.first_bad_owner_explicit` carries the owner an operator wrote into the
# submission, distinct from the derived one the column holds. A v3 projection cannot answer "did a human
# blame the Prompt", so an Objective Plan built from one would grant GEPA every derived owner in the
# corpus — the exact permission #199 exists to withdraw.
# v5 (#437): accepted Review v6 taxonomy contributes its four model-owned axes to the projection root.
# v6 (#456): explicit taxonomy ownership is the sole optimization authority and therefore part of the
# frozen episode identity consumed by the direct taxonomy Objective.
COMPILE_EPISODE_PROJECTION_SCHEMA: Literal["tracefold.news.development_compile_episode.v6"] = (
    "tracefold.news.development_compile_episode.v6"
)
OptimizerRole = Literal["task", "reflection"]
ModelExecutionRole = Literal["task", "reflection", "metric_judge"]
# The reflection role's budget is its own. Until #143 both roles were built from the task route's numbers,
# which capped a proposed instruction at 1,200 tokens — below what the instruction bound itself accepts — and
# gave a reflection call the 20 s route deadline. These live here, not beside the optimizer, because the
# metric judge is built by the baseline harness too and must not pull DSPy's GEPA in to learn its ceiling.
REFLECTION_MAX_TOKENS = 32_000
REFLECTION_TIMEOUT_SECONDS = 300.0
METRIC_JUDGE_MAX_TOKENS = 4_096
METRIC_JUDGE_TIMEOUT_SECONDS = 120.0
# v3 (#456): an interrupted GEPA compile cannot expose an exact public metric-evaluation count. Its report
# records that count as unavailable instead of claiming zero while retaining exact physical model usage.
OPTIMIZATION_RUN_REPORT_SCHEMA: Literal["news_optimization_run_report_v3"] = "news_optimization_run_report_v3"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

# The three terminal states one offline optimization can end in (#202 §5). Every one of them is a complete,
# retained artifact: `NO_OP` and `REJECTED` are answers, not failures, and an operator has to be able to read
# why a run spent a budget and shipped nothing.
OptimizationOutcome = Literal["NO_OP", "REJECTED", "ADVANCE"]


def _sha(value: Any) -> str:
    return canonical_sha(value)


def epoch_id_for_bundle(bundle_sha: str) -> str:
    """The learning epoch one running bundle accrues evidence under.

    Derived, never declared (#314). An epoch used to be a hand-written `program_vN` opened by a hand-written
    migration, which meant a deployment that changed behavior without anyone writing a migration kept
    accruing evidence into the previous epoch — the shape of all three identity-clearing incidents. A bundle
    already names everything a judgment is conditioned on: the two instructions, the computed execution
    envelope, the four model slots, the retrieval contract and the policy. Keying the epoch to it makes
    "behavior changed but the epoch did not" unrepresentable rather than merely discouraged.

    The label is truncated for readability; `news_learning_epochs.bundle_sha` carries the whole identity and
    is what every lookup joins on.
    """

    identity = str(bundle_sha)
    if not is_bundle_sha(identity):
        raise ValueError("news_learning_epoch_bundle_sha_invalid")
    return f"bundle_{identity[:8]}"


def is_bundle_sha(value: object) -> bool:
    """Whether a value is shaped like a bundle identity, for callers reading untrusted sealed payloads."""

    identity = str(value)
    return len(identity) == 64 and all(char in "0123456789abcdef" for char in identity)


def _proposal_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize tuple/list representation before hashing a registration receipt."""

    normalized = json.loads(canonical_json(dict(value)))
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping input guarantees this
        raise TypeError("news_learning_proposal_payload_invalid")
    return normalized


class ModelExecutionIdentity(BaseModel):
    """One optimizer role's complete, secret-free execution contract.

    This replaced a three-level digest chain — `endpoint_sha256` -> `model_sha256` -> `binding_sha256`,
    and then a fourth `binding_sha256` over the whole role config. Only the first addressed anything the
    record cannot carry: the endpoint URL, which holds the host and travels beside a credential. The rest
    hashed values printed immediately below them, so a verifier that had the object never needed them and
    a verifier that did not have the object could not use them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["tracefold.news.model_execution_identity.v1"] = MODEL_EXECUTION_IDENTITY_SCHEMA
    role: ModelExecutionRole
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    # The one digest here: the canonical endpoint URL identifies the host a credential is presented to, so
    # it is fingerprinted rather than stored.
    endpoint_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    max_output_tokens: int = Field(ge=64, le=32_000)
    timeout_seconds: float = Field(gt=0, le=3_600)
    temperature: float = Field(ge=0, le=2)
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    adapter: Literal["strict_json", "gepa_reflection"]
    cache: Literal[False] = False
    num_retries: Literal[0] = 0

    @classmethod
    def issue(
        cls,
        *,
        role: ModelExecutionRole,
        model: str,
        api_base: str,
        max_output_tokens: int,
        timeout_seconds: float,
        temperature: float,
        model_kwargs: Mapping[str, Any],
    ) -> ModelExecutionIdentity:
        normalized_model = str(model).strip()
        if not normalized_model:
            raise ValueError("news_program_compile_model_identity_unavailable")
        provider = normalized_model.split("/", maxsplit=1)[0] if "/" in normalized_model else "unknown"
        return cls(
            role=role,
            provider=provider,
            model=normalized_model,
            endpoint_fingerprint=endpoint_fingerprint(api_base),
            max_output_tokens=max_output_tokens,
            timeout_seconds=float(timeout_seconds),
            temperature=float(temperature),
            model_kwargs=_model_or_mapping_payload(model_kwargs),
            adapter="gepa_reflection" if role == "reflection" else "strict_json",
        )

    @model_validator(mode="after")
    def _role_contract_is_exact(self) -> ModelExecutionIdentity:
        if self.role == "task":
            valid = self.temperature == 0 and self.adapter == "strict_json"
        elif self.role == "reflection":
            valid = (
                self.temperature == 1
                and self.adapter == "gepa_reflection"
                and self.max_output_tokens == REFLECTION_MAX_TOKENS
                and self.timeout_seconds == REFLECTION_TIMEOUT_SECONDS
            )
        else:
            valid = (
                self.temperature == 0
                and self.adapter == "strict_json"
                and self.max_output_tokens == METRIC_JUDGE_MAX_TOKENS
                and self.timeout_seconds == METRIC_JUDGE_TIMEOUT_SECONDS
            )
        if not valid:
            raise ValueError(f"news_program_compile_{self.role}_role_contract_invalid")
        return self


def endpoint_fingerprint(api_base: str) -> str:
    """The secret-free identity of one provider endpoint."""

    return canonical_sha(
        {
            "identity_schema": MODEL_EXECUTION_IDENTITY_SCHEMA,
            "canonical_endpoint": _canonical_endpoint(api_base),
        }
    )


def _canonical_endpoint(value: str) -> str:
    raw = str(value).strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("news_program_compile_endpoint_identity_invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("news_program_compile_endpoint_identity_invalid")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(SplitResult(scheme, netloc, path, "", ""))


def _model_or_mapping_payload(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    reject_nonfinite_json(payload)
    return payload


class ClosedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_ms: int = Field(ge=0)
    to_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> ClosedWindow:
        if self.to_ms <= self.from_ms:
            raise ValueError("news_learning_window_invalid")
        return self


class ArmManifest(BaseModel):
    """Everything one running Program arm is conditioned on, and the bundle hash over all of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    program_version: str = Field(min_length=1, max_length=128)
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The computed identity of the code around the two instructions (#314). It took over `factory_id`'s job
    # in cohort compatibility, and it does the job better: a declared factory could stay put across an
    # envelope change, and this cannot.
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_model_bindings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: dict[str, Any]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hashes_match(self) -> ArmManifest:
        if _sha(self.policy) != self.policy_sha256:
            raise ValueError("news_learning_policy_sha_mismatch")
        # Parse now, before a model call, so a malformed candidate policy can
        # never consume provider budget.
        DecidePolicy(**self.policy)
        return self

    @property
    def bundle_sha(self) -> str:
        return _sha(self.model_dump(mode="json"))


class ProposalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The corpus this registration bound the candidate to, as the registrar projected it — not as the
    # generator claimed it. A frozen dataset pins which cases are in scope; the reviews behind them can
    # still be edited, so without this a corpus that changed between generation and evaluation would keep
    # the same dataset SHA and the same case count while being a different corpus.
    development_episode_projection_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    # Audit only, and deliberately not a permission (#202 §5). Until then a Program candidate had to
    # declare `model`, and "produced by the trusted compiler" was what made it registrable at all — so an
    # instruction a person wrote could not be evaluated without a container reproducing it first.
    generator_kind: Literal["human", "model"]
    registered_at_ms: int = Field(ge=0)
    registration_receipt_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_target_dimensions: tuple[str, ...] = Field(min_length=1)
    guardrails: tuple[str, ...] = ()
    program_parent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    program_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The typed write-set this candidate is, stored under its own hash. It replaced a compile record
    # carrying a sandbox launch receipt, a proxy ledger, a three-party build attestation and a tariff:
    # none of which said anything about the two instructions being registered.
    prompt_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(cls, **values: Any) -> ProposalReceipt:
        """Issue a content-addressed DB/CI registration receipt.

        The caller still has to persist ``registration_payload`` under the
        returned SHA before CandidateEvaluator may use the candidate.  Keeping
        construction and verification on the value type prevents the CLI and
        tests from inventing subtly different receipt hashes.
        """

        draft = cls.model_construct(registration_receipt_sha="0" * 64, **values)
        registration_sha = _sha({"kind": "candidate_registration", "payload": draft.registration_payload})
        return cls(registration_receipt_sha=registration_sha, **values)

    @model_validator(mode="after")
    def registration_is_exact(self) -> ProposalReceipt:
        if self.program_parent_sha256 == self.program_candidate_sha256:
            raise ValueError("news_learning_program_sha_unchanged")
        expected = _sha({"kind": "candidate_registration", "payload": self.registration_payload})
        if self.registration_receipt_sha != expected:
            raise ValueError("news_learning_registration_receipt_sha_mismatch")
        return self

    @property
    def registration_payload(self) -> dict[str, Any]:
        return _proposal_json(self.model_dump(mode="json", exclude={"registration_receipt_sha"}))


class CandidateManifest(BaseModel):
    """One registered candidate arm, and the only kind there is.

    `target: program | policy` is gone (#202 §1.3). A policy change is a configuration release with its
    own gradual-rollout capability; dressing it as a News learning candidate gave it GEPA's release
    vocabulary — an Objective Plan, a development dataset, a blind pairwise stage — for a change no
    optimizer proposed and no metric scored. It also meant this contract's state space was strictly larger
    than the one write-set #199 allows.

    Old `program|policy` rows stay in the ledger as append-only audit and no longer parse here, which is
    what makes "audit-only, never re-activatable" a property rather than a promise (§10.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_stable_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_arm: ArmManifest
    hypothesis: str = Field(min_length=1, max_length=2_000)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_receipt: ProposalReceipt

    @property
    def candidate_sha(self) -> str:
        return _sha(self.model_dump(mode="json"))


class DevelopmentDatasetRef(BaseModel):
    """The identity of one frozen development dataset, without its episodes.

    An offline optimization is handed a corpus and must be able to prove *which* corpus, in a document a
    reader can check later. The episodes themselves are the payload; this is the binding, and
    `episode_projection_root_sha256` is what makes the two inseparable — `FrozenDevelopmentDataset` rehashes
    the episodes it was given and refuses a ref that describes a different projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    development_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_projection_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_count: int = Field(gt=0)
    learning_epoch: str = Field(pattern=r"^bundle_[0-9a-f]{8}$")
    learning_epoch_started_at_ms: int = Field(ge=0)
    review_rubric_version: str = Field(min_length=1, max_length=64)


class OptimizationBudget(BaseModel):
    """The complete bound one offline optimization runs under.

    Typed and in-process, because #202 removes the metered proxy that used to enforce it from outside. The
    wall clock is here for the same reason the call and cost ceilings are: a run that stops answering is
    still spending, and `REJECTED` on a deadline is an auditable terminal state where a killed container was
    a missing one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_metric_calls: int = Field(gt=0)
    max_task_model_calls: int = Field(gt=0)
    max_reflection_model_calls: int = Field(gt=0)
    max_cost_microusd: int = Field(gt=0)
    max_call_cost_microusd: int = Field(gt=0)
    max_wall_clock_seconds: float = Field(gt=0, le=86_400)
    seed: int = Field(ge=0)

    @model_validator(mode="after")
    def _reservation_can_admit_one_call(self) -> OptimizationBudget:
        if self.max_call_cost_microusd > self.max_cost_microusd:
            raise ValueError("news_learning_optimize_call_cost_reservation_invalid")
        return self


class PromptPatchV1(BaseModel):
    """The complete two-instruction candidate payload accepted by News release.

    `ProgramStrategyPatchV1` says the same two things bound to a parent, because applying a patch to a
    Program is the Program package's business and needs the parent to refuse a mismatch. The taxonomy
    optimizer may change only EventSemantics and copies ReaderCard byte-identically; retaining both here
    makes that equality independently verifiable at registration. The safety bounds are not restated:
    `validate_program_instruction` is the one implementation, so a candidate cannot be admitted under
    looser rules than the artifact it becomes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_semantics_instruction: str
    reader_card_instruction: str

    @model_validator(mode="after")
    def _write_set_is_safe(self) -> PromptPatchV1:
        validate_program_instruction(self.event_semantics_instruction)
        validate_program_instruction(self.reader_card_instruction)
        return self

    @classmethod
    def of(cls, patch: ProgramStrategyPatchV1) -> PromptPatchV1:
        return cls(
            event_semantics_instruction=patch.event_semantics_instruction,
            reader_card_instruction=patch.reader_card_instruction,
        )

    def applied_to(self, parent: ProgramStrategyArtifactV1) -> ProgramStrategyPatchV1:
        """Bind this write-set to the Program it was optimized against."""

        return ProgramStrategyPatchV1.issue(
            parent=parent,
            event_semantics_instruction=self.event_semantics_instruction,
            reader_card_instruction=self.reader_card_instruction,
        )

    def instruction_for(self, predictor: str) -> str:
        return self.event_semantics_instruction if predictor == "event_semantics" else self.reader_card_instruction

    def changes(self, parent: ProgramStrategyArtifactV1) -> bool:
        return (
            self.event_semantics_instruction != parent.event_semantics_instruction
            or self.reader_card_instruction != parent.reader_card_instruction
        )


class PromptCandidateV1(BaseModel):
    """One prompt candidate, whatever produced it.

    Provenance is recorded and audited; it grants nothing. Until #202 a candidate's release eligibility came
    from *where it was generated* — inside a sealed compiler image, against a metered proxy — so an
    experiment that found a better instruction had to be reproduced by a container before any gate would
    look at it. The write-set is two strings; the generator cannot be the authority for them. Registration,
    independent evaluation, future holdout, shadow, canary and a human promotion are.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["news_prompt_candidate_v2"] = PROMPT_CANDIDATE_SCHEMA
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    development_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    patch: PromptPatchV1
    objective_summary: dict[str, Any]
    optimizer: dict[str, Any]
    model_identities: dict[str, Any]
    budget: dict[str, Any]
    usage: dict[str, Any]
    created_at_ms: int = Field(ge=0)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> PromptCandidateV1:
        draft = cls.model_construct(candidate_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"candidate_sha256"})
        return cls(**values, candidate_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _identity_is_exact_and_carries_no_credential(self) -> PromptCandidateV1:
        payload = self.model_dump(mode="json", exclude={"candidate_sha256"})
        reject_nonfinite_json(payload, path="prompt_candidate")
        if self.candidate_sha256 != canonical_sha(payload):
            raise ValueError("news_learning_prompt_candidate_hash_mismatch")
        return self


class OptimizationRunReport(BaseModel):
    """The retained record of one optimization, in every terminal state.

    A `NO_OP` and a `REJECTED` produce this and nothing else; an `ADVANCE` produces this *and* a candidate,
    and the report names the candidate's hash so the two are readable as one run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["news_optimization_run_report_v3"] = OPTIMIZATION_RUN_REPORT_SCHEMA
    outcome: OptimizationOutcome
    dataset: DevelopmentDatasetRef
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    objective: dict[str, Any]
    # Absent on a run the Objective Plan refused before any model call: there was no split or metric, and
    # writing an empty object for either would make an unspent refusal look like a spent run.
    split: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None
    metric: dict[str, Any] | None = None
    optimizer: dict[str, Any] | None = None
    gepa_public_result: dict[str, Any] | None = None
    model_identities: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any]
    usage: dict[str, Any]
    reasons: tuple[str, ...] = ()
    started_at_ms: int = Field(ge=0)
    completed_at_ms: int = Field(ge=0)
    candidate_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> OptimizationRunReport:
        draft = cls.model_construct(report_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"report_sha256"})
        return cls(**values, report_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _terminal_state_is_coherent(self) -> OptimizationRunReport:
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        reject_nonfinite_json(payload, path="optimization_run_report")
        if self.completed_at_ms < self.started_at_ms:
            raise ValueError("news_learning_optimize_report_window_invalid")
        if (self.outcome == "ADVANCE") != (self.candidate_sha256 is not None):
            raise ValueError("news_learning_optimize_report_outcome_mismatch")
        if self.outcome != "ADVANCE" and not self.reasons:
            raise ValueError("news_learning_optimize_report_reason_required")
        if self.report_sha256 != canonical_sha(payload):
            raise ValueError("news_learning_optimize_report_hash_mismatch")
        return self


class OptimizationResult(BaseModel):
    """What the one offline entry point returns. Never a promotion, in any branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: OptimizationOutcome
    report: OptimizationRunReport
    candidate: PromptCandidateV1 | None = None

    @model_validator(mode="after")
    def _candidate_belongs_to_this_run(self) -> OptimizationResult:
        if self.outcome != self.report.outcome:
            raise ValueError("news_learning_optimize_result_outcome_mismatch")
        if (self.outcome == "ADVANCE") != (self.candidate is not None):
            raise ValueError("news_learning_optimize_result_outcome_mismatch")
        if self.candidate is not None and self.candidate.candidate_sha256 != self.report.candidate_sha256:
            raise ValueError("news_learning_optimize_result_candidate_mismatch")
        return self


class DatasetCaseRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    subject_kind: Literal["event", "external_miss"]
    event_id: str | None = None
    evidence_version: int | None = None
    external_snapshot_id: str | None = None
    evidence_sha256: str
    review_id: str
    cluster_id: str
    stratum: str
    should_push: str
    opened_at_ms: int
    delivery_truth: Literal["observed_sent", "observed_not_sent", "unknown"] = "unknown"


# The dataset coverage a report publishes, in one fixed order (#259 §5.2). Here rather than beside
# `_dataset_counts`, because the two consumers are the readiness command and the run summary, and the
# summary is a pure projection that must not import the dataset store and its database dependencies to
# learn the shape of a block it forwards.
_COVERAGE_FIELDS: tuple[str, ...] = (
    "case_n",
    "independent_cluster_n",
    "boundary_cluster_n",
    "retention_cluster_n",
    "negative_cluster_n",
    "safety_cluster_n",
    "stratum_n",
    "eligible_event_n",
    "calibration",
    "contract_cluster_receipt",
    "distributions",
    # Below this line: how concentrated the accepted cases are in time, and nothing a gate reads (#259).
    "natural_day_n",
    "window_duration_hours",
)


def dataset_coverage(counts: Mapping[str, Any]) -> dict[str, Any]:
    """Project a frozen dataset's sealed counts into the fixed coverage block reports publish.

    A projection, not a second tally: it reads what `DevelopmentDatasetStore._dataset_counts` sealed and
    re-derives nothing. Absent keys come back as `None` rather than `0`, because "this corpus was never
    projected" and "this corpus has none of these" are different answers and a reader has to be able to
    tell them apart. One shape on every path is the whole point, so callers with nothing to report pass
    `{}` here rather than publishing an empty object.
    """

    return {field: counts.get(field) for field in _COVERAGE_FIELDS}


__all__ = [
    "COMPILE_EPISODE_PROJECTION_SCHEMA",
    "LEARNING_PROFILE_ID",
    "LEARNING_PROGRAM_VERSION",
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "MODEL_EXECUTION_IDENTITY_SCHEMA",
    "OPTIMIZATION_RUN_REPORT_SCHEMA",
    "PROMPT_CANDIDATE_SCHEMA",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "ArmManifest",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetCaseRef",
    "DevelopmentDatasetRef",
    "ModelExecutionIdentity",
    "ModelExecutionRole",
    "OptimizationBudget",
    "OptimizationOutcome",
    "OptimizationResult",
    "OptimizationRunReport",
    "OptimizerRole",
    "PromptCandidateV1",
    "PromptPatchV1",
    "ProposalReceipt",
    "dataset_coverage",
    "endpoint_fingerprint",
    "epoch_id_for_bundle",
    "is_bundle_sha",
]
