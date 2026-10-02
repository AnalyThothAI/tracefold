"""A deterministic generative provider for the golden Workers process (#706).

It sits below the production DSPy JSON adapter as a custom engine, exactly where a chat provider would
answer, so the News Agent, the judgments, the notification planner and the card composer all run their
real signatures, parsing and validation. It answers the four generative News signatures from the
request's own inputs: one grounded claim per visible task segment, fixed narrow judgments, a reader
judgment worth a push with no earlier message, and Chinese card copy for exactly the selected claims.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import dspy
from dspy.lm15 import Message, Request, Response, Usage, response_to_events

from tracefold.news.notifications.reader import REPORT_KIND_OPTIONS

if TYPE_CHECKING:
    from tracefold.news.notifications.policy import ReaderCalibration

_SECTION = re.compile(r"\[\[ ## (\w+) ## \]\]\n(.*?)(?=\n\n\[\[ ## |\Z)", re.S)
JUDGMENTS: dict[str, Any] = {
    "mode": "decision",
    "phase": "announced",
    "content_kind": "state_change",
    "relation": "adds_information",
    "support": "supports",
    "next_read": "no_useful_read",
    "impact_channel": "not_applicable",
    "topic": False,
}
HEADLINE_ZH = "交易所宣布上线新的永续合约"
LINE_ZH = "交易所宣布将上线该永续合约。"


def _inputs(request: Request) -> dict[str, str]:
    text = "\n".join(
        str(getattr(part, "text", "")) for message in request.messages for part in getattr(message, "parts", ())
    )
    return {name: value.strip() for name, value in _SECTION.findall(text)}


def _json(value: str) -> Any:
    # The last input section is followed by the adapter's own output instructions.
    return json.JSONDecoder().raw_decode(value)[0]


def _answer(request: Request) -> dict[str, Any]:
    inputs = _inputs(request)
    if "evidence_json" in inputs:
        frozen = _json(inputs["evidence_json"])
        task_segments = [
            (item["ref"], segment["text"])
            for item in frozen["evidence"]
            for segment in item["segments"]
            if segment["role"] in {"task", "whole"}
        ]
        claims = [
            {
                "slot": f"s{index}",
                "statement": text,
                "fields": {
                    "subject": "exchange",
                    "action": "announces a custody service" if "custody service" in text else "lists perpetual futures",
                    "mode": "decision",
                    "phase": "announced",
                    "content_kind": "state_change",
                },
                "citations": [{"evidence_ref": evidence_ref, "quote": text}],
            }
            for index, (evidence_ref, text) in enumerate(task_segments)
        ]
        return {"result": {"claims": claims}}
    if "criteria_json" in inputs:
        task = inputs["task"].splitlines()[0].strip()
        items = _json(inputs["items"])
        return {"result": {"answers": [{"item_id": item["item_id"], "value": JUDGMENTS[task]} for item in items]}}
    if "claim" in inputs:
        answer: dict[str, Any] = {
            "report_kind": {
                "probabilities": {value: 1.0 if value == "new_action" else 0.0 for value, _ in REPORT_KIND_OPTIONS},
                "confidence": 0.9,
            },
            "materiality": {"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0}, "confidence": 0.9},
            "interrupt_now": {"noul": 0.05},
        }
        if "messages" in inputs:
            ids = [message["id"] for message in _json(inputs["messages"])]
            answer["anchor_message"] = {
                "probabilities": {**dict.fromkeys(ids, 0.0), "none": 1.0},
                "confidence": 0.9,
            }
        return answer
    if "selected_claims_json" in inputs:
        claims = _json(inputs["selected_claims_json"])
        custody = any("custody service" in claim["statement"] for claim in claims)
        headline = "交易所宣布推出数字资产托管服务" if custody else HEADLINE_ZH
        lines = [
            {
                "claim_ref": claim["claim_ref"],
                "text_zh": "交易所宣布推出新的数字资产托管服务。"
                if "custody service" in claim["statement"]
                else LINE_ZH,
            }
            for claim in claims
        ]
        return {"result": {"headline_zh": headline, "lines": lines}}
    raise AssertionError(f"golden provider got an unknown signature: {sorted(inputs)}")


def _respond(model: str, request: Request) -> Response:
    return Response(
        id=None,
        model=model,
        message=Message.assistant(json.dumps(_answer(request), ensure_ascii=False)),
        finish_reason="stop",
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Engine:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, request: Request) -> Response:
        return _respond(self.model, request)

    def stream(self, request: Request) -> Iterator[Any]:
        return iter(response_to_events(self.complete(request)))

    def close(self) -> None:
        return None


class _AsyncEngine:
    def __init__(self, model: str) -> None:
        self.model = model

    async def complete(self, request: Request) -> Response:
        return _respond(self.model, request)

    async def stream(self, request: Request) -> AsyncIterator[Any]:
        for event in response_to_events(_respond(self.model, request)):
            yield event

    async def aclose(self) -> None:
        return None


class _ScriptedNewsLM(dspy.LM):
    def __init__(self, model: str) -> None:
        super().__init__(model, cache=False, num_retries=0, engine=_Engine(model), async_engine=_AsyncEngine(model))

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    @property
    def supports_response_schema(self) -> bool:
        return False


def scripted_generative_lm(endpoint: Any, *, max_tokens: int, timeout: float) -> dspy.LM:
    """Stands in for `tracefold.app.learning_runtime.generative_lm`: same endpoint, a deterministic answer."""

    del max_tokens, timeout
    return _ScriptedNewsLM(str(endpoint.model_name))


def synthetic_reader_calibration() -> ReaderCalibration:
    """A test-only arithmetic fixture that exercises the ordinary certified-policy gate.

    This is not a fitted model or an empirical release certificate. Creating
    the scripted provider never installs it; the isolated golden Workers
    entry point explicitly supplies it in that subprocess only.
    """

    from tracefold.news.notifications.policy import ReaderCalibration

    return ReaderCalibration(
        materiality_floor=2,
        push_coefficients=(0.0, 0.0, 1.0, -1.0),
        key_coefficients=(0.0, 1.0, 0.0),
        push_cut=0.65,
        key_cut=0.75,
        certification_status="certified",
    )
