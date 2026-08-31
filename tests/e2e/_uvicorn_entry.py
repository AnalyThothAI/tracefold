"""Entrypoint for the e2e uvicorn subprocess.

Run as:
  python -m tests.e2e._uvicorn_entry --port 0

Reads TRACEFOLD_POSTGRES_DSN and TRACEFOLD_E2E_WS_TOKEN from env. Starts the FastAPI app
using a
hand-built Settings object that points at the test Postgres (no YAML config
file is required). Prints the bound port to stdout once ready in the form
`READY port=12345` so the parent test process can parse it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    dsn = os.environ.get("TRACEFOLD_POSTGRES_DSN")
    if not dsn:
        print("FATAL: TRACEFOLD_POSTGRES_DSN not set", file=sys.stderr)
        return 1
    ws_token = os.environ.get("TRACEFOLD_E2E_WS_TOKEN", "e2e-token")

    # Import after env validation to keep error pretty.
    from tracefold.app.http.app import create_app
    from tracefold.platform.config.models import Settings

    settings = Settings(
        ws_token=ws_token,
        storage={"postgres": {"dsn": dsn, "password_file": None}},
    )
    app_home = os.environ.get("TRACEFOLD_E2E_APP_HOME")
    if not app_home:
        print("FATAL: TRACEFOLD_E2E_APP_HOME not set", file=sys.stderr)
        return 1
    settings.set_config_dir(Path(app_home))

    app = create_app(
        settings=settings,
        frontend_dist=os.environ.get("TRACEFOLD_FRONTEND_DIST"),
    )

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    # uvicorn binds the listening socket inside `Server.startup()` and stores
    # the resulting `asyncio.Server` objects on `self.servers`. We wrap that
    # method so we can print the actual bound port after binding (port=0 in
    # the parent test conftest means "let the OS pick").
    original_startup = server.startup

    async def _startup_print_port(*args_: Any, **kwargs_: Any) -> None:
        await original_startup(*args_, **kwargs_)
        for srv in getattr(server, "servers", []):
            sockets = getattr(srv, "sockets", None) or []
            for sock in sockets:
                bound_port = sock.getsockname()[1]
                print(f"READY port={bound_port}", flush=True)

    server.startup = _startup_print_port  # type: ignore[method-assign]
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
