"""Constructed source examples and explicitly manual claims for #809 seam tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracefold.news.updates.contracts import Citation, DraftClaim, Evidence, Extraction, FrozenInput, Source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/news/extraction_809_regressions.json"
STAMP = 1_790_405_000_000


def regression(case_id: str) -> dict[str, Any]:
    return next(row for row in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if row["id"] == case_id)


def extraction_case(case_id: str) -> tuple[FrozenInput, Extraction]:
    case = regression(case_id)
    evidence = Evidence.issue(
        case["source_text"],
        Source(
            publisher_id="fixture-809",
            artifact_id=case_id,
            artifact_revision=case["provenance"]["update_ref"],
            first_available_at_ms=STAMP,
        ),
    )
    manual = case["manual_contract_claim"]
    claim = DraftClaim(
        slot=manual["slot"],
        statement=manual["statement"],
        fields=manual["fields"],
        citations=tuple(Citation(evidence_ref=evidence.ref, quote=quote) for quote in manual["quotes"]),
    )
    return (
        FrozenInput(event_id=f"fixture-{case_id}", revision=1, lineage_id="fixture-809", evidence=(evidence,)),
        Extraction(claims=(claim,)),
    )


def boundary_case(case_id: str) -> tuple[FrozenInput, Extraction]:
    case = next(
        row for row in json.loads(FIXTURE.read_text(encoding="utf-8"))["boundary_cases"] if row["id"] == case_id
    )
    evidence = Evidence.issue(
        case["source_text"],
        Source(
            publisher_id="constructed-809",
            artifact_id=case_id,
            artifact_revision="1",
            first_available_at_ms=STAMP,
        ),
    )
    claim = DraftClaim(
        slot=case_id,
        statement=case["statement"],
        fields=case["fields"],
        citations=(Citation(evidence_ref=evidence.ref, quote=evidence.text),),
    )
    return FrozenInput(event_id=case_id, revision=1, lineage_id="constructed-809", evidence=(evidence,)), Extraction(
        claims=(claim,)
    )
