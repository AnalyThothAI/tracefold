"""Wallet ingestion, research and digests have independent supervised lifetimes (#614).

What is checked here is composition, not the tape's rules: the flag decides whether the capability is
`running` or `disabled`, the runner ticks on the stop event, an unexpected program error leaves the
runner rather than being swallowed, and whatever the provider adapters hold is released either way.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.workers.runtime import CHAIN_TAPE, WALLET_DIGEST, WALLET_RESEARCH, CapabilityStates
from tracefold.app.workers.task_contract import worker_business_tasks
from tracefold.app.workers.wiring.chain_tape import (
    CHAIN_TAPE_TASK_NAME,
    WALLET_DIGEST_TASK_NAME,
    WALLET_RESEARCH_TASK_NAME,
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
    for task in worker_business_tasks(news_pipeline=None, signal_lane=None, chain_tape=composed):
        await task.run(stop)


def test_the_flag_off_is_a_disabled_capability_and_no_task() -> None:
    """Default-off is the whole risk posture of PR-1: nothing calls a provider until asked."""

    capabilities = CapabilityStates()

    loop = _wire_chain_tape(settings=_settings(), db=object(), capabilities=capabilities)  # type: ignore[arg-type]

    assert loop is None
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "disabled", "reason": "news_chain_tape_disabled"}
    assert set(capabilities.payload()) == {CHAIN_TAPE, WALLET_RESEARCH, WALLET_DIGEST}


def test_the_flag_on_builds_one_loop_and_reports_the_capability_running(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()

    composed = _wire_chain_tape(
        settings=_settings(enabled=True, roster={"top_quality": 5, "top_whale_by_open_cost": 3}),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert isinstance(composed, ChainTapeComposition)
    loop = composed.loop
    assert isinstance(loop, ChainTapeLoop)
    assert (loop.rules.top_quality, loop.rules.top_whale_by_open_cost) == (5, 3)
    assert loop.chain.chain_id == 4663
    assert capabilities.payload()[CHAIN_TAPE] == {"state": "running", "reason": None}
    assert capabilities.payload()[WALLET_RESEARCH]["state"] == "running"
    assert capabilities.payload()[WALLET_DIGEST]["state"] == "running"
    tasks = worker_business_tasks(news_pipeline=None, signal_lane=None, chain_tape=composed)
    assert [(task.name, task.capability, task.foundational) for task in tasks] == [
        (CHAIN_TAPE_TASK_NAME, CHAIN_TAPE, False),
        (WALLET_RESEARCH_TASK_NAME, WALLET_RESEARCH, False),
        (WALLET_DIGEST_TASK_NAME, WALLET_DIGEST, False),
    ]
    assert composed.research.chain is not loop.chain
    assert composed.research.site is not loop.roster_provider
    assert composed.digest is not None
    assert composed.digest.bags is not composed.research.site
    asyncio.run(_close_composition(composed))


def test_the_operators_endpoints_and_list_rules_reach_the_loop(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()

    composed = _wire_chain_tape(
        settings=_settings(
            enabled=True,
            rpc_url="https://rpc.example/",
            roster_provider_url="https://roster.example/",
            poll_interval_s=7.5,
            roster={"min_closed_trades": 3, "min_profit_factor": 2.5},
            rules={"exit_notifications_enabled": True, "buy_min_usd": 1234.5, "buy_window_s": 600},
        ),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert composed is not None
    loop = composed.loop
    assert loop.chain.rpc_url == "https://rpc.example"  # type: ignore[attr-defined]
    assert loop.roster_provider.base_url == "https://roster.example"  # type: ignore[attr-defined]
    assert (loop.rules.min_closed_trades, loop.rules.min_profit_factor) == (3, 2.5)
    # The operator's cadence is a runtime parameter, not a decoration on a config page: it has to
    # reach the thing that ticks the loop.
    assert composed.poll_seconds == 7.5
    assert composed.research.rules.exit_notifications_enabled is True
    assert str(composed.research.rules.buy_min_usd) == "1234.5"
    assert composed.research.rules.buy_window_s == 600
    asyncio.run(_close_composition(composed))


def test_disabling_digest_keeps_ingestion_and_buy_research(no_proxy_environment: None) -> None:
    capabilities = CapabilityStates()
    composed = _wire_chain_tape(
        settings=_settings(enabled=True, digest={"enabled": False}),
        db=object(),  # type: ignore[arg-type]
        capabilities=capabilities,
    )

    assert composed is not None
    assert composed.digest is None
    assert capabilities.payload()[WALLET_DIGEST] == {"state": "disabled", "reason": "news_wallet_digest_disabled"}
    assert len(worker_business_tasks(news_pipeline=None, signal_lane=None, chain_tape=composed)) == 2
    asyncio.run(_close_composition(composed))


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

    tape = ChainTapeComposition(loop=_Tape(), research=_Loop(), digest=None, poll_seconds=11.0)  # type: ignore[arg-type]
    tasks = worker_business_tasks(news_pipeline=None, signal_lane=None, chain_tape=tape)
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

        class SlowDigest(_Loop):
            async def advance(self) -> dict[str, Any]:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                return {}

        digest = SlowDigest()
        task = asyncio.create_task(run_chain_tape(digest, stop_event=stop, poll_seconds=0.01))  # type: ignore[arg-type]
        await asyncio.wait_for(entered.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=0.2)
        assert cancelled.is_set()
        assert digest.closed

    asyncio.run(drive())


def test_a_slow_digest_does_not_stop_the_production_ingestion_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real composition, task declarations, poll runner and ingestion loop."""

    from tracefold.news.chain_tape.contracts import RosterMember, RosterSnapshot

    async def drive() -> None:
        digest_entered = asyncio.Event()
        ingestion_continued = asyncio.Event()
        stop = asyncio.Event()
        roster = RosterSnapshot(
            roster_version=1,
            taken_at_ms=10**15,
            members=(RosterMember("0x" + "1" * 40, "one", 0, 0.0, 10, 0.0, 2.0, 1000.0, 1, None),),
        )

        class Repository:
            def chain_tape_state(self) -> None:
                return None

            def chain_tape_current_roster(self) -> Any:
                return roster

            def chain_tape_record_fills(self, fills: Any) -> int:
                return len(fills)

            def chain_tape_save_state(self, **kwargs: Any) -> None:
                pass

        class Db:
            async def read(self, name: str, fn: Any, **kwargs: Any) -> Any:
                return fn(SimpleNamespace(news=Repository()))

            async def tx(self, name: str, fn: Any, **kwargs: Any) -> Any:
                return await self.read(name, fn, **kwargs)

        class Chain:
            chain_id = 4663
            last_response_bytes = 0

            def __init__(self, **kwargs: Any) -> None:
                self.closed = False

            async def block_number(self) -> int:
                if digest_entered.is_set():
                    ingestion_continued.set()
                return 100

            async def logs(self, **kwargs: Any) -> tuple[Any, ...]:
                return ()

            async def aclose(self) -> None:
                self.closed = True

        class Deriver(_Loop):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__()

            async def derive(self, *args: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(checks=0, exits=0, crowding=0)

            async def take_outcomes(self, *args: Any) -> Any:
                return SimpleNamespace(outcomes=0, unavailable=0)

        class Digest(_Loop):
            async def advance(self) -> Any:
                digest_entered.set()
                await asyncio.Event().wait()

            async def take_digest(self, **kwargs: Any) -> Any:
                return await self.advance()

        digest = Digest()
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape.WorkerChainTapeDatabase", lambda _: Db())
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape.RobinhoodChainClient", Chain)
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape.RobinhoodTrenchesClient", Chain)
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape.DexScreenerClient", Chain)
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape.WalletCardDeriver", Deriver)
        monkeypatch.setattr("tracefold.app.workers.wiring.chain_tape._wire_digest", lambda *args, **kwargs: digest)
        composed = _wire_chain_tape(
            settings=_settings(enabled=True, poll_interval_s=0.5),
            db=object(),  # type: ignore[arg-type]
            capabilities=CapabilityStates(),
        )
        assert composed is not None
        declarations = worker_business_tasks(news_pipeline=None, signal_lane=None, chain_tape=composed)
        tasks = [asyncio.create_task(item.run(stop)) for item in declarations]
        try:
            await asyncio.wait_for(digest_entered.wait(), timeout=1)
            await asyncio.wait_for(ingestion_continued.wait(), timeout=1)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert digest.closed

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
        "min_closed_trades": 10,
        "min_profit_factor": 1.2,
        "top_quality": 20,
        "top_whale_by_open_cost": 20,
    }
