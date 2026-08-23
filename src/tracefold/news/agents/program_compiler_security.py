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

from ..artifact_identity import canonical_sha

COMPILER_INPUT_SCHEMA: Literal["tracefold.news.compile_input_bundle.v3"] = "tracefold.news.compile_input_bundle.v3"
COMPILER_CORPUS_SCHEMA: Literal["tracefold.news.compile_corpus_receipt.v3"] = "tracefold.news.compile_corpus_receipt.v3"
COMPILER_ENDPOINT_IDENTITY_SCHEMA: Literal["tracefold.news.compiler_endpoint_identity.v3"] = (
    "tracefold.news.compiler_endpoint_identity.v3"
)
COMPILER_ROLE_BINDING_SCHEMA: Literal["tracefold.news.compiler_role_binding.v3"] = (
    "tracefold.news.compiler_role_binding.v3"
)
COMPILER_RECEIPT_SCHEMA: Literal["tracefold.news.compile_receipt_payload.v3"] = (
    "tracefold.news.compile_receipt_payload.v3"
)
COMPILER_RECEIPT_CHAIN_SCHEMA: Literal["tracefold.news.compile_receipt_chain.v3"] = (
    "tracefold.news.compile_receipt_chain.v3"
)
COMPILER_RUNNER_RECEIPTS_SCHEMA: Literal["tracefold.news.compiler_runner_receipts.v3"] = (
    "tracefold.news.compiler_runner_receipts.v3"
)
LEARNING_EPOCH: Literal["program_v6"] = "program_v6"
# v3 (#150): the projection now carries `policy_values`/`policy_sha256`/`policy_source`, and the metric
# fails closed without them. A dataset sealed under v2 is content-addressed by its `dataset_sha` and so
# cannot be regenerated without changing identity — left at v2 it would still validate here and then
# raise inside every single metric call, which a compile run reports as 100% provider failure with zero
# provider calls actually made. This is the one field whose job is to detect exactly that change.
COMPILE_EPISODE_PROJECTION_SCHEMA: Literal["tracefold.news.development_compile_episode.v3"] = (
    "tracefold.news.development_compile_episode.v3"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
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


class CompilerEndpointIdentity(_ExactModel):
    """Secret-free identity of one task or reflection compiler endpoint."""

    schema_version: Literal["tracefold.news.compiler_endpoint_identity.v3"] = COMPILER_ENDPOINT_IDENTITY_SCHEMA
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    endpoint_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, *, model: str, api_base: str) -> CompilerEndpointIdentity:
        normalized_model = str(model).strip()
        if not normalized_model:
            raise ValueError("news_program_compile_model_identity_unavailable")
        provider = normalized_model.split("/", maxsplit=1)[0] if "/" in normalized_model else "unknown"
        canonical_endpoint = _canonical_endpoint(api_base)
        endpoint_sha = canonical_sha(
            {
                "identity_schema": COMPILER_ENDPOINT_IDENTITY_SCHEMA,
                "canonical_endpoint": canonical_endpoint,
            }
        )
        model_sha = canonical_sha(
            {
                "identity_schema": COMPILER_ENDPOINT_IDENTITY_SCHEMA,
                "provider": provider,
                "model": normalized_model,
                "endpoint_sha256": endpoint_sha,
            }
        )
        return cls(
            provider=provider,
            model=normalized_model,
            endpoint_sha256=endpoint_sha,
            model_sha256=model_sha,
            binding_sha256=canonical_sha(
                {
                    "identity_schema": COMPILER_ENDPOINT_IDENTITY_SCHEMA,
                    "provider": provider,
                    "model": normalized_model,
                    "model_sha256": model_sha,
                }
            ),
        )

    @model_validator(mode="after")
    def _identity_matches(self) -> CompilerEndpointIdentity:
        expected_model = canonical_sha(
            {
                "identity_schema": self.schema_version,
                "provider": self.provider,
                "model": self.model,
                "endpoint_sha256": self.endpoint_sha256,
            }
        )
        expected_binding = canonical_sha(
            {
                "identity_schema": self.schema_version,
                "provider": self.provider,
                "model": self.model,
                "model_sha256": self.model_sha256,
            }
        )
        if self.model_sha256 != expected_model or self.binding_sha256 != expected_binding:
            raise ValueError("news_program_compile_endpoint_identity_mismatch")
        return self


class CompilerRoleBindingV3(_ExactModel):
    """Secret-free, content-addressed execution contract for one compiler role."""

    schema_version: Literal["tracefold.news.compiler_role_binding.v3"] = COMPILER_ROLE_BINDING_SCHEMA
    role: CompilerRole
    endpoint: CompilerEndpointIdentity
    max_output_tokens: int = Field(ge=64, le=32_000)
    timeout_seconds: float = Field(gt=0, le=3_600)
    temperature: float = Field(ge=0, le=2)
    model_kwargs_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter: Literal["strict_json", "gepa_reflection"]
    cache: Literal[False] = False
    num_retries: Literal[0] = 0
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        role: CompilerRole,
        endpoint: CompilerEndpointIdentity,
        max_output_tokens: int,
        timeout_seconds: float,
        temperature: float,
        model_kwargs: Mapping[str, Any],
    ) -> CompilerRoleBindingV3:
        safe_model_kwargs = _model_or_mapping_payload(model_kwargs)
        values = {
            "schema_version": COMPILER_ROLE_BINDING_SCHEMA,
            "role": role,
            "endpoint": endpoint.model_dump(mode="json"),
            "max_output_tokens": max_output_tokens,
            "timeout_seconds": float(timeout_seconds),
            "temperature": float(temperature),
            "model_kwargs_sha256": canonical_sha(safe_model_kwargs),
            "adapter": "gepa_reflection" if role == "reflection" else "strict_json",
            "cache": False,
            "num_retries": 0,
        }
        return cls(**values, binding_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _binding_matches(self) -> CompilerRoleBindingV3:
        if self.binding_sha256 != canonical_sha(self.model_dump(mode="json", exclude={"binding_sha256"})):
            raise ValueError("news_program_compile_role_binding_hash_mismatch")
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


class CompileCorpusReceipt(_ExactModel):
    """Trusted roots proving which accepted development projection was exported."""

    schema_version: Literal["tracefold.news.compile_corpus_receipt.v3"] = COMPILER_CORPUS_SCHEMA
    learning_epoch: Literal["program_v6"] = LEARNING_EPOCH
    development_dataset_sha: str = Field(pattern=_SHA256_PATTERN)
    development_dataset_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    learning_epoch_started_at_ms: int = Field(ge=0)
    projection_schema_id: Literal["tracefold.news.development_compile_episode.v3"] = COMPILE_EPISODE_PROJECTION_SCHEMA
    case_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    cluster_root_sha256: str = Field(pattern=_SHA256_PATTERN)
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

    @property
    def tariff_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))

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


class OptimizerCompileProvenanceV3(_ExactModel):
    """Framework-neutral exact optimizer provenance used by release governance.

    This mirrors the optimizer-only projection of the Program manifest without
    importing the Program implementation into ``CandidateEvaluator``.  Unlike
    the manifest's baseline/candidate union, every candidate field is required.
    """

    mode: Literal["optimizer_candidate"] = "optimizer_candidate"
    development_dataset_sha: str = Field(pattern=_SHA256_PATTERN)
    learning_epoch: Literal["program_v6"] = LEARNING_EPOCH
    learning_epoch_started_at_ms: int = Field(ge=0)
    projection_schema_id: str = Field(min_length=1)
    optimizer: Literal["dspy.GEPA@3.3.0/gepa@0.1.1"] = "dspy.GEPA@3.3.0/gepa@0.1.1"
    dspy_version: Literal["3.3.0"] = "3.3.0"
    gepa_version: Literal["0.1.1"] = "0.1.1"
    metric_sha256: str = Field(pattern=_SHA256_PATTERN)
    optimizer_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    seed: int = Field(ge=0)
    max_metric_calls: int = Field(gt=0)
    max_task_model_calls: int = Field(gt=0)
    max_reflection_model_calls: int = Field(gt=0)
    max_metric_judge_model_calls: int = Field(gt=0)
    max_cost_microusd: int = Field(gt=0)
    max_call_cost_microusd: int = Field(gt=0)
    metric_calls: int = Field(ge=0)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    metric_judge_attempts: int = Field(ge=0)
    metric_judge_model_calls: int = Field(ge=0)
    metric_judge_failures: int = Field(ge=0)
    task_cost_microusd: int = Field(ge=0)
    reflection_cost_microusd: int = Field(ge=0)
    metric_judge_cost_microusd: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)
    trajectory_sha256: str = Field(pattern=_SHA256_PATTERN)
    checkpoint_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    quality_kernel_sha256: str = Field(pattern=_SHA256_PATTERN)
    rule_pack_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    development_dataset_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    cluster_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_projection_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    episode_count: int = Field(gt=0)
    eligible_demo_bank_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    patch_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipt_payload_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_launch_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    task_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    reflection_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    metric_judge_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_policy_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _budgets_are_exact(self) -> OptimizerCompileProvenanceV3:
        if (
            self.task_model_calls > self.max_task_model_calls
            or self.reflection_model_calls > self.max_reflection_model_calls
            or self.metric_judge_model_calls > self.max_metric_judge_model_calls
            or self.metric_judge_model_calls > self.metric_judge_attempts
            or self.metric_judge_failures > self.metric_judge_attempts
            or self.actual_cost_microusd
            != self.task_cost_microusd + self.reflection_cost_microusd + self.metric_judge_cost_microusd
            or self.actual_cost_microusd > self.max_cost_microusd
            or self.max_call_cost_microusd > self.max_cost_microusd
        ):
            raise ValueError("news_program_compile_provenance_budget_exceeded")
        return self


class ProgramStrategyDiffV3(_ExactModel):
    predictor: Literal["event_semantics", "reader_card"]
    before_text_sha256: str = Field(pattern=_SHA256_PATTERN)
    after_text_sha256: str = Field(pattern=_SHA256_PATTERN)
    before_source: Literal["code_owned_baseline", "optimizer_patch"]
    after_source: Literal["optimizer_patch"] = "optimizer_patch"
    changed: bool

    @model_validator(mode="after")
    def _changed_matches_hashes(self) -> ProgramStrategyDiffV3:
        if self.changed != (self.before_text_sha256 != self.after_text_sha256):
            raise ValueError("news_program_compile_machine_diff_strategy_flag_invalid")
        return self


class ProgramDemoRefChangeV3(_ExactModel):
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _refs_are_sha256s(self) -> ProgramDemoRefChangeV3:
        for value in (*self.before, *self.after):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("news_program_compile_machine_diff_demo_ref_invalid")
        return self


class ProgramDemoRefDiffV3(_ExactModel):
    event_semantics: ProgramDemoRefChangeV3
    reader_card: ProgramDemoRefChangeV3


class ProgramImmutableDiffV3(_ExactModel):
    factory_id: Literal["tracefold.news.semantic_program.factory_v4"]
    quality_kernel_sha256: str = Field(pattern=_SHA256_PATTERN)
    rule_pack_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    route_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_sha256: str = Field(pattern=_SHA256_PATTERN)


class ProgramMachineDiffV3(_ExactModel):
    """Hash/ID-only proposal diff; prompts and demo payloads are forbidden."""

    schema_version: Literal["tracefold.news.program_machine_diff.v3"]
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    immutable: ProgramImmutableDiffV3
    learned_strategies: tuple[ProgramStrategyDiffV3, ProgramStrategyDiffV3] = Field(
        min_length=2,
        max_length=2,
    )
    demo_refs: ProgramDemoRefDiffV3
    selected_record_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    eligible_demo_bank_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    diff_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _diff_is_exact(self) -> ProgramMachineDiffV3:
        if tuple(item.predictor for item in self.learned_strategies) != (
            "event_semantics",
            "reader_card",
        ):
            raise ValueError("news_program_compile_machine_diff_strategy_order_invalid")
        changed_demo = any(
            item.before != item.after for item in (self.demo_refs.event_semantics, self.demo_refs.reader_card)
        )
        if not any(item.changed for item in self.learned_strategies) and not changed_demo:
            raise ValueError("news_program_compile_machine_diff_empty")
        values = self.model_dump(mode="json", exclude={"diff_sha256"})
        if self.diff_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_machine_diff_hash_mismatch")
        _reject_secret_material(values, path="program_machine_diff")
        return self


class CompileInputBundle(_ExactModel):
    """Canonical input handed to the hermetic optimizer runner."""

    schema_version: Literal["tracefold.news.compile_input_bundle.v3"] = COMPILER_INPUT_SCHEMA
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    stable_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_runtime_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    eligible_demo_bank_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    task: CompilerRoleBindingV3
    reflection: CompilerRoleBindingV3
    metric_judge: CompilerRoleBindingV3
    proxy_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    tariff_sha256: str = Field(pattern=_SHA256_PATTERN)
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
        parent_state_sha256: str,
        stable_bundle_sha256: str,
        target_runtime_manifest_sha256: str,
        eligible_demo_bank_root_sha256: str,
        task: CompilerRoleBindingV3,
        reflection: CompilerRoleBindingV3,
        metric_judge: CompilerRoleBindingV3,
        proxy_grant_sha256: str,
        proxy_config_sha256: str,
        tariff_sha256: str,
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
            "parent_state_sha256": parent_state_sha256,
            "stable_bundle_sha256": stable_bundle_sha256,
            "target_runtime_manifest_sha256": target_runtime_manifest_sha256,
            "eligible_demo_bank_root_sha256": eligible_demo_bank_root_sha256,
            "task": task.model_dump(mode="json"),
            "reflection": reflection.model_dump(mode="json"),
            "metric_judge": metric_judge.model_dump(mode="json"),
            "proxy_grant_sha256": proxy_grant_sha256,
            "proxy_config_sha256": proxy_config_sha256,
            "tariff_sha256": tariff_sha256,
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
        if self.tariff_sha256 != self.proxy_tariff.tariff_sha256:
            raise ValueError("news_program_compile_proxy_tariff_hash_mismatch")
        if self.corpus.development_dataset_payload_sha256 != canonical_sha(
            self.dataset_payload
        ) or self.corpus.development_dataset_sha != canonical_sha({"kind": "dataset", "payload": self.dataset_payload}):
            raise ValueError("news_program_compile_dataset_payload_root_mismatch")
        if self.corpus.episode_count != len(episode_payloads):
            raise ValueError("news_program_compile_episode_count_mismatch")
        if self.corpus.episode_projection_root_sha256 != canonical_sha(episode_payloads):
            raise ValueError("news_program_compile_episode_projection_root_mismatch")
        case_ids, cluster_ids = _episode_ids(episode_payloads)
        if self.corpus.case_root_sha256 != canonical_sha(case_ids):
            raise ValueError("news_program_compile_case_root_mismatch")
        if self.corpus.cluster_root_sha256 != canonical_sha(cluster_ids):
            raise ValueError("news_program_compile_cluster_root_mismatch")
        values = self.model_dump(mode="json", exclude={"bundle_sha256"})
        if self.bundle_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_input_bundle_hash_mismatch")
        _reject_secret_material(values, path="compile_input")
        return self


class ContentAddressedCompileReceipt(_ExactModel):
    """One retained compile payload with an independently verifiable identity."""

    schema_version: Literal["tracefold.news.compile_receipt_payload.v3"] = COMPILER_RECEIPT_SCHEMA
    kind: Literal[
        "corpus",
        "metric",
        "optimizer_config",
        "trajectory",
        "checkpoint",
        "sandbox_launch",
        "patch",
    ]
    payload: dict[str, Any]
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, kind: str, payload: BaseModel | Mapping[str, Any]) -> ContentAddressedCompileReceipt:
        safe_payload = _model_or_mapping_payload(payload)
        values = {
            "schema_version": COMPILER_RECEIPT_SCHEMA,
            "kind": kind,
            "payload": safe_payload,
        }
        return cls(**values, receipt_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _receipt_matches(self) -> ContentAddressedCompileReceipt:
        values = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != canonical_sha(values):
            raise ValueError(f"news_program_compile_{self.kind}_receipt_hash_mismatch")
        _reject_secret_material(self.payload, path=f"compile_receipt.{self.kind}")
        return self


class CompileReceiptChain(_ExactModel):
    """The complete retained payload chain required by propose/evaluate."""

    schema_version: Literal["tracefold.news.compile_receipt_chain.v3"] = COMPILER_RECEIPT_CHAIN_SCHEMA
    receipts: tuple[ContentAddressedCompileReceipt, ...] = Field(min_length=1)
    receipt_payload_root_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, receipts: Sequence[ContentAddressedCompileReceipt]) -> CompileReceiptChain:
        ordered = tuple(sorted(receipts, key=lambda receipt: receipt.kind))
        values = [receipt.model_dump(mode="json") for receipt in ordered]
        return cls(receipts=ordered, receipt_payload_root_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _chain_matches(self) -> CompileReceiptChain:
        kinds = tuple(receipt.kind for receipt in self.receipts)
        if len(kinds) != len(set(kinds)) or set(kinds) != _REQUIRED_RECEIPT_KINDS:
            raise ValueError("news_program_compile_receipt_chain_incomplete")
        if tuple(sorted(kinds)) != kinds:
            raise ValueError("news_program_compile_receipt_chain_order_invalid")
        values = [receipt.model_dump(mode="json") for receipt in self.receipts]
        if self.receipt_payload_root_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_receipt_chain_hash_mismatch")
        return self

    def payload(self, kind: str) -> dict[str, Any]:
        for receipt in self.receipts:
            if receipt.kind == kind:
                return dict(receipt.payload)
        raise ValueError(f"news_program_compile_receipt_missing:{kind}")


class CompilerRunnerReceiptsV3(_ExactModel):
    """Exact untrusted runner result, cross-checked against trusted receipts."""

    schema_version: Literal["tracefold.news.compiler_runner_receipts.v3"] = COMPILER_RUNNER_RECEIPTS_SCHEMA
    input_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    task_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    reflection_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    metric_judge_endpoint_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    metric_judge_attempts: int = Field(ge=0)
    metric_judge_model_calls: int = Field(ge=0)
    metric_judge_failures: int = Field(ge=0)
    task_cost_microusd: int = Field(ge=0)
    reflection_cost_microusd: int = Field(ge=0)
    metric_judge_cost_microusd: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)
    runner_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> CompilerRunnerReceiptsV3:
        payload = {"schema_version": COMPILER_RUNNER_RECEIPTS_SCHEMA, **values}
        return cls(**payload, runner_receipt_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _receipt_matches(self) -> CompilerRunnerReceiptsV3:
        values = self.model_dump(mode="json", exclude={"runner_receipt_sha256"})
        if self.runner_receipt_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_runner_receipt_hash_mismatch")
        if (
            self.metric_judge_model_calls > self.metric_judge_attempts
            or self.metric_judge_failures > self.metric_judge_attempts
            or self.actual_cost_microusd
            != self.task_cost_microusd + self.reflection_cost_microusd + self.metric_judge_cost_microusd
        ):
            raise ValueError("news_program_compile_runner_receipt_accounting_mismatch")
        _reject_secret_material(values, path="compiler_runner_receipts")
        return self


def validate_compile_receipt_chain_v3(
    chain: CompileReceiptChain | Mapping[str, Any],
    *,
    provenance: OptimizerCompileProvenanceV3 | Mapping[str, Any],
    patch_sha256: str,
    parent_program_sha256: str,
    parent_state_sha256: str,
    eligible_demo_bank_root_sha256: str,
    target_runtime_manifest_sha256: str,
) -> CompileReceiptChain:
    """Cross-bind the retained v3 chain without importing DSPy or Program code."""

    parsed = chain if isinstance(chain, CompileReceiptChain) else CompileReceiptChain.model_validate(chain)
    proof = (
        provenance
        if isinstance(provenance, OptimizerCompileProvenanceV3)
        else OptimizerCompileProvenanceV3.model_validate(provenance)
    )
    if (
        parsed.receipt_payload_root_sha256 != proof.receipt_payload_root_sha256
        or proof.patch_sha256 != patch_sha256
        or proof.parent_program_sha256 != parent_program_sha256
        or proof.parent_state_sha256 != parent_state_sha256
        or proof.eligible_demo_bank_root_sha256 != eligible_demo_bank_root_sha256
        or proof.target_runtime_manifest_sha256 != target_runtime_manifest_sha256
    ):
        raise ValueError("news_program_compile_receipt_chain_identity_mismatch")
    corpus = CompileCorpusReceipt.model_validate(parsed.payload("corpus"))
    if (
        corpus.development_dataset_sha != proof.development_dataset_sha
        or corpus.learning_epoch != proof.learning_epoch
        or corpus.learning_epoch_started_at_ms != proof.learning_epoch_started_at_ms
        or corpus.projection_schema_id != proof.projection_schema_id
        or corpus.development_dataset_payload_sha256 != proof.development_dataset_payload_sha256
        or corpus.case_root_sha256 != proof.case_root_sha256
        or corpus.cluster_root_sha256 != proof.cluster_root_sha256
        or corpus.episode_projection_root_sha256 != proof.episode_projection_root_sha256
        or corpus.episode_count != proof.episode_count
    ):
        raise ValueError("news_program_compile_receipt_chain_corpus_mismatch")
    if (
        canonical_sha(parsed.payload("metric")) != proof.metric_sha256
        or canonical_sha(parsed.payload("optimizer_config")) != proof.optimizer_config_sha256
        or canonical_sha(parsed.payload("trajectory")) != proof.trajectory_sha256
        or canonical_sha(parsed.payload("checkpoint")) != proof.checkpoint_sha256
    ):
        raise ValueError("news_program_compile_receipt_chain_payload_mismatch")
    patch = parsed.payload("patch")
    expected_patch_keys = {
        "schema_version",
        "parent_program_sha256",
        "parent_state_sha256",
        "learning_epoch",
        "learned_strategies",
        "demo_refs",
        "eligible_demo_bank_root_sha256",
        "patch_sha256",
    }
    if (
        set(patch) != expected_patch_keys
        or patch.get("schema_version") != "news_semantic_program_patch_v2"
        or patch.get("learning_epoch") != LEARNING_EPOCH
        or patch.get("parent_program_sha256") != parent_program_sha256
        or patch.get("parent_state_sha256") != parent_state_sha256
        or patch.get("eligible_demo_bank_root_sha256") != eligible_demo_bank_root_sha256
        or patch.get("patch_sha256") != patch_sha256
        or canonical_sha({key: value for key, value in patch.items() if key != "patch_sha256"}) != patch_sha256
    ):
        raise ValueError("news_program_compile_receipt_chain_patch_mismatch")
    strategies = patch.get("learned_strategies")
    refs = patch.get("demo_refs")
    if (
        not isinstance(strategies, list)
        or len(strategies) != 2
        or [item.get("predictor") if isinstance(item, Mapping) else None for item in strategies]
        != ["event_semantics", "reader_card"]
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"predictor", "text", "text_sha256", "source"}
            or item.get("source") != "optimizer_patch"
            or canonical_sha(item.get("text")) != item.get("text_sha256")
            for item in strategies
        )
        or not isinstance(refs, Mapping)
        or set(refs) != {"event_semantics", "reader_card"}
    ):
        raise ValueError("news_program_compile_receipt_chain_patch_write_set_invalid")

    from .program_compiler_sandbox import CompilerSandboxLaunchReceipt

    launch = CompilerSandboxLaunchReceipt.model_validate(parsed.payload("sandbox_launch"))
    optimizer = parsed.payload("optimizer_config")
    if set(optimizer) != {
        "runner_optimizer_config",
        "proxy_grant",
        "proxy_execution",
        "input_bundle_sha256",
    }:
        raise ValueError("news_program_compile_receipt_chain_optimizer_config_invalid")
    runner_optimizer_config = optimizer.get("runner_optimizer_config")
    if not isinstance(runner_optimizer_config, Mapping):
        raise ValueError("news_program_compile_receipt_chain_optimizer_config_invalid")
    try:
        metric_call_ceiling = gepa_metric_call_ceiling(
            max_metric_calls=proof.max_metric_calls,
            optimizer_config=runner_optimizer_config,
            expected_example_count=corpus.episode_count,
        )
    except ValueError as exc:
        raise ValueError("news_program_compile_receipt_chain_optimizer_config_invalid") from exc
    if proof.metric_calls > metric_call_ceiling:
        raise ValueError("news_program_compile_receipt_chain_metric_budget_exceeded")
    raw_grant = optimizer.get("proxy_grant")
    if not isinstance(raw_grant, Mapping):
        raise ValueError("news_program_compile_receipt_chain_proxy_grant_invalid")
    grant = dict(raw_grant)
    grant_keys = {
        "schema_version",
        "task",
        "reflection",
        "metric_judge",
        "max_task_model_calls",
        "max_reflection_model_calls",
        "max_metric_judge_model_calls",
        "max_cost_microusd",
        "max_call_cost_microusd",
        "tariff",
        "tariff_sha256",
        "proxy_config_sha256",
        "proxy_source_sha256",
        "max_request_bytes",
        "max_response_bytes",
        "grant_sha256",
    }
    try:
        tariff = CompilerProxyTariff.model_validate(grant.get("tariff"))
        roles: tuple[CompilerRole, ...] = ("task", "reflection", "metric_judge")
        bindings = {role: CompilerRoleBindingV3.model_validate(grant.get(role)) for role in roles}
        expected_max_call = max(
            tariff.worst_case_cost_microusd(
                role=role,
                request_bytes=int(grant["max_request_bytes"]),
                max_output_tokens=binding.max_output_tokens,
            )
            for role, binding in bindings.items()
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("news_program_compile_receipt_chain_proxy_grant_invalid") from exc
    if (
        set(grant) != grant_keys
        or grant.get("schema_version") != "tracefold.news.compiler_proxy_grant.v3"
        or grant.get("grant_sha256")
        != canonical_sha({key: value for key, value in grant.items() if key != "grant_sha256"})
        or grant.get("tariff_sha256") != tariff.tariff_sha256
        or grant.get("max_task_model_calls") != proof.max_task_model_calls
        or grant.get("max_reflection_model_calls") != proof.max_reflection_model_calls
        or grant.get("max_metric_judge_model_calls") != proof.max_metric_judge_model_calls
        or grant.get("max_cost_microusd") != proof.max_cost_microusd
        or grant.get("max_call_cost_microusd") != proof.max_call_cost_microusd
        or grant.get("max_call_cost_microusd") != expected_max_call
        or bindings["task"].endpoint.binding_sha256 != proof.task_endpoint_identity_sha256
        or bindings["reflection"].endpoint.binding_sha256 != proof.reflection_endpoint_identity_sha256
        or bindings["metric_judge"].endpoint.binding_sha256 != proof.metric_judge_endpoint_identity_sha256
    ):
        raise ValueError("news_program_compile_receipt_chain_proxy_grant_mismatch")
    execution = optimizer.get("proxy_execution")
    if not isinstance(execution, Mapping):
        raise ValueError("news_program_compile_receipt_chain_proxy_execution_invalid")
    execution_keys = {
        "schema_version",
        "grant_sha256",
        "task_model_calls",
        "reflection_model_calls",
        "metric_judge_model_calls",
        "task_cost_microusd",
        "reflection_cost_microusd",
        "metric_judge_cost_microusd",
        "task_failures",
        "reflection_failures",
        "metric_judge_failures",
        "actual_cost_microusd",
        "reserved_cost_microusd",
        "tariff_sha256",
        "calls",
        "call_root_sha256",
        "request_root_sha256",
        "response_root_sha256",
        "error_codes",
        "receipt_sha256",
    }
    execution_payload = dict(execution)
    call_payloads = execution_payload.get("calls")
    if not isinstance(call_payloads, list):
        raise ValueError("news_program_compile_receipt_chain_proxy_calls_invalid")
    expected_call_keys = {
        "role",
        "sequence",
        "request_sha256",
        "response_sha256",
        "runtime_identity_sha256",
        "provider_invoked",
        "request_bytes",
        "max_output_tokens",
        "reserved_cost_microusd",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
        "provider_cost_microusd",
        "finish_reason",
        "error_code",
        "leaf_sha256",
    }
    call_identities: set[tuple[str, int]] = set()
    role_calls: dict[str, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
    role_costs: dict[str, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
    role_failures: dict[str, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
    actual_cost = reserved_cost = 0
    request_shas: list[str] = []
    response_shas: list[str] = []
    error_codes: list[str] = []
    for item in call_payloads:
        if not isinstance(item, Mapping):
            raise ValueError("news_program_compile_receipt_chain_proxy_calls_invalid")
        call = dict(item)
        role = call.get("role")
        sequence = call.get("sequence")
        identity = (str(role), int(sequence)) if isinstance(sequence, int) else ("", 0)
        provider_invoked = call.get("provider_invoked")
        error_code = call.get("error_code")
        if (
            set(call) != expected_call_keys
            or role not in bindings
            or not isinstance(sequence, int)
            or sequence <= 0
            or identity in call_identities
            or not isinstance(provider_invoked, bool)
            or (role != "metric_judge" and (provider_invoked is not True or error_code is not None))
            or (provider_invoked is False and error_code is None)
            or (error_code is not None and not isinstance(error_code, str))
            or not isinstance(call.get("request_bytes"), int)
            or int(call["request_bytes"]) <= 0
            or int(call["request_bytes"]) > int(grant["max_request_bytes"])
            or call.get("max_output_tokens") != bindings[role].max_output_tokens
            or not isinstance(call.get("reserved_cost_microusd"), int)
            or (
                provider_invoked
                and (
                    int(call["reserved_cost_microusd"]) <= 0
                    or int(call["reserved_cost_microusd"])
                    != tariff.worst_case_cost_microusd(
                        role=role,
                        request_bytes=int(call["request_bytes"]),
                        max_output_tokens=int(call["max_output_tokens"]),
                    )
                )
            )
            or (not provider_invoked and int(call["reserved_cost_microusd"]) != 0)
            or not isinstance(call.get("total_tokens"), int)
            or (error_code is None and int(call["total_tokens"]) <= 0)
            or int(call["total_tokens"]) < 0
            or not isinstance(call.get("provider_cost_microusd"), int)
            or int(call["provider_cost_microusd"]) < 0
            or int(call["provider_cost_microusd"]) > int(call["reserved_cost_microusd"])
            or call.get("leaf_sha256")
            != canonical_sha({key: value for key, value in call.items() if key != "leaf_sha256"})
        ):
            raise ValueError("news_program_compile_receipt_chain_proxy_calls_invalid")
        for field in ("request_sha256", "response_sha256", "runtime_identity_sha256"):
            value = call.get(field)
            if not isinstance(value, str) or re.fullmatch(_SHA256_PATTERN, value) is None:
                raise ValueError("news_program_compile_receipt_chain_proxy_calls_invalid")
        call_identities.add(identity)
        role_calls[str(role)] += int(provider_invoked)
        role_costs[str(role)] += int(call["provider_cost_microusd"])
        role_failures[str(role)] += int(error_code is not None)
        actual_cost += int(call["provider_cost_microusd"])
        reserved_cost += int(call["reserved_cost_microusd"])
        request_shas.append(str(call["request_sha256"]))
        response_shas.append(str(call["response_sha256"]))
        if error_code is not None:
            error_codes.append(str(error_code))
    if (
        set(execution_payload) != execution_keys
        or execution_payload.get("schema_version") != "tracefold.news.compiler_proxy_execution.v3"
        or execution_payload.get("receipt_sha256")
        != canonical_sha({key: value for key, value in execution_payload.items() if key != "receipt_sha256"})
        or execution_payload.get("grant_sha256") != launch.proxy_identity_sha256
        or execution_payload.get("grant_sha256") != grant.get("grant_sha256")
        or execution_payload.get("tariff_sha256") != launch.proxy_tariff_sha256
        or launch.proxy_tariff_sha256 != tariff.tariff_sha256
        or launch.proxy_config_sha256 != grant.get("proxy_config_sha256")
        or launch.proxy_source_sha256 != grant.get("proxy_source_sha256")
        or execution_payload.get("receipt_sha256") != launch.proxy_execution_receipt_sha256
        or execution_payload.get("task_model_calls") != proof.task_model_calls
        or execution_payload.get("reflection_model_calls") != proof.reflection_model_calls
        or execution_payload.get("metric_judge_model_calls") != proof.metric_judge_model_calls
        or execution_payload.get("task_cost_microusd") != proof.task_cost_microusd
        or execution_payload.get("reflection_cost_microusd") != proof.reflection_cost_microusd
        or execution_payload.get("metric_judge_cost_microusd") != proof.metric_judge_cost_microusd
        or execution_payload.get("task_failures") != 0
        or execution_payload.get("reflection_failures") != 0
        or execution_payload.get("metric_judge_failures", 0) > proof.metric_judge_failures
        or execution_payload.get("actual_cost_microusd") != proof.actual_cost_microusd
        or execution_payload.get("task_model_calls") != role_calls["task"]
        or execution_payload.get("reflection_model_calls") != role_calls["reflection"]
        or execution_payload.get("metric_judge_model_calls") != role_calls["metric_judge"]
        or execution_payload.get("task_cost_microusd") != role_costs["task"]
        or execution_payload.get("reflection_cost_microusd") != role_costs["reflection"]
        or execution_payload.get("metric_judge_cost_microusd") != role_costs["metric_judge"]
        or execution_payload.get("task_failures") != role_failures["task"]
        or execution_payload.get("reflection_failures") != role_failures["reflection"]
        or execution_payload.get("metric_judge_failures") != role_failures["metric_judge"]
        or execution_payload.get("actual_cost_microusd") != actual_cost
        or execution_payload.get("reserved_cost_microusd") != reserved_cost
        or reserved_cost > proof.max_cost_microusd
        or any(int(item["reserved_cost_microusd"]) > proof.max_call_cost_microusd for item in call_payloads)
        or execution_payload.get("call_root_sha256") != canonical_sha(call_payloads)
        or execution_payload.get("request_root_sha256") != canonical_sha(request_shas)
        or execution_payload.get("response_root_sha256") != canonical_sha(response_shas)
        or execution_payload.get("error_codes") != error_codes
        or any(
            item.get("role") != "metric_judge"
            for item in call_payloads
            if isinstance(item, Mapping) and item.get("error_code") is not None
        )
        or role_calls["task"] > proof.max_task_model_calls
        or role_calls["reflection"] > proof.max_reflection_model_calls
        or role_calls["metric_judge"] > proof.max_metric_judge_model_calls
        or role_calls["metric_judge"] > proof.metric_judge_attempts
        or launch.launch_receipt_sha256 != proof.sandbox_launch_receipt_sha256
        or launch.compiler_source_sha256 != proof.compiler_source_sha256
        or launch.compiler_lock_sha256 != proof.compiler_lock_sha256
        or launch.policy_sha256 != proof.sandbox_policy_sha256
        or launch.input_bundle_sha256 != optimizer.get("input_bundle_sha256")
    ):
        raise ValueError("news_program_compile_receipt_chain_proxy_execution_mismatch")
    return parsed


def seal_compile_input(
    *,
    dataset_sha: str,
    dataset_payload: Mapping[str, Any],
    episodes: Sequence[BaseModel | Mapping[str, Any]],
    parent_program_sha256: str,
    parent_state_sha256: str,
    stable_bundle_sha256: str,
    target_runtime_manifest_sha256: str,
    eligible_demo_bank_root_sha256: str,
    task: CompilerRoleBindingV3,
    reflection: CompilerRoleBindingV3,
    metric_judge: CompilerRoleBindingV3,
    proxy_grant_sha256: str,
    proxy_config_sha256: str,
    tariff_sha256: str,
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
        or cohort.get("program_version") != "news_semantic_program_v4"
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
        development_dataset_payload_sha256=canonical_sha(payload),
        learning_epoch_started_at_ms=learning_epoch_started_at_ms,
        projection_schema_id=COMPILE_EPISODE_PROJECTION_SCHEMA,
        case_root_sha256=canonical_sha(case_ids),
        cluster_root_sha256=canonical_sha(cluster_ids),
        episode_projection_root_sha256=canonical_sha(list(episode_payloads)),
        episode_count=len(episode_payloads),
        review_rubric_version=review_rubric_version,
    )
    return CompileInputBundle.issue(
        parent_program_sha256=parent_program_sha256,
        parent_state_sha256=parent_state_sha256,
        stable_bundle_sha256=stable_bundle_sha256,
        target_runtime_manifest_sha256=target_runtime_manifest_sha256,
        eligible_demo_bank_root_sha256=eligible_demo_bank_root_sha256,
        task=task,
        reflection=reflection,
        metric_judge=metric_judge,
        proxy_grant_sha256=proxy_grant_sha256,
        proxy_config_sha256=proxy_config_sha256,
        tariff_sha256=tariff_sha256,
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
    "COMPILER_CORPUS_SCHEMA",
    "COMPILER_ENDPOINT_IDENTITY_SCHEMA",
    "COMPILER_INPUT_SCHEMA",
    "COMPILER_RECEIPT_CHAIN_SCHEMA",
    "COMPILER_RECEIPT_SCHEMA",
    "COMPILER_ROLE_BINDING_SCHEMA",
    "COMPILER_RUNNER_RECEIPTS_SCHEMA",
    "COMPILE_EPISODE_PROJECTION_SCHEMA",
    "LEARNING_EPOCH",
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "CompileBudgetV3",
    "CompileCorpusReceipt",
    "CompileInputBundle",
    "CompileReceiptChain",
    "CompilerEndpointIdentity",
    "CompilerProxyTariff",
    "CompilerRole",
    "CompilerRoleBindingV3",
    "CompilerRunnerReceiptsV3",
    "ContentAddressedCompileReceipt",
    "OptimizerCompileProvenanceV3",
    "ProgramDemoRefChangeV3",
    "ProgramDemoRefDiffV3",
    "ProgramImmutableDiffV3",
    "ProgramMachineDiffV3",
    "ProgramStrategyDiffV3",
    "gepa_metric_call_ceiling",
    "seal_compile_input",
    "validate_compile_receipt_chain_v3",
]
