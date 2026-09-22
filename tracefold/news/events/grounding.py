"""Whether a symbol an Event names is actually grounded in that Event's own text and catalogue (#675 PR-3).

The Gate keeps no name table: the provider resolved names to symbols and a B+/A/A+ tag *is* the grounded
asset (see :mod:`.gate`). A 24 h audit of the delivered ledger measured where that premise breaks, and the
answer decided what this module does and — more importantly — what it refuses to do.

**What it does.** Two readings, both code-owned and both about evidence the pipeline already holds:

* :func:`commodity_context_present` — a commodity tag needs the commodity itself in the text. This is the
  ``CL``-needs-energy-context rule (#509 D3) generalized to the rest of the underlyings: over 3,362 Events
  it removed 40 tags a day — ``XAU``/``XYZ-GOLD`` on 央行票据, on an IMF debt line, on SoftBank's bond sale,
  on seven Hong Kong filings, ``COPPER`` on soybean planting and on diesel exports — and removed no tag
  from an Event whose text mentioned that commodity at all. `CL` keeps its own branch in the Gate: oil's
  context is a storyline-registry flag, not a word list, and widening it is not this change.
* :func:`asset_grounding` — the support class and catalogue reading for one asset the model named, so a
  verdict trace can say *why* an instrument is on a card. It decides nothing by itself.

**What it refuses to do.** Two rules the Issue proposed were measured on the same 417 delivered cards and
both are over-eager, because the provider tag and the model's answer are *name resolutions* and neither the
frame nor the catalogue carries entity names:

* "a provider tag grounds only when its symbol appears in the text" would strip grounding from 155 of 338
  primaries, 40 of them labelled *keep* by the reviewers — ``BTC`` on "Bitcoin rises above $82,000",
  ``NVO`` on "Novo Nordisk CEO ...", ``HYPE`` on Hyperliquid. The symbol is not the name.
* "the model's primary must be textually supported" would demote 33 primaries that are neither tagged nor
  spelled in the text and are simply right: ``LMT`` for Lockheed Martin, ``ACN`` for Accenture, ``ALKS``
  for Alkermes, ``0700.HK`` for 腾讯. Resolving a company name to its ticker is what the seed asks for.

So :data:`GroundingSupport` is published as a *signal* rather than a gate. The failures it cannot separate —
``CRCL`` for Funding Circle, ``AAPL`` for Anterix, ``PUMP`` for Walrus Pump — need an issuer-name table the
catalogue does not have; that is the next measurement, not another rule here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

GROUNDING_POLICY_VERSION: Final = "news_gate_grounding_v1"

# How an asset is carried by the Event it sits on, strongest first. `provider_tag` is a name the provider
# resolved and the text therefore need not spell; `unsupported` is "nothing on this Event names it", which
# is a normal state for a correct ticker the model read off a company name.
GroundingSupport = Literal["cashtag", "text", "alias", "provider_tag", "unsupported"]
GROUNDING_SUPPORT_ORDER: Final[tuple[GroundingSupport, ...]] = (
    "cashtag",
    "text",
    "alias",
    "provider_tag",
    "unsupported",
)

# The commodity underlyings whose tag needs the commodity named in the text, and the bilingual words that
# name it. Keys are provider tag spellings after the `XYZ-` prefix is stripped, including the alias forms
# (`XAU`/`XAUT` for gold, `XAG` for silver) because the Gate grounds tags before alias resolution runs.
#
# Every key must be a commodity the instrument catalogue also calls one (`COMMODITY_SYMBOLS`, asserted in
# `tests/news/test_news_grounding.py`) — this table narrows an existing class, it does not invent one. `CL`
# and its `WTI`/`OIL`/`BRENTOIL` spellings are deliberately absent: the Gate already requires the registry's
# energy context for them.
COMMODITY_CONTEXT: Final[Mapping[str, re.Pattern[str]]] = {
    "GOLD": re.compile(r"gold|黄金|黃金|金价|金價|au\s*\d|\bxau", re.IGNORECASE),
    "XAU": re.compile(r"gold|黄金|黃金|金价|金價|au\s*\d|\bxau", re.IGNORECASE),
    "XAUT": re.compile(r"gold|黄金|黃金|金价|金價|\bxaut", re.IGNORECASE),
    "SILVER": re.compile(r"silver|白银|白銀|银价|銀價|\bxag", re.IGNORECASE),
    "XAG": re.compile(r"silver|白银|白銀|银价|銀價|\bxag", re.IGNORECASE),
    "COPPER": re.compile(r"copper|铜|銅", re.IGNORECASE),
    "ALUMINIUM": re.compile(r"alumin|铝|鋁", re.IGNORECASE),
    "PLATINUM": re.compile(r"platin|铂|鉑", re.IGNORECASE),
    "PALLADIUM": re.compile(r"palladium|钯|鈀", re.IGNORECASE),
    "NATGAS": re.compile(r"gas|lng|天然气|天然氣", re.IGNORECASE),
    "WHEAT": re.compile(r"wheat|小麦|小麥", re.IGNORECASE),
    "CORN": re.compile(r"corn|玉米", re.IGNORECASE),
    "SOY": re.compile(r"soy|大豆|豆粕", re.IGNORECASE),
    "URANIUM": re.compile(r"uranium|铀|鈾", re.IGNORECASE),
}

# One token of a headline: Latin/CJK word characters plus the punctuation a symbol may legitimately carry
# (`0700.HK`, `BRK.B`, `i-80`). Splitting on it is what keeps `FIRE` out of "Fireblocks" and `BA` out of
# "BAE" without a stemmer.
_TOKEN: Final = re.compile(r"[A-Za-z0-9一-鿿.\-']+")
_HK_CODE: Final = re.compile(r"^(?:HK)?0*(\d{1,5})(?:\.HK)?$", re.IGNORECASE)


def commodity_context_present(symbol: str, text: str) -> bool:
    """Does ``text`` name the commodity ``symbol`` trades on? ``True`` for anything not in the table.

    The default is deliberately permissive: this narrows the named commodity tags and says nothing about
    every other symbol, exactly as the ``CL`` branch says nothing about ``BTC``.
    """

    pattern = COMMODITY_CONTEXT.get(_base(symbol))
    return True if pattern is None else pattern.search(text) is not None


def _base(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if value.startswith("XYZ-"):
        value = value[4:]
    if ":" in value:
        value = value.split(":", 1)[1]
    return value


def symbol_spellings(symbol: str) -> tuple[str, ...]:
    """The code-owned spellings of one symbol: the venue forms and the Hong Kong code variants.

    Not alias resolution and not a name table. `0700.HK`, `00700.HK` and `HK0700` are three spellings of
    one listing and the catalogue has no Hong Kong venue at all (#504 PR-A), so a Hong Kong primary can
    only ever be recognised by its own code appearing in the text. That is what this makes possible.
    """

    base = _base(symbol)
    out = [base]
    hk = _HK_CODE.match(base)
    if hk is not None:
        digits = hk.group(1)
        out.extend((f"{int(digits):04d}.HK", f"{int(digits):05d}.HK", f"HK{digits}", f"HK{int(digits):04d}"))
    return tuple(dict.fromkeys(spelling for spelling in out if spelling))


def _tokens(text: str) -> frozenset[str]:
    out: set[str] = set()
    for token in _TOKEN.findall(text):
        out.add(token.upper())
        out.update(part.upper() for part in re.split(r"[.\-']", token) if part)
    return frozenset(out)


def symbol_support(
    symbol: str,
    *,
    text: str,
    grounded: Sequence[str] = (),
) -> GroundingSupport:
    """How this Event carries ``symbol``: as a cashtag, as its own spelling, as a variant, or as a tag."""

    base = _base(symbol)
    if not base:
        return "unsupported"
    spellings = symbol_spellings(base)
    for spelling in spellings:
        if re.search(rf"\${re.escape(spelling)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            return "cashtag"
    tokens = _tokens(text)
    if base in tokens:
        return "text"
    if any(spelling in tokens for spelling in spellings):
        return "alias"
    if any(_base(value) == base for value in grounded):
        return "provider_tag"
    return "unsupported"


@dataclass(frozen=True, slots=True)
class AssetGrounding:
    """What the code knows about one asset a verdict names. Evidence for a decision, never the decision."""

    symbol: str
    role: str
    market_type: str
    support: GroundingSupport
    in_catalogue: bool
    catalogue_classes: tuple[str, ...]
    # The catalogue proves exactly one market for this symbol and the model named a different one. The
    # only reading in here that is a contradiction rather than an absence: the candidate row was in the
    # evidence the model was shown (#651 §A), so "the catalogue does not know" and "the answer disagrees
    # with what it was shown" stay separate facts.
    class_conflict: bool

    def as_trace(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "role": self.role,
            "market_type": self.market_type,
            "support": self.support,
            "in_catalogue": self.in_catalogue,
            "catalogue_classes": list(self.catalogue_classes),
            "class_conflict": self.class_conflict,
        }


def asset_grounding(
    symbol: str,
    *,
    role: str = "primary",
    market_type: str = "unknown",
    text: str = "",
    grounded: Sequence[str] = (),
    candidates: Mapping[str, Sequence[str]] | None = None,
) -> AssetGrounding:
    """The grounding reading of one asset against this Event's text and catalogue candidates."""

    base = _base(symbol)
    classes = tuple(str(value) for value in (candidates or {}).get(base) or ())
    return AssetGrounding(
        symbol=str(symbol),
        role=str(role),
        market_type=str(market_type),
        support=symbol_support(base, text=text, grounded=grounded),
        in_catalogue=bool(classes),
        catalogue_classes=classes,
        class_conflict=len(classes) == 1 and str(market_type) != classes[0],
    )


def verdict_grounding(
    assets: Sequence[Mapping[str, object]],
    *,
    text: str,
    grounded: Sequence[str] = (),
    candidates: Mapping[str, Sequence[str]] | None = None,
) -> tuple[AssetGrounding, ...]:
    """One reading per asset on a verdict, in the verdict's own order."""

    return tuple(
        asset_grounding(
            str(asset.get("symbol") or ""),
            role=str(asset.get("role") or ""),
            market_type=str(asset.get("market_type") or "unknown"),
            text=text,
            grounded=grounded,
            candidates=candidates,
        )
        for asset in assets
        if str(asset.get("symbol") or "")
    )


__all__ = [
    "COMMODITY_CONTEXT",
    "GROUNDING_POLICY_VERSION",
    "GROUNDING_SUPPORT_ORDER",
    "AssetGrounding",
    "GroundingSupport",
    "asset_grounding",
    "commodity_context_present",
    "symbol_spellings",
    "symbol_support",
    "verdict_grounding",
]
