"""`tracefold news learning judge-calibration`: measure the card judge against its fixed corpus."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.loader import load_settings


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    action = str(getattr(args, "learning_command", ""))
    try:
        if action == "judge-calibration":
            return _handle_learning_judge_calibration(args, settings)
    except (ValueError, PermissionError, RuntimeError) as exc:
        return 2, {"ok": False, "error": str(exc)}
    return 2, {"ok": False, "error": f"unknown learning command: {action}"}


def _handle_learning_judge_calibration(args: Namespace, settings: Any) -> tuple[int, dict[str, Any]]:
    """Ask one real judge model the fixed calibration corpus and write the receipt (#651 §7.3).

    Read-only and database-free: the fourteen pairs are the whole spend. The endpoint credentials come
    from the News extraction fallback slot, and `--model` names the judge model on that endpoint.
    """

    from tracefold.app.learning_runtime import generative_lm
    from tracefold.app.llm import configured_lm_endpoint
    from tracefold.news.learning.judge import JUDGE_MAX_TOKENS, JUDGE_TIMEOUT_SECONDS, CardEvidenceJudge, JudgeEndpoint
    from tracefold.news.learning.judge_calibration import (
        calibration_receipt_sha256,
        load_calibration_cases,
        run_judge_calibration,
    )

    model = str(getattr(args, "model", "") or "")
    if not model:
        raise ValueError("news_judge_calibration_requires_model")
    source = settings.llm.news_triage_fallback
    if not source.configured:
        raise ValueError("news_judge_calibration_endpoint_not_configured")
    endpoint = configured_lm_endpoint(
        settings,
        model_name=model,
        api_key=source.api_key,
        base_url=source.base_url,
        request_config=source.request,
    )
    lm = generative_lm(endpoint, max_tokens=JUDGE_MAX_TOKENS, timeout=JUDGE_TIMEOUT_SECONDS)
    judge = CardEvidenceJudge(JudgeEndpoint(lm))
    receipt = run_judge_calibration(judge, load_calibration_cases())
    payload = {**receipt, "receipt_sha256": calibration_receipt_sha256(receipt)}
    out = str(getattr(args, "out", "") or "")
    if out:
        _write_json(out, payload)
    summary = {key: value for key, value in payload.items() if key not in {"judge", "disagreements"}}
    summary["disagreement_n"] = len(receipt["disagreements"])
    summary["receipt_written_to"] = out or None
    return 0, {"ok": True, "data": summary}


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    """Write one `--out` document, creating the directory the operator named it in."""

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
