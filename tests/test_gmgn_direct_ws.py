from __future__ import annotations

import asyncio

import pytest
from curl_cffi.curl import CurlError

from tracefold.integrations.gmgn import direct_ws
from tracefold.integrations.gmgn.direct_ws import DirectGmgnWebSocketClient


class _LogSink:
    def __init__(self) -> None:
        self.errors: list[tuple[object, ...]] = []
        self.warnings: list[tuple[object, ...]] = []

    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    def error(self, *args: object, **_kwargs: object) -> None:
        self.errors.append(args)

    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, *args: object, **_kwargs: object) -> None:
        self.warnings.append(args)


def test_connect_curl_error_is_retried_instead_of_stopping_client() -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def ws_connect(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts >= 2:
                retried.set()
            raise CurlError("TLS connect failed")

    async def on_frame(_frame: str) -> None:
        return None

    async def scenario() -> None:
        client = DirectGmgnWebSocketClient(
            app_version="test",
            channels=["public_broadcast"],
            chains=["sol"],
            on_frame=on_frame,
            reconnect_delay=0,
            session_factory=_Session,
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(retried.wait(), timeout=0.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert attempts >= 2

    attempts = 0
    retried = asyncio.Event()
    asyncio.run(scenario())


def test_repeated_connect_failures_back_off_cap_and_reduce_error_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def ws_connect(self, *_args, **_kwargs):
            raise CurlError("TLS connect failed")

    async def on_frame(_frame: str) -> None:
        return None

    async def scenario() -> None:
        client = DirectGmgnWebSocketClient(
            app_version="test",
            channels=["public_broadcast"],
            chains=["sol"],
            on_frame=on_frame,
            reconnect_delay=3,
            session_factory=_Session,
        )

        with pytest.raises(asyncio.CancelledError):
            await client.run()

        assert delays == [3, 6, 12, 24, 48, 60]
        assert len(log_sink.errors) == 1
        assert len(log_sink.warnings) == 2
        assert client.connection_state == "disconnected"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 6:
            raise asyncio.CancelledError

    delays: list[float] = []
    log_sink = _LogSink()
    monkeypatch.setattr(direct_ws.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(direct_ws, "logger", log_sink)
    asyncio.run(scenario())


def test_receiving_a_frame_resets_reconnect_backoff() -> None:
    class _WebSocket:
        async def recv_str(self) -> str:
            return "{}"

    async def scenario() -> None:
        client = DirectGmgnWebSocketClient(
            app_version="test",
            channels=["public_broadcast"],
            chains=["sol"],
            on_frame=on_frame,
        )
        client._consecutive_failures = 5
        client._last_failure_key = ("CurlError", "TLS connect failed")
        client._same_failure_count = 5

        with pytest.raises(RuntimeError, match="stop after first frame"):
            await client._receive_frames(_WebSocket())

        assert client._consecutive_failures == 0
        assert client._last_failure_key is None
        assert client._same_failure_count == 0
        assert client.connection_state == "streaming"

    async def on_frame(_frame: str) -> None:
        raise RuntimeError("stop after first frame")

    asyncio.run(scenario())


def test_connect_unknown_error_still_stops_client() -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def ws_connect(self, *_args, **_kwargs):
            raise RuntimeError("connection invariant failed")

    async def on_frame(_frame: str) -> None:
        return None

    async def scenario() -> None:
        client = DirectGmgnWebSocketClient(
            app_version="test",
            channels=["public_broadcast"],
            chains=["sol"],
            on_frame=on_frame,
            session_factory=_Session,
        )

        with pytest.raises(RuntimeError, match="connection invariant failed"):
            await asyncio.wait_for(client.run(), timeout=0.5)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "failure_type"),
    [
        (OSError("heartbeat transport failed"), OSError),
        (RuntimeError("heartbeat invariant failed"), RuntimeError),
    ],
    ids=("expected-transport", "unknown-invariant"),
)
def test_heartbeat_failure_propagates_and_cancels_receiver(
    failure: BaseException,
    failure_type: type[BaseException],
) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.send_calls = 0
            self.receive_started = asyncio.Event()
            self.receive_cancelled = asyncio.Event()
            self.close_calls = 0

        async def send_str(self, _payload: str) -> None:
            self.send_calls += 1
            if self.send_calls > 1:
                raise failure

        async def recv_str(self) -> str:
            self.receive_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.receive_cancelled.set()
                raise
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.close_calls += 1

    class _Session:
        def __init__(self, websocket: _WebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def ws_connect(self, *_args, **_kwargs) -> _WebSocket:
            return self.websocket

    async def on_frame(_frame: str) -> None:
        return None

    async def scenario() -> None:
        websocket = _WebSocket()
        client = DirectGmgnWebSocketClient(
            app_version="test",
            channels=["public_broadcast"],
            chains=["sol"],
            on_frame=on_frame,
            heartbeat_interval=0,
            session_factory=lambda: _Session(websocket),
        )

        with pytest.raises(failure_type, match=str(failure)):
            await asyncio.wait_for(client._run_once(), timeout=0.5)

        assert websocket.receive_started.is_set()
        assert websocket.receive_cancelled.is_set()
        assert websocket.close_calls == 1

    asyncio.run(scenario())
