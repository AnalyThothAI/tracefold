"""One claim candidate fusion for semantics, receipts and offline replay.

Ranks choose comparisons; they never prove identity or reader coverage. PostgreSQL
owns lexical scores and provenance; exact dense scoring is deliberately portable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
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
    revision: str
    max_tokens: int
    pooling: str
    dtype: str
    normalization: str = "l2"
    template: str = TEXT_TEMPLATE

    @property
    def key(self) -> str:
        return digest(
            (
                self.model,
                self.dimensions,
                self.revision,
                self.max_tokens,
                self.pooling,
                self.dtype,
                self.normalization,
                self.template,
            )
        )


@dataclass(frozen=True, slots=True)
class Cuts:
    k: int
    dense_floor: float
    lexical_floor: float
    degraded_lexical_floor: float
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
    member_key: str | None = None


@dataclass(frozen=True, slots=True)
class Ranking:
    hits: tuple[Hit, ...]
    degraded: bool
    route_hits: tuple[tuple[str, int], ...] = ()
    candidate_count: int = 0

    def diagnostics(self) -> dict[str, object]:
        return {
            "policy": RECALL_POLICY,
            "degraded": self.degraded,
            "route_hits": dict(self.route_hits),
            "candidate_count": self.candidate_count,
            "selected_count": len(self.hits),
            "highest_dense": max((h.dense for h in self.hits if h.dense is not None), default=None),
        }


def vector_bytes(values: Sequence[float], identity: EmbedderIdentity) -> bytes:
    vector = np.asarray(values, dtype=np.float32)
    if vector.shape != (identity.dimensions,) or not np.isfinite(vector).all():
        raise ValueError("news_embedding_shape_invalid")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise ValueError("news_embedding_zero_vector")
    return bytes((vector / norm).astype("<f2").tobytes())


def dense_scores(
    probe: Probe, candidates: Sequence[Candidate], *, identity: EmbedderIdentity = CALIBRATION.embedder
) -> dict[str, float]:
    """Exact cosine scores for identity-matching facts, shared by rank and the daily receipt proxy."""
    size = identity.dimensions * 2
    expected = identity.key
    if probe.vector is None or probe.embedder != expected or len(probe.vector) != size:
        return {}
    usable = [r for r in candidates if r.vector is not None and r.embedder == expected and len(r.vector) == size]
    if not usable:
        return {}
    matrix = (
        np.frombuffer(b"".join(r.vector or b"" for r in usable), dtype="<f2")
        .reshape(len(usable), identity.dimensions)
        .astype(np.float32)
    )
    query = np.frombuffer(probe.vector, dtype="<f2").astype(np.float32)
    # A short exact scan is a row reduction, not a large threaded GEMM. Avoid
    # BLAS thread fanout competing with concurrent Workers on the same host.
    norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix, optimize=False)) * np.linalg.norm(query)
    numerators = np.einsum("ij,j->i", matrix, query, optimize=False)
    values = np.divide(numerators, norms, out=np.zeros(len(usable)), where=norms > 0)
    return {r.key: float(v) for r, v in zip(usable, values, strict=True) if np.isfinite(v)}


@dataclass(frozen=True, slots=True)
class PreparedRecall:
    consumer: Literal["prior", "receipt"]
    calibration: Calibration
    dense: Mapping[str, float]
    ready: bool
    fts_eligible_keys: frozenset[str]
    eligible_keys: frozenset[str]
    candidate_count: int
    started_at: float


def prepare_rank(
    probe: Probe,
    candidates: Sequence[Candidate],
    consumer: Literal["prior", "receipt"],
    *,
    calibration: Calibration = CALIBRATION,
) -> PreparedRecall:
    """Score the full bounded window once, before deferred FTS and exact-head reads.

    No top-n truncation happens here. Stale high-scoring versions can be excluded
    by the adapter without consuming a current fact's rank slot.
    """
    started = time.perf_counter()
    cuts = calibration.prior if consumer == "prior" else calibration.receipt
    dense = dense_scores(probe, candidates, identity=calibration.embedder)
    fts = frozenset(r.key for r in candidates if r.key not in dense or dense[r.key] >= cuts.dense_floor)
    return PreparedRecall(
        consumer,
        calibration,
        dense,
        probe.vector is not None and probe.embedder == calibration.embedder.key,
        fts,
        fts | frozenset(r.key for r in candidates if r.same_source),
        len(candidates),
        started,
    )


def rank(prepared: PreparedRecall, candidates: Sequence[Candidate]) -> Ranking:
    """Union of dense top-n, PostgreSQL FTS top-n and same-source, fused once by RRF.

    Every tie uses a stable fact key. Missing or incompatible vectors degrade only
    this route. Foreign priors reserve slots for already sent propositions.
    """
    calibration = prepared.calibration
    consumer = prepared.consumer
    cuts = calibration.prior if consumer == "prior" else calibration.receipt
    by_key = {row.key: row for row in candidates}
    dense = {k: v for k, v in prepared.dense.items() if k in by_key}
    degraded = not prepared.ready or len(dense) < len(by_key)
    routes = {
        "dense": sorted((k for k, v in dense.items() if v >= cuts.dense_floor), key=lambda k: (-dense[k], k)),
        "fts": sorted(
            (
                k
                for k, r in by_key.items()
                if r.lexical > (cuts.lexical_floor if k in dense else cuts.degraded_lexical_floor)
                and k in prepared.fts_eligible_keys
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
    else:
        winners = {}
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
        (time.perf_counter() - prepared.started_at) * 1000,
    )
    return Ranking(
        tuple(Hit(k, scores[k], dense.get(k), tuple(evidence[k]), by_key[k].sent, winners.get(k)) for k in selected),
        degraded,
        tuple((route, len(keys)) for route, keys in routes.items()),
        prepared.candidate_count,
    )
