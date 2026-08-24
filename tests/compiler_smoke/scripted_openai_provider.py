"""Tiny OpenAI-compatible endpoint used only by the production-launcher smoke test."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SEMANTICS = {
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "audience": "us_equity",
    "scope": "single_name",
    "confidence": 0.9,
    "relevance": {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "material_vs_expectation",
        "development_delta": "state_change",
        "channels": ["earnings_cashflow"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    },
}
ADVISORY = "Prefer the stated accepted magnitude when the evidence names a concrete product."
CARD = {
    "headline_zh": "特斯拉发布 Cybercab",
    "why_zh": "新车型进入量产排程，改变该名字的交付预期",
}


def _content(payload: dict[str, Any]) -> str:
    model = str(payload.get("model") or "")
    prompt = json.dumps(payload.get("messages") or payload.get("prompt") or "", ensure_ascii=False)
    if "reflection" in model:
        return f"```\n{ADVISORY}\n```"
    if "judge" in model:
        return json.dumps(
            {
                "verdict": {
                    "headline_equivalent": False,
                    "why_equivalent": False,
                    "facts_preserved": False,
                }
            }
        )
    if "semantics_json" in prompt:
        return json.dumps({"card": CARD}, ensure_ascii=False)
    magnitude = 2 if ADVISORY in prompt else 0
    return json.dumps({"semantics": {**SEMANTICS, "magnitude": magnitude}})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            model = str(payload.get("model") or "")
            response = {
                "id": "chatcmpl-tracefold-compiler-smoke",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _content(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
            document = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)
        except Exception as exc:
            document = json.dumps({"error": {"message": type(exc).__name__}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


if __name__ == "__main__":
    print("scripted-provider-ready", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), _Handler).serve_forever()  # noqa: S104 - container-only bridge
