"""How the wallet tape joins Workers: one capability, one task, one runner (#572 PR-1).

What is checked here is composition, not the tape's rules: the flag decides whether the capability is
`running` or `disabled`, the runner ticks on the stop event, an unexpected program error leaves the
runner rather than being swallowed, and whatever the provider adapters hold is released either way.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.app.workers.runtime import CHAIN_TAPE, CapabilityStates
from tracefold.app.workers.wiring.chain_tape import (
    CHAIN_TAPE_TASK_NAME,
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


def test_the_flag_off_is_a_disabled_capability_and_no_task() -> None:
    """Default-off is the whole risk posture of PR-1: nothing calls a provider until asked."""

    capabilities = CapabilityStates()

    loop = _wire_chain_tape(settings=_settings(), db=object(), capabilities=capabilities)  # type: ignore[arg-type]

    assert loop is None
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "disabled", "reason": "news_chain_tape_disabled"}


def test_the_flag_on_builds_one_loop_and_reports_the_capability_running(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()

    loop = _wire_chain_tape(
        settings=_settings(enabled=True, roster={"top_quality": 5, "top_whale_by_open_cost": 3}),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert isinstance(loop, ChainTapeLoop)
    assert (loop.rules.top_quality, loop.rules.top_whale_by_open_cost) == (5, 3)
    assert loop.chain.chain_id == 4663
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "running", "reason": None}
    asyncio.run(loop.aclose())


def test_the_operators_endpoints_and_list_rules_reach_the_loop(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()

    loop = _wire_chain_tape(
        settings=_settings(
            enabled=True,
            rpc_url="https://rpc.example/",
            roster_provider_url="https://roster.example/",
            roster={"min_closed_trades": 3, "min_profit_factor": 2.5},
        ),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert loop is not None
    assert loop.chain.rpc_url == "https://rpc.example"  # type: ignore[attr-defined]
    assert loop.roster_provider.base_url == "https://roster.example"  # type: ignore[attr-defined]
    assert (loop.rules.min_closed_trades, loop.rules.min_profit_factor) == (3, 2.5)
    asyncio.run(loop.aclose())


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
    assert chain_tape.roster_provider_url == "https://robinhoodtrenches.com"
    assert (chain_tape.poll_interval_s, chain_tape.retention_days) == (2.0, 90)
    assert chain_tape.roster.model_dump() == {
        "min_closed_trades": 10,
        "min_profit_factor": 1.2,
        "top_quality": 20,
        "top_whale_by_open_cost": 20,
    }
