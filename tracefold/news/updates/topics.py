"""IPTC navigation topics projected from Noul answers, and the code-owned key topic families."""

from __future__ import annotations

from typing import Final

from ..taxonomy import IPTC_CODEBOOK_SHA256, IPTC_SUBJECT_CODEBOOK
from .judgment import Answer

# The canonical, pinned News codebook. Topics are navigation; they do not decide news value on their own.
CODEBOOK: Final[tuple[tuple[str, str], ...]] = IPTC_SUBJECT_CODEBOOK
CODEBOOK_SHA256: Final = IPTC_CODEBOOK_SHA256
MAX_TOPICS: Final = 3

_BUSINESS_ROOT: Final = "medtop:04000000"
_CONFLICT_ROOT: Final = "medtop:16000000"
_ECONOMY: Final = "medtop:20000344"
# Specific macro topics that make the broad `economy` parent redundant.
_MACRO_SPECIFIC: Final[frozenset[str]] = frozenset(
    {
        "medtop:20000346",  # economic trends and indicators
        "medtop:20000350",  # central bank
        "medtop:20000359",  # gross domestic product
        "medtop:20000365",  # employment statistics
        "medtop:20000370",  # inflation
        "medtop:20000371",  # interest rates
        "medtop:20000373",  # international trade
        "medtop:20000379",  # monetary policy
        "medtop:20000384",  # tariff
    }
)

# The families whose state changes are about access, safety or the price of money: the retired Gate's
# escalate families expressed in the kept codebook. It has no incident topic, so a security/operational
# incident is key only through a family it also belongs to -- an exchange or venue incident is carried by
# `market and exchange` -- until the codebook gains one.
KEY_TOPIC_CODES: Final[frozenset[str]] = frozenset(
    {
        # geopolitical conflict
        _CONFLICT_ROOT,
        # macro policy and data
        _ECONOMY,
        *_MACRO_SPECIFIC,
        # market access (listing, delisting, admission to trade) and venue incidents
        "medtop:20000385",  # market and exchange
        "medtop:20000187",  # stock flotation
    }
)
if KEY_TOPIC_CODES - {code for code, _label in CODEBOOK}:
    raise RuntimeError("news_key_topics_outside_codebook")


def project_topics(answers: tuple[Answer, ...], codebook: tuple[tuple[str, str], ...] = CODEBOOK) -> tuple[str, ...]:
    """At most three chosen topics, with a redundant parent pruned, in codebook order."""

    order = {code: index for index, (code, _label) in enumerate(codebook)}
    chosen = {
        answer.item_id: answer
        for answer in answers
        if answer.status == "available" and answer.value is True and answer.item_id in order
    }
    if chosen.keys() - {_BUSINESS_ROOT, _CONFLICT_ROOT}:
        chosen.pop(_BUSINESS_ROOT, None)
    if chosen.keys() & _MACRO_SPECIFIC:
        chosen.pop(_ECONOMY, None)

    # Raw true probability orders topic projection only. Generated Booleans have
    # no provider probability; ties use the original codebook order, not a fake 1.0.
    def rank(code: str) -> tuple[float, int]:
        probabilities = chosen[code].probabilities or {}
        return -probabilities.get("true", 0.0), order[code]

    ranked = sorted(chosen, key=rank)[:MAX_TOPICS]
    return tuple(sorted(ranked, key=order.__getitem__))
