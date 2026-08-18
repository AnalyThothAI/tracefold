from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from tracefold.macro.dependencies import (
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.projection import MacroModuleClaim, MacroProjectionService


def test_macro_publish_rejects_lost_claim_before_serving_write() -> None:
    calls: list[str] = []

    class _Macro:
        @staticmethod
        def dataset_projection_states(*, dataset_ids: tuple[str, ...]) -> list[dict[str, object]]:
            del dataset_ids
            return []

        @staticmethod
        def upsert_module_current(**_kwargs: Any) -> int:
            calls.append("write")
            return 1

    class _Frontiers:
        @staticmethod
        def complete(*_args: Any, **_kwargs: Any) -> bool:
            calls.append("cas")
            return False

    repos = SimpleNamespace(
        macro=_Macro(),
        projection_frontiers=_Frontiers(),
        transaction=nullcontext,
    )

    class _Database:
        @staticmethod
        def worker_session(*_args: Any, **_kwargs: Any) -> Any:
            return nullcontext(repos)

    service = MacroProjectionService(db=_Database())
    claim = MacroModuleClaim(
        module_id="rates_fed",
        runtime_id=str(uuid4()),
        input_fingerprint=module_input_fingerprint("rates_fed", []),
        projection_version=module_projection_version("rates_fed"),
        deadline_at_ms=1,
    )

    result = service.publish_module(
        claim,
        {"module_id": "rates_fed", "module_payload": {}},
        now_ms=2,
    )

    assert result == {
        "projection_status": "stale_snapshot",
        "module_id": "rates_fed",
        "rows_written": 0,
    }
    assert calls == ["cas"]
