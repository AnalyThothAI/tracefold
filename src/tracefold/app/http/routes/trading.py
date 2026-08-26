from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.trading_config import (
    CANDIDATE_GATE_VERSION,
    trading_settings_gate,
    trading_settings_strategies,
)
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.platform.config.secret_file import secret_file_configured

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_OrdersEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingOrdersData]
_EventCaseEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingEventCaseData]
_GateEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateData]

_WINDOW_MS: Final = 24 * 3_600_000
_ORDER_LIMIT: Final = 100
# The lane persists about 110 OI facts a day, so one page of this covers the window the frame table
# shows with room to spare; past that the response says so rather than looking like a quiet day.
_GATE_LIMIT: Final = 400
# The half of `OiTradeCandidate.source_key` that is not the Event id. Imported from News rather than
# retyped: it is the News lane's own measurement version, and a literal here would silently stop matching
# the day `oi_signals` bumps it.
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")
_DAY_KEY: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The ledger's own predicate for "holds, or may yet turn out to hold, exposure" (`20260823_0300`). The page
# calls this 当前暴露, and the states it deliberately includes are the dangerous ones: an order whose provider
# write is ambiguous is *more* likely to be carrying a position than one that is merely open.
_ACTIVE_STATES: Final[tuple[str, ...]] = (
    "PREPARED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "SUBMITTING",
    "AMBIGUOUS",
    "RECONCILING",
    "MANUAL_REVIEW_REQUIRED",
    "ACKNOWLEDGED",
    "PARTIAL",
    "OPEN",
    "UNPROTECTED",
    "SAFETY_CLOSING",
)
_STATE_FILTERS: Final[frozenset[str]] = frozenset({"active", "closed", "all"})


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    """The capital lane's mandate, readiness and 24 h funnel — the same facts as CLI `trading status`.

    Read-only and configuration-derived. `live_ready` is reported and never offered: a serve process cannot
    observe the Workers process's startup and canary result, so it says `not_proven` rather than guessing,
    and there is no field here a page could render as a switch (#185 P1-2).
    """

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    settings = runtime.settings
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        state = repos.trading.runtime_state() or {}
        counts = repos.trading.status_counts(
            since_ms=now_ms - _WINDOW_MS,
            now_ms=now_ms,
            day_key=state.get("day_key"),
        )
        admission = repos.trading.candidate_admission_report(now_ms=now_ms)
    order = settings.trading.order
    return _etagged(
        {
            "budget": {
                "notional_usd": str(order.fixed_notional_usd),
                "stop_loss_bps": order.fixed_stop_bps,
                "max_hold_ms": order.max_holding_seconds * 1000,
                "nominal_daily_stop_loss_usd": str(order.nominal_daily_stop_loss_usd),
                "max_orders_per_day": order.max_orders_per_day,
                "orders_today": int(state.get("orders_today") or 0),
            },
            "readiness": {
                "control": str(state.get("control") or "RUNNING"),
                "enabled": settings.trading.enabled,
                "mode": settings.trading.mode,
                "venues": list(settings.trading.venues.enabled),
                **_execution_capability(settings),
            },
            "floors": {
                "lookback_ms": settings.trading.regime.lookback_seconds * 1000,
                "max_price_move_bps": settings.trading.regime.max_price_move_bps,
                "min_oi_value_usd": str(settings.trading.candidates.min_oi_value_usd),
                "min_price_move_bps": settings.trading.regime.min_price_move_bps,
                "min_whale_long_profit_bps": settings.trading.policy.min_whale_long_profit_bps,
            },
            # #269. `floors` above is the operator's settings document, and after #264/#265 it is no
            # longer the set of numbers that decides an OI frame: admission is the Candidate Gate's,
            # and the Alpha thresholds belong to whichever versioned strategy answers the Case. A
            # console comparing a frame against `min_whale_long_profit_bps` was measuring it with the
            # 95% floor of a strategy that did not decide it. These two are the rules as the lane
            # actually holds them, digest included, so a page can name the threshold it is showing.
            "gate": _gate_config(settings),
            "strategies": _strategy_configs(settings),
            # `merge_funnel` resets the document on `day_key`, so this is the current UTC *calendar day*,
            # not a rolling window — publishing it as `funnel_24h` beside genuinely rolling counts made two
            # different intervals look like one, and a Workers process stopped over midnight would leave
            # yesterday's totals sitting under a 24 h label. The counter is worth keeping; the name was the
            # lie. `funnel_day_key` rides beside it so a stale document is visible rather than inferred.
            "counts": {
                **counts,
                # #264. The rest of this document is keyed on a case or an order existing, and
                # `funnel_today` is overwritten at UTC midnight — so a lane at zero orders could report
                # nothing at all about why. This half reads the admission ledger and outlives both.
                **admission,
                "funnel_today": _int_map(state.get("funnel")),
            },
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_StatusEnvelope,
    )


@router.get("/trading/orders", response_model=_OrdersEnvelope)
def get_trading_orders(
    request: Request,
    day: Annotated[str, Query(max_length=10)] = "",
    underlying: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Orders with the case that authored them, plus the cases that never got that far.

    Both halves are needed to describe the lane honestly. A `POLICY_REJECTED` case is where the capital
    floors actually bite and it has no order to join through, so listing orders alone would make the whole
    rejected population invisible — the funnel would count them and nothing would be able to name them.

    `underlying` accepts either the base symbol (`WIF`) or the full key (`crypto:WIF`); the response carries
    both so a caller never has to build the key itself.
    """

    _validate_query_params(request, supported={"day", "state", "token", "underlying"})
    if state and state not in _STATE_FILTERS:
        raise ApiBadRequest("trading_orders_state_invalid", field="state")
    underlying_key = _underlying_key(underlying)
    closed_from_ms: int | None = None
    closed_until_ms: int | None = None
    if day:
        try:
            if _DAY_KEY.fullmatch(day) is None:
                raise ValueError
            closed_from_ms = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)
        except ValueError as exc:
            raise ApiBadRequest("trading_orders_day_invalid", field="day") from exc
        closed_until_ms = closed_from_ms + 86_400_000
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - _WINDOW_MS
    states: tuple[str, ...] = ()
    if state == "active":
        states = _ACTIVE_STATES
    elif state == "closed":
        states = ("CLOSED",)
    with runtime.repositories() as repos:
        orders = repos.trading.console_orders(
            since_ms=since_ms,
            closed_from_ms=closed_from_ms,
            closed_until_ms=closed_until_ms,
            underlying_key=underlying_key,
            states=states,
            limit=_ORDER_LIMIT + 1,
        )
        # An explicit order-state filter is a question about orders; listing cases that authored none beside
        # it would answer a question the caller did not ask. Keyed on whether `state` was *supplied*, not on
        # whether it translated to a non-empty tuple: `state=all` is an explicit filter that narrows to
        # nothing, and testing the tuple let it fall through to the case list.
        cases = (
            []
            if state
            else repos.trading.console_cases_without_orders(
                since_ms=since_ms, underlying_key=underlying_key, limit=_ORDER_LIMIT + 1
            )
        )
    complete = len(orders) <= _ORDER_LIMIT and len(cases) <= _ORDER_LIMIT
    return _etagged(
        {
            "orders": [_order(row) for row in orders[:_ORDER_LIMIT]],
            "cases_without_orders": [_case(row) for row in cases[:_ORDER_LIMIT]],
            "complete": complete,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_OrdersEnvelope,
    )


@router.get("/trading/gate", response_model=_GateEnvelope)
def get_trading_gate(request: Request) -> Response:
    """Every OI source the lane admitted or refused in the window, one answer each (#269).

    `/trading/events/{id}` answers this for one Event, which is the Event detail's question. A frame
    *table* asks it for a page of frames at once, and asking it one row at a time would be a hundred
    round trips to render a screen — so the console read this column as "未成案" for every row and the
    durable reason the ledger holds never reached anyone.

    Keyed on `event_id` for the deterministic lane, recovered from the source key the same way the
    order and case projections recover theirs. A source whose key does not round-trip is still listed,
    with `event_id: null`: the counts above the table include it, and dropping it here would make the
    page's own total disagree with them.
    """

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.gate_decisions_since(since_ms=now_ms - _WINDOW_MS, limit=_GATE_LIMIT + 1)
    return _etagged(
        {
            "decisions": [_gate_decision(row) for row in rows[:_GATE_LIMIT]],
            "complete": len(rows) <= _GATE_LIMIT,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_GateEnvelope,
    )


@router.get("/trading/events/{event_id}", response_model=_EventCaseEnvelope)
def get_trading_event_case(request: Request, event_id: str) -> Response:
    """Whether one News Event became a case — the Event detail's 成案 badge (#207 PR-W4).

    Answers `joinable: false` for a model-lane Event rather than pretending the lane declined it. The
    deterministic OI lane's source key is `oi:{event_id}:{metric_version}` and can be rebuilt here; the model
    lane's is a content hash of an artifact and a fingerprint (#154), which no Event id reconstructs. Joining
    by symbol and time instead would be the console recording a link the ledger does not have.
    """

    _validate_query_params(request, supported={"lane", "token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("trading_event_id_invalid", field="event_id")
    lane = request.query_params.get("lane", "")
    if lane and lane != "oi":
        raise ApiBadRequest("trading_event_lane_invalid", field="lane")
    runtime = _authenticated_runtime(request)
    if lane != "oi":
        return _etagged({"event_id": event_id, "joinable": False}, request, envelope=_EventCaseEnvelope)
    source_key = f"oi:{event_id}:{_OI_METRIC_VERSION}"
    with runtime.repositories() as repos:
        row = repos.trading.console_case_for_source_key(primary_source_key=source_key)
        gate = repos.trading.gate_decision_for_source_key(source_key=source_key)
    if row is None:
        # "No case" used to be the whole answer, which is the same shape for "the lane never saw it",
        # "it was below the liquidity floor" and "there is no perp at the venue whose OI moved" (#264).
        return _etagged(
            {"event_id": event_id, "joinable": True, **_gate(gate)},
            request,
            envelope=_EventCaseEnvelope,
        )
    return _etagged(
        {
            "case": _case(row),
            **_gate(gate),
            "entry_reference": _decimal(row.get("entry_reference")),
            "event_id": event_id,
            "exit_price": _decimal(row.get("exit_price")),
            "exit_reason": row.get("exit_reason"),
            "joinable": True,
            "notional_usd": _decimal(row.get("notional_usd")),
            "order_id": row.get("order_id"),
            "order_state": row.get("order_state"),
            "order_state_reason": row.get("order_state_reason"),
            "position_closed_at_ms": row.get("position_closed_at_ms"),
            "position_opened_at_ms": row.get("position_opened_at_ms"),
            "realized_bps": row.get("realized_bps"),
            "side": row.get("side"),
            "stop_price": _decimal(row.get("stop_price")),
        },
        request,
        envelope=_EventCaseEnvelope,
    )


def _gate_config(settings: Any) -> dict[str, Any]:
    """The Candidate Gate exactly as the scanner builds it, digest and all (#269).

    Assembled through the one shared helper, so the digest published here is the digest the ledger's
    rows are filed under. Reporting the settings fields directly would let the two drift the moment
    the gate reads one of them differently.
    """

    config = trading_settings_gate(settings)
    return {"version": CANDIDATE_GATE_VERSION, "config_digest": config.digest, **config.snapshot}


def _strategy_configs(settings: Any) -> list[dict[str, Any]]:
    """Every code-owned strategy with the numbers it executes, in the lane's own configuration.

    The whole set rather than the OI one: a Case names the strategy that decided it, and a console
    row has to compare against *that* strategy's thresholds. Publishing one of them would put the
    same wrong-floor comparison back, one strategy later.

    Every value is rendered as text. A config snapshot mixes booleans, basis points and millisecond
    windows under keys each strategy owns, and a schema that promised `int` would break the day a
    strategy adds a decimal threshold — the console prints these beside their own labels anyway.
    """

    return [
        {
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.strategy_version,
            "config_digest": strategy.config_digest,
            "permission": strategy.permission,
            "trigger_kinds": sorted(strategy.trigger_kinds),
            "config": {key: str(value) for key, value in sorted(strategy.config_snapshot.items())},
        }
        for strategy in trading_settings_strategies(settings)
    ]


def _gate_decision(row: dict[str, Any]) -> dict[str, Any]:
    """One durable admission answer, in the table's own vocabulary.

    `underlying_key` rather than a venue: this lane publishes no market for a frame, and the gate's own
    `evidence.venue` is the venue whose open interest moved, which is a different claim.
    """

    return {
        "source_key": str(row["source_key"]),
        "event_id": _oi_event_id(row.get("source_key")),
        "underlying_key": row.get("underlying_key"),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "trigger_kind": str(row["trigger_kind"]),
        "source_observed_at_ms": int(row["source_observed_at_ms"]),
        "case_id": row.get("case_id"),
        **_gate(row),
    }


def _gate(row: dict[str, Any] | None) -> dict[str, Any]:
    """The admission decision, or an explicit absence.

    An absent row is its own answer and is reported as one: the lane has not evaluated this source
    under any gate version, which after a deploy of a new `gate_version` is the honest state rather
    than a refusal the console would otherwise have to invent.
    """

    if row is None:
        return {"gate_status": None}
    return {
        "gate_status": str(row["status"]),
        "gate_stage": str(row["stage"]),
        "gate_reason": str(row["reason"]),
        "gate_retryable": bool(row["retryable"]),
        "gate_version": str(row["gate_version"]),
        "gate_config_digest": str(row["gate_config_digest"]).strip(),
        "gate_evidence": row.get("evidence") or {},
        "gate_first_evaluated_at_ms": int(row["first_evaluated_at_ms"]),
        "gate_last_evaluated_at_ms": int(row["last_evaluated_at_ms"]),
        "gate_attempt_count": int(row["attempt_count"]),
    }


def _underlying_key(value: str) -> str | None:
    """`WIF` or `crypto:WIF` -> `crypto:WIF`.

    The construction is duplicated rather than imported: `tracefold.trading.contracts.underlying_key` owns
    the identity, and `tracefold.app` importing it here would put a Trading import in the HTTP layer for one
    string concatenation. If the prefix ever stops being `crypto:`, the boundary test that pins the contract
    catches this.
    """

    raw = str(value or "").strip().upper().removeprefix("XYZ-")
    if not raw:
        return None
    base = raw.removeprefix("CRYPTO:")
    if not _BASE_SYMBOL.fullmatch(base):
        raise ApiBadRequest("trading_orders_underlying_invalid", field="underlying")
    return f"crypto:{base}"


def _base_symbol(underlying_key: object) -> str:
    return str(underlying_key or "").split(":", 1)[-1]


def _decimal(value: object) -> str | None:
    return None if value is None else str(value)


def _oi_event_id(primary_source_key: object) -> str | None:
    """Recover the Event identity only from the deterministic OI source contract.

    Model-lane source keys are hashes and stay unjoinable. A partial prefix/suffix match is not enough:
    round-tripping the exact key prevents a future source-key variant from being exposed as an Event link.
    """

    raw = str(primary_source_key or "")
    prefix = "oi:"
    suffix = f":{_OI_METRIC_VERSION}"
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        return None
    event_id = raw[len(prefix) : -len(suffix)]
    return event_id if event_id and raw == f"oi:{event_id}:{_OI_METRIC_VERSION}" else None


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, count in value.items():
        try:
            out[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "average_price": _decimal(row.get("average_price")),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "trigger_kind": str(row["trigger_kind"]),
        "case_observed_at_ms": row.get("case_observed_at_ms"),
        "case_state": str(row["case_state"]),
        "created_at_ms": int(row["created_at_ms"]),
        "entry_reference": str(row["entry_reference"]),
        "exchange_id": str(row["exchange_id"]),
        "exit_attempt_total": int(row.get("exit_attempt_total") or 0),
        "exit_price": _decimal(row.get("exit_price")),
        "exit_reason": row.get("exit_reason"),
        "filled_quantity": _decimal(row.get("filled_quantity")),
        "mode": str(row["mode"]),
        "must_close_at_ms": row.get("must_close_at_ms"),
        "notional_usd": str(row["notional_usd"]),
        "order_id": str(row["order_id"]),
        "policy_decision": row.get("policy_decision"),
        "policy_reason": row.get("policy_reason"),
        "position_closed_at_ms": row.get("position_closed_at_ms"),
        "position_opened_at_ms": row.get("position_opened_at_ms"),
        "provider_attempt_count": int(row.get("provider_attempt_count") or 0),
        "provider_symbol": str(row["provider_symbol"]),
        "quantity": str(row["quantity"]),
        "realized_bps": row.get("realized_bps"),
        "regime": row.get("regime"),
        "side": str(row["side"]),
        "state": str(row["state"]),
        "state_reason": row.get("state_reason"),
        "stop_price": str(row["stop_price"]),
        "take_profit_price": _decimal(row.get("take_profit_price")),
        "underlying_key": str(row["underlying_key"]),
        "updated_at_ms": int(row["updated_at_ms"]),
    }


def _case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "trigger_kind": str(row["trigger_kind"]),
        "created_at_ms": int(row["created_at_ms"]),
        "decided_at_ms": row.get("decided_at_ms"),
        "mode": str(row["mode"]),
        "observed_at_ms": int(row["observed_at_ms"]),
        "policy_decision": row.get("policy_decision"),
        "policy_reason": row.get("policy_reason"),
        "regime": row.get("regime"),
        "state": str(row["state"]),
        "underlying_key": str(row["underlying_key"]),
    }


def _execution_capability(settings: Any) -> dict[str, Any]:
    """The same four answers the CLI computes, for the same reason it computes them there.

    Not imported from the CLI: that module loads settings, opens a repository session and returns exit
    codes. Duplicating six lines of branching is cheaper than an HTTP route reaching into a CLI command, and
    `test_trading_status_matches_the_cli` pins the two together.
    """

    trading = settings.trading
    if not trading.enabled:
        return {
            "execution_backend": "disabled",
            "execution_configured": False,
            "live_mode_supported": False,
            "live_ready": False,
            "live_readiness": "not_applicable",
        }
    if trading.mode == "paper":
        return {
            "execution_backend": "paper",
            "execution_configured": True,
            "live_mode_supported": False,
            "live_ready": False,
            "live_readiness": "not_applicable",
        }
    token_file = settings.trading_opentrade_token_file()
    return {
        "execution_backend": "opentrade_reviewed",
        "execution_configured": bool(trading.opentrade.base_url and secret_file_configured(token_file)),
        "live_mode_supported": True,
        # A read-only process cannot infer a separate Workers process's startup and canary result.
        "live_ready": False,
        "live_readiness": "not_proven",
    }


__all__ = ["router"]
