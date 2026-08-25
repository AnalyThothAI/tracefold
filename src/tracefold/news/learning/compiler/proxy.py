"""Secret-free Unix model proxy for the isolated Program compiler.

Only this trusted host-side server owns provider LM objects and their
credentials.  The container receives a content-addressed grant and connects to
one exact Unix socket.  Every request is bounded, capability keys are rejected,
provider cost is charged by the host, and the final execution receipt is derived
from requests observed by the server rather than optimizer self-attestation.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import struct
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...artifact_identity import canonical_json, canonical_sha
from ...program.dspy_adapter import (
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
)
from .security import (
    PROXY_EXECUTION_SCHEMA,
    CompilerProxyCall,
    CompilerProxyExecution,
    CompilerProxyTariff,
    CompilerRole,
    ModelExecutionIdentity,
)

PROXY_GRANT_SCHEMA: Literal["tracefold.news.compiler_proxy_grant.v3"] = "tracefold.news.compiler_proxy_grant.v3"
PROXY_REQUEST_SCHEMA: Literal["tracefold.news.compiler_proxy_request.v3"] = "tracefold.news.compiler_proxy_request.v3"
PROXY_RESPONSE_SCHEMA: Literal["tracefold.news.compiler_proxy_response.v3"] = (
    "tracefold.news.compiler_proxy_response.v3"
)
PROXY_READY_SCHEMA: Literal["tracefold.news.compiler_proxy_ready.v3"] = "tracefold.news.compiler_proxy_ready.v3"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FRAME_PREFIX_BYTES = 4
_FORBIDDEN_CAPABILITY_KEYS = frozenset(
    {
        "api_base",
        "api_key",
        "authorization",
        "base_url",
        "credential",
        "credentials",
        "database_url",
        "db_dsn",
        "endpoint_url",
        "headers",
        "password",
        "secret",
        "token",
    }
)
_ALLOWED_REQUEST_KWARGS = frozenset(
    {
        "messages",
        "prompt",
        "response_format",
    }
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompilerProviderEndpointSecret(_ExactModel):
    """Ephemeral sidecar-only provider config; never a receipt payload."""

    model: str = Field(min_length=1)
    api_base: str = Field(min_length=1, repr=False)
    api_key: str = Field(min_length=1, repr=False)
    timeout: float = Field(gt=0, le=3_600)
    max_tokens: int = Field(ge=64, le=32_000)
    temperature: float = Field(ge=0, le=2)
    model_kwargs: dict[str, Any] = Field(default_factory=dict, repr=False)

    def binding(self, role: CompilerRole) -> ModelExecutionIdentity:
        return ModelExecutionIdentity.issue(
            role=role,
            model=self.model,
            api_base=self.api_base,
            max_output_tokens=self.max_tokens,
            timeout_seconds=self.timeout,
            temperature=self.temperature,
            model_kwargs=self.model_kwargs,
        )


class CompilerProxySecretConfig(_ExactModel):
    """Ephemeral 0600 file mounted only into the trusted proxy sidecar."""

    task: CompilerProviderEndpointSecret
    reflection: CompilerProviderEndpointSecret
    metric_judge: CompilerProviderEndpointSecret
    tariff: CompilerProxyTariff

    @model_validator(mode="after")
    def _role_contracts_are_exact(self) -> CompilerProxySecretConfig:
        self.task.binding("task")
        self.reflection.binding("reflection")
        self.metric_judge.binding("metric_judge")
        return self

    @property
    def secret_free_config_sha256(self) -> str:
        """The identity of a configuration that exists only as a 0600 file in the sidecar.

        The bundle names it; nothing carries it, which is what makes this digest an address rather than
        a restatement.
        """

        return canonical_sha(
            {
                "task": self.task.binding("task").model_dump(mode="json"),
                "reflection": self.reflection.binding("reflection").model_dump(mode="json"),
                "metric_judge": self.metric_judge.binding("metric_judge").model_dump(mode="json"),
                "tariff": self.tariff.model_dump(mode="json"),
            }
        )


class CompilerModelProxyGrant(_ExactModel):
    """Secret-free authority and budget visible to both sides of the socket."""

    schema_version: Literal["tracefold.news.compiler_proxy_grant.v3"] = PROXY_GRANT_SCHEMA
    task: ModelExecutionIdentity
    reflection: ModelExecutionIdentity
    metric_judge: ModelExecutionIdentity
    max_task_model_calls: int = Field(gt=0, le=100_000)
    max_reflection_model_calls: int = Field(gt=0, le=100_000)
    max_metric_judge_model_calls: int = Field(gt=0, le=100_000)
    max_cost_microusd: int = Field(gt=0)
    max_call_cost_microusd: int = Field(gt=0)
    tariff: CompilerProxyTariff
    proxy_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    max_request_bytes: int = Field(default=2_000_000, ge=1_024, le=8_000_000)
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=8_000_000)
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        task: ModelExecutionIdentity,
        reflection: ModelExecutionIdentity,
        metric_judge: ModelExecutionIdentity,
        max_task_model_calls: int,
        max_reflection_model_calls: int,
        max_metric_judge_model_calls: int,
        max_cost_microusd: int,
        tariff: CompilerProxyTariff,
        proxy_config_sha256: str,
        proxy_source_sha256: str,
        max_request_bytes: int = 2_000_000,
        max_response_bytes: int = 2_000_000,
    ) -> CompilerModelProxyGrant:
        bindings = (task, reflection, metric_judge)
        max_call_cost_microusd = max(
            tariff.worst_case_cost_microusd(
                role=binding.role,
                request_bytes=max_request_bytes,
                max_output_tokens=binding.max_output_tokens,
            )
            for binding in bindings
        )
        values = {
            "schema_version": PROXY_GRANT_SCHEMA,
            "task": task.model_dump(mode="json"),
            "reflection": reflection.model_dump(mode="json"),
            "metric_judge": metric_judge.model_dump(mode="json"),
            "max_task_model_calls": max_task_model_calls,
            "max_reflection_model_calls": max_reflection_model_calls,
            "max_metric_judge_model_calls": max_metric_judge_model_calls,
            "max_cost_microusd": max_cost_microusd,
            "max_call_cost_microusd": max_call_cost_microusd,
            "tariff": tariff.model_dump(mode="json"),
            "proxy_config_sha256": proxy_config_sha256,
            "proxy_source_sha256": proxy_source_sha256,
            "max_request_bytes": max_request_bytes,
            "max_response_bytes": max_response_bytes,
        }
        return cls(**values, grant_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _identity_matches(self) -> CompilerModelProxyGrant:
        values = self.model_dump(mode="json", exclude={"grant_sha256"})
        if self.grant_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_proxy_grant_hash_mismatch")
        if (self.task.role, self.reflection.role, self.metric_judge.role) != (
            "task",
            "reflection",
            "metric_judge",
        ):
            raise ValueError("news_program_compile_proxy_role_binding_order_invalid")
        roles: tuple[CompilerRole, ...] = ("task", "reflection", "metric_judge")
        expected_max_call = max(
            self.reservation_microusd(role=role, request_bytes=self.max_request_bytes) for role in roles
        )
        if self.max_call_cost_microusd != expected_max_call or self.max_call_cost_microusd > self.max_cost_microusd:
            raise ValueError("news_program_compile_proxy_call_cost_reservation_invalid")
        return self

    def binding(self, role: CompilerRole) -> ModelExecutionIdentity:
        return {"task": self.task, "reflection": self.reflection, "metric_judge": self.metric_judge}[role]

    def max_model_calls(self, role: CompilerRole) -> int:
        return {
            "task": self.max_task_model_calls,
            "reflection": self.max_reflection_model_calls,
            "metric_judge": self.max_metric_judge_model_calls,
        }[role]

    def reservation_microusd(self, *, role: CompilerRole, request_bytes: int) -> int:
        binding = self.binding(role)
        return self.tariff.worst_case_cost_microusd(
            role=role,
            request_bytes=request_bytes,
            max_output_tokens=binding.max_output_tokens,
        )

    def timeout_seconds(self, role: CompilerRole) -> float:
        return self.binding(role).timeout_seconds


class CompilerProxyRequest(_ExactModel):
    schema_version: Literal["tracefold.news.compiler_proxy_request.v3"] = PROXY_REQUEST_SCHEMA
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: CompilerRole
    sequence: int = Field(gt=0)
    args: tuple[Any, ...] = Field(default=(), repr=False)
    kwargs: dict[str, Any] = Field(default_factory=dict, repr=False)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        grant_sha256: str,
        role: CompilerRole,
        sequence: int,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> CompilerProxyRequest:
        safe_args = _json_safe(list(args))
        safe_kwargs = _json_safe(dict(kwargs))
        if not isinstance(safe_args, list) or not isinstance(safe_kwargs, dict):
            raise TypeError("news_program_compile_proxy_request_arguments_invalid")
        values = {
            "schema_version": PROXY_REQUEST_SCHEMA,
            "grant_sha256": grant_sha256,
            "role": role,
            "sequence": sequence,
            "args": safe_args,
            "kwargs": safe_kwargs,
        }
        _reject_capability_keys(values, path="proxy_request")
        _validate_request_call_shape(safe_args, safe_kwargs)
        return cls(**values, request_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _request_matches(self) -> CompilerProxyRequest:
        values = self.model_dump(mode="json", exclude={"request_sha256"})
        _reject_capability_keys(values, path="proxy_request")
        _validate_request_call_shape(list(self.args), dict(self.kwargs))
        if self.request_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_proxy_request_hash_mismatch")
        return self


class CompilerProxyResponse(_ExactModel):
    schema_version: Literal["tracefold.news.compiler_proxy_response.v3"] = PROXY_RESPONSE_SCHEMA
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_invoked: bool
    ok: bool
    output: Any | None = Field(default=None, repr=False)
    metadata: ExactProviderMetadata | None = None
    error_code: str | None = None
    response_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        request_sha256: str,
        provider_invoked: bool,
        output: Any | None = None,
        metadata: ExactProviderMetadata | None = None,
        error_code: str | None = None,
    ) -> CompilerProxyResponse:
        ok = error_code is None
        safe_output = _json_safe(output)
        values = {
            "schema_version": PROXY_RESPONSE_SCHEMA,
            "request_sha256": request_sha256,
            "provider_invoked": provider_invoked,
            "ok": ok,
            "output": safe_output,
            "metadata": metadata.model_dump(mode="json") if metadata is not None else None,
            "error_code": error_code,
        }
        return cls(**values, response_sha256=canonical_sha(values))

    @model_validator(mode="after")
    def _response_matches(self) -> CompilerProxyResponse:
        values = self.model_dump(mode="json", exclude={"response_sha256"})
        if self.response_sha256 != canonical_sha(values):
            raise ValueError("news_program_compile_proxy_response_hash_mismatch")
        if self.ok != (self.error_code is None):
            raise ValueError("news_program_compile_proxy_response_status_invalid")
        if self.ok and not self.provider_invoked:
            raise ValueError("news_program_compile_proxy_success_without_provider")
        if not self.provider_invoked and self.metadata is not None:
            raise ValueError("news_program_compile_proxy_nonprovider_metadata_forbidden")
        if self.ok and (self.metadata is None or self.output is None):
            raise ValueError("news_program_compile_proxy_success_incomplete")
        if not self.ok and self.output is not None:
            raise ValueError("news_program_compile_proxy_failure_output_forbidden")
        return self


class CompilerProxyReadyReceipt(_ExactModel):
    """What the sidecar tells the launcher before the compiler container starts.

    Each field addresses an object that reaches the sidecar by a different channel and is not carried
    here: the grant over the socket, the secret config as a 0600 mount, the proxy source inside the
    image. The receipt has no digest of itself — the launcher compares the three values it already holds.
    """

    schema_version: Literal["tracefold.news.compiler_proxy_ready.v3"] = PROXY_READY_SCHEMA
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    proxy_source_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(
        cls,
        *,
        grant: CompilerModelProxyGrant,
        proxy_source_sha256: str,
    ) -> CompilerProxyReadyReceipt:
        return cls(
            grant_sha256=grant.grant_sha256,
            proxy_config_sha256=grant.proxy_config_sha256,
            proxy_source_sha256=proxy_source_sha256,
        )


class CompilerProxyError(RuntimeError):
    """Stable proxy failure without provider exception or response leakage."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_proxy_provider_lm(config: CompilerProviderEndpointSecret, *, role: CompilerRole) -> Any:
    """Build one credential-owning LM only inside the trusted proxy sidecar."""

    extras = dict(config.model_kwargs)
    owned = {
        "api_key",
        "api_base",
        "base_url",
        "cache",
        "num_retries",
        "temperature",
        "max_tokens",
        "timeout",
    }
    overlap = owned.intersection(extras)
    if overlap:
        raise ValueError("news_program_compile_proxy_model_kwargs_owned")
    lm = ExactMetadataDspyLM(
        config.model,
        api_key=config.api_key,
        api_base=config.api_base,
        timeout=config.timeout,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        cache=False,
        num_retries=0,
        **extras,
    )
    lm.tracefold_compiler_endpoint_identity = config.binding(role)
    return lm


class CompilerProxyLM(dspy.BaseLM):  # type: ignore[misc]
    """DSPy-compatible container client with no provider credential or URL."""

    cache = False
    num_retries = 0

    def __init__(
        self,
        *,
        socket_path: Path,
        grant: CompilerModelProxyGrant,
        role: CompilerRole,
    ) -> None:
        self._socket_path = _exact_socket_path(socket_path, require_existing=True)
        self._grant = grant
        self._role = role
        self._timeout_seconds = grant.timeout_seconds(role)
        self._sequence = 0
        self._capture: ExactProviderCallCapture | None = None
        binding = grant.binding(role)
        super().__init__(
            model=binding.model,
            model_type="chat",
            temperature=binding.temperature,
            max_tokens=binding.max_output_tokens,
            cache=False,
            num_retries=0,
            timeout=grant.timeout_seconds(role),
        )
        self.tracefold_compiler_endpoint_identity = binding
        self.tracefold_compiler_role_binding = binding

    @contextmanager
    def observe_exact_call(self) -> Iterator[ExactProviderCallCapture]:
        if self._capture is not None:
            raise CompilerProxyError("news_program_compile_proxy_nested_capture")
        capture = ExactProviderCallCapture()
        self._capture = capture
        try:
            yield capture
        finally:
            self._capture = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._sequence += 1
        safe_kwargs = self._strip_trusted_owned_options(kwargs)
        request = CompilerProxyRequest.issue(
            grant_sha256=self._grant.grant_sha256,
            role=self._role,
            sequence=self._sequence,
            args=args,
            kwargs=safe_kwargs,
        )
        response = _proxy_exchange(
            self._socket_path,
            request,
            timeout_seconds=self._timeout_seconds,
            max_request_bytes=self._grant.max_request_bytes,
            max_response_bytes=self._grant.max_response_bytes,
        )
        if response.metadata is not None and self._capture is not None:
            self._capture.record_metadata(response.metadata)
        elif response.provider_invoked and self._capture is not None:
            # The trusted proxy knows a physical call happened even when the provider failed before returning
            # usage metadata. Preserve that fact for the runner's per-role call accounting; zero usage/cost is
            # explicit unavailability, not an assertion that the provider call was free.
            self._capture.record_metadata(
                ExactProviderMetadata(
                    response_model=self._grant.binding(self._role).model,
                    total_tokens=0,
                    provider_cost_microusd=0,
                )
            )
        if not response.ok:
            raise CompilerProxyError(str(response.error_code))
        return response.output

    def _strip_trusted_owned_options(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        safe = dict(kwargs)
        binding = self._grant.binding(self._role)
        expected: dict[str, Any] = {
            "max_tokens": binding.max_output_tokens,
            "temperature": binding.temperature,
            "cache": False,
            "timeout": self._grant.timeout_seconds(self._role),
            "num_retries": 0,
        }
        for name, trusted in expected.items():
            if name not in safe:
                continue
            supplied = safe.pop(name)
            if name == "max_tokens":
                matches = (
                    isinstance(supplied, int)
                    and not isinstance(supplied, bool)
                    and 64 <= supplied <= trusted
                    and (self._role != "metric_judge" or supplied == trusted)
                )
            elif name in {"temperature", "timeout"}:
                matches = isinstance(supplied, int | float) and float(supplied) == trusted
            else:
                matches = supplied == trusted and type(supplied) is type(trusted)
            if not matches:
                raise CompilerProxyError("news_program_compile_proxy_request_option_override")
        return safe

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        # GEPA is configured with num_threads=1.  Avoid creating an executor
        # thread inside the one-PID compiler container.
        return self(*args, **kwargs)


class TrustedCompilerModelProxy:
    """Sequential host server enforcing the grant against exact provider calls."""

    def __init__(
        self,
        *,
        grant: CompilerModelProxyGrant,
        task_lm: Any,
        reflection_lm: Any,
        metric_judge_lm: Any,
    ) -> None:
        self._grant = grant
        self._lms: dict[CompilerRole, Any] = {
            "task": task_lm,
            "reflection": reflection_lm,
            "metric_judge": metric_judge_lm,
        }
        for role, lm in self._lms.items():
            if getattr(lm, "cache", True) is not False or int(getattr(lm, "num_retries", -1)) != 0:
                raise ValueError("news_program_compile_proxy_lm_policy_invalid")
            if not callable(getattr(lm, "observe_exact_call", None)):
                raise ValueError("news_program_compile_proxy_metadata_seam_required")
            if getattr(lm, "tracefold_compiler_endpoint_identity", None) != grant.binding(role):
                raise ValueError("news_program_compile_proxy_endpoint_identity_mismatch")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._server_error: str | None = None
        self._seen_sequences: set[tuple[str, int]] = set()
        self._request_sha256s: list[str] = []
        self._response_sha256s: list[str] = []
        self._error_codes: list[str] = []
        self._calls: list[CompilerProxyCall] = []
        self._role_calls: dict[CompilerRole, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
        self._role_costs: dict[CompilerRole, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
        self._role_failures: dict[CompilerRole, int] = {"task": 0, "reflection": 0, "metric_judge": 0}
        self._actual_cost_microusd = 0
        self._imputed_cost_calls = 0
        self._reserved_cost_microusd = 0

    @contextmanager
    def serve(self, socket_path: Path) -> Iterator[Path]:
        path = _exact_socket_path(socket_path, require_existing=False)
        if path.exists():
            raise ValueError("news_program_compile_proxy_socket_already_exists")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(1)
            server.settimeout(0.1)
            self._server = server
            self._thread = threading.Thread(target=self._serve, name="tracefold-compiler-proxy", daemon=True)
            self._thread.start()
            yield path
        finally:
            self._stop.set()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                    wake.connect(str(path))
            except OSError:
                pass
            if self._thread is not None:
                self._thread.join(timeout=5)
            server.close()
            self._server = None
            with suppress(FileNotFoundError):
                path.unlink()
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("news_program_compile_proxy_shutdown_failed")
        if self._server_error is not None:
            raise RuntimeError(self._server_error)

    def execution_receipt(self) -> CompilerProxyExecution:
        if self._thread is not None and self._thread.is_alive():
            raise ValueError("news_program_compile_proxy_receipt_before_shutdown")
        calls = tuple(self._calls)
        return CompilerProxyExecution(
            grant_sha256=self._grant.grant_sha256,
            task_model_calls=sum(call.provider_invoked and call.role == "task" for call in calls),
            reflection_model_calls=sum(call.provider_invoked and call.role == "reflection" for call in calls),
            metric_judge_model_calls=sum(call.provider_invoked and call.role == "metric_judge" for call in calls),
            task_cost_microusd=sum(call.provider_cost_microusd for call in calls if call.role == "task"),
            reflection_cost_microusd=sum(call.provider_cost_microusd for call in calls if call.role == "reflection"),
            metric_judge_cost_microusd=sum(
                call.provider_cost_microusd for call in calls if call.role == "metric_judge"
            ),
            task_failures=sum(call.error_code is not None and call.role == "task" for call in calls),
            reflection_failures=sum(call.error_code is not None and call.role == "reflection" for call in calls),
            metric_judge_failures=sum(call.error_code is not None and call.role == "metric_judge" for call in calls),
            actual_cost_microusd=sum(call.provider_cost_microusd for call in calls),
            reserved_cost_microusd=sum(call.reserved_cost_microusd for call in calls),
            error_codes=tuple(call.error_code for call in calls if call.error_code is not None),
            calls=calls,
        )

    def _serve(self) -> None:
        server = self._server
        if server is None:
            self._server_error = "news_program_compile_proxy_server_not_started"
            return
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    if self._stop.is_set():
                        return
                    self._handle(connection)
        except Exception:
            self._server_error = "news_program_compile_proxy_server_failed"

    def _handle(self, connection: socket.socket) -> None:
        try:
            request_payload = _recv_frame(connection, max_bytes=self._grant.max_request_bytes)
            request = CompilerProxyRequest.model_validate(_strict_json_loads(request_payload))
        except (OSError, TypeError, ValueError):
            return
        self._request_sha256s.append(request.request_sha256)
        reservation = self._grant.reservation_microusd(
            role=request.role,
            request_bytes=len(request_payload),
        )
        provider_invoked = False
        reserved = 0
        if request.grant_sha256 != self._grant.grant_sha256:
            response = self._failure(request, "news_program_compile_proxy_grant_mismatch", provider_invoked=False)
        elif (request.role, request.sequence) in self._seen_sequences:
            response = self._failure(request, "news_program_compile_proxy_sequence_reused", provider_invoked=False)
        elif self._role_calls[request.role] >= self._grant.max_model_calls(request.role):
            response = self._failure(
                request, "news_program_compile_proxy_call_budget_exhausted", provider_invoked=False
            )
        elif self._reserved_cost_microusd + reservation > self._grant.max_cost_microusd:
            response = self._failure(
                request, "news_program_compile_proxy_cost_reservation_exhausted", provider_invoked=False
            )
        else:
            self._seen_sequences.add((request.role, request.sequence))
            self._reserved_cost_microusd += reservation
            reserved = reservation
            provider_invoked = True
            response = self._invoke(request, reservation_microusd=reservation)
        encoded = canonical_json(response.model_dump(mode="json")).encode("utf-8")
        if len(encoded) > self._grant.max_response_bytes:
            response = self._failure(
                request,
                "news_program_compile_proxy_response_too_large",
                provider_invoked=provider_invoked,
                metadata=response.metadata,
            )
            encoded = canonical_json(response.model_dump(mode="json")).encode("utf-8")
        self._response_sha256s.append(response.response_sha256)
        metadata = response.metadata
        self._calls.append(
            CompilerProxyCall(
                role=request.role,
                sequence=request.sequence,
                request_sha256=request.request_sha256,
                response_sha256=response.response_sha256,
                responding_model=metadata.response_model if metadata is not None else None,
                provider_invoked=provider_invoked,
                request_bytes=len(request_payload),
                max_output_tokens=self._grant.binding(request.role).max_output_tokens,
                reserved_cost_microusd=reserved,
                input_tokens=metadata.input_tokens if metadata is not None else 0,
                output_tokens=metadata.output_tokens if metadata is not None else 0,
                cached_tokens=metadata.cached_tokens if metadata is not None else 0,
                total_tokens=metadata.total_tokens if metadata is not None else 0,
                provider_cost_microusd=(
                    metadata.provider_cost_microusd
                    if metadata is not None and metadata.provider_cost_microusd is not None
                    else 0
                ),
                finish_reason=metadata.finish_reason if metadata is not None else None,
                error_code=response.error_code,
            )
        )
        _send_frame(connection, encoded, max_bytes=self._grant.max_response_bytes)

    def _invoke(
        self,
        request: CompilerProxyRequest,
        *,
        reservation_microusd: int,
    ) -> CompilerProxyResponse:
        lm = self._lms[request.role]
        self._role_calls[request.role] += 1
        metadata: ExactProviderMetadata | None = None
        try:
            with lm.observe_exact_call() as capture:
                output = lm(*request.args, **request.kwargs)
            metadata = capture.require_exactly_one()
        except PredictorAdapterError:
            return self._failure(
                request,
                "news_program_compile_proxy_provider_metadata_unavailable",
                provider_invoked=True,
            )
        except Exception:
            try:
                metadata = capture.require_exactly_one()
            except (PredictorAdapterError, UnboundLocalError):
                metadata = None
            if metadata is not None:
                self._charge(request.role, metadata)
            return self._failure(
                request,
                "news_program_compile_proxy_model_call_failed",
                provider_invoked=True,
                metadata=metadata,
            )
        # A provider that reports no price is the normal case here, not an anomaly: neither the local
        # llama.cpp endpoint nor DeepSeek returns one litellm can resolve, so failing closed on `None` meant a
        # real compile died on its first model call. The grant already computed this exact request's trusted
        # worst-case reservation from the tariff; charging that is strictly conservative — it over-charges, so
        # the budget stops the run early rather than late — and keeps the spend provable without depending on
        # a price the provider does not publish.
        imputed = metadata.provider_cost_microusd is None
        if imputed:
            metadata = metadata.model_copy(update={"provider_cost_microusd": reservation_microusd})
            self._imputed_cost_calls += 1
        self._charge(request.role, metadata)
        if metadata.total_tokens <= 0:
            return self._failure(
                request,
                "news_program_compile_proxy_provider_usage_unavailable",
                provider_invoked=True,
                metadata=metadata,
            )
        provider_cost_microusd = cast(int, metadata.provider_cost_microusd)
        if provider_cost_microusd > reservation_microusd:
            return self._failure(
                request,
                "news_program_compile_proxy_call_cost_reservation_exceeded",
                provider_invoked=True,
                metadata=metadata,
            )
        try:
            return CompilerProxyResponse.issue(
                request_sha256=request.request_sha256,
                provider_invoked=True,
                output=output,
                metadata=metadata,
            )
        except (TypeError, ValueError):
            return self._failure(
                request,
                "news_program_compile_proxy_output_invalid",
                provider_invoked=True,
                metadata=metadata,
            )

    def _charge(self, role: CompilerRole, metadata: ExactProviderMetadata) -> None:
        if metadata.provider_cost_microusd is not None:
            self._actual_cost_microusd += metadata.provider_cost_microusd
            self._role_costs[role] += metadata.provider_cost_microusd

    def _failure(
        self,
        request: CompilerProxyRequest,
        code: str,
        *,
        provider_invoked: bool,
        metadata: ExactProviderMetadata | None = None,
    ) -> CompilerProxyResponse:
        self._error_codes.append(code)
        self._role_failures[request.role] += 1
        return CompilerProxyResponse.issue(
            request_sha256=request.request_sha256,
            provider_invoked=provider_invoked,
            metadata=metadata,
            error_code=code,
        )


def _proxy_exchange(
    socket_path: Path,
    request: CompilerProxyRequest,
    *,
    timeout_seconds: float,
    max_request_bytes: int,
    max_response_bytes: int,
) -> CompilerProxyResponse:
    document = canonical_json(request.model_dump(mode="json")).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            _send_frame(connection, document, max_bytes=max_request_bytes)
            response_payload = _recv_frame(connection, max_bytes=max_response_bytes)
        response = CompilerProxyResponse.model_validate(_strict_json_loads(response_payload))
    except (OSError, TypeError, ValueError) as exc:
        raise CompilerProxyError("news_program_compile_proxy_exchange_failed") from exc
    if response.request_sha256 != request.request_sha256:
        raise CompilerProxyError("news_program_compile_proxy_response_request_mismatch")
    return response


def _send_frame(connection: socket.socket, payload: bytes, *, max_bytes: int) -> None:
    if not payload or len(payload) > max_bytes:
        raise ValueError("news_program_compile_proxy_frame_size_invalid")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _recv_frame(connection: socket.socket, *, max_bytes: int) -> bytes:
    prefix = _recv_exact(connection, _FRAME_PREFIX_BYTES)
    size = struct.unpack("!I", prefix)[0]
    if size <= 0 or size > max_bytes:
        raise ValueError("news_program_compile_proxy_frame_size_invalid")
    return _recv_exact(connection, size)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise OSError("news_program_compile_proxy_frame_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _strict_json_loads(payload: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"news_program_compile_proxy_duplicate_key:{key}")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"news_program_compile_proxy_nonfinite:{value}")),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("news_program_compile_proxy_non_string_key")
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("news_program_compile_proxy_nonfinite_value")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"news_program_compile_proxy_non_json_value:{type(value).__name__}")


def _reject_capability_keys(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_CAPABILITY_KEYS:
                raise ValueError(f"news_program_compile_proxy_capability_key:{path}.{key}")
            _reject_capability_keys(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_capability_keys(child, path=f"{path}[{index}]")


def _validate_request_call_shape(args: list[Any], kwargs: dict[str, Any]) -> None:
    """Allow only DSPy's data-bearing structured-call surface.

    Generation, transport, cache, retry, provider and model options are owned by
    the trusted sidecar config and cannot cross the optimizer socket.
    """

    if len(args) > 1 or (args and not isinstance(args[0], str)):
        raise ValueError("news_program_compile_proxy_request_arguments_invalid")
    unexpected = set(kwargs).difference(_ALLOWED_REQUEST_KWARGS)
    if unexpected:
        raise ValueError("news_program_compile_proxy_request_option_forbidden")
    if args and ("prompt" in kwargs or "messages" in kwargs):
        raise ValueError("news_program_compile_proxy_request_arguments_invalid")
    if "prompt" in kwargs and not isinstance(kwargs["prompt"], str):
        raise ValueError("news_program_compile_proxy_request_arguments_invalid")
    messages = kwargs.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ValueError("news_program_compile_proxy_request_arguments_invalid")


def _exact_socket_path(path: Path, *, require_existing: bool) -> Path:
    candidate = Path(path)
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_compile_proxy_socket_parent_invalid") from exc
    if candidate.is_symlink() or not parent.is_dir():
        raise ValueError("news_program_compile_proxy_socket_invalid")
    resolved = parent / candidate.name
    if require_existing and not resolved.exists():
        raise ValueError("news_program_compile_proxy_socket_invalid")
    if require_existing:
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ValueError("news_program_compile_proxy_socket_invalid") from exc
        if not stat.S_ISSOCK(mode):
            raise ValueError("news_program_compile_proxy_socket_invalid")
    return resolved


__all__ = [
    "PROXY_EXECUTION_SCHEMA",
    "PROXY_GRANT_SCHEMA",
    "PROXY_READY_SCHEMA",
    "PROXY_REQUEST_SCHEMA",
    "PROXY_RESPONSE_SCHEMA",
    "CompilerModelProxyGrant",
    "CompilerProviderEndpointSecret",
    "CompilerProxyError",
    "CompilerProxyLM",
    "CompilerProxyReadyReceipt",
    "CompilerProxyRequest",
    "CompilerProxyResponse",
    "CompilerProxySecretConfig",
    "CompilerProxyTariff",
    "TrustedCompilerModelProxy",
    "build_proxy_provider_lm",
]
