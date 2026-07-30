"""Golden-path end-to-end test.

Asserts the runtime signals from spec §6.4 across a real cross-process boundary:

1. /readyz returns 200 + Postgres probe ok (app is ready).
2. e2e_writer injects 1 mention -> writer exits 0 + DB row appears (critical
   path + side effect; confirmed by querying the events table from a separate
   psycopg connection).
3. /api/recent returns the injected mention (cross-process read; the API runs
   in one process, the writer ran in another).
4. WebSocket /ws receives a push within 5s of a follow-up writer (async
   propagation through the persisted live journal read by the single Serve
   broadcaster).
5. Resource cleanup is implicit (testcontainers ryuk + subprocess.terminate
   in conftest; no orphan containers/processes).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable

import httpx
import psycopg
import pytest
import websockets


@pytest.mark.e2e
def test_golden_path_readyz(e2e_uvicorn: str) -> None:
    """Spec §6.4 step 1: /readyz returns 200 with PG probe ok."""
    r = httpx.get(f"{e2e_uvicorn}/readyz", timeout=5.0)
    assert r.status_code == 200, f"readyz body: {r.text}"
    payload = r.json()
    db = payload.get("db") or {}
    assert db.get("ok") is True, f"PG probe not ok in /readyz payload: {payload}"


@pytest.mark.e2e
def test_golden_path_writer_persists_to_db(
    e2e_postgres: str,
    e2e_writer: Callable[[str, str], None],
) -> None:
    """Spec §6.4 step 2: writer subprocess writes a row visible from a separate connection."""
    event_id = f"e2e-{uuid.uuid4().hex[:12]}"
    text = "$E2E test mention for golden path"
    e2e_writer(event_id, text)

    with psycopg.connect(e2e_postgres) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE event_id = %s", (event_id,))
        row = cur.fetchone() or (0,)
        count = row[0]
        assert count >= 1, f"expected >=1 events row for {event_id}, got {count}"


@pytest.mark.e2e
def test_golden_path_recent_returns_writer_event(
    e2e_uvicorn: str,
    e2e_writer: Callable[[str, str], None],
    e2e_ws_token: str,
) -> None:
    """Spec §6.4 step 3: /api/recent (cross-process read) sees the injected event."""
    event_id = f"e2e-{uuid.uuid4().hex[:12]}"
    e2e_writer(event_id, f"$RECENT {event_id}")

    r = httpx.get(
        f"{e2e_uvicorn}/api/recent",
        params={"limit": 50, "token": e2e_ws_token},
        timeout=5.0,
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    data = payload.get("data") or {}
    items = data.get("items") or data.get("events") or []
    matched = [item for item in items if event_id in json.dumps(item, default=str)]
    assert matched, f"recent endpoint did not return event_id={event_id}; payload keys={list(data.keys())}"


@pytest.mark.e2e
def test_golden_path_websocket_pushes_after_writer(
    e2e_uvicorn: str,
    e2e_writer: Callable[[str, str], None],
    e2e_ws_token: str,
) -> None:
    """Spec §6.4 step 4: WS /ws delivers the writer's event within 5s.

    Cross-process channel: writer subprocess atomically inserts the material
    event and its persisted live journal row; the API server's one broadcaster
    serves a WS subscription with replay=N by reading that journal.

    The replay uses an explicit symbol filter and returns matching events in
    chronological order (oldest first), so we drain the subscription and assert
    the new event id is somewhere in the delivered batch. This is the right
    contract for "the WS surface saw the write that another process performed"
    without treating an empty event filter as broadcast-all permission.
    """
    event_id = f"e2e-{uuid.uuid4().hex[:12]}"
    # Insert BEFORE connecting so replay finds it deterministically.
    e2e_writer(event_id, f"$WS push for {event_id}")
    ws_url = e2e_uvicorn.replace("http://", "ws://") + "/ws"

    async def _run() -> None:
        seen_ids: list[str] = []
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": e2e_ws_token}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert ready.get("type") == "ready", f"unexpected handshake reply: {ready}"

            await ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "symbols": ["WS"],
                        "replay": 100,
                    }
                )
            )
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                seen = msg.get("event", {}).get("event_id")
                if seen:
                    seen_ids.append(seen)
                if seen == event_id:
                    return

        pytest.fail(
            f"WS did not deliver event_id={event_id} within 5s; "
            f"received {len(seen_ids)} events, last few={seen_ids[-5:]}"
        )

    asyncio.run(_run())
