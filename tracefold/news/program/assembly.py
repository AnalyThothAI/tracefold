"""Code-owned semantic normalization rules applied after the model answers.

Split out of `graph._assemble` (#314 review) so that `identity.py` can render these rules and hash the
render. While they were expressions inside the executor, they were behavior-deciding bytes that no identity
covered. These rules decide what lands in the current semantic atom, so their implementation identity moves
with the Program envelope.

They are pure functions over their inputs so the identity render can enumerate the whole surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def contradicted_primary_symbols(
    assets: Sequence[Mapping[str, object]],
    candidates: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    """Primaries the instrument catalogue the model was shown says are not that kind of instrument (#675 PR-3).

    `gate.catalog_candidates` is the uncollapsed catalogue row for every symbol this Event already names
    (#651 §A). When it holds exactly one market for a symbol, the catalogue has *proved* what that symbol
    is, and a `primary` that calls it something else is not a reading of ambiguous evidence — it is an
    answer contradicting the evidence it was given. On the 2026-09-21 delivered ledger the rule fires on
    two of 132 unambiguous primaries and both were reviewer-labelled noise: `SILVER` (`market_type`
    `unknown`) for Sunshine Silver's land package, and `XYZ-COPPER` (`market_type` `equity`) for King
    Copper Discovery's. The other 130 agree with the catalogue.

    Deliberately silent on the ambiguous case: 156 of 338 primaries have two or more candidate classes
    (`PUMP` is a token and a ticker, `GOLD` is an underlying and Barrick), and choosing between them is
    what the text is for and what the model is asked to do. Silent as well on a symbol the candidate list
    does not hold at all — "the catalogue does not know" is not "the answer is wrong", and the measured
    cost of treating it as one is 33 correct primaries (`LMT`, `ACN`, `0700.HK`) demoted.
    """

    if not candidates:
        return ()
    out: list[str] = []
    for asset in assets:
        if str(asset.get("role") or "") != "primary":
            continue
        symbol = str(asset.get("symbol") or "")
        base = symbol.upper().removeprefix("XYZ-")
        classes = tuple(str(value) for value in candidates.get(base) or ())
        if len(classes) == 1 and str(asset.get("market_type") or "unknown") != classes[0]:
            out.append(symbol)
    return tuple(out)


def restatement_index_error(*, novelty: str, restates: int, told_count: int) -> str | None:
    """The domain rule a structured-output constraint cannot express, and the code therefore must.

    `restates` is a visible `event_status.told` index if and only if novelty is restatement. A JSON schema
    can say "integer >= -1"; it cannot say "in range of a list you were shown, and only in one case". The
    named error is the failure class that dominated the primary route when this rule's documentation
    stopped reaching the model (#315).
    """

    if novelty == "restatement":
        if restates < 0 or restates >= told_count:
            return "news_program_restatement_index_invalid"
        return None
    if restates != -1:
        return "news_program_non_restatement_index_invalid"
    return None


def normalize_restates(*, novelty: str, restates: int) -> int:
    """The index a non-restatement judgment is stored with, whatever the model emitted.

    A model that answers `progression` with a leftover index is not wrong about the *judgment*, so the code
    silently rewrites the field rather than refusing the call — which means this quietly decides what lands
    in the verdict, and belongs in the identity for the same reason the decision map does (#314 review).
    """

    return restates if novelty == "restatement" or restates == -1 else -1


__all__ = [
    "contradicted_primary_symbols",
    "normalize_restates",
    "restatement_index_error",
]
