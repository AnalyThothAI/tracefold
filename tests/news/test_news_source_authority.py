"""The two code facts `tracefold.news.taxonomy` still owns: the pinned IPTC codebook and source authority."""

from __future__ import annotations

import pytest

from tracefold.news.taxonomy import (
    IPTC_CODEBOOK_SHA256,
    IPTC_SUBJECT_CODES,
    IPTC_SUBJECT_LABELS_ZH,
    source_authority,
    source_authority_from_evidence,
    source_authority_zh,
)
from tracefold.news.updates.topics import CODEBOOK, CODEBOOK_SHA256


def test_the_iptc_codebook_is_pinned_labelled_and_is_the_topic_codebook() -> None:
    assert set(IPTC_SUBJECT_LABELS_ZH) == set(IPTC_SUBJECT_CODES)
    assert CODEBOOK_SHA256 == IPTC_CODEBOOK_SHA256
    assert tuple(code for code, _label in CODEBOOK) == IPTC_SUBJECT_CODES


def test_source_authority_reads_as_chinese_and_an_unknown_value_as_itself() -> None:
    assert source_authority_zh("regulatory_filing") == "监管申报"
    assert source_authority_zh("not_a_value") == "not_a_value"
    assert source_authority_zh(None) == ""


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("sec.gov",), "regulatory_filing"),
        (("edgar.sec.gov",), "regulatory_filing"),
        (("@coinbase",), "issuer_first_party"),
        (("https://www.reuters.com/world",), "reputable_secondary"),
        (("Reuters fan account",), "unknown"),
        (("fan:reuters",), "unknown"),
        (("fake|sec",), "unknown"),
        (("notreuters.com",), "unknown"),
        (("https://reuters.com.evil.example/world",), "unknown"),
        (("reuters.com.evil.example",), "unknown"),
        (("https://reuters.com@evil.example/world",), "unknown"),
        # #522 D1: a registered domain owns its subdomains. The three above still do not match, because
        # the boundary is a leading dot at the end of the host, not a substring.
        (("https://wire.reuters.com/world",), "reputable_secondary"),
        (("investor.uber.com",), "issuer_first_party"),
        (("www.barrons.com",), "reputable_secondary"),
        # A newswire distributes the issuer's own release verbatim, so it is first-party, not secondary.
        (("globenewswire.com",), "issuer_first_party"),
        (("prnewswire.com",), "issuer_first_party"),
        (("businesswire.com",), "issuer_first_party"),
        # The two highest-volume reporting origins of the #504 receipt, and one issuer product line.
        (("jin10",), "reputable_secondary"),
        (("first squawk",), "reputable_secondary"),
        (("@firstsquawk",), "reputable_secondary"),
        (("binance wallet",), "issuer_first_party"),
        # Deliberately out of the registry: an aggregator, a relay and a personal account carry no
        # institutional authority, and a belligerent's state media is a party to what it reports.
        (("opennews",), "unknown"),
        (("zerohedge",), "unknown"),
        (("alexbward",), "unknown"),
        (("tass",), "unknown"),
        (("irib",), "unknown"),
    ],
)
def test_source_authority_is_exact_code_owned_reporting_source(
    sources: tuple[str, ...],
    expected: str,
) -> None:
    assert source_authority(sources) == expected


def test_strategy_routing_ids_cannot_claim_source_authority() -> None:
    assert (
        source_authority_from_evidence({"source": "fan account", "strategies": ["reuters"], "provenance": ["sec"]})
        == "unknown"
    )
