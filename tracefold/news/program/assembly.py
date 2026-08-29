"""Code-owned semantic normalization rules applied after the model answers.

Split out of `graph._assemble` (#314 review) so that `identity.py` can render these rules and hash the
render. While they were expressions inside the executor, they were behavior-deciding bytes that no identity
covered. These rules decide what lands in the current semantic atom, so their implementation identity moves
with the Program envelope.

They are pure functions over their inputs so the identity render can enumerate the whole surface.
"""

from __future__ import annotations


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
    "normalize_restates",
    "restatement_index_error",
]
