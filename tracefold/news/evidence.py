"""Provider evidence text, related-Event candidate queries, and archived execution evidence views.

This module does no I/O. The bounded span selector that packed evidence for the retired three-Predictor
Program was deleted with it (#706); archived executions it produced stay readable through
`execution_evidence_views`.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from typing import Any


def normalized_provider_text(params: Mapping[str, Any]) -> str:
    value = params.get("text")
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<br\s*/?>|</(?:p|div)>", "\n", value, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()).strip()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def execution_evidence_views(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read frozen executions only; failed and superseded calls remain inspectable."""
    views = []
    for verdict in verdicts:
        trace = dict(verdict.get("trace") or {})
        for execution in trace.get("program_executions") or ():
            context = dict(execution.get("context") or {})
            prepared = context.get("prepared_evidence")
            if not isinstance(prepared, Mapping):
                continue
            refs: list[str] = []
            card_output_seen = False
            for call in dict(execution.get("trace") or {}).get("calls") or ():
                if call.get("predictor") == "reader_card" and isinstance(call.get("validated_output"), Mapping):
                    card_output_seen = True
                    value = call["validated_output"]
                    refs.extend(dict(value.get("card") or value).get("source_refs") or ())
            views.append(
                {
                    "execution_index": execution["execution_index"],
                    "status": execution["status"],
                    "selected": execution["execution_index"] == trace.get("program_execution_index"),
                    "focus_fact_id": dict(context.get("evidence") or {}).get("focus_fact_id", ""),
                    "input_version": prepared["input_version"],
                    "cutoff_at_ms": prepared["cutoff_at_ms"],
                    "current_evidence": prepared["current_evidence"],
                    "related_evidence": prepared["related_evidence"],
                    "missing": prepared.get("missing", []),
                    "exclusions": prepared.get("exclusions", []),
                    # Archive-only fields; new executions never emit a webpage receipt.
                    **(
                        {
                            "document_status": prepared["document_status"],
                            "document_receipt": prepared.get("document_receipt", {}),
                        }
                        if "document_status" in prepared
                        else {}
                    ),
                    "candidate_count": prepared["candidate_count"],
                    "selected_count": prepared["selected_count"],
                    "declared_source_refs": list(dict.fromkeys(refs)),
                    "reference_issues": ["empty_source_refs"] if card_output_seen and not refs else [],
                    "elapsed_ms": prepared.get("elapsed_ms", 0),
                }
            )
    return views
