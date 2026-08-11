from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence

import httpx

from tracefold.app.macro_acceptance import collect_macro_http_acceptance
from tracefold.platform.config.settings import load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the deployed Macro overview and six typed module reads.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TRACEFOLD_API_URL", "http://127.0.0.1:8765"),
    )
    args = parser.parse_args(argv)
    settings = load_settings(require_ws_token=True)
    auth_token = settings.ws_token
    if auth_token is None:
        raise RuntimeError("macro_acceptance_ws_token_required")
    with httpx.Client(base_url=str(args.base_url).rstrip("/"), timeout=30.0) as client:
        report = collect_macro_http_acceptance(client, auth_token=auth_token)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
