"""Deduper transaction: Item upsert -> title/identity -> exact/near Event assignment -> Gate -> storyline key.

Runs inside one worker_session transaction; the caller publishes the returned event message after commit.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .exact_atom_identity import event_family, event_window_ms
from .gate import GateInput, GateVerdict, evaluate_gate
from .minhash import band_keys, minhash_signature
from .models import EVENT_IDENTITY_VERSION
from .opennews import OPENNEWS_SOURCE_ID, OpenNewsEvent
from .storyline import preliminary_storyline_key
from .titles import description_after_title, extract_title
from .tokens import comparison_tokens, jaccard

NEAR_DUPLICATE_THRESHOLD = 0.55
_TICKER_RE = re.compile(r"\$([A-Z]{2,6})\b")
_NUMBER_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(%|bn|billion|m|million|k|bps|tn|trillion)?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AdmitResult:
    item_id: str
    item_inserted: bool
    event_id: str
    event_created: bool
    admission: str
    match_kind: str  # leader | exact | near | none
    gate: GateVerdict | None
    family: str
    storyline_key: str
    comparison_fingerprint: str
    title: str


def item_identity(*, source_id: str, source_item_key: str) -> str:
    return hashlib.sha256(f"{source_id}\x1f{source_item_key}".encode()).hexdigest()


def _strong_facts(title: str, grounded: Sequence[str]) -> tuple[set[str], set[str]]:
    tickers = set(_TICKER_RE.findall(title)) | {g.upper().replace("XYZ-", "") for g in grounded}
    numbers = {
        m.group(0).replace(",", "").replace(" ", "").lower()
        for m in _NUMBER_RE.finditer(title)
        if len(m.group(1).replace(",", "")) >= 2
    }
    return tickers, numbers


def _compatible(a: tuple[set[str], set[str]], b: tuple[set[str], set[str]]) -> bool:
    if a[0] and b[0] and not (a[0] & b[0]):
        return False
    return not (a[1] and b[1] and not (a[1] & b[1]))


def _engine_type(metadata: Mapping[str, Any]) -> str:
    strategies = metadata.get("strategies") or []
    engine = ""
    if strategies and isinstance(strategies[0], Mapping):
        engine = str(strategies[0].get("engine_type") or "")
    return engine if engine in {"news", "meme", "listing", "market"} else "unknown"


def admit_item(
    repos: Any,
    *,
    event: OpenNewsEvent,
    ingest_mode: str,
    observed_at_ms: int,
    trace_id: str,
    watchlist_symbols: frozenset[str],
    now_ms: int,
    text_override: str | None = None,
    suppress_low_signal: bool = False,
) -> AdmitResult:
    """Idempotent by Item identity. Returns the Event assignment for the (possibly pre-existing) Item.

    A member that joins an existing non-candidate Event re-runs the Gate when it is stronger evidence (score >= 80,
    an A/A+ grounded tag, or a different reporting origin); the Event is then upgraded in place and published once.
    """

    news = repos.news
    metadata = dict(event.provider_metadata)
    strategy_ids = tuple(
        str(s.get("id")) for s in (metadata.get("strategies") or []) if isinstance(s, Mapping) and s.get("id")
    )
    raw_text = text_override if text_override is not None else (event.raw_text or _reconstruct_text(event))
    extracted = extract_title(raw_text)
    title = extracted.title or (event.entry.title or "")[:500]
    comparison = extracted.comparison
    family = event_family(comparison)
    fingerprint = hashlib.sha256(comparison.encode("utf-8")).hexdigest()
    item_id = item_identity(source_id=OPENNEWS_SOURCE_ID, source_item_key=event.provider_record_id)
    published_at_ms = int(event.entry.published_at_ms or observed_at_ms)
    inserted = news.upsert_item(
        item_id=item_id,
        source_id=OPENNEWS_SOURCE_ID,
        source_item_key=event.provider_record_id,
        title=title or "(untitled)",
        raw_first_line=extracted.first_line[:500],
        description=description_after_title(raw_text) or event.entry.description or "",
        canonical_url=event.entry.link,
        reporting_origin=event.entry.reporting_origin or "opennews",
        published_at_ms=published_at_ms,
        observed_at_ms=int(observed_at_ms),
        provider_metadata=metadata,
        strategy_ids=strategy_ids,
        ingest_mode=ingest_mode,
        trace_id=trace_id,
        now_ms=now_ms,
    )
    existing_membership = repos.conn.execute(
        "SELECT event_id, match_kind FROM news_event_members WHERE item_id = %s LIMIT 1", (item_id,)
    ).fetchone()
    if existing_membership is not None:
        ev = repos.conn.execute(
            "SELECT admission, storyline_key FROM news_events WHERE event_id = %s", (existing_membership["event_id"],)
        ).fetchone()
        return AdmitResult(
            item_id=item_id,
            item_inserted=inserted,
            event_id=str(existing_membership["event_id"]),
            event_created=False,
            admission=str(ev["admission"]) if ev else "candidate",
            match_kind=str(existing_membership["match_kind"]),
            gate=None,
            family=family,
            storyline_key=str(ev["storyline_key"]) if ev else "",
            comparison_fingerprint=fingerprint,
            title=title,
        )

    coins = tuple(c for c in (metadata.get("coins") or []) if isinstance(c, Mapping))
    provider_score = metadata.get("score")
    gate = evaluate_gate(
        GateInput(
            title=title,
            engine_type=_engine_type(metadata),  # type: ignore[arg-type]
            strategy_ids=strategy_ids,
            provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
            coins=coins,
            ingest_mode=ingest_mode,
            watchlist_symbols=watchlist_symbols,
            raw_first_line=extracted.first_line,
            suppress_low_signal=suppress_low_signal,
        )
    )
    tokens = comparison_tokens(comparison)
    window_ms = event_window_ms(family)
    shareable = len(tokens) >= 3

    if shareable:
        exact = news.find_exact_event(family=family, fingerprint=fingerprint, now_ms=now_ms)
        if exact is not None and int(exact["opened_at_ms"]) + window_ms > published_at_ms:
            news.add_member(
                event_id=str(exact["event_id"]),
                item_id=item_id,
                joined_at_ms=published_at_ms,
                match_kind="exact",
                jaccard_estimate=1.0,
                provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
                now_ms=now_ms,
            )
            return _member_result(
                repos,
                event_id=str(exact["event_id"]),
                item_id=item_id,
                inserted=inserted,
                match_kind="exact",
                gate=gate,
                family=family,
                fingerprint=fingerprint,
                title=title,
                reporting_origin=event.entry.reporting_origin or "opennews",
                now_ms=now_ms,
            )
        signature = minhash_signature(tokens)
        keys = band_keys(signature)
        mine = _strong_facts(title, gate.grounded_assets)
        best_id, best_j = None, 0.0
        for cand in news.find_band_candidates(family=family, band_keys=keys, now_ms=now_ms):
            cand_tokens = comparison_tokens(str(cand["comparison_title"]))
            j = jaccard(tokens, cand_tokens)
            if j >= NEAR_DUPLICATE_THRESHOLD and j > best_j:
                theirs = _strong_facts(str(cand["leader_title"]), list(cand.get("grounded_assets") or []))
                if _compatible(mine, theirs):
                    best_id, best_j = str(cand["event_id"]), j
        if best_id is not None:
            news.add_member(
                event_id=best_id,
                item_id=item_id,
                joined_at_ms=published_at_ms,
                match_kind="near",
                jaccard_estimate=round(best_j, 4),
                provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
                now_ms=now_ms,
            )
            return _member_result(
                repos,
                event_id=best_id,
                item_id=item_id,
                inserted=inserted,
                match_kind="near",
                gate=gate,
                family=family,
                fingerprint=fingerprint,
                title=title,
                reporting_origin=event.entry.reporting_origin or "opennews",
                now_ms=now_ms,
            )
    else:
        keys = ()

    storyline = preliminary_storyline_key(
        title=title, grounded_assets=gate.strong_assets, asset_class=gate.asset_class, family=family
    )
    context_line = f"[{gate.asset_class}/{family}/{_engine_type(metadata)}] " + " ".join(gate.grounded_assets)
    news.insert_event(
        event_id=item_id,
        leader_item_id=item_id,
        family=family,
        comparison_fingerprint=fingerprint,
        comparison_title=comparison,
        leader_title=title or "(untitled)",
        opened_at_ms=published_at_ms,
        expires_at_ms=published_at_ms + window_ms,
        admission=gate.admission,
        priority=gate.priority,
        provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
        engine_type=_engine_type(metadata),
        asset_class=gate.asset_class,
        grounded_assets=gate.grounded_assets,
        watchlist_hits=gate.watchlist_hits,
        macro_lexicon=gate.macro_lexicon,
        storyline_key=storyline,
        context_line=context_line.strip(),
        ingest_mode=ingest_mode,
        trace_id=trace_id,
        band_keys=keys if shareable else (),
        now_ms=now_ms,
    )
    return AdmitResult(
        item_id, inserted, item_id, True, gate.admission, "leader", gate, family, storyline, fingerprint, title
    )


_REGATE_ADMISSIONS = frozenset({"candidate", "listing_deterministic", "recovery"})
_STRONG_MEMBER_SCORE = 80.0


def _member_result(
    repos: Any,
    *,
    event_id: str,
    item_id: str,
    inserted: bool,
    match_kind: str,
    gate: GateVerdict,
    family: str,
    fingerprint: str,
    title: str,
    reporting_origin: str,
    now_ms: int,
) -> AdmitResult:
    """Attach a member and, when the member is stronger evidence than the leader, re-gate a suppressed Event."""

    row = repos.conn.execute(
        """
        SELECT e.admission, e.storyline_key, e.priority, e.published_at_ms, i.reporting_origin AS leader_origin
          FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
         WHERE e.event_id = %s
        """,
        (event_id,),
    ).fetchone()
    admission = str(row["admission"]) if row else "candidate"
    upgraded = False
    if row and admission not in _REGATE_ADMISSIONS and gate.admission == "candidate":
        stronger = (
            (gate.grounded_assets and _strong_tag(gate))
            or _member_score(repos, item_id) >= _STRONG_MEMBER_SCORE
            or (reporting_origin and reporting_origin != str(row["leader_origin"] or ""))
        )
        if stronger:
            repos.news.upgrade_event_admission(
                event_id=event_id,
                admission="candidate",
                priority=gate.priority,
                asset_class=gate.asset_class,
                grounded_assets=gate.grounded_assets,
                watchlist_hits=gate.watchlist_hits,
                macro_lexicon=gate.macro_lexicon,
                now_ms=now_ms,
            )
            admission, upgraded = "candidate", True
    return AdmitResult(
        item_id,
        inserted,
        event_id,
        upgraded,  # an upgraded Event is published exactly like a new candidate (idempotent by published_at_ms)
        admission,
        match_kind,
        gate,
        family,
        str(row["storyline_key"]) if row else "",
        fingerprint,
        title,
    )


def _strong_tag(gate: GateVerdict) -> bool:
    """A member whose Gate facts are strong on their own: high priority with a grounded asset, or a watchlist hit."""

    return (bool(gate.grounded_assets) and gate.priority == "high") or bool(gate.watchlist_hits)


def _member_score(repos: Any, item_id: str) -> float:
    row = repos.conn.execute(
        "SELECT provider_metadata ->> 'score' AS score FROM news_items WHERE item_id = %s", (item_id,)
    ).fetchone()
    try:
        return float(row["score"]) if row and row["score"] is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _reconstruct_text(event: OpenNewsEvent) -> str:
    """The adapter keeps title/description; rebuild a text blob so extract_title sees the same blocks."""

    parts = [event.entry.title or ""]
    if event.entry.description:
        parts.append(event.entry.description)
    return "<br/>".join(p for p in parts if p)


__all__ = ["EVENT_IDENTITY_VERSION", "NEAR_DUPLICATE_THRESHOLD", "AdmitResult", "admit_item", "item_identity"]
