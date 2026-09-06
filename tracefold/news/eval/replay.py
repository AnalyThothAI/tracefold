"""In-memory replay of Deduper+Gate over provider hits (no DB, no broker, no model).

Used by `tracefold news replay`, golden tests, and threshold tuning reports.
"""

from __future__ import annotations

import collections
import hashlib
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any, cast

from ..events.gate import GateInput, evaluate_gate
from ..events.identity import dedupe_family, dedupe_window_ms
from ..events.minhash import band_keys, minhash_signature
from ..events.storyline import preliminary_storyline_key
from ..events.titles import extract_title
from ..events.tokens import comparison_tokens
from ..models import EngineType
from ..opennews import parse_opennews_message
from ..pipeline.admission import engine_type, select_near_duplicate, strong_facts
from ..source_contracts import classify_source_contract, source_contract_admission


def _stored_facts(event: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """The strong facts this replay already computed for one open Event, read back for the shared scan."""

    return cast(tuple[set[str], set[str]], event["facts"])


def replay_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    watchlist_symbols: frozenset[str],
    instrument_classes: Mapping[str, str] | None,
) -> dict[str, Any]:
    """``instrument_classes`` is what the live Gate reads to tell a stock headline from a coin one (#89).

    It has no default: passing ``None`` exercises the fallback rather than the deployed behaviour, and a
    replay that measured the fallback by omission would report a Gate nothing runs. Every caller says
    which of the two it is asking for — the CLI loads the map from the universe when a database is
    reachable and passes ``None`` with ``instruments_error`` when it is not.
    """

    seen_items: set[str] = set()
    seen_contracts: set[tuple[str, str]] = set()
    events: list[dict[str, Any]] = []
    band_index: dict[tuple[int, str, str, str], list[int]] = collections.defaultdict(list)
    fp_index: dict[tuple[str, str, str], int] = {}
    counts: collections.Counter[str] = collections.Counter()
    for raw in sorted(hits, key=lambda h: str(h.get("ts") or "")):
        event = parse_opennews_message({"method": "strategy.triggered", "params": dict(raw)})
        if event is None:
            counts["unparseable_frame"] += 1
            continue
        contract = classify_source_contract(event.provider_metadata)
        if contract.is_market:
            # A market frame is an observation, not an Event. It is stored with its typed fact at
            # admission and never reaches the dedupe this replay measures (#553), so counting it here
            # would make the Event denominator include rows no Event lane produced.
            counts["market_observations"] += 1
            continue
        extracted = extract_title(event.raw_text)
        title = extracted.title or (event.entry.title or "")
        published = int(event.entry.published_at_ms or 0)
        event_kind = contract.event_kind or "news"
        # Same provider fact + kind is one Event.
        dedupe_key = (event.provider_record_id, event_kind)
        if dedupe_key in seen_contracts:
            counts["duplicate_provider_id"] += 1
            continue
        seen_contracts.add(dedupe_key)
        if event.provider_record_id not in seen_items:
            seen_items.add(event.provider_record_id)
            counts["items"] += 1
        dedupe_family_name = dedupe_family(extracted.comparison)
        fingerprint = hashlib.sha256(extracted.comparison.encode()).hexdigest()
        metadata = event.provider_metadata
        coins = tuple(c for c in (metadata.get("coins") or []) if isinstance(c, Mapping))
        score = metadata.get("score")
        gate = evaluate_gate(
            GateInput(
                title=title,
                engine_type=cast(EngineType, engine_type(metadata)),
                provider_score=float(score) if isinstance(score, (int, float)) else None,
                coins=coins,
                ingest_mode="live",
                watchlist_symbols=watchlist_symbols,
                raw_first_line=extracted.first_line,
                instrument_classes=instrument_classes,
            )
        )
        admission = source_contract_admission(contract, generic_admission=gate.admission, ingest_mode="live")
        tokens = comparison_tokens(extracted.comparison)
        window = dedupe_window_ms(dedupe_family_name)
        if len(tokens) >= 3:
            exact = fp_index.get((dedupe_family_name, fingerprint, event_kind))
            if exact is not None and events[exact]["opened_at_ms"] + window > published:
                events[exact]["members"] += 1
                counts["exact_members"] += 1
                continue
            keys = band_keys(minhash_signature(tokens))
            candidate_ids: set[int] = set()
            for i, key in enumerate(keys):
                candidate_ids.update(band_index[(i, key, dedupe_family_name, event_kind)])
            chosen = select_near_duplicate(
                tokens,
                strong_facts(title, gate.grounded_assets),
                (
                    (idx, events[idx]["tokens"], partial(_stored_facts, events[idx]))
                    for idx in candidate_ids
                    if events[idx]["opened_at_ms"] + window > published
                ),
            )
            if chosen is not None:
                events[chosen[0]]["members"] += 1
                counts["near_members"] += 1
                continue
        else:
            keys = ()
        idx = len(events)
        events.append(
            {
                "title": title,
                "dedupe_family": dedupe_family_name,
                "opened_at_ms": published,
                "tokens": tokens,
                "facts": strong_facts(title, gate.grounded_assets),
                "members": 1,
                "admission": admission,
                "event_kind": event_kind,
                "queue_priority": gate.queue_priority,
                "asset_class": gate.asset_class,
                "grounded_assets": gate.grounded_assets,
                "storyline_key": preliminary_storyline_key(
                    title=title,
                    strong_assets=gate.strong_assets,
                    asset_class=gate.asset_class,
                    dedupe_family=dedupe_family_name,
                ),
            }
        )
        if len(tokens) >= 3:
            fp_index[(dedupe_family_name, fingerprint, event_kind)] = idx
            for i, key in enumerate(keys):
                band_index[(i, key, dedupe_family_name, event_kind)].append(idx)
        counts["events"] += 1
        counts[f"admission:{admission}"] += 1
    candidates = [e for e in events if e["admission"] == "candidate"]
    return {
        "gate": {
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
                "event_kind": e["event_kind"],
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
