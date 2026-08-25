"""Trusted, framework-neutral security contracts for the cold Program compiler.

This module deliberately does not import DSPy or the Program implementation.  It
is used by the trusted launcher before any untrusted optimizer code runs and by
the proposal path after the runner exits.  The optimizer receives a sealed input
bundle and may return content-addressed receipt payloads; it never receives a DB
connection or authority to manufacture those identities.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...artifact_identity import canonical_sha
from ...program.artifact import ProgramStrategyPatchV1
from .sandbox import CompilerSandboxLaunchReceipt

COMPILER_INPUT_SCHEMA: Literal["tracefold.news.compile_input_bundle.v3"] = "tracefold.news.compile_input_bundle.v3"
COMPILER_CORPUS_SCHEMA: Literal["tracefold.news.compile_corpus_receipt.v4"] = "tracefold.news.compile_corpus_receipt.v4"
MODEL_EXECUTION_IDENTITY_SCHEMA: Literal["tracefold.news.model_execution_identity.v1"] = (
    "tracefold.news.model_execution_identity.v1"
)
COMPILER_BUILD_ATTESTATION_SCHEMA: Literal["tracefold.news.compiler_build_attestation.v1"] = (
    "tracefold.news.compiler_build_attestation.v1"
)
COMPILE_RECORD_SCHEMA: Literal["news_program_compile_record_v1"] = "news_program_compile_record_v1"
PROXY_EXECUTION_SCHEMA: Literal["tracefold.news.compiler_proxy_execution.v4"] = (
    "tracefold.news.compiler_proxy_execution.v4"
)
COMPILER_RUNNER_RECEIPTS_SCHEMA: Literal["tracefold.news.compiler_runner_receipts.v5"] = (
    "tracefold.news.compiler_runner_receipts.v5"
)
LEARNING_EPOCH: Literal["program_v7"] = "program_v7"
# v3 (#150): the projection now carries `policy_values`/`policy_sha256`/`policy_source`, and the metric
# fails closed without them. A dataset sealed under v2 is content-addressed by its `dataset_sha` and so
# cannot be regenerated without changing identity — left at v2 it would still validate here and then
# raise inside every single metric call, which a compile run reports as 100% provider failure with zero
# provider calls actually made. This is the one field whose job is to detect exactly that change.
COMPILE_EPISODE_PROJECTION_SCHEMA: Literal["tracefold.news.development_compile_episode.v3"] = (
    "tracefold.news.development_compile_episode.v3"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
CompilerRole = Literal["task", "reflection", "metric_judge"]
REFLECTION_MAX_TOKENS = 32_000
REFLECTION_TIMEOUT_SECONDS = 300.0
METRIC_JUDGE_MAX_TOKENS = 4_096
METRIC_JUDGE_TIMEOUT_SECONDS = 120.0
_REQUIRED_RECEIPT_KINDS = frozenset(
    {
        "corpus",
        "metric",
        "optimizer_config",
        "trajectory",
        "checkpoint",
        "sandbox_launch",
        "patch",
    }
)
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        ("api", "key"),
        ("authorization",),
        ("base", "url"),
        ("credential",),
        ("credentials",),
        ("endpoint", "url"),
        ("header",),
        ("headers",),
        ("password",),
        ("private", "key"),
        ("secret",),
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:sk|ghp|github_pat|xox[abprs])[-_][a-z0-9_-]{12,}"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SAFE_NEGATIVE_ATTESTATIONS = frozenset(
    {
        "ambient_credentials_present",
        "db_credentials_present",
        "holdout_mounted",
    }
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelExecutionIdentity(_ExactModel):
    """One compiler role's complete, secret-free execution contract.

    This replaced a three-level digest chain — `endpoint_sha256` -> `model_sha256` -> `binding_sha256`,
    and then a fourth `binding_sha256` over the whole role config. Only the first addressed anything the
    record cannot carry: the endpoint URL, which holds the host and travels beside a credential. The rest
    hashed values printed immediately below them, so a verifier that had the object never needed them and
    a verifier that did not have the object could not use them.
    """

    schema_version: Literal["tracefold.news.model_execution_identity.v1"] = MODEL_EXECUTION_IDENTITY_SCHEMA
    role: CompilerRole
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
        role: CompilerRole,
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
        _reject_secret_material(self.model_dump(mode="json"), path=f"model_execution_identity.{self.role}")
        return self


def endpoint_fingerprint(api_base: str) -> str:
    """The secret-free identity of one provider endpoint."""

    return canonical_sha(
        {
            "identity_schema": MODEL_EXECUTION_IDENTITY_SCHEMA,
            "canonical_endpoint": _canonical_endpoint(api_base),
        }
    )


class CompileCorpusReceipt(_ExactModel):
    """Trusted roots proving which accepted development projection was exported."""

    schema_version: Literal["tracefold.news.compile_corpus_receipt.v4"] = COMPILER_CORPUS_SCHEMA
    learning_epoch: Literal["program_v7"] = LEARNING_EPOCH
    # The ledger key for the dataset artifact, which is a separately stored object. That is what earns a
    # digest here; `development_dataset_payload_sha256`, `case_root_sha256` and `cluster_root_sha256` did
    # not. The bundle embeds `dataset_payload` and `episodes` and commits to both through `bundle_sha256`,
    # so those three addressed content sitting in the same document — and the validator that checked them
    # computed each side from that one object, which is a comparison that cannot fail.
    development_dataset_sha: str = Field(pattern=_SHA256_PATTERN)
    learning_epoch_started_at_ms: int = Field(ge=0)
    projection_schema_id: Literal["tracefold.news.development_compile_episode.v3"] = COMPILE_EPISODE_PROJECTION_SCHEMA
    # This one stays, and it is the only one that ever had a reason to. It survives into `CompileRecordV1`,
    # where `CandidateEvaluator` re-projects the episodes from live tables and compares it — a second party
    # checking a value it did not compute, which is the whole test §2 sets for keeping a digest.
    episode_projection_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_count: int = Field(gt=0)
    # The rubric the trusted side accepted this corpus under. The untrusted compiler records it in the metric
    # receipt but must not choose it, and must not reach into the review plane to look it up.
    review_rubric_version: str = Field(min_length=1, max_length=64)


class CompileBudgetV3(_ExactModel):
    max_metric_calls: int = Field(gt=0)
    max_task_model_calls: int = Field(gt=0)
    max_reflection_model_calls: int = Field(gt=0)
    max_metric_judge_model_calls: int = Field(gt=0)
    max_cost_microusd: int = Field(gt=0)
    max_call_cost_microusd: int = Field(gt=0)
    seed: int = Field(ge=0)

    @model_validator(mode="after")
    def _reservation_can_admit_one_call(self) -> CompileBudgetV3:
        if self.max_call_cost_microusd > self.max_cost_microusd:
            raise ValueError("news_program_compile_call_cost_reservation_invalid")
        return self


class CompilerProxyTariff(_ExactModel):
    """Trusted, versioned rates used for pre-call worst-case reservation."""

    tariff_id: str = Field(min_length=1, max_length=256)
    input_token_overhead: int = Field(gt=0, le=100_000)
    task_input_microusd_per_million: int = Field(gt=0)
    task_output_microusd_per_million: int = Field(gt=0)
    reflection_input_microusd_per_million: int = Field(gt=0)
    reflection_output_microusd_per_million: int = Field(gt=0)
    metric_judge_input_microusd_per_million: int = Field(gt=0)
    metric_judge_output_microusd_per_million: int = Field(gt=0)

    def worst_case_cost_microusd(
        self,
        *,
        role: CompilerRole,
        request_bytes: int,
        max_output_tokens: int,
    ) -> int:
        if request_bytes < 0 or max_output_tokens <= 0:
            raise ValueError("news_program_compile_proxy_tariff_input_invalid")
        rates = {
            "task": (self.task_input_microusd_per_million, self.task_output_microusd_per_million),
            "reflection": (
                self.reflection_input_microusd_per_million,
                self.reflection_output_microusd_per_million,
            ),
            "metric_judge": (
                self.metric_judge_input_microusd_per_million,
                self.metric_judge_output_microusd_per_million,
            ),
        }
        input_rate, output_rate = rates[role]
        input_token_upper_bound = request_bytes + self.input_token_overhead
        return max(
            1,
            _ceil_million(input_token_upper_bound * input_rate) + _ceil_million(max_output_tokens * output_rate),
        )


def gepa_metric_call_ceiling(
    *,
    max_metric_calls: int,
    optimizer_config: Mapping[str, Any],
    expected_example_count: int,
) -> int:
    """Return GEPA's sealed end-of-step metric ceiling.

    GEPA checks ``max_metric_calls`` between steps. A started step can consume one
    reflection minibatch and, when accepted, one full validation pass before it
    stops. Those widths are trustworthy only when they are bound to the complete
    train/validation split retained in the optimizer receipt.
    """

    constructor = optimizer_config.get("constructor_scalar_arguments")
    compile_call = optimizer_config.get("compile_call")
    if not isinstance(constructor, Mapping) or not isinstance(compile_call, Mapping):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    requested = constructor.get("max_metric_calls")
    minibatch = constructor.get("reflection_minibatch_size")
    example_count = compile_call.get("example_count")
    train_count = compile_call.get("trainset_count")
    val_count = compile_call.get("valset_count")
    values = (requested, minibatch, example_count, train_count, val_count, max_metric_calls, expected_example_count)
    if any(type(value) is not int for value in values):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    requested = cast(int, requested)
    minibatch = cast(int, minibatch)
    example_count = cast(int, example_count)
    train_count = cast(int, train_count)
    val_count = cast(int, val_count)
    if (
        requested != max_metric_calls
        or max_metric_calls <= 0
        or expected_example_count <= 0
        or example_count != expected_example_count
        or train_count <= 0
        or train_count > example_count
        or val_count <= 0
        or val_count > example_count
        or train_count + val_count != example_count
        or minibatch <= 0
        or minibatch > train_count
    ):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    return max_metric_calls + val_count + minibatch


class CompilerBuildAttestation(_ExactModel):
    """The one place in this chain where two independent parties look at the same thing.

    The host computes the source and lock identity of its own tree; it then copies `/app/src/tracefold`
    and `/app/uv.lock` out of the pinned image and computes them again, before any secret is staged; and
    the runner recomputes the source identity a third time from inside the running container. Recording
    all three is not redundancy — each is a different party's answer, and the attestation *is* their
    agreement. Everything else the compile produces is one party describing itself.
    """

    schema_version: Literal["tracefold.news.compiler_build_attestation.v1"] = COMPILER_BUILD_ATTESTATION_SCHEMA
    compiler_image_digest: str = Field(pattern=_IMAGE_DIGEST_PATTERN)
    proxy_image_digest: str = Field(pattern=_IMAGE_DIGEST_PATTERN)
    host_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    host_proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    host_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _every_party_agrees(self) -> CompilerBuildAttestation:
        if (
            self.image_source_sha256 != self.host_source_sha256
            or self.container_source_sha256 != self.host_source_sha256
            or self.image_proxy_source_sha256 != self.host_proxy_source_sha256
            or self.container_proxy_source_sha256 != self.host_proxy_source_sha256
            or self.image_lock_sha256 != self.host_lock_sha256
        ):
            raise ValueError("news_program_compile_build_attestation_mismatch")
        return self


class CompilerProxyCall(_ExactModel):
    """One trusted server observation of one socket request.

    Defined here rather than beside the socket server because it is a security contract: the proxy
    sidecar produces it, the host arithmetic verifies it, and the compile record carries it. It has no
    digest of itself — the record that embeds it is what is content-addressed.
    """

    role: CompilerRole
    sequence: int = Field(gt=0)
    # These two address request and response bytes the receipt deliberately does not retain.
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    response_sha256: str = Field(pattern=_SHA256_PATTERN)
    responding_model: str | None = None
    provider_invoked: bool
    request_bytes: int = Field(gt=0)
    max_output_tokens: int = Field(ge=64, le=32_000)
    reserved_cost_microusd: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    provider_cost_microusd: int = Field(ge=0)
    finish_reason: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def _reservation_is_coherent(self) -> CompilerProxyCall:
        if (
            (self.provider_invoked and self.reserved_cost_microusd <= 0)
            or (not self.provider_invoked and self.reserved_cost_microusd != 0)
            or (not self.provider_invoked and self.error_code is None)
            or (self.error_code is None and self.total_tokens <= 0)
            or self.provider_cost_microusd > self.reserved_cost_microusd
        ):
            raise ValueError("news_program_compile_proxy_call_reservation_invalid")
        return self


class CompilerProxyExecution(_ExactModel):
    """The trusted per-call ledger, embedded whole.

    The Merkle roots this used to carry — over the calls, over their request digests and over their
    response digests — addressed a list printed directly beside them. So did its own `receipt_sha256`.
    The record root is what makes any of it tamper-evident.
    """

    schema_version: Literal["tracefold.news.compiler_proxy_execution.v4"] = PROXY_EXECUTION_SCHEMA
    # The grant travels to the sidecar and the container by other channels and is not carried here, so
    # this is the address the host uses to prove the ledger belongs to the grant it issued.
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    metric_judge_model_calls: int = Field(ge=0)
    task_cost_microusd: int = Field(ge=0)
    reflection_cost_microusd: int = Field(ge=0)
    metric_judge_cost_microusd: int = Field(ge=0)
    task_failures: int = Field(ge=0)
    reflection_failures: int = Field(ge=0)
    metric_judge_failures: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)
    reserved_cost_microusd: int = Field(ge=0)
    calls: tuple[CompilerProxyCall, ...]
    error_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _ledger_sums_to_its_totals(self) -> CompilerProxyExecution:
        """The ledger must add up. It may still contain refusals.

        The sidecar writes this receipt whether it served a call or refused one — a budget it would have
        exceeded, a reused sequence, a grant that did not match — and those refusals are exactly the
        evidence that the boundary held. Whether a *successful compile* may contain one is a different
        question, answered by `CompileRecordV1`, which only exists when a compile succeeded.
        """

        seen: set[tuple[str, int]] = set()
        role_calls = {"task": 0, "reflection": 0, "metric_judge": 0}
        role_costs = {"task": 0, "reflection": 0, "metric_judge": 0}
        role_failures = {"task": 0, "reflection": 0, "metric_judge": 0}
        actual = reserved = 0
        errors: list[str] = []
        for call in self.calls:
            identity = (call.role, call.sequence)
            if identity in seen:
                raise ValueError("news_program_compile_proxy_call_sequence_duplicate")
            seen.add(identity)
            role_calls[call.role] += int(call.provider_invoked)
            role_costs[call.role] += call.provider_cost_microusd
            role_failures[call.role] += int(call.error_code is not None)
            actual += call.provider_cost_microusd
            reserved += call.reserved_cost_microusd
            if call.error_code is not None:
                errors.append(call.error_code)
        if (
            (self.task_model_calls, self.reflection_model_calls, self.metric_judge_model_calls)
            != (role_calls["task"], role_calls["reflection"], role_calls["metric_judge"])
            or (self.task_cost_microusd, self.reflection_cost_microusd, self.metric_judge_cost_microusd)
            != (role_costs["task"], role_costs["reflection"], role_costs["metric_judge"])
            or (self.task_failures, self.reflection_failures, self.metric_judge_failures)
            != (role_failures["task"], role_failures["reflection"], role_failures["metric_judge"])
            or self.actual_cost_microusd != actual
            or self.reserved_cost_microusd != reserved
            or tuple(errors) != self.error_codes
        ):
            raise ValueError("news_program_compile_proxy_execution_accounting_mismatch")
        return self


class CompileInputBundle(_ExactModel):
    """Canonical input handed to the hermetic optimizer runner."""

    schema_version: Literal["tracefold.news.compile_input_bundle.v3"] = COMPILER_INPUT_SCHEMA
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    stable_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    task: ModelExecutionIdentity
    reflection: ModelExecutionIdentity
    metric_judge: ModelExecutionIdentity
    # Each of these addresses an object handed to the container by a separate channel: the grant over the
    # socket, the policy and the source tree inside the image. None of them is carried here.
    proxy_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_tariff: CompilerProxyTariff
    compiler_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    budget: CompileBudgetV3
    corpus: CompileCorpusReceipt
    dataset_payload: dict[str, Any] = Field(repr=False)
    episodes: tuple[dict[str, Any], ...] = Field(min_length=1)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        parent_program_sha256: str,
        stable_bundle_sha256: str,
        target_runtime_manifest_sha256: str,
        task: ModelExecutionIdentity,
        reflection: ModelExecutionIdentity,
        metric_judge: ModelExecutionIdentity,
        proxy_grant_sha256: str,
        proxy_config_sha256: str,
        proxy_tariff: CompilerProxyTariff,
        compiler_source_sha256: str,
        proxy_source_sha256: str,
        compiler_lock_sha256: str,
        sandbox_policy_sha256: str,
        compiler_image_digest: str,
        budget: CompileBudgetV3,
        corpus: CompileCorpusReceipt,
        dataset_payload: Mapping[str, Any],
        episodes: Sequence[BaseModel | Mapping[str, Any]],
    ) -> CompileInputBundle:
        payloads = tuple(_model_or_mapping_payload(episode) for episode in episodes)
        values: dict[str, Any] = {
            "schema_version": COMPILER_INPUT_SCHEMA,
            "parent_program_sha256": parent_program_sha256,
            "stable_bundle_sha256": stable_bundle_sha256,
            "target_runtime_manifest_sha256": target_runtime_manifest_sha256,
            "task": task.model_dump(mode="json"),
            "reflection": reflection.model_dump(mode="json"),
            "metric_judge": metric_judge.model_dump(mode="json"),
            "proxy_grant_sha256": proxy_grant_sha256,
            "proxy_config_sha256": proxy_config_sha256,
            "proxy_tariff": proxy_tariff.model_dump(mode="json"),
            "compiler_source_sha256": compiler_source_sha256,
            "proxy_source_sha256": proxy_source_sha256,
            "compiler_lock_sha256": compiler_lock_sha256,
            "sandbox_policy_sha256": sandbox_policy_sha256,
            "compiler_image_digest": compiler_image_digest,
            "budget": budget.model_dump(mode="json"),
            "corpus": corpus.model_dump(mode="json"),
            "dataset_payload": _model_or_mapping_payload(dataset_payload),
            "episodes": payloads,
        }
        return cls(**values, bundle_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _roots_match(self) -> CompileInputBundle:
        episode_payloads = list(self.episodes)
        if (self.task.role, self.reflection.role, self.metric_judge.role) != (
            "task",
            "reflection",
            "metric_judge",
        ):
            raise ValueError("news_program_compile_role_binding_order_invalid")
        if self.corpus.development_dataset_sha != canonical_sha({"kind": "dataset", "payload": self.dataset_payload}):
            raise ValueError("news_program_compile_dataset_payload_root_mismatch")
        if self.corpus.episode_count != len(episode_payloads):
            raise ValueError("news_program_compile_episode_count_mismatch")
        if self.corpus.episode_projection_root_sha256 != canonical_sha(episode_payloads):
            raise ValueError("news_program_compile_episode_projection_root_mismatch")
        values = self.model_dump(mode="json", exclude={"bundle_sha256"})
        if self.bundle_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_input_bundle_hash_mismatch")
        _reject_secret_material(values, path="compile_input")
        return self


class GepaRunResult(_ExactModel):
    """Everything one optimization run produced, and nothing about who paid for it.

    Both planes produce one of these: the trusted compiler inside its container, and the operator's
    experiment loop in process. It lives here rather than beside `run_gepa` so that the host — which must
    never import DSPy — can hold the same object the runner produced instead of a second model of it.

    Three documents used to restate these ten fields: the runner's own result, the receipts the host
    parsed out of the container, and the compile record built around them, with a field-by-field copy
    between each pair. They are one object now, carried whole.
    """

    patch: ProgramStrategyPatchV1
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]
    # The two things a scalar score cannot answer: was the winner picked on examples it never trained
    # on, and did the model see the card it was supposed to recognise. Both were computed and validated
    # inside the container and then dropped before the host saw them, so the documented proof never
    # reached any receipt.
    split: dict[str, Any]
    retrieval: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)


class CompileSpend(_ExactModel):
    """What the metered plane counted. Deliberately not part of the optimization it paid for.

    `run_gepa` is shared with the experiment loop, which runs against unmetered endpoints; keeping the
    spend beside the run rather than inside it is what lets one algorithm serve both planes.
    """

    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    metric_judge_attempts: int = Field(ge=0)
    metric_judge_model_calls: int = Field(ge=0)
    metric_judge_failures: int = Field(ge=0)
    task_cost_microusd: int = Field(ge=0)
    reflection_cost_microusd: int = Field(ge=0)
    metric_judge_cost_microusd: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)

    @model_validator(mode="after")
    def _accounting_is_coherent(self) -> CompileSpend:
        """The arithmetic, checked once. It used to be written out twice, byte for byte."""

        if (
            self.metric_judge_model_calls > self.metric_judge_attempts
            or self.metric_judge_failures > self.metric_judge_attempts
            or self.actual_cost_microusd
            != self.task_cost_microusd + self.reflection_cost_microusd + self.metric_judge_cost_microusd
        ):
            raise ValueError("news_program_compile_result_accounting_mismatch")
        return self


class CompilerRunnerReceipts(_ExactModel):
    """What the untrusted runner produced, and what it says it spent, cross-checked by the host.

    It no longer restates the compiler source, lock, sandbox policy or endpoint identities the sealed
    input already fixed and the record commits to once — nor the optimization itself, which is carried
    whole rather than copied field by field out of the runner's own result.
    """

    schema_version: Literal["tracefold.news.compiler_runner_receipts.v5"] = COMPILER_RUNNER_RECEIPTS_SCHEMA
    input_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    run: GepaRunResult
    spend: CompileSpend

    @model_validator(mode="after")
    def _carries_no_secret(self) -> CompilerRunnerReceipts:
        _reject_secret_material(self.model_dump(mode="json"), path="compiler_runner_receipts")
        return self


class CompileRecordV1(_ExactModel):
    """One trusted compile execution, whole.

    This replaced seven content-addressed receipts, a chain root, a runner receipt, a provenance record
    and a machine diff, which between them carried the same identity up to four times. Everything the
    compile produced is embedded here, so `compile_record_sha256` — the ledger key — is the only thing
    that has to be checked for any of it to be tamper-evident.
    """

    schema_version: Literal["news_program_compile_record_v1"] = COMPILE_RECORD_SCHEMA
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    program_sha256: str = Field(pattern=_SHA256_PATTERN)
    development_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    projection_schema_id: Literal["tracefold.news.development_compile_episode.v3"] = COMPILE_EPISODE_PROJECTION_SCHEMA
    learning_epoch: Literal["program_v7"] = LEARNING_EPOCH
    learning_epoch_started_at_ms: int = Field(ge=0)
    review_rubric_version: str = Field(min_length=1, max_length=64)
    episode_count: int = Field(gt=0)
    # The episodes are sealed into the input bundle and never persisted, so nothing downstream can read
    # them back: `CandidateEvaluator` re-projects them from live tables and compares this root. Without
    # it the only corpus binding is a count, and a review edited between compile and evaluate would go
    # unnoticed.
    episode_projection_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    task_model: ModelExecutionIdentity
    reflection_model: ModelExecutionIdentity
    metric_judge_model: ModelExecutionIdentity
    # The optimization, exactly as the runner produced it: the patch, the metric, the GEPA configuration,
    # the search path it walked and the checkpoint it ended on. Carried whole rather than as digests —
    # nothing stores these separately, so a digest here would address nothing and the record would commit
    # to no evidence about how the winner was found.
    run: GepaRunResult
    budget: CompileBudgetV3
    tariff: CompilerProxyTariff
    # Two independent accountings of one compile: what the sidecar metered at the wire, and what the
    # runner counted inside the container. The host compares them; neither is trusted alone.
    usage: CompilerProxyExecution
    spend: CompileSpend
    sandbox: CompilerSandboxLaunchReceipt
    compiler_build: CompilerBuildAttestation
    created_at_ms: int = Field(ge=0)
    compile_record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> CompileRecordV1:
        """Build the record and address it by exactly what the validator will re-hash.

        Hashing the caller's mapping instead would omit every field the caller let default, and would
        choke on the nested models it is handed — the same shape `ProposalReceipt.issue` gets right by
        drafting first and dumping the draft.
        """

        draft = cls.model_construct(compile_record_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"compile_record_sha256"})
        return cls(**values, compile_record_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _record_is_exact(self) -> CompileRecordV1:
        values = self.model_dump(mode="json", exclude={"compile_record_sha256"})
        if self.compile_record_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_record_hash_mismatch")
        if (self.task_model.role, self.reflection_model.role, self.metric_judge_model.role) != (
            "task",
            "reflection",
            "metric_judge",
        ):
            raise ValueError("news_program_compile_record_role_order_invalid")
        if self.parent_program_sha256 == self.program_sha256:
            raise ValueError("news_program_compile_record_no_program_change")
        # The patch is the whole write set by construction now: `ProgramStrategyPatchV1` forbids extra
        # keys and pins its own schema, so what used to be a four-key spelling check over an untyped dict
        # is left with the one thing typing cannot say — which parent this patch was written against.
        if self.run.patch.parent_program_sha256 != self.parent_program_sha256:
            raise ValueError("news_program_compile_record_patch_parent_mismatch")
        self._budget_held()
        _reject_secret_material(values, path="compile_record")
        return self

    def _budget_held(self) -> None:
        """Every physical call was reserved at the trusted worst-case rate and stayed inside the budget.

        This is the arithmetic the old chain performed across five documents that had to be cross-bound
        by hash first. Here every operand is a field of one object.
        """

        usage, budget = self.usage, self.budget
        roles = {
            "task": (self.task_model, usage.task_model_calls, budget.max_task_model_calls),
            "reflection": (self.reflection_model, usage.reflection_model_calls, budget.max_reflection_model_calls),
            "metric_judge": (
                self.metric_judge_model,
                usage.metric_judge_model_calls,
                budget.max_metric_judge_model_calls,
            ),
        }
        for role, (_identity, calls, limit) in roles.items():
            if calls > limit:
                raise ValueError(f"news_program_compile_record_{role}_call_budget_exceeded")
        for call in usage.calls:
            identity = roles[call.role][0]
            if call.max_output_tokens != identity.max_output_tokens:
                raise ValueError("news_program_compile_record_call_binding_mismatch")
            expected = (
                self.tariff.worst_case_cost_microusd(
                    role=call.role,
                    request_bytes=call.request_bytes,
                    max_output_tokens=call.max_output_tokens,
                )
                if call.provider_invoked
                else 0
            )
            if call.reserved_cost_microusd != expected:
                raise ValueError("news_program_compile_record_call_reservation_invalid")
            if call.reserved_cost_microusd > budget.max_call_cost_microusd:
                raise ValueError("news_program_compile_record_call_cost_reservation_exceeded")
        if self.sandbox.egress_manifest.get("proxy_grant_sha256") != self.usage.grant_sha256:
            raise ValueError("news_program_compile_record_proxy_grant_mismatch")
        # A record exists only for a compile that finished, so the two roles that must answer for it to
        # have finished may not carry a refusal. The metric judge may: its answer is a score component,
        # and the metric already treats an unavailable judgment as zero.
        if any(
            call.role != "metric_judge" and (not call.provider_invoked or call.error_code is not None)
            for call in usage.calls
        ):
            raise ValueError("news_program_compile_record_task_call_failed")
        if (
            usage.actual_cost_microusd > budget.max_cost_microusd
            or usage.reserved_cost_microusd > budget.max_cost_microusd
            or usage.metric_judge_model_calls > self.spend.metric_judge_attempts
            or self.run.metric_calls
            > gepa_metric_call_ceiling(
                max_metric_calls=budget.max_metric_calls,
                optimizer_config=self.run.optimizer_config,
                expected_example_count=self.episode_count,
            )
        ):
            raise ValueError("news_program_compile_record_budget_exceeded")


def validate_compile_record(
    record: CompileRecordV1 | Mapping[str, Any],
    *,
    parent_program_sha256: str,
    program_sha256: str,
    development_dataset_sha256: str,
    target_runtime_manifest_sha256: str,
) -> CompileRecordV1:
    """Re-verify one persisted record against the candidate that claims it."""

    parsed = record if isinstance(record, CompileRecordV1) else CompileRecordV1.model_validate(record)
    if (
        parsed.parent_program_sha256 != parent_program_sha256
        or parsed.program_sha256 != program_sha256
        or parsed.development_dataset_sha256 != development_dataset_sha256
        or parsed.target_runtime_manifest_sha256 != target_runtime_manifest_sha256
    ):
        raise ValueError("news_program_compile_record_identity_mismatch")
    return parsed


def seal_compile_input(
    *,
    dataset_sha: str,
    dataset_payload: Mapping[str, Any],
    episodes: Sequence[BaseModel | Mapping[str, Any]],
    parent_program_sha256: str,
    stable_bundle_sha256: str,
    target_runtime_manifest_sha256: str,
    task: ModelExecutionIdentity,
    reflection: ModelExecutionIdentity,
    metric_judge: ModelExecutionIdentity,
    proxy_grant_sha256: str,
    proxy_config_sha256: str,
    proxy_tariff: CompilerProxyTariff,
    review_rubric_version: str,
    compiler_source_sha256: str,
    proxy_source_sha256: str,
    compiler_lock_sha256: str,
    sandbox_policy_sha256: str,
    compiler_image_digest: str,
    budget: CompileBudgetV3,
) -> CompileInputBundle:
    """Recompute the DB artifact and ordered projection roots before launch."""

    payload = _model_or_mapping_payload(dataset_payload)
    expected_artifact_sha = canonical_sha({"kind": "dataset", "payload": payload})
    if dataset_sha != expected_artifact_sha:
        raise ValueError("news_program_compile_development_dataset_hash_mismatch")
    if payload.get("role") != "development":
        raise ValueError("news_program_compile_requires_development_dataset")
    if payload.get("learning_epoch") != LEARNING_EPOCH:
        raise ValueError("news_program_compile_epoch_mismatch")
    cohort = payload.get("agent_cohort")
    if not isinstance(cohort, Mapping):
        raise ValueError("news_program_compile_dataset_agent_cohort_invalid")
    if (
        cohort.get("bundle_sha") != stable_bundle_sha256
        or cohort.get("program_version") != "news_semantic_program_v5"
        or cohort.get("program_sha256") != parent_program_sha256
        or cohort.get("runtime_model_bindings_sha256") != target_runtime_manifest_sha256
        or cohort.get("learning_epoch") != LEARNING_EPOCH
    ):
        raise ValueError("news_program_compile_dataset_agent_cohort_mismatch")
    learning_epoch_started_at_ms = payload.get("learning_epoch_started_at_ms")
    if not isinstance(learning_epoch_started_at_ms, int) or learning_epoch_started_at_ms < 0:
        raise ValueError("news_program_compile_dataset_epoch_start_invalid")
    episode_payloads = tuple(_model_or_mapping_payload(episode) for episode in episodes)
    case_ids, cluster_ids = _episode_ids(episode_payloads)
    dataset_cases = payload.get("cases")
    if not isinstance(dataset_cases, list):
        raise ValueError("news_program_compile_dataset_cases_invalid")
    dataset_case_ids: list[str] = []
    dataset_cluster_ids: list[str] = []
    for item in dataset_cases:
        if not isinstance(item, Mapping):
            raise ValueError("news_program_compile_dataset_cases_invalid")
        dataset_case_ids.append(str(item.get("case_id") or ""))
        dataset_cluster_ids.append(str(item.get("cluster_id") or ""))
    if dataset_case_ids != case_ids or dataset_cluster_ids != cluster_ids:
        raise ValueError("news_program_compile_dataset_episode_membership_mismatch")
    corpus = CompileCorpusReceipt(
        development_dataset_sha=dataset_sha,
        learning_epoch_started_at_ms=learning_epoch_started_at_ms,
        projection_schema_id=COMPILE_EPISODE_PROJECTION_SCHEMA,
        episode_projection_root_sha256=canonical_sha(list(episode_payloads)),
        episode_count=len(episode_payloads),
        review_rubric_version=review_rubric_version,
    )
    return CompileInputBundle.issue(
        parent_program_sha256=parent_program_sha256,
        stable_bundle_sha256=stable_bundle_sha256,
        target_runtime_manifest_sha256=target_runtime_manifest_sha256,
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        proxy_grant_sha256=proxy_grant_sha256,
        proxy_config_sha256=proxy_config_sha256,
        proxy_tariff=proxy_tariff,
        compiler_source_sha256=compiler_source_sha256,
        proxy_source_sha256=proxy_source_sha256,
        compiler_lock_sha256=compiler_lock_sha256,
        sandbox_policy_sha256=sandbox_policy_sha256,
        compiler_image_digest=compiler_image_digest,
        budget=budget,
        corpus=corpus,
        dataset_payload=payload,
        episodes=episode_payloads,
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


def _episode_ids(episodes: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    case_ids: list[str] = []
    cluster_ids: list[str] = []
    for episode in episodes:
        case_id = str(episode.get("case_id") or "")
        cluster_id = str(episode.get("cluster_id") or "")
        if not case_id or not cluster_id:
            raise ValueError("news_program_compile_episode_identity_missing")
        case_ids.append(case_id)
        cluster_ids.append(cluster_id)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("news_program_compile_episode_case_duplicate")
    return case_ids, cluster_ids


def _model_or_mapping_payload(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    _reject_nonfinite_json(payload)
    return payload


def _ceil_million(value: int) -> int:
    return (value + 999_999) // 1_000_000


def _reject_nonfinite_json(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"news_program_compile_nonfinite_value:{path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"news_program_compile_non_string_key:{path}")
            _reject_nonfinite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_nonfinite_json(child, path=f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(f"news_program_compile_non_json_value:{path}:{type(value).__name__}")


def _reject_secret_material(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            parts = _key_parts(raw_key)
            safe_negative = str(raw_key) in _SAFE_NEGATIVE_ATTESTATIONS and child is False
            if not safe_negative and any(_contains_parts(parts, forbidden) for forbidden in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"news_program_compile_secret_key:{path}.{raw_key}")
            _reject_secret_material(child, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ValueError(f"news_program_compile_secret_value:{path}")


def _key_parts(value: object) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    return tuple(part for part in re.split(r"[^a-z0-9]+", separated.casefold()) if part)


def _contains_parts(parts: tuple[str, ...], forbidden: tuple[str, ...]) -> bool:
    width = len(forbidden)
    return any(parts[index : index + width] == forbidden for index in range(len(parts) - width + 1))


__all__ = [
    "COMPILER_BUILD_ATTESTATION_SCHEMA",
    "COMPILER_CORPUS_SCHEMA",
    "COMPILER_INPUT_SCHEMA",
    "COMPILER_RUNNER_RECEIPTS_SCHEMA",
    "COMPILE_EPISODE_PROJECTION_SCHEMA",
    "COMPILE_RECORD_SCHEMA",
    "LEARNING_EPOCH",
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "MODEL_EXECUTION_IDENTITY_SCHEMA",
    "PROXY_EXECUTION_SCHEMA",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "CompileBudgetV3",
    "CompileCorpusReceipt",
    "CompileInputBundle",
    "CompileRecordV1",
    "CompileSpend",
    "CompilerBuildAttestation",
    "CompilerProxyCall",
    "CompilerProxyExecution",
    "CompilerProxyTariff",
    "CompilerRole",
    "CompilerRunnerReceipts",
    "GepaRunResult",
    "ModelExecutionIdentity",
    "endpoint_fingerprint",
    "gepa_metric_call_ceiling",
    "seal_compile_input",
    "validate_compile_record",
]
