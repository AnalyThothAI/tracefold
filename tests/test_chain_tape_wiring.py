"""Wallet ingestion, detection and prices have independent supervised lifetimes (#614).

What is checked here is composition, not the tape's rules: the flag decides whether the capability is
`running` or `disabled`, the runner ticks on the stop event, an unexpected program error leaves the
runner rather than being swallowed, and whatever the provider adapters hold is released either way.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.app.workers.runtime import (
    CHAIN_TAPE,
    WALLET_NET_BUY,
    WALLET_ROSTER,
    CapabilityStates,
)
from tracefold.app.workers.task_contract import worker_business_tasks
from tracefold.app.workers.wiring.chain_tape import (
    CHAIN_TAPE_TASK_NAME,
    WALLET_NET_BUY_TASK_NAME,
    WALLET_ROSTER_TASK_NAME,
    ChainTapeComposition,
    _wire_chain_tape,
    run_chain_tape,
)
from tracefold.news.chain_tape import ChainTapeLoop
from tracefold.platform.config.models import Settings


@pytest.fixture()
def no_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the real adapters without whatever proxy the developer's shell exports.

    Composition constructs one long-lived `httpx.AsyncClient` per provider, and httpx resolves proxy
    environment variables at construction. A workstation exporting `ALL_PROXY=socks5h://...` would make
    this test about the developer's shell instead of about the wiring.
    """

    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)


def _settings(**chain_tape: Any) -> Settings:
    return Settings(
        news={"enabled": True, "chain_tape": chain_tape},
        storage={"postgres": {"dsn": "postgresql://tracefold@127.0.0.1:5432/x", "password_file": None}},
    )


class _Loop:
    """Only the two methods the runner calls."""

    def __init__(self, *, fail_on_turn: int | None = None) -> None:
        self.turns = 0
        self.closed = False
        self.fail_on_turn = fail_on_turn

    async def advance(self) -> dict[str, Any]:
        self.turns += 1
        if self.fail_on_turn is not None and self.turns >= self.fail_on_turn:
            raise RuntimeError("chain_tape_program_error")
        return {}

    async def aclose(self) -> None:
        self.closed = True


async def _close_composition(composed: ChainTapeComposition) -> None:
    stop = asyncio.Event()
    stop.set()
    for task in worker_business_tasks(news_pipeline=None, chain_tape=composed):
        await task.run(stop)


def test_the_flag_off_is_a_disabled_capability_and_no_task() -> None:
    """Default-off is the whole risk posture of PR-1: nothing calls a provider until asked."""

    capabilities = CapabilityStates()

    loop = _wire_chain_tape(settings=_settings(), db=object(), capabilities=capabilities)  # type: ignore[arg-type]

    assert loop is None
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "disabled", "reason": "news_chain_tape_disabled"}
    assert set(capabilities.payload()) == {CHAIN_TAPE, WALLET_ROSTER, WALLET_NET_BUY}


@pytest.mark.parametrize("notifications_enabled", [True, False])
def test_the_flag_on_builds_one_loop_and_reports_the_capability_running(
    no_proxy_environment: None, notifications_enabled: bool
) -> None:
    capabilities = CapabilityStates()

    composed = _wire_chain_tape(
        settings=_settings(
            enabled=True,
            notifications_enabled=notifications_enabled,
            roster={"refresh_interval_s": 600},
        ),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert isinstance(composed, ChainTapeComposition)
    loop = composed.loop
    assert isinstance(loop, ChainTapeLoop)
    assert composed.roster.refresh_period_ms == 600_000
    assert loop.chain.chain_id == 4663
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "running", "reason": None}
    assert capabilities.payload()[WALLET_NET_BUY]["state"] == "running"
    assert capabilities.payload()[WALLET_ROSTER]["state"] == "running"
    tasks = worker_business_tasks(news_pipeline=None, chain_tape=composed)
    assert [(task.name, task.capability, task.foundational) for task in tasks] == [
        (WALLET_ROSTER_TASK_NAME, WALLET_ROSTER, False),
        (CHAIN_TAPE_TASK_NAME, CHAIN_TAPE, False),
        (WALLET_NET_BUY_TASK_NAME, WALLET_NET_BUY, False),
    ]
    assert composed.detector.notifications_enabled is notifications_enabled
    assert not hasattr(composed.detector, "chain")
    assert not hasattr(composed.detector, "prices")
    asyncio.run(_close_composition(composed))


def test_the_operators_endpoints_and_list_rules_reach_the_loop(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()

    composed = _wire_chain_tape(
        settings=_settings(
            enabled=True,
            rpc_url="https://rpc.example/",
            roster_provider_url="https://roster.example/",
            poll_interval_s=7.5,
            roster={"window": "90d", "refresh_interval_s": 600},
            rules={"net_buy_slow_n": 6, "min_net_buy_usd": "1234.5", "trigger_max_age_s": 45},
        ),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert composed is not None
    loop = composed.loop
    assert loop.chain.rpc_url == "https://rpc.example"  # type: ignore[attr-defined]
    # The roster site belongs to the refresh task, and to nothing else: the collector has no provider
    # to point at it any more (#649 §5.1).
    assert not hasattr(loop, "roster_provider")
    roster = composed.roster
    assert roster.provider.base_url == "https://roster.example"  # type: ignore[attr-defined]
    # Both endpoints get the operator's statistics window, and the period is the operator's too.
    assert (roster.window, roster.refresh_period_ms) == ("90d", 600_000)
    # The operator's cadence is a runtime parameter, not a decoration on a config page: it has to
    # reach the thing that ticks the loop.
    assert composed.poll_seconds == 7.5
    assert str(composed.detector.rules.min_net_buy_usd) == "1234.5"
    assert composed.detector.rules.trigger_max_age_s == 45
    asyncio.run(_close_composition(composed))


@pytest.mark.parametrize(
    "old",
    [
        {"digest": {"enabled": False}},
        {"rules": {"buy_min_usd": 1000}},
        {"rules": {"exit_notifications_enabled": True}},
        {"rules": {"crowding_min_wallets": 3}},
    ],
)
def test_retired_config_is_rejected_without_runtime_conversion(old) -> None:
    with pytest.raises(ValueError):
        _settings(enabled=True, **old)


def test_the_configured_cadence_is_what_the_workers_task_actually_polls_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`news.chain_tape.poll_interval_s` used to be validated, printed and read by nothing."""

    ticks: list[float] = []

    class _Tape:
        def __init__(self) -> None:
            self.turns = 0

        async def advance(self) -> dict[str, Any]:
            self.turns += 1
            return {}

        async def aclose(self) -> None:
            return None

    async def _record(loop: Any, *, stop_event: asyncio.Event, poll_seconds: float) -> None:
        ticks.append(poll_seconds)
        del loop, stop_event

    tape = ChainTapeComposition(  # type: ignore[arg-type]
        loop=_Tape(), roster=_Loop(), detector=_Loop(), poll_seconds=11.0
    )
    tasks = worker_business_tasks(news_pipeline=None, chain_tape=tape)
    task = next(item for item in tasks if item.name == CHAIN_TAPE_TASK_NAME)

    monkeypatch.setattr("tracefold.app.workers.task_contract.run_chain_tape", _record)
    asyncio.run(task.run(asyncio.Event()))

    assert ticks == [11.0]


def test_the_runner_ticks_until_the_stop_event_and_then_releases_the_adapters() -> None:
    loop = _Loop()

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_chain_tape(loop, stop_event=stop, poll_seconds=0.01))  # type: ignore[arg-type]
        await asyncio.sleep(0.2)
        stop.set()
        await task

    asyncio.run(drive())

    assert loop.turns >= 2
    assert loop.closed is True


def test_stopping_a_wallet_task_cancels_and_joins_its_slow_inflight_turn() -> None:
    async def drive() -> None:
        stop = asyncio.Event()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class SlowPrice(_Loop):
            async def advance(self) -> dict[str, Any]:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                return {}

        price = SlowPrice()
        task = asyncio.create_task(run_chain_tape(price, stop_event=stop, poll_seconds=0.01))  # type: ignore[arg-type]
        await asyncio.wait_for(entered.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)
        assert cancelled.is_set()
        assert price.closed

    asyncio.run(drive())


def test_a_program_error_leaves_the_runner_so_the_root_can_fault_one_capability() -> None:
    """Every provider failure is already an outcome on the tape's own row; what is left is a bug."""

    loop = _Loop(fail_on_turn=2)

    async def drive() -> None:
        await run_chain_tape(loop, stop_event=asyncio.Event(), poll_seconds=0.01)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="chain_tape_program_error"):
        asyncio.run(drive())

    assert loop.turns == 2
    assert loop.closed is True


def test_the_task_name_is_the_one_the_workers_task_set_publishes() -> None:
    assert CHAIN_TAPE_TASK_NAME == "news-chain-tape"


@pytest.mark.parametrize(
    "chain_tape",
    [
        {"enabled": True, "rpc_url": "ftp://rpc.example"},
        {"enabled": True, "rpc_url": ""},
        {"poll_interval_s": 0.1},
        {"retention_days": 0},
        {"roster": {"top_quality": 0, "top_whale_by_open_cost": 0}},
        {"roster": {"min_profit_factor": -1}},
    ],
)
def test_a_configuration_that_cannot_run_is_refused_at_load(chain_tape: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _settings(**chain_tape)


def test_the_defaults_are_off_and_public() -> None:
    chain_tape = _settings().news.chain_tape

    assert chain_tape.enabled is False
    assert chain_tape.rpc_url == "https://rpc.mainnet.chain.robinhood.com"
    assert chain_tape.roster_provider_url == "https://rhtrenches.com"
    assert (chain_tape.poll_interval_s, chain_tape.retention_days) == (2.0, 90)
    assert chain_tape.roster.model_dump() == {
        # #649 §5.3: the provider statistics window both roster endpoints are asked for, and how old
        # a published list may be before the refresh task rebuilds it.
        "window": "30d",
        "refresh_interval_s": 3600,
    }
