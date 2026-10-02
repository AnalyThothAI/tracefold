"""Shared retrieval boundaries, receipt grouping and route degradation."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from tracefold.app import claim_embedding
from tracefold.app.claim_embedding import SELF_TEST_RETRY_SECONDS, ClaimEmbedder
from tracefold.news.claim_recall import CALIBRATION, Candidate, Probe, prepare_rank, rank, vector_bytes
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.recall import select_for_claim
from tracefold.news.updates.contracts import Claim


def vector(x: float, y: float = 0) -> bytes:
    return vector_bytes((x, y, *([0.0] * (CALIBRATION.embedder.dimensions - 2))), CALIBRATION.embedder)


def test_one_rank_fuses_routes_deterministically_and_groups_receipts_by_best_claim() -> None:
    probe = Probe("A policy decision", vector(1), CALIBRATION.embedder.key)
    rows = (
        Candidate("a1", vector(1), probe.embedder, lexical=0.8, group="a"),
        Candidate("a2", vector(0.9, 0.1), probe.embedder, lexical=0.7, group="a"),
        Candidate("b1", vector(0.8, 0.2), probe.embedder, lexical=0.6, group="b"),
        Candidate("c1", vector(-1), probe.embedder, group="c"),
    )
    result = rank(prepare_rank(probe, rows, "receipt"), rows)
    assert [h.key for h in result.hits] == ["a", "b"]
    assert result == rank(prepare_rank(probe, tuple(reversed(rows)), "receipt"), tuple(reversed(rows)))
    assert result.hits[0].routes == ("dense", "fts")
    one = rank(prepare_rank(probe, (rows[0], rows[2]), "receipt"), (rows[0], rows[2]))
    assert result.hits[0].score == one.hits[0].score


def test_missing_mismatched_and_malformed_vectors_degrade_without_blocking_fts_or_source() -> None:
    probe = Probe("A policy decision", vector(1), CALIBRATION.embedder.key)
    rows = (
        Candidate("missing", lexical=0.8),
        Candidate("wrong", vector(1), "old", same_source=True),
        Candidate("broken", b"x", probe.embedder),
    )
    result = rank(prepare_rank(probe, rows, "prior"), rows)
    assert result.degraded and {h.key for h in result.hits} == {"missing", "wrong"}
    assert all(h.dense is None for h in result.hits)
    assert rank(prepare_rank(Probe(probe.text), (), "receipt"), ()).hits == ()


def test_linked_receipts_precede_ranked_candidates_but_require_a_valid_receipt() -> None:
    novelty = ReaderNovelty(novelty="known", linked_intents=("linked", "missing"))
    ranking = rank(
        prepare_rank(Probe("policy"), (Candidate("other", lexical=0.9),), "receipt"), (Candidate("other", lexical=0.9),)
    )
    selected = select_for_claim(novelty, ranking, available=frozenset({"linked", "other"}))
    assert selected.intent_ids == ("linked", "other")


def test_sent_prior_slots_do_not_manufacture_matches_below_all_floors() -> None:
    probe = Probe("policy", vector(1), CALIBRATION.embedder.key)
    rows = (
        Candidate("strong", vector(1), probe.embedder),
        Candidate("sent", vector(0.7, 0.7), probe.embedder, sent=True),
        Candidate("noise", vector(-1), probe.embedder, sent=True),
    )
    calibrated = replace(CALIBRATION, prior=replace(CALIBRATION.prior, k=2, sent_reserved=1))
    assert [h.key for h in rank(prepare_rank(probe, rows, "prior", calibration=calibrated), rows).hits] == [
        "sent",
        "strong",
    ]


def test_missing_vectors_use_the_calibrated_degraded_lexical_floor_per_candidate() -> None:
    probe = Probe("policy", vector(1), CALIBRATION.embedder.key)
    rows = (
        Candidate("ready", vector(1), probe.embedder, lexical=0.8),
        Candidate("pending", lexical=0.8),
    )
    calibrated = replace(CALIBRATION, prior=replace(CALIBRATION.prior, lexical_floor=0.9, degraded_lexical_floor=0.2))
    ranking = rank(prepare_rank(probe, rows, "prior", calibration=calibrated), rows)
    assert ranking.degraded
    assert {hit.key: hit.routes for hit in ranking.hits} == {"ready": ("dense",), "pending": ("fts",)}


def golden_vectors() -> dict[str, bytes]:
    data = json.loads((Path(__file__).parents[1] / "fixtures/news/issue_791_recall_golden_vectors.json").read_text())
    assert all(
        data[field] == getattr(CALIBRATION.embedder, field)
        for field in ("model", "dimensions", "revision", "max_tokens", "pooling", "dtype", "normalization", "template")
    )
    return {text: base64.b64decode(value, validate=True) for text, value in data["vectors"].items()}


def test_issue_750_four_gold_receipts_reach_the_reader_through_shared_rank_and_links() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/news/issue_750_gold_recall.json").read_text())
    vectors = golden_vectors()
    links = tuple(ClaimLink.model_validate(row) for row in fixture["links"])
    receipts = tuple(LinkedReceipt.model_validate(row) for row in fixture["link_receipts"])
    candidates = tuple(
        Candidate(
            f"{row['intent_id']}:{claim['ref']}",
            vectors[claim["statement"]],
            CALIBRATION.embedder.key,
            group=row["intent_id"],
        )
        for row in fixture["candidates"]
        for claim in row["claims"]
    )
    available = frozenset(row["intent_id"] for row in fixture["candidates"])
    gold, data = (Claim.model_validate(row) for row in fixture["claims"])

    def select(current: Claim):
        ranking = rank(
            prepare_rank(
                Probe(current.statement, vectors[current.statement], CALIBRATION.embedder.key), candidates, "receipt"
            ),
            candidates,
        )
        return select_for_claim(reader_novelty(current.ref, links, receipts), ranking, available=available)

    selected = {intent[7:13] for intent in select(gold).intent_ids}
    positives = {key for key, label in fixture["labels"].items() if label == 2}
    assert len(positives) == 4 and positives <= selected
    assert not selected & {key for key, label in fixture["labels"].items() if label == 0}
    assert select(data).intent_ids == ()


def test_issue_755_shared_words_do_not_admit_unrelated_market_stories_above_the_dense_floor() -> None:
    vectors = golden_vectors()
    probe = (
        "The House Oversight Committee expanded its prediction market insider trading probe "
        "to Hyperliquid, Crypto.com, and PredictIt"
    )
    related = "The House Oversight Committee widens its insider trading probe to Hyperliquid"
    noise = (
        "Russian drone hits a Kyiv market as trading halts",
        "Trump administration ends the fuel-economy credit trading market",
        "Iran stock market index falls in thin trading",
        "Arm and Intel rise in premarket trading as the chip market rallies",
    )
    candidates = tuple(
        Candidate(text, vectors[text], CALIBRATION.embedder.key, lexical=0.9) for text in (related, *noise)
    )
    ranking = rank(
        prepare_rank(Probe(probe, vectors[probe], CALIBRATION.embedder.key), candidates, "receipt"), candidates
    )
    assert related in {hit.key for hit in ranking.hits}
    assert len({hit.key for hit in ranking.hits} & set(noise)) <= 1


@pytest.mark.parametrize(
    "values",
    [
        [],
        [0.0] * CALIBRATION.embedder.dimensions,
        [float("nan")] * CALIBRATION.embedder.dimensions,
        [1.0] * (CALIBRATION.embedder.dimensions - 1),
    ],
)
def test_embedding_rejects_invalid_provider_vectors(values) -> None:
    with pytest.raises(ValueError):
        vector_bytes(values, CALIBRATION.embedder)


def test_route_self_test_and_batch_failures_return_lexical_probes_without_retrying_or_exposing_credentials() -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    route = ClaimEmbedder(
        model=CALIBRATION.embedder.model,
        base_url="https://example.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(respond),
    )

    async def run():
        assert await route.probes(["policy"]) == (Probe("policy"),)
        assert await route.probes(["second"]) == (Probe("second"),)
        await route.aclose()

    asyncio.run(run())
    assert len(calls) == 1
    assert "test-key" not in repr(route)
    with pytest.raises(ValueError, match="calibration_identity_mismatch"):
        ClaimEmbedder(model="wrong", base_url="https://example.test/v1", api_key="test-key")


def test_embedding_health_reports_an_outage_and_the_next_successful_batch_recovers() -> None:
    statuses = []
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(503)
        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": i,
                        "embedding": [
                            1.0 if i != 2 else 0.0,
                            1.0 if i == 2 else 0.0,
                            *([0.0] * (CALIBRATION.embedder.dimensions - 2)),
                        ],
                    }
                    for i in range(len(texts))
                ]
            },
        )

    route = ClaimEmbedder(
        model=CALIBRATION.embedder.model,
        base_url="https://example.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(respond),
        on_status=statuses.append,
        max_batch_size=4,
    )

    async def run():
        assert await route.probes(["policy"]) == (Probe("policy"),)
        assert statuses == [True, False]
        (probe,) = await route.probes(["policy"])
        assert probe.vector is not None and probe.embedder == CALIBRATION.embedder.key
        await route.aclose()

    asyncio.run(run())
    assert calls == 3 and statuses == [True, False, True]


def test_startup_outage_recovers_after_backoff_without_restarting_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(claim_embedding.time, "monotonic", lambda: now)
    statuses: list[bool] = []
    batches: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        batches.append(texts)
        if len(batches) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": index,
                        "embedding": [0.0, 1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 2))]
                        if "software" in text
                        else [1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))],
                    }
                    for index, text in enumerate(texts)
                ]
            },
        )

    route = ClaimEmbedder(
        model=CALIBRATION.embedder.model,
        base_url="https://example.test/v1",
        api_key="private-key",
        transport=httpx.MockTransport(respond),
        on_status=statuses.append,
    )

    async def run() -> None:
        nonlocal now
        assert await route.probes(["policy"]) == (Probe("policy"),)
        now += SELF_TEST_RETRY_SECONDS - 1
        assert await route.probes(["second"]) == (Probe("second"),)
        assert len(batches) == 1
        now += 1
        (probe,) = await route.probes(["third"])
        assert probe.vector == vector(1) and probe.embedder == CALIBRATION.embedder.key
        await route.aclose()

    asyncio.run(run())
    assert statuses == [False, True, True]


def test_bounded_batches_preserve_provider_order_and_never_publish_partial_probe_vectors() -> None:
    batches: list[list[str]] = []
    outage = False

    def respond(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        batches.append(texts)
        assert len(texts) <= 2
        if outage and "fact-2" in texts:
            return httpx.Response(503)
        rows = []
        for index, text in enumerate(texts):
            second = 1.0 if "software" in text else float(text[-1]) if text.startswith("fact-") else 0.0
            rows.append(
                {
                    "index": index,
                    "embedding": [
                        0.0 if "software" in text else 1.0,
                        second,
                        *([0.0] * (CALIBRATION.embedder.dimensions - 2)),
                    ],
                }
            )
        return httpx.Response(200, json={"data": list(reversed(rows))})

    route = ClaimEmbedder(
        model=CALIBRATION.embedder.model,
        base_url="https://example.test/v1",
        api_key="private-key",
        max_batch_size=2,
        transport=httpx.MockTransport(respond),
    )
    texts = [f"fact-{i}" for i in range(5)]

    async def run() -> None:
        nonlocal outage
        probes = await route.probes(texts)
        assert [probe.text for probe in probes] == texts
        assert [probe.vector for probe in probes] == [vector(1, i) for i in range(5)]
        assert batches[-3:] == [texts[:2], texts[2:4], texts[4:]]
        outage = True
        assert await route.probes(texts) == tuple(Probe(text) for text in texts)
        outage = False
        assert all(probe.vector is not None for probe in await route.probes(texts))
        await route.aclose()

    asyncio.run(run())
