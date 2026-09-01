"""`news learning run`: the only candidate-generating GEPA path (#453).

The command writes zero-call readiness first, then invokes the existing optimizer once over the same frozen
development dataset. Candidate zero is the Stable baseline inside that GEPA run; no standalone provider
baseline or public `optimize` route exists. The directory must be new and empty so official GEPA state,
the existing optimization report, and an optional PromptCandidate cannot be mixed with another run.

It registers nothing, accepts nothing, promotes nothing and deploys nothing.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .news_learning_baseline import _handle_learning_readiness

_READINESS_FILE = "readiness.json"
_OPTIMIZATION_DIR = "optimization"
_OPTIMIZATION_FILE = "optimization_report.json"
_CANDIDATE_FILE = "prompt_candidate.json"


def _handle_learning_run(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Write readiness and one bounded stock-GEPA result into a new empty directory."""

    out = Path(str(args.out))
    _prepare_new_empty_directory(out)
    development = str(args.development).strip()
    readiness = _readiness(settings, stable, out=out, development=development)
    blockers = [
        *list(readiness["objective"]["blockers"]),
        *list(readiness["development_profile"]["blockers"]),
    ]
    if not readiness["objective"]["compilable"] or not readiness["development_profile"]["ready"]:
        raise ValueError("news_learning_run_readiness_blocked:" + ",".join(blockers))
    optimization = _optimize(args, settings, stable, out=out, development=development)

    candidate = out / _OPTIMIZATION_DIR / _CANDIDATE_FILE
    advance = str(optimization.get("outcome")) == "ADVANCE"
    return (0 if advance else 1), {
        "ok": advance,
        "data": {
            "out": str(out),
            "outcome": optimization.get("outcome"),
            "reasons": list(optimization.get("reasons") or ()),
            "readiness": str(out / _READINESS_FILE),
            "optimization": str(out / _OPTIMIZATION_DIR / _OPTIMIZATION_FILE),
            "prompt_candidate": str(candidate) if candidate.is_file() else None,
        },
    }


def _prepare_new_empty_directory(out: Path) -> None:
    if out.exists():
        if not out.is_dir() or next(out.iterdir(), None) is not None:
            raise ValueError("news_learning_run_out_must_be_new_empty_directory")
        return
    out.mkdir(parents=True)


def _readiness(settings: Any, stable: Any, *, out: Path, development: str) -> dict[str, Any]:
    """Compose the existing zero-call readiness handler and read its full report."""

    path = out / _READINESS_FILE
    code, payload = _handle_learning_readiness(Namespace(development=development, out=str(path)), settings, stable)
    if code != 0:
        raise ValueError(_error_code(payload, fallback="news_learning_run_readiness_failed"))
    return _read(path)


def _optimize(args: Namespace, settings: Any, stable: Any, *, out: Path, development: str) -> dict[str, Any]:
    """Invoke the internal optimization leg once with the operator's declared budget."""

    from .news_learning_experiment import execute_optimization

    directory = out / _OPTIMIZATION_DIR
    code, payload = execute_optimization(
        Namespace(
            development=development,
            out=str(directory),
            max_metric_calls=int(args.max_metric_calls),
            max_task_model_calls=int(args.max_task_model_calls),
            max_reflection_model_calls=int(args.max_reflection_model_calls),
            max_cost_microusd=int(args.max_cost_microusd),
            max_call_cost_microusd=int(args.max_call_cost_microusd),
            max_wall_clock_seconds=int(args.max_wall_clock_seconds),
            seed=int(args.seed),
        ),
        settings,
        stable,
    )
    if code not in {0, 1}:
        raise ValueError(_error_code(payload, fallback="news_learning_run_optimize_failed"))
    return _read(directory / _OPTIMIZATION_FILE)


def _error_code(payload: Mapping[str, Any], *, fallback: str) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("code") or fallback)
    return str(error or fallback)


def _read(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"news_learning_run_artifact_not_a_mapping:{path}")
    return document


__all__ = ["_handle_learning_run"]
