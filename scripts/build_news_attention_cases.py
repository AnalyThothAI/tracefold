"""Build the small, explicitly synthetic #725 attention comparison fixture."""

from __future__ import annotations

import json
from pathlib import Path

from tracefold.news.updates.attention import assessment_input
from tracefold.news.updates.contracts import DraftClaim, Evidence, Extraction, FrozenInput, Source
from tracefold.news.updates.identity import canonical_json
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000
OUTPUT = Path("tests/fixtures/news/issue_725_attention_cases.json")


def _fields(
    subject: str,
    action: str,
    object_: str,
    mode: str,
    phase: str,
    content_kind: str,
    *,
    symbol: str | None = None,
    market: str = "equity",
) -> dict[str, object]:
    value: dict[str, object] = {
        "subject": subject,
        "action": action,
        "object": object_,
        "mode": mode,
        "phase": phase,
        "content_kind": content_kind,
    }
    if symbol:
        value["assets"] = [{"symbol": symbol, "market_type": market, "role": "primary"}]
    return value


def _case(
    name: str,
    facts: list[tuple[str, dict[str, object], str]],
    labels: list[str],
    *,
    watch: tuple[str, ...] = (),
) -> dict[str, object]:
    evidence = []
    claims = []
    for number, (statement, fields, attribution) in enumerate(facts, 1):
        source = Source(
            publisher_id=f"{name}-source",
            artifact_id=f"article-{number}",
            artifact_revision="1",
            first_available_at_ms=STAMP + number,
            attribution=attribution,
            source_authority="issuer_first_party"
            if attribution in {"Company", "Project team", "Agency"}
            else "unknown",
        )
        item = Evidence.issue(statement, source)
        evidence.append(item)
        claims.append(
            DraftClaim.model_validate(
                {
                    "slot": f"c{number}",
                    "statement": statement,
                    "fields": fields,
                    "citations": [{"evidence_ref": item.ref, "quote": statement}],
                }
            )
        )
    event_id = f"eval-{name}"
    update = assemble_update(
        FrozenInput(event_id=event_id, revision=1, lineage_id=f"{event_id}:1", evidence=tuple(evidence)),
        Extraction(claims=tuple(claims)),
        None,
        adopted_at_ms=STAMP + 10,
    )
    if update is None or len(update.claims) != len(facts):
        raise ValueError("news_attention_eval_fixture_assembly_failed")
    snapshot = {
        "update": update.model_dump(mode="json"),
        "candidate": assessment_input(
            update.claims, sources={item.ref: item.source for item in update.evidence}, watch_symbols=watch
        ),
        "reader_receipts": [],
        "preselection": {},
        "assessor_identity": "frozen-evaluation",
    }
    return {
        "case_id": name,
        "input_snapshot": json.loads(canonical_json(snapshot)),
        "recording_origin": "synthetic_notify_all_baseline",
        "recorded_decisions": [
            {"claim_ref": claim.ref, "decision": "notify", "reason": "editor_notify"} for claim in update.claims
        ],
        "labels": {claim.ref: label for claim, label in zip(update.claims, labels, strict=True)},
    }


def main() -> None:
    cases = [
        _case(
            "official-policy",
            [
                (
                    "Agency announces a 25% tariff on steel imports effective October 1.",
                    _fields("Agency", "announces", "steel import tariff", "decision", "announced", "official_measure"),
                    "Agency",
                )
            ],
            ["should_push"],
        ),
        _case(
            "product-release",
            [
                (
                    "ACME launched a paid API with a lower monthly fee for developers.",
                    _fields("ACME", "launched", "paid API", "observation", "effective", "state_change", symbol="ACME"),
                    "Company",
                )
            ],
            ["should_push"],
        ),
        _case(
            "proposed-project",
            [
                (
                    "Project team proposed a testnet bridge for developer review; no launch date is set.",
                    _fields(
                        "Project team",
                        "proposed",
                        "testnet bridge",
                        "decision",
                        "proposed",
                        "other",
                        symbol="TKN",
                        market="crypto",
                    ),
                    "Project team",
                )
            ],
            ["uncertain"],
            watch=("TKN",),
        ),
        _case(
            "routine-promo",
            [
                (
                    "Join the ACME giveaway for a chance to win merchandise; no product changes were announced.",
                    _fields("ACME", "promotes", "giveaway", "promotion", "announced", "other", symbol="ACME"),
                    "ACME marketing",
                )
            ],
            ["should_hold"],
        ),
        _case(
            "empty-event-promo",
            [
                (
                    "Project team invites followers to a weekly community chat with no new agenda or announcement.",
                    _fields(
                        "Project team",
                        "invites",
                        "weekly community chat",
                        "promotion",
                        "announced",
                        "schedule",
                        symbol="TKN",
                        market="crypto",
                    ),
                    "Project team",
                )
            ],
            ["should_hold"],
        ),
        _case(
            "legal-fact-and-solicitation",
            [
                (
                    "A securities complaint against ACME seeks $2 billion in damages; "
                    "the allegations have not been adjudicated.",
                    _fields(
                        "Complaint",
                        "seeks",
                        "$2 billion in damages",
                        "observation",
                        "announced",
                        "other",
                        symbol="ACME",
                    ),
                    "Court filing",
                ),
                (
                    "A law firm asks ACME investors to contact it about potential representation.",
                    _fields(
                        "Law firm", "asks", "investors to contact it", "promotion", "announced", "other", symbol="ACME"
                    ),
                    "Law firm",
                ),
            ],
            ["should_push", "should_hold"],
        ),
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps({"schema": "news_attention_eval_cases_v1", "cases": cases}, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
