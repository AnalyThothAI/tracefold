"""Historical operator retention and recent-first durable Janitor repair."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.news_update_pg import EVENT, adopted_head, sql, store
from tracefold.news.claim_recall import CALIBRATION, text_sha, vector_bytes
from tracefold.news.updates.contracts import content_revision_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

DAY_MS = 86400_000


def test_operator_keyset_projects_exact_versions_across_seven_and_thirty_day_boundaries() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    now_ms = clock.now_ms + 2
    versions = {}
    previous = head.content_revision
    for name, age in (
        ("recent", 1),
        ("seven-day-edge", 7 * DAY_MS),
        ("older", 8 * DAY_MS),
        ("retained-edge", 30 * DAY_MS),
        ("expired", 30 * DAY_MS + 1),
    ):
        # The operator's retention follows adoption time, including a recently
        # adopted version of a proposition first available over thirty days ago.
        claim = head.claims[0].model_copy(
            update={
                "statement": f"Agency announced the {name} tariff proposal.",
                "first_available_at_ms": now_ms - 45 * DAY_MS,
            }
        )
        historical = head.model_copy(
            update={
                "claims": (claim,),
                "previous_content_revision": previous,
                "content_revision": content_revision_for(head.content_sha, previous),
            }
        )
        previous = historical.content_revision
        versions[name] = claim
        sql(
            """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,work_id,
                 input_sha256,program_identity,understanding,content_revision,update_ref,adopted_at_ms,document)
               VALUES (%s,%s,'semantic',1,%s,%s,%s,'fixture','{}',%s,%s,%s,%s::jsonb)""",
            (
                name,
                EVENT,
                now_ms - age,
                name,
                name,
                historical.content_revision,
                historical.ref,
                now_ms - age,
                json.dumps(historical.model_dump(mode="json")),
            ),
        )

    # Operator recovery is one ascending keyset pass. Existing exact index rows
    # do not hide another wording carried by the same persistent claim ref.
    after = [0, "", 0]
    projected = []
    while rows := asyncio.run(
        db.read(
            "historical-page",
            lambda r, after=after: r.news.claim_index.historical_batch(
                phase="adopted", after=after, limit=1, now_ms=now_ms
            ),
        )
    ):
        assert len(rows) == 1
        asyncio.run(db.tx("project-page", lambda r: r.news.claim_index.project_batch(rows)))
        projected.append(rows[0]["claim"]["statement"])
        last = rows[-1]
        after = [last["cursor_ms"], last["cursor_id"], last["cursor_claim"]]

    assert projected == [versions[name].statement for name in ("retained-edge", "older", "seven-day-edge")] + [
        head.claims[0].statement,
        versions["recent"].statement,
    ]
    indexed = sql("SELECT claim_ref,text_sha256 FROM news_claim_index")
    assert {row["claim_ref"] for row in indexed} == {head.claims[0].ref}
    assert {row["text_sha256"] for row in indexed} == {
        text_sha(head.claims[0]),
        *(text_sha(claim) for name, claim in versions.items() if name != "expired"),
    }


def test_janitor_pending_repairs_recent_exact_versions_before_older_retained_versions() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    now_ms = clock.now_ms + 2
    encoded = vector_bytes([1.0, *([0.0] * 383)], CALIBRATION.embedder)
    versions = {
        name: head.claims[0].model_copy(
            update={
                "statement": f"Agency announced the {name} tariff proposal.",
                "first_available_at_ms": now_ms - age,
            }
        )
        for name, age in (
            ("expired", 30 * DAY_MS + 1),
            ("retained-edge", 30 * DAY_MS),
            ("older", 8 * DAY_MS),
            ("seven-day-edge", 7 * DAY_MS),
            ("recent", 1),
        )
    }
    asyncio.run(
        db.tx(
            "pending-versions",
            lambda r: (
                r.news.claim_index.save_vectors(
                    [(head.claims[0].ref, text_sha(head.claims[0]), encoded)], embedder=CALIBRATION.embedder.key
                ),
                [r.news.claim_index.index_claim(EVENT, claim) for claim in versions.values()],
            ),
        )
    )
    repaired = []
    while rows := asyncio.run(db.read("pending-one", lambda r: r.news.claim_index.pending(1, now_ms=now_ms))):
        assert len(rows) == 1
        repaired.append(rows[0]["embed_text"])
        asyncio.run(
            db.tx(
                "complete-one",
                lambda r: r.news.claim_index.save_vectors(
                    [(rows[0]["claim_ref"], rows[0]["text_sha256"], encoded)], embedder=CALIBRATION.embedder.key
                ),
            )
        )
    assert repaired == [versions[name].statement for name in ("recent", "seven-day-edge", "older", "retained-edge")]
    assert sql(
        "SELECT vector FROM news_claim_index WHERE claim_ref=%s AND text_sha256=%s",
        (versions["expired"].ref, text_sha(versions["expired"])),
    ) == [{"vector": None}]
    status = asyncio.run(db.read("status", lambda r: r.news.claim_index.status(now_ms=now_ms)))
    assert status["claim_index_pending"] == 0
