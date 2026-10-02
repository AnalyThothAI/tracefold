"""One claim candidate fusion for semantics, receipts and offline replay.

Ranks choose comparisons; they never prove identity or reader coverage. PostgreSQL
owns lexical scores and provenance; exact dense scoring is deliberately portable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, Protocol

import numpy as np

from .updates.contracts import Claim, DraftClaim
from .updates.identity import digest

TEXT_TEMPLATE = "claim_embed_text_v1"
RECEIPT_WINDOW_MS = 48 * 60 * 60 * 1000
PRIOR_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
log = logging.getLogger("tracefold.news")


def embed_text(claim: Claim | DraftClaim) -> str:
    """The proposition alone: no product copy, source boilerplate or invented context."""
    return claim.statement


def text_sha(claim: Claim | DraftClaim) -> str:
    return digest(embed_text(claim))


def numbers(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"\d+(?:[.,]\d+)*(?:%|[KMBT])?", text))))


def structure_keys(claim: Claim | DraftClaim) -> tuple[str, ...]:
    # Typed primary assets are diagnostic features, never a hard retrieval filter.
    return tuple(sorted({f"{a.market_type}:{a.symbol}" for a in claim.fields.assets if a.role == "primary"}))


@dataclass(frozen=True, slots=True)
class EmbedderIdentity:
    model: str
    dimensions: int
    normalization: str = "l2"
    template: str = TEXT_TEMPLATE

    @property
    def key(self) -> str:
        return digest((self.model, self.dimensions, self.normalization, self.template))


@dataclass(frozen=True, slots=True)
class Cuts:
    k: int
    dense_floor: float
    lexical_floor: float
    sent_reserved: int = 0


@dataclass(frozen=True, slots=True)
class Calibration:
    embedder: EmbedderIdentity
    prior: Cuts
    receipt: Cuts
    route_n: int
    rrf_k: int
    dataset_sha256: str
    digest: str

    @classmethod
    def load(cls) -> Calibration:
        data = json.loads(files("tracefold.news").joinpath("claim_recall_calibration.json").read_text())
        return cls(
            embedder=EmbedderIdentity(**data["embedder"]),
            prior=Cuts(**data["prior"]),
            receipt=Cuts(**data["receipt"]),
            route_n=int(data["route_n"]),
            rrf_k=int(data["rrf_k"]),
            dataset_sha256=str(data["dataset_sha256"]),
            digest=digest(data),
        )


CALIBRATION = Calibration.load()
RECALL_POLICY = f"claim_recall_v1:{CALIBRATION.digest}"


@dataclass(frozen=True, slots=True)
class Probe:
    text: str
    vector: bytes | None = None
    embedder: str | None = None


class EmbeddingPort(Protocol):
    identity: EmbedderIdentity

    async def probes(self, texts: Sequence[str]) -> tuple[Probe, ...]: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    vector: bytes | None = None
    embedder: str | None = None
    lexical: float = 0.0
    same_source: bool = False
    sent: bool = False
    group: str | None = None


@dataclass(frozen=True, slots=True)
class Hit:
    key: str
    score: float
    dense: float | None
    routes: tuple[str, ...]
    sent: bool = False


@dataclass(frozen=True, slots=True)
class Ranking:
    hits: tuple[Hit, ...]
    degraded: bool


def vector_bytes(values: Sequence[float], identity: EmbedderIdentity) -> bytes:
    vector = np.asarray(values, dtype=np.float32)
    if vector.shape != (identity.dimensions,) or not np.isfinite(vector).all():
        raise ValueError("news_embedding_shape_invalid")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise ValueError("news_embedding_zero_vector")
    return bytes((vector / norm).astype("<f2").tobytes())


def rank(
    probe: Probe,
    candidates: Sequence[Candidate],
    consumer: Literal["prior", "receipt"],
    *,
    calibration: Calibration = CALIBRATION,
) -> Ranking:
    """Union of dense top-n, PostgreSQL FTS top-n and same-source, fused once by RRF.

    Every tie uses a stable fact key. Missing or incompatible vectors degrade only
    this route. Foreign priors reserve slots for already sent propositions.
    """
    started = time.perf_counter()
    cuts = calibration.prior if consumer == "prior" else calibration.receipt
    by_key = {row.key: row for row in candidates}
    expected = calibration.embedder.key
    dense: dict[str, float] = {}
    ready = probe.vector is not None and probe.embedder == expected
    usable = [r for r in by_key.values() if r.vector is not None and r.embedder == expected]
    size = calibration.embedder.dimensions * 2
    usable = [r for r in usable if len(r.vector or b"") == size]
    if ready and len(probe.vector or b"") == size and usable:
        matrix = np.stack([np.frombuffer(r.vector or b"", dtype="<f2") for r in usable]).astype(np.float32)
        query = np.frombuffer(probe.vector or b"", dtype="<f2").astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
        values = np.divide(matrix @ query, norms, out=np.zeros(len(usable)), where=norms > 0)
        dense = {r.key: float(v) for r, v in zip(usable, values, strict=True) if np.isfinite(v)}
    degraded = not ready or len(dense) < len(by_key)
    routes = {
        "dense": sorted((k for k, v in dense.items() if v >= cuts.dense_floor), key=lambda k: (-dense[k], k)),
        "fts": sorted(
            (
                k
                for k, r in by_key.items()
                if r.lexical > cuts.lexical_floor and (k not in dense or dense[k] >= cuts.dense_floor)
            ),
            key=lambda k: (-by_key[k].lexical, k),
        ),
        "source": sorted(k for k, r in by_key.items() if r.same_source),
    }
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for route, keys in routes.items():
        for position, key in enumerate(keys[: calibration.route_n], 1):
            scores[key] = scores.get(key, 0.0) + 1 / (calibration.rrf_k + position)
            evidence.setdefault(key, []).append(route)
    # A receipt's score is its best frozen proposition. Fuse proposition
    # routes before collapsing groups, so long cards gain no extra weight.
    winners: dict[str, str] = {}
    for key in sorted(scores, key=lambda k: (-scores[k], k)):
        winners.setdefault(by_key[key].group or key, key)
    if any(row.group is not None for row in by_key.values()):
        grouped = {group: Candidate(group, sent=by_key[key].sent) for group, key in winners.items()}
        scores = {group: scores[key] for group, key in winners.items()}
        dense = {group: dense[key] for group, key in winners.items() if key in dense}
        evidence = {group: evidence[key] for group, key in winners.items()}
        by_key = grouped
    ordered = sorted(scores, key=lambda k: (-scores[k], -(dense.get(k, -1)), k))
    reserved = [k for k in ordered if by_key[k].sent][: cuts.sent_reserved]
    selected = list(dict.fromkeys((*reserved, *ordered)))[: cuts.k]
    log.info(
        "news_claim_recall consumer=%s dense=%s fts=%s source=%s highest=%s recall_degraded=%s elapsed_ms=%.3f",
        consumer,
        len(routes["dense"]),
        len(routes["fts"]),
        len(routes["source"]),
        max(dense.values(), default=None),
        degraded,
        (time.perf_counter() - started) * 1000,
    )
    return Ranking(
        tuple(Hit(k, scores[k], dense.get(k), tuple(evidence[k]), by_key[k].sent) for k in selected), degraded
    )
