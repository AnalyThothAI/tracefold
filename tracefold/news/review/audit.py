"""Compare a drafted review batch with what the pipeline actually did (#675 §4).

The daily loop this serves is: draft a batch with `news learning draft-reviews`, run it through here, and
hand a human the two short lists that are worth a person's attention — every task where the draft and the
shipped decision disagree, plus a fixed 10% sample of the agreements so agreement itself stays audited.
The rest of the batch is not printed at all; reading 300 drafts a day is what stopped happening.

Everything in this module is a pure function over the batch file and a mapping of task identity to the
decision the desk already recorded. It opens no connection and calls no model: the caller reads decisions
through `ReviewDesk.open`, which is the same view an authorized reviewer sees.

Outcome classes here follow `final_decision`, not the sampler stratum: `push`/`escalate` are delivered,
`drop`/`throttled` are dropped, everything else (degraded, unjudged) is unclassified and enters no ratio.
The two status ratios group by the sampler's stratum instead, which excludes `escalate` (stratum
`critical`) and delivery failures — the same question asked of a slightly narrower population.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from ..artifact_identity import canonical_sha

AUDIT_REPORT_SCHEMA: Final = "tracefold.news.review_audit_report.v1"
#: Sampling identity. Changing it reshuffles which agreements a person is asked to read, so it is a version.
AUDIT_SAMPLER_VERSION: Final = "news_review_audit_sampler_v1"
AGREEMENT_SAMPLE_PROBABILITY: Final = 0.10
PUSH_LABELS: Final = frozenset({"must_push", "should_push"})
HOLD_LABELS: Final = frozenset({"must_hold", "should_hold"})
DELIVERED_DECISIONS: Final = frozenset({"push", "escalate"})
DROPPED_DECISIONS: Final = frozenset({"drop", "throttled"})

Outcome = Literal["delivered", "dropped", "unclassified"]


def outcome_of(final_decision: str | None) -> Outcome:
    """Which side of the loop this task is evidence about."""

    decision = str(final_decision or "")
    if decision in DELIVERED_DECISIONS:
        return "delivered"
    if decision in DROPPED_DECISIONS:
        return "dropped"
    return "unclassified"


def agreement_sampled(task_id: str, *, probability: float = AGREEMENT_SAMPLE_PROBABILITY) -> bool:
    """Deterministic per-task sample, in the style of the desk's own `_sampler_selected`.

    Same task, same answer, on every machine and every rerun: an operator who re-runs the report after
    accepting half of it gets the same reading list rather than a fresh 10%.
    """

    if probability >= 1:
        return True
    if probability <= 0:
        return False
    bucket = int(canonical_sha({"sampler": AUDIT_SAMPLER_VERSION, "task_id": str(task_id)})[:16], 16)
    return bucket < int(probability * (1 << 64))


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "ratio": round(numerator / denominator, 4) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def audit_report(
    batch: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    *,
    probability: float = AGREEMENT_SAMPLE_PROBABILITY,
) -> dict[str, Any]:
    """Fold one draft batch and the decisions its tasks actually got into the daily report.

    `decisions` maps `task_id` to a desk queue row (`final_decision`, `selection`, `headline`). A task with
    no row is skipped and counted: the Event's evidence may have aged out of the review window, and guessing
    its decision would put a fabricated denominator under a product metric.
    """

    disagreements: list[dict[str, Any]] = []
    agreements: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    counts = {"delivered": 0, "dropped": 0, "delivered_push": 0, "dropped_push": 0}

    for entry in batch.get("drafts") or ():
        task_id = str(entry.get("task_id") or "")
        if entry.get("error"):
            skipped["drafting_failed"] = skipped.get("drafting_failed", 0) + 1
            continue
        decision = decisions.get(task_id)
        if decision is None:
            skipped["decision_unavailable"] = skipped.get("decision_unavailable", 0) + 1
            continue
        should_push = str((entry.get("draft") or {}).get("should_push") or "")
        outcome = outcome_of(decision.get("final_decision"))
        row = {
            "task_id": task_id,
            "task_version": str(entry.get("task_version") or ""),
            "event_id": str(entry.get("event_id") or ""),
            "headline_zh": str(entry.get("headline_zh") or decision.get("headline") or ""),
            "final_decision": str(decision.get("final_decision") or ""),
            "stratum": str(dict(decision.get("selection") or {}).get("stratum") or ""),
            "outcome": outcome,
            "should_push": should_push,
            "confidence": float((entry.get("draft") or {}).get("confidence") or 0.0),
        }
        if outcome == "unclassified":
            unclassified.append(row)
            continue
        counts[outcome] += 1
        if should_push in PUSH_LABELS:
            counts[f"{outcome}_push"] += 1
        disagrees = (outcome == "dropped" and should_push in PUSH_LABELS) or (
            outcome == "delivered" and should_push in HOLD_LABELS
        )
        if disagrees:
            disagreements.append({**row, "disagreement": "missed" if outcome == "dropped" else "unwanted"})
        elif agreement_sampled(task_id, probability=probability):
            agreements.append(row)

    selected = [*disagreements, *agreements]
    return {
        "schema_id": AUDIT_REPORT_SCHEMA,
        "batch_sha256": batch.get("batch_sha256"),
        "sampler": {"version": AUDIT_SAMPLER_VERSION, "agreement_probability": probability},
        "tasks": len(tuple(batch.get("drafts") or ())),
        "counts": {
            **counts,
            "disagreement": len(disagreements),
            "agreement_sampled": len(agreements),
            "unclassified": len(unclassified),
            "skipped": skipped,
        },
        # Product metrics over this batch: how much of what shipped the drafter would keep, and how much of
        # what did not ship it would have pushed. `uncertain` is in both denominators and neither numerator.
        "keep_ratio_sent": _ratio(counts["delivered_push"], counts["delivered"]),
        "missed_ratio_dropped": _ratio(counts["dropped_push"], counts["dropped"]),
        "disagreements": disagreements,
        "agreement_sample": agreements,
        "unclassified": unclassified,
        # Paste straight into `news review accept-drafts --only`.
        "only": ",".join(row["task_id"] for row in selected),
    }


def render_table(report: Mapping[str, Any]) -> str:
    """The short human table: the two ratios, then one line per task a person has to read."""

    counts = dict(report.get("counts") or {})
    keep, missed = dict(report.get("keep_ratio_sent") or {}), dict(report.get("missed_ratio_dropped") or {})
    lines = [
        f"batch {str(report.get('batch_sha256') or '')[:16]}  tasks {report.get('tasks')}  "
        f"delivered {counts.get('delivered')}  dropped {counts.get('dropped')}  "
        f"unclassified {counts.get('unclassified')}  skipped {sum(dict(counts.get('skipped') or {}).values())}",
        f"keep_ratio_sent      {_pct(keep)}   missed_ratio_dropped {_pct(missed)}",
        f"disagreements {counts.get('disagreement')}   agreement sample {counts.get('agreement_sampled')}",
        "",
        f"{'KIND':<9} {'DECISION':<10} {'DRAFT':<12} {'CONF':<5} TASK / HEADLINE",
    ]
    for kind, rows in (("disagree", report.get("disagreements")), ("sample", report.get("agreement_sample"))):
        for row in rows or ():
            item = dict(row)
            lines.append(
                f"{kind:<9} {item.get('final_decision') or ''!s:<10} {item.get('should_push') or ''!s:<12} "
                f"{float(item.get('confidence') or 0.0):<5.2f} {str(item.get('task_id') or '')[:28]} "
                f"{str(item.get('headline_zh') or '')[:40]}"
            )
    return "\n".join(lines)


def _pct(ratio: Mapping[str, Any]) -> str:
    value = ratio.get("ratio")
    share = "—" if value is None else f"{float(value) * 100:.1f}%"
    return f"{share} ({ratio.get('numerator')}/{ratio.get('denominator')})"


def decision_task_ids(batch: Mapping[str, Any]) -> tuple[str, ...]:
    """Every drafted task identity in the batch, in file order, for the caller's desk reads."""

    ordered: list[str] = []
    seen: set[str] = set()
    for entry in batch.get("drafts") or ():
        task_id = str(entry.get("task_id") or "")
        if task_id and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return tuple(ordered)


def decisions_from_queue(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index desk queue rows by `task_id`, keeping only what the report reads."""

    return {
        str(row["task_id"]): {
            "final_decision": row.get("final_decision"),
            "selection": dict(row.get("selection") or {}),
            "headline": row.get("agent_headline") or row.get("headline") or "",
        }
        for row in rows
        if row.get("task_id")
    }


__all__ = [
    "AGREEMENT_SAMPLE_PROBABILITY",
    "AUDIT_REPORT_SCHEMA",
    "AUDIT_SAMPLER_VERSION",
    "DELIVERED_DECISIONS",
    "DROPPED_DECISIONS",
    "HOLD_LABELS",
    "PUSH_LABELS",
    "agreement_sampled",
    "audit_report",
    "decision_task_ids",
    "decisions_from_queue",
    "outcome_of",
    "render_table",
]
