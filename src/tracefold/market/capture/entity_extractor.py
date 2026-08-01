from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from eth_utils.address import to_checksum_address
from solders.pubkey import Pubkey

from tracefold.market.capture.entity import ExtractedEntity, is_valid_ton_friendly_address
from tracefold.market.capture.tweet_text import CASHTAG_RE, HASHTAG_RE, MENTION_RE, URL_RE

EVM_CA_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOLANA_CA_RE = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
TON_CA_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{48}(?![A-Za-z0-9_-])")
RESOLVED_EVM_CHAINS = frozenset({"eth", "base", "bsc"})
EVM_CHAIN_HINT_PATTERNS = (
    ("bsc", re.compile(r"\b(?:bsc|bnb\s+chain|binance\s+smart\s+chain|bep[-\s]?20)\b", re.IGNORECASE)),
    ("eth", re.compile(r"\b(?:eth|ethereum|erc[-\s]?20)\b", re.IGNORECASE)),
    ("base", re.compile(r"\b(?:on\s+base|base\s+(?:mainnet|chain|token|ca))\b", re.IGNORECASE)),
)
EXPLORER_HOST_CHAINS = {
    "etherscan.io": "eth",
    "bscscan.com": "bsc",
    "basescan.org": "base",
}
IGNORED_CASHTAG_SYMBOLS = frozenset({"NAN"})


@dataclass(frozen=True, slots=True)
class TextSurface:
    surface: str
    text: str


def extract_entities_from_surfaces(surfaces: Sequence[TextSurface]) -> list[ExtractedEntity]:
    entities: list[ExtractedEntity] = []
    seen: set[tuple[str, str, str | None, str, int, int]] = set()
    for surface in surfaces:
        if not surface.text:
            continue
        _extract_surface_entities(surface, entities=entities, seen=seen)
    return entities


def _extract_surface_entities(
    surface: TextSurface,
    *,
    entities: list[ExtractedEntity],
    seen: set[tuple[str, str, str | None, str, int, int]],
) -> None:
    text = surface.text

    for match in EVM_CA_RE.finditer(text):
        raw = match.group(0)
        _append_unique(
            entities,
            seen,
            _with_span(
                _evm_ca_entity(raw, chain=_chain_for_evm_ca(text, raw=raw, start=match.start(), end=match.end())),
                surface=surface.surface,
                text=text,
                start=match.start(),
                end=match.end(),
            ),
        )

    for match in SOLANA_CA_RE.finditer(text):
        raw = match.group(0)
        if raw.startswith("0x"):
            continue
        entity = _solana_ca_entity(raw)
        if entity is not None:
            _append_unique(
                entities,
                seen,
                _with_span(entity, surface=surface.surface, text=text, start=match.start(), end=match.end()),
            )

    for match in TON_CA_RE.finditer(text):
        raw = match.group(0)
        entity = _ton_ca_entity(raw)
        if entity is not None:
            _append_unique(
                entities,
                seen,
                _with_span(entity, surface=surface.surface, text=text, start=match.start(), end=match.end()),
            )

    for match in CASHTAG_RE.finditer(text):
        symbol = match.group(1)
        normalized_symbol = symbol.upper()
        if normalized_symbol in IGNORED_CASHTAG_SYMBOLS:
            continue
        _append_unique(
            entities,
            seen,
            _with_span(
                ExtractedEntity(
                    entity_type="symbol",
                    raw_value=f"${symbol}",
                    normalized_value=normalized_symbol,
                    chain=None,
                    token_resolution_status="unresolved_symbol",
                    confidence=0.8,
                    source="cashtag",
                ),
                surface=surface.surface,
                text=text,
                start=match.start(),
                end=match.end(),
            ),
        )

    for match in HASHTAG_RE.finditer(text):
        hashtag = match.group(1)
        _append_unique(
            entities,
            seen,
            _with_span(
                ExtractedEntity("hashtag", f"#{hashtag}", hashtag.lower(), None, "non_token_entity", 1.0, "regex"),
                surface=surface.surface,
                text=text,
                start=match.start(),
                end=match.end(),
            ),
        )

    for match in MENTION_RE.finditer(text):
        mention = match.group(1)
        _append_unique(
            entities,
            seen,
            _with_span(
                ExtractedEntity("mention", f"@{mention}", mention.lower(), None, "non_token_entity", 1.0, "regex"),
                surface=surface.surface,
                text=text,
                start=match.start(),
                end=match.end(),
            ),
        )

    for match in URL_RE.finditer(text):
        raw_url = match.group(0)
        cleaned = raw_url.rstrip(".,!?;:)]}")
        end = match.start() + len(cleaned)
        _append_unique(
            entities,
            seen,
            _with_span(
                ExtractedEntity("url", cleaned, cleaned, None, "non_token_entity", 1.0, "url"),
                surface=surface.surface,
                text=text,
                start=match.start(),
                end=end,
            ),
        )
        domain = _url_host(cleaned)
        if domain:
            _append_unique(
                entities,
                seen,
                _with_span(
                    ExtractedEntity("domain", domain, domain, None, "non_token_entity", 1.0, "url"),
                    surface=surface.surface,
                    text=text,
                    start=match.start(),
                    end=end,
                ),
            )


def entity_key(entity: ExtractedEntity) -> str:
    if entity.chain:
        return f"{entity.entity_type}:{entity.chain}:{entity.normalized_value}"
    return f"{entity.entity_type}:{entity.normalized_value}"


def _evm_ca_entity(raw: str, *, chain: str | None = None) -> ExtractedEntity:
    resolved_chain = chain if chain in RESOLVED_EVM_CHAINS else "evm_unknown"
    status = "resolved_ca" if resolved_chain != "evm_unknown" else "unresolved_chain_ca"
    return ExtractedEntity("ca", raw, to_checksum_address(raw), resolved_chain, status, 1.0, "regex")


def _solana_ca_entity(raw: str) -> ExtractedEntity | None:
    try:
        pubkey = Pubkey.from_string(raw)
    except ValueError:
        return None
    return ExtractedEntity("ca", raw, str(pubkey), "solana", "resolved_ca", 1.0, "regex")


def _ton_ca_entity(raw: str) -> ExtractedEntity | None:
    if not is_valid_ton_friendly_address(raw):
        return None
    return ExtractedEntity("ca", raw, raw, "ton", "resolved_ca", 1.0, "regex")


def _chain_for_evm_ca(text: str, *, raw: str, start: int, end: int) -> str | None:
    direct_url_chain = _chain_from_explorer_url_containing(text, raw.lower())
    if direct_url_chain:
        return direct_url_chain

    snippet = text[max(0, start - 180) : min(len(text), end + 180)]
    local_hint = _single_evm_chain_hint(snippet)
    if local_hint:
        return local_hint
    return _single_evm_chain_hint(text)


def _chain_from_explorer_url_containing(text: str, address: str) -> str | None:
    for raw_url in URL_RE.findall(text):
        cleaned = raw_url.rstrip(".,!?;:)]}")
        if address not in cleaned.lower():
            continue
        chain = _chain_from_url(cleaned)
        if chain:
            return chain
    return None


def _single_evm_chain_hint(text: str) -> str | None:
    hints = set()
    for raw_url in URL_RE.findall(text):
        chain = _chain_from_url(raw_url.rstrip(".,!?;:)]}"))
        if chain:
            hints.add(chain)
    for chain, pattern in EVM_CHAIN_HINT_PATTERNS:
        if pattern.search(text):
            hints.add(chain)
    return next(iter(hints)) if len(hints) == 1 else None


def _chain_from_url(raw_url: str) -> str | None:
    host = _url_host(raw_url)
    if host is None:
        return None
    for domain, chain in EXPLORER_HOST_CHAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return chain
    return None


def _url_host(raw_url: str) -> str | None:
    try:
        return urlparse(raw_url).netloc.lower()
    except ValueError:
        return None


def _append_unique(
    entities: list[ExtractedEntity],
    seen: set[tuple[str, str, str | None, str, int, int]],
    entity: ExtractedEntity,
) -> None:
    key = (
        entity.entity_type,
        entity.normalized_value,
        entity.chain,
        entity.text_surface,
        entity.span_start,
        entity.span_end,
    )
    if key in seen:
        return
    seen.add(key)
    entities.append(entity)


def _with_span(
    entity: ExtractedEntity,
    *,
    surface: str,
    text: str,
    start: int,
    end: int,
) -> ExtractedEntity:
    sentence_id = _sentence_id(text, start)
    return ExtractedEntity(
        entity.entity_type,
        entity.raw_value,
        entity.normalized_value,
        entity.chain,
        entity.token_resolution_status,
        entity.confidence,
        entity.source,
        surface,
        int(start),
        int(end),
        sentence_id,
        f"{surface}:{sentence_id}",
    )


def _sentence_id(text: str, offset: int) -> int:
    sentence = 0
    for char in text[: max(0, offset)]:
        if char in {".", "!", "?", "。", "！", "？", "\n"}:
            sentence += 1
    return sentence
