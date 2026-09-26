"""Subprocess entry for the real Workers composition used by the golden pipeline test."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


def _scripted_push_sender():
    from tracefold.integrations.feishu import FeishuNewsPushSender

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        card = payload.get("card") if isinstance(payload, dict) else None
        valid = payload.get("msg_type") == "interactive" and isinstance(card, dict) and bool(card.get("elements"))
        return httpx.Response(200, json={"code": 0 if valid else 1}, request=request)

    return FeishuNewsPushSender(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/tracefold-golden",
        transport=httpx.MockTransport(respond),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--amqp-url", required=True)
    parser.add_argument("--name-prefix", required=True)
    parser.add_argument("--probe-port", required=True, type=int)
    parser.add_argument("--app-home", required=True)
    parser.add_argument("--management-url", default=None)
    args = parser.parse_args()

    from tests.golden._scripted_news_lm import scripted_generative_lm
    from tracefold.app import learning_runtime
    from tracefold.app.workers import root as workers_root
    from tracefold.app.workers import run_workers
    from tracefold.app.workers.wiring import news as news_wiring
    from tracefold.platform.config.models import Settings

    settings = Settings(
        ws_token="golden-token",
        storage={"postgres": {"dsn": args.dsn, "password_file": None}},
        # A configured News route, so the production semantic worker, planner and card composer run.
        llm={"api_key": "golden-key", "base_url": "https://golden-llm.invalid/v1", "news_triage_model": "golden-model"},
        news={
            "enabled": True,
            "broker": {
                "url": args.amqp_url,
                "name_prefix": args.name_prefix,
                "management_url": args.management_url,
            },
            "push": {
                "enabled": True,
                "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tracefold-golden",
                "min_interval_seconds": 0,
            },
            "venues": {"enabled": False},
        },
        trading={"enabled": False},
    )
    settings.set_config_dir(Path(args.app_home))
    # The provider sides are deterministic: the push provider, and the generative model provider below
    # the production DSPy adapter. Broker routing, Workers composition, the News Agent, adoption,
    # notification planning, transactions, card rendering, durable delivery and HTTP stay real.
    news_wiring._news_push_sender = lambda _settings: news_wiring._ComposedPushSender(sender=_scripted_push_sender())
    learning_runtime.generative_lm = scripted_generative_lm
    workers_root._WORKER_INTERNAL_PORT = args.probe_port
    asyncio.run(run_workers(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
