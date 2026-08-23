"""In-memory replay of Deduper+Gate over provider hits (no DB, no broker, no model).

Used by `tracefold news replay`, golden tests, and threshold tuning reports.
"""

from __future__ import annotations

import collections
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ..events.gate import GateInput, evaluate_gate
from ..events.identity import event_family, event_window_ms
from ..events.minhash import band_keys, minhash_signature
from ..events.storyline import preliminary_storyline_key
from ..events.titles import extract_title
from ..events.tokens import comparison_tokens, jaccard
from ..models import EngineType
from ..opennews import parse_opennews_message
from ..pipeline.admission import NEAR_DUPLICATE_THRESHOLD, _compatible, _engine_type, _strong_facts


def replay_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    watchlist_symbols: frozenset[str],
    suppress_low_signal: bool = False,
    instrument_classes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """``instrument_classes`` is what the live Gate reads to tell a stock headline from a coin one (#89). Leave it
    out and the replay silently exercises the fallback instead of the deployed behaviour — which is why the CLI
    loads it from the universe when a database is reachable."""

    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    band_index: dict[tuple[int, str, str], list[int]] = collections.defaultdict(list)
    fp_index: dict[tuple[str, str], int] = {}
    counts: collections.Counter[str] = collections.Counter()
    for raw in sorted(hits, key=lambda h: str(h.get("ts") or "")):
        event = parse_opennews_message({"method": "strategy.triggered", "params": dict(raw)})
        if event is None:
            counts["unparseable_frame"] += 1
            continue
        if event.provider_record_id in seen:
            counts["duplicate_provider_id"] += 1
            continue
        seen.add(event.provider_record_id)
        counts["items"] += 1
        extracted = extract_title(event.raw_text)
        title = extracted.title or (event.entry.title or "")
        family = event_family(extracted.comparison)
        fingerprint = hashlib.sha256(extracted.comparison.encode()).hexdigest()
        published = int(event.entry.published_at_ms or 0)
        metadata = event.provider_metadata
        coins = tuple(c for c in (metadata.get("coins") or []) if isinstance(c, Mapping))
        score = metadata.get("score")
        strategies = tuple(str(s.get("id")) for s in (metadata.get("strategies") or []) if isinstance(s, Mapping))
        gate = evaluate_gate(
            GateInput(
                title=title,
                engine_type=cast(EngineType, _engine_type(metadata)),
                strategy_ids=strategies,
                provider_score=float(score) if isinstance(score, (int, float)) else None,
                coins=coins,
                ingest_mode="live",
                watchlist_symbols=watchlist_symbols,
                raw_first_line=extracted.first_line,
                suppress_low_signal=suppress_low_signal,
                instrument_classes=instrument_classes,
            )
        )
        tokens = comparison_tokens(extracted.comparison)
        window = event_window_ms(family)
        if len(tokens) >= 3:
            exact = fp_index.get((family, fingerprint))
            if exact is not None and events[exact]["opened_at_ms"] + window > published:
                events[exact]["members"] += 1
                counts["exact_members"] += 1
                continue
            keys = band_keys(minhash_signature(tokens))
            mine = _strong_facts(title, gate.grounded_assets)
            best, best_j = None, 0.0
            candidate_ids: set[int] = set()
            for i, key in enumerate(keys):
                candidate_ids.update(band_index[(i, key, family)])
            for idx in candidate_ids:
                cand = events[idx]
                if cand["opened_at_ms"] + window <= published:
                    continue
                j = jaccard(tokens, cand["tokens"])
                if j >= NEAR_DUPLICATE_THRESHOLD and j > best_j and _compatible(mine, cand["facts"]):
                    best, best_j = idx, j
            if best is not None:
                events[best]["members"] += 1
                counts["near_members"] += 1
                continue
        else:
            keys = ()
        idx = len(events)
        events.append(
            {
                "title": title,
                "family": family,
                "opened_at_ms": published,
                "tokens": tokens,
                "facts": _strong_facts(title, gate.grounded_assets),
                "members": 1,
                "admission": gate.admission,
                "queue_priority": gate.queue_priority,
                "asset_class": gate.asset_class,
                "grounded_assets": gate.grounded_assets,
                "storyline_key": preliminary_storyline_key(
                    title=title, grounded_assets=gate.strong_assets, asset_class=gate.asset_class, family=family
                ),
            }
        )
        if len(tokens) >= 3:
            fp_index[(family, fingerprint)] = idx
            for i, key in enumerate(keys):
                band_index[(i, key, family)].append(idx)
        counts["events"] += 1
        counts[f"admission:{gate.admission}"] += 1
    candidates = [e for e in events if e["admission"] == "candidate"]
    return {
        "gate": {
            "suppress_low_signal": bool(suppress_low_signal),
            "instrument_classes": len(instrument_classes or {}),
        },
        "counts": dict(counts),
        "candidate_share_of_items": round(len(candidates) / counts["items"], 4) if counts["items"] else None,
        "candidate_asset_class": dict(collections.Counter(e["asset_class"] for e in candidates)),
        "candidate_queue_priority": dict(collections.Counter(e["queue_priority"] for e in candidates)),
        "storylines": len({e["storyline_key"] for e in candidates}),
        "events": [
            {
                "title": e["title"][:160],
                "admission": e["admission"],
                "queue_priority": e["queue_priority"],
                "asset_class": e["asset_class"],
                "grounded_assets": list(e["grounded_assets"]),
                "storyline_key": e["storyline_key"],
                "members": e["members"],
            }
            for e in events
        ],
        "sample_candidates": [
            {
                "title": e["title"][:120],
                "assets": list(e["grounded_assets"]),
                "storyline_key": e["storyline_key"],
                "members": e["members"],
            }
            for e in candidates[:25]
        ],
    }


__all__ = ["replay_hits"]
