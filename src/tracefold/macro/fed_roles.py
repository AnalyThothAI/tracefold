from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from tracefold.macro.domain import DocumentFact, FedOfficialRoleFact

_ROSTER_DATASET_ID = "federal_reserve.fomc.roster"


def derive_fomc_role_facts(document: DocumentFact) -> tuple[FedOfficialRoleFact, ...]:
    if document.dataset_id != "federal_reserve.fomc.documents" or document.document_type != "minutes":
        return ()
    raw_records = document.metadata.get("fomc_role_records")
    if not isinstance(raw_records, list):
        return ()
    facts: list[FedOfficialRoleFact] = []
    for record in raw_records:
        if not isinstance(record, dict):
            continue
        official_name = _official_name(record.get("official_name"))
        role_title = _text(record.get("role_title"))
        if not official_name or not role_title:
            continue
        official_id = official_id_for_name(official_name)
        identity = f"{document.document_id}|{official_id}|{role_title}|{document.effective_date.isoformat()}"
        facts.append(
            FedOfficialRoleFact(
                role_fact_id="macrofr_" + hashlib.sha256(identity.encode()).hexdigest(),
                dataset_id=_ROSTER_DATASET_ID,
                official_id=official_id,
                official_name=official_name,
                role_title=role_title,
                organization=_text(record.get("organization")) or "Federal Open Market Committee",
                effective_start=document.effective_date,
                effective_end=None,
                fomc_participant=True,
                fomc_voter=bool(record.get("fomc_voter")),
                source_url=document.source_url,
                received_at_ms=document.received_at_ms,
                raw_data={
                    "source_document_id": document.document_id,
                    "source_document_hash": document.metadata.get("content_hash"),
                    "source_kind": "fomc_minutes_attendance",
                    **record,
                },
            )
        )
    return tuple(facts)


def official_id_for_name(value: str) -> str:
    normalized = normalize_official_name(value)
    return "fedoff_" + hashlib.sha256(normalized.encode()).hexdigest()


def normalize_official_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode().lower()
    ascii_text = re.sub(
        r"\b(?:chairman|chair|vice chair|governor|president|first vice president|mr|ms|mrs|dr)\b",
        " ",
        ascii_text,
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


def match_effective_role(
    speaker_name: str | None,
    *,
    effective_date: Any,
    role_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized = normalize_official_name(speaker_name or "")
    if not normalized:
        return None
    candidates = []
    for row in role_rows:
        if normalize_official_name(str(row.get("official_name") or "")) != normalized:
            continue
        start = row.get("effective_start")
        end = row.get("effective_end")
        if start is None or start > effective_date:
            continue
        if end is not None and end < effective_date:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            row["effective_start"],
            int(row.get("received_at_ms") or 0),
            str(row.get("role_fact_id") or ""),
        ),
    )


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _official_name(value: Any) -> str:
    normalized = _text(value).strip(" ,")
    return re.sub(r"^\d+\s*", "", normalized).strip(" ,")


__all__ = [
    "derive_fomc_role_facts",
    "match_effective_role",
    "normalize_official_name",
    "official_id_for_name",
]
