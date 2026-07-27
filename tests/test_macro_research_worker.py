from __future__ import annotations

import asyncio
from datetime import date

from tracefold.macro.research.completed_session import MacroResearchNotReady
from tracefold.macro.research.worker import MacroResearchWorker
from tracefold.platform.config.settings import MacroResearchWorkerSettings


class _MissingEvidencePack:
    async def run(self) -> None:
        raise MacroResearchNotReady(
            session_date=date(2026, 7, 24),
            reason="evidence_pack_missing",
        )


def test_macro_research_skips_a_completed_session_without_an_evidence_pack() -> None:
    worker = MacroResearchWorker(
        settings=MacroResearchWorkerSettings(enabled=True),
        db=object(),
        telemetry=object(),
        completed_session_macro=_MissingEvidencePack(),  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result.skipped == 1
    assert result.failed == 0
    assert result.notes == {
        "session_date": "2026-07-24",
        "status": "not_ready",
        "reason": "evidence_pack_missing",
    }
