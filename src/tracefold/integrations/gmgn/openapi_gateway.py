from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import TypeVar

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from tracefold.integrations.gmgn.openapi_client import (
    GmgnOpenApiClient,
    GmgnOpenApiProviderUnavailableError,
    GmgnOpenApiTransientError,
    GmgnTokenInfo,
    GmgnTokenInfoLookup,
)
from tracefold.platform.validation import (
    require_nonnegative_float,
    require_nonnegative_int,
    require_positive_float,
    require_positive_int,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class GmgnOpenApiRoute:
    name: str
    weight: float


TOKEN_INFO_ROUTE = GmgnOpenApiRoute(name="token_info", weight=1.0)
_TOKEN_INFO_CACHE_MAX_ENTRIES = 256


class GmgnOpenApiGateway:
    def __init__(
        self,
        client: GmgnOpenApiClient,
        *,
        token_info_cache_ttl_seconds: int = 60,
        rate_per_second: float = 20.0,
        rate_capacity: float = 20.0,
        provider_cooldown_seconds: float = 300.0,
        retry_attempts: int = 2,
        retry_initial_wait_seconds: float = 0.25,
        retry_max_wait_seconds: float = 2.0,
        retry_jitter_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._token_info_cache_ttl_seconds = require_nonnegative_int(
            token_info_cache_ttl_seconds,
            error_code="gmgn_openapi_token_info_cache_ttl_seconds_required",
        )
        self._bucket = _WeightedLeakyBucket(
            rate_per_second=require_positive_float(
                rate_per_second,
                error_code="gmgn_openapi_rate_per_second_required",
            ),
            capacity=require_positive_float(
                rate_capacity,
                error_code="gmgn_openapi_rate_capacity_required",
            ),
            clock=clock,
            sleep=sleep,
        )
        self._provider_cooldown_seconds = require_nonnegative_float(
            provider_cooldown_seconds,
            error_code="gmgn_openapi_provider_cooldown_seconds_required",
        )
        self._retry_attempts = require_positive_int(
            retry_attempts,
            error_code="gmgn_openapi_retry_attempts_required",
        )
        self._retry_initial_wait_seconds = require_nonnegative_float(
            retry_initial_wait_seconds,
            error_code="gmgn_openapi_retry_initial_wait_seconds_required",
        )
        self._retry_max_wait_seconds = require_nonnegative_float(
            retry_max_wait_seconds,
            error_code="gmgn_openapi_retry_max_wait_seconds_required",
        )
        self._retry_jitter_seconds = require_nonnegative_float(
            retry_jitter_seconds,
            error_code="gmgn_openapi_retry_jitter_seconds_required",
        )
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()
        self._circuit_open_until = 0.0
        self._token_info_cache: dict[tuple[str, str], tuple[float, GmgnTokenInfo | None]] = {}

    def lookup_token_info(self, *, chain: str, address: str) -> GmgnTokenInfoLookup:
        with self._lock:
            key = (str(chain), str(address))
            now = self._clock()
            self._prune_expired_token_info(now)
            cached = self._token_info_cache.get(key)
            if cached is not None:
                return GmgnTokenInfoLookup(info=cached[1], cache_status="hit")

            lookup = self._execute(
                TOKEN_INFO_ROUTE,
                lambda: self._client.lookup_token_info(chain=chain, address=address),
            )
            if self._token_info_cache_ttl_seconds > 0:
                if len(self._token_info_cache) >= _TOKEN_INFO_CACHE_MAX_ENTRIES:
                    self._token_info_cache.pop(next(iter(self._token_info_cache)))
                self._token_info_cache[key] = (
                    self._clock() + self._token_info_cache_ttl_seconds,
                    lookup.info,
                )
            return GmgnTokenInfoLookup(info=lookup.info, cache_status="miss")

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def _prune_expired_token_info(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._token_info_cache.items() if expires_at < now]
        for key in expired:
            del self._token_info_cache[key]

    def _execute(self, route: GmgnOpenApiRoute, operation: Callable[[], T]) -> T:
        self._raise_if_circuit_open()
        self._bucket.acquire(route.weight)
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self._retry_attempts),
                wait=wait_exponential_jitter(
                    initial=self._retry_initial_wait_seconds,
                    max=self._retry_max_wait_seconds,
                    jitter=self._retry_jitter_seconds,
                ),
                retry=retry_if_exception_type(GmgnOpenApiTransientError),
                sleep=self._sleep,
                reraise=True,
            ):
                with attempt:
                    return operation()
        except GmgnOpenApiProviderUnavailableError as exc:
            self._open_circuit(exc)
            raise
        raise RuntimeError(f"GMGN gateway route {route.name} exited without a result")

    def _raise_if_circuit_open(self) -> None:
        remaining = self._circuit_open_until - self._clock()
        if remaining > 0:
            raise GmgnOpenApiProviderUnavailableError(
                f"GMGN OpenAPI circuit open for {remaining:.1f}s",
                cooldown_seconds=remaining,
            )

    def _open_circuit(self, exc: GmgnOpenApiProviderUnavailableError) -> None:
        cooldown = exc.cooldown_seconds
        if cooldown is None:
            cooldown = self._provider_cooldown_seconds
        self._circuit_open_until = max(
            self._circuit_open_until,
            self._clock()
            + require_nonnegative_float(
                cooldown,
                error_code="gmgn_openapi_provider_cooldown_seconds_required",
            ),
        )


class _WeightedLeakyBucket:
    def __init__(
        self,
        *,
        rate_per_second: float,
        capacity: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._rate_per_second = rate_per_second
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._updated_at = clock()

    def acquire(self, weight: float) -> None:
        requested = require_positive_float(weight, error_code="gmgn_openapi_route_weight_required")
        self._refill()
        if self._tokens < requested:
            wait_seconds = (requested - self._tokens) / self._rate_per_second
            self._sleep(wait_seconds)
            self._refill()
        self._tokens = max(0.0, self._tokens - requested)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        self._updated_at = now


__all__ = ["GmgnOpenApiGateway", "GmgnOpenApiRoute"]
