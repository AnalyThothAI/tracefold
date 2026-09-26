"""The IPTC subject codebook and the code-owned source-authority classifier.

Two code facts survive the #706 hard cut of the four model-owned taxonomy axes: the finite IPTC Media Topics
subset the EventUpdate topic projection chooses from (`news/updates/topics.py`), and the registry that
classifies a reporting source's authority from structured source identity, so a model can never promote its
own answer to first-party or filing status.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from .artifact_identity import canonical_sha

IPTC_MEDIA_TOPICS_VERSION: Final = "2026-01-05"

# A bounded, reviewed subset of IPTC Media Topics.  The qcodes are upstream
# stable identities; labels are retained only to make the pin reviewable.
IPTC_SUBJECT_CODEBOOK: Final[tuple[tuple[str, str], ...]] = (
    ("medtop:04000000", "economy, business and finance"),
    ("medtop:20000174", "bankruptcy"),
    ("medtop:20000175", "stock buyback"),
    ("medtop:20000177", "corporate dividends"),
    ("medtop:20000178", "corporate earnings"),
    ("medtop:20000180", "financial statement"),
    ("medtop:20000183", "business financing"),
    ("medtop:20000186", "stock activity"),
    ("medtop:20000187", "stock flotation"),
    ("medtop:20000189", "layoffs and downsizing"),
    ("medtop:20000190", "executive officer"),
    ("medtop:20000192", "business strategy and marketing"),
    ("medtop:20000195", "board of directors"),
    ("medtop:20000196", "commercial contract"),
    ("medtop:20000197", "spin-off"),
    ("medtop:20000199", "business governance"),
    ("medtop:20000200", "joint venture"),
    ("medtop:20000204", "merger and acquisition"),
    ("medtop:20000205", "new product or service"),
    ("medtop:20000207", "product recall"),
    ("medtop:20000208", "research and development"),
    ("medtop:20000344", "economy"),
    ("medtop:20000346", "economic trends and indicators"),
    ("medtop:20000350", "central bank"),
    ("medtop:20000359", "gross domestic product"),
    ("medtop:20000365", "employment statistics"),
    ("medtop:20000370", "inflation"),
    ("medtop:20000371", "interest rates"),
    ("medtop:20000373", "international trade"),
    ("medtop:20000379", "monetary policy"),
    ("medtop:20000384", "tariff"),
    ("medtop:20000385", "market and exchange"),
    ("medtop:20001164", "payment service"),
    ("medtop:20001279", "cryptocurrency"),
    ("medtop:16000000", "conflict, war and peace"),
)
IPTC_SUBJECT_CODES: Final[tuple[str, ...]] = tuple(code for code, _label in IPTC_SUBJECT_CODEBOOK)
IPTC_CODEBOOK_SHA256: Final = "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac"
if (
    canonical_sha(
        {
            "upstream": "IPTC Media Topics",
            "upstream_version": IPTC_MEDIA_TOPICS_VERSION,
            "codes": IPTC_SUBJECT_CODEBOOK,
        }
    )
    != IPTC_CODEBOOK_SHA256
):
    raise RuntimeError("news_taxonomy_codebook_pin_mismatch")
IPTC_SUBJECT_LABELS_ZH: Final[dict[str, str]] = {
    "medtop:04000000": "经济、商业与金融",
    "medtop:20000174": "破产",
    "medtop:20000175": "股份回购",
    "medtop:20000177": "股息",
    "medtop:20000178": "公司业绩",
    "medtop:20000180": "财务报表",
    "medtop:20000183": "企业融资",
    "medtop:20000186": "股票活动",
    "medtop:20000187": "股票发行上市",
    "medtop:20000189": "裁员",
    "medtop:20000190": "高管",
    "medtop:20000192": "商业战略",
    "medtop:20000195": "董事会",
    "medtop:20000196": "商业合同",
    "medtop:20000197": "分拆",
    "medtop:20000199": "公司治理",
    "medtop:20000200": "合资",
    "medtop:20000204": "并购",
    "medtop:20000205": "新产品或服务",
    "medtop:20000207": "产品召回",
    "medtop:20000208": "研发",
    "medtop:20000344": "经济",
    "medtop:20000346": "经济指标",
    "medtop:20000350": "中央银行",
    "medtop:20000359": "国内生产总值",
    "medtop:20000365": "就业统计",
    "medtop:20000370": "通胀",
    "medtop:20000371": "利率",
    "medtop:20000373": "国际贸易",
    "medtop:20000379": "货币政策",
    "medtop:20000384": "关税",
    "medtop:20000385": "市场与交易所",
    "medtop:20001164": "支付服务",
    "medtop:20001279": "加密货币",
    "medtop:16000000": "冲突、战争与和平",
}
SOURCE_AUTHORITY_ZH: Final[dict[str, str]] = {
    "regulatory_filing": "监管申报",
    "issuer_first_party": "发行方一手来源",
    "reputable_secondary": "可信二手来源",
    "unknown": "来源权威未知",
}

SOURCE_AUTHORITIES: Final[tuple[str, ...]] = tuple(SOURCE_AUTHORITY_ZH)
SourceAuthority = Literal["regulatory_filing", "issuer_first_party", "reputable_secondary", "unknown"]


# v3 (#522) widens coverage and changes one matching rule. The 9 h receipt after the #504 deploy found
# 109 of 116 pushed cards at `unknown`: the issuer entries carried no hostnames at all, the secondary
# hostnames were literal `www.` strings, and the two highest-volume real reporting origins (`jin10`,
# `first squawk`) were absent. Unknown authority is not free — policy v12 D3 downgrades an uncorroborated
# `escalate` — so a registry that cannot recognize Barron's or an issuer's own investor-relations host is
# a delivery defect, not caution.
#
# Deliberately absent, because an allowlist entry grants corroboration weight that these cannot carry:
# personal accounts (analysts, traders, journalists posting under their own name) are one person's word,
# not an institution's; aggregators and relays (`opennews`, `zerohedge`) restate an origin they do not
# own, so authority would be inherited from whoever they copied; and a belligerent's state media (TASS,
# IRIB) is a party to the event it reports, which is exactly the `claimed` case D3 exists to catch.
#
# v4 (#675 §3) adds the official government and military accounts the 24 h audit found classified as
# `unknown`. `decide()` reads this classification twice now -- the uncorroborated-escalate rule and the
# v15 conflict row -- so a US Central Command post about its own ships, a Department of War release or a
# White House statement was being weighed as one anonymous party's claim. These are first-party sources by
# the same rule Binance's own announcement is: the institution is reporting what it itself did.
#
# The exclusions above are unchanged and one candidate was refused under them: a US state governor's press
# office is an official account, but the week of posts under it is political messaging about a third party,
# which is the "party to the event it reports" case. A personal account still gets nothing, whatever office
# its owner holds -- `realdonaldtrump` is absent while `potus`, the office's own account, is present.
#
# The SEC's own account belongs beside the SEC's own domain: it publishes the orders `sec.gov` hosts.
_REGULATORY_SOURCE_NAMES: Final = frozenset({"sec", "edgar", "secgov", "securities and exchange commission"})
_REGULATORY_HANDLES: Final = frozenset({"secgov"})
# Registered domains only: `_hostname_in` matches a registered domain and its subdomains, so
# `edgar.sec.gov` and `www.sec.gov` resolve through `sec.gov` rather than needing their own entries.
_REGULATORY_HOSTNAMES: Final = frozenset({"sec.gov"})
_ISSUER_SOURCE_NAMES: Final = frozenset(
    {
        "aave",
        "binance",
        "binance alpha",
        "binance futures",
        "binance wallet",
        "bybit",
        "centcom",
        "chainlink",
        "coinbase",
        "coinbase status",
        "deptofwar",
        "ethereum",
        "hyperliquid",
        "kraken",
        "nasdaq",
        "nyse",
        "okx",
        "potus",
        "solana",
        "statedeptspox",
        "tesla",
        "tron dao",
        "upbit",
        "whitehouse",
    }
)
# The `@handle` form of the same identities. The product-line names above are reporting-origin strings,
# not accounts, so they stay out of this set: a handle is matched exactly and inventing one would
# recognize an account that may belong to someone else.
_ISSUER_HANDLES: Final = frozenset(
    {
        "aave",
        "binance",
        "bybit",
        "centcom",
        "chainlink",
        "coinbase",
        "deptofwar",
        "ethereum",
        "hyperliquid",
        "kraken",
        "nasdaq",
        "nyse",
        "okx",
        "potus",
        "solana",
        "statedeptspox",
        "tesla",
        "tron dao",
        "upbit",
        "whitehouse",
    }
)
# Each issuer's own official registered domain, checked one by one against the name above; a name whose
# official domain is ambiguous gets none. `circle.com` and `uber.com` are here without a matching name
# because a bare `uber` or `circle` in a free-text source field is ambiguous while the company's own host
# is not — `investor.uber.com` is the issuer publishing its own results.
#
# The three newswires distribute an issuer's own release verbatim under the issuer's byline, so a release
# carried on one of them is first-party evidence of what the issuer said, not a secondary outlet's report
# of it. They are the wire's own domains only: a story *about* an issuer syndicated elsewhere never
# reaches this classifier, which reads the structured reporting source and nothing else.
_ISSUER_HOSTNAMES: Final = frozenset(
    {
        "aave.com",
        "binance.com",
        "bybit.com",
        "chain.link",
        "circle.com",
        "coinbase.com",
        "ethereum.org",
        "hyperliquid.xyz",
        "kraken.com",
        "nasdaq.com",
        "nyse.com",
        "okx.com",
        "solana.com",
        "tesla.com",
        "tron.network",
        "uber.com",
        "upbit.com",
        "businesswire.com",
        "globenewswire.com",
        "prnewswire.com",
        # Official US government publishers, each the institution's own registered domain (#675 §3). A
        # registered domain owns its subdomains, so `home.treasury.gov`, `www.war.gov` and
        # `disclosures-clerk.house.gov` -- the three forms the 7-day production sample actually carries --
        # resolve through these four without an entry each.
        "federalreserve.gov",
        "house.gov",
        "justice.gov",
        "treasury.gov",
        "war.gov",
    }
)
_SECONDARY_SOURCE_NAMES: Final = frozenset(
    {
        "associated press",
        "ap",
        "bloomberg",
        "cnbc",
        "coindesk",
        "deitaone",
        "financial times",
        "first squawk",
        "jin10",
        "reuters",
        "the block",
        "the wall street journal",
        "wall street journal",
        "wsj",
    }
)
_SECONDARY_HANDLES: Final = frozenset({"deitaone", "firstsquawk"})
_SECONDARY_HOSTNAMES: Final = frozenset(
    {
        "apnews.com",
        "axios.com",
        "barrons.com",
        "bloomberg.com",
        "cnbc.com",
        "cnn.com",
        "coindesk.com",
        "ft.com",
        "jin10.com",
        "marketwatch.com",
        "nytimes.com",
        "politico.com",
        "reuters.com",
        "techcrunch.com",
        "theblock.co",
        "wsj.com",
    }
)
_SOURCE_AUTHORITY_REGISTRY: Final = {
    "regulatory_filing": {
        "names": sorted(_REGULATORY_SOURCE_NAMES),
        "handles": sorted(_REGULATORY_HANDLES),
        "hostnames": sorted(_REGULATORY_HOSTNAMES),
    },
    "issuer_first_party": {
        "names": sorted(_ISSUER_SOURCE_NAMES),
        "handles": sorted(_ISSUER_HANDLES),
        "hostnames": sorted(_ISSUER_HOSTNAMES),
    },
    "reputable_secondary": {
        "names": sorted(_SECONDARY_SOURCE_NAMES),
        "handles": sorted(_SECONDARY_HANDLES),
        "hostnames": sorted(_SECONDARY_HOSTNAMES),
    },
}


def _source_identity(raw: str) -> tuple[str, str] | None:
    value = str(raw).strip().casefold()
    if not value:
        return None
    if value.startswith("@"):
        return ("handles", value[1:]) if value.count("@") == 1 else None
    if "://" not in value:
        kind = "hostnames" if "." in value and " " not in value else "names"
        return kind, value
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return "hostnames", parsed.hostname.casefold()


def _hostname_in(host: str, registered: Sequence[str]) -> bool:
    """A registered domain owns itself and every subdomain of it, and nothing else.

    The boundary is the dot: `investor.uber.com` is Uber publishing, `notreuters.com` and
    `reuters.com.evil.example` are not Reuters. Suffix matching without that dot, or without anchoring at
    the end of the host, is how an allowlist becomes a substring search.
    """

    return any(host == entry or host.endswith(f".{entry}") for entry in registered)


def source_authority(values: Sequence[str]) -> SourceAuthority:
    """Classify exact structured source identities; uncertainty stays unknown."""

    identities = {identity for value in values if (identity := _source_identity(value)) is not None}
    for authority in ("regulatory_filing", "issuer_first_party", "reputable_secondary"):
        registry = _SOURCE_AUTHORITY_REGISTRY[authority]
        if any(
            _hostname_in(value, registry["hostnames"]) if kind == "hostnames" else value in registry[kind]
            for kind, value in identities
        ):
            return authority
    return "unknown"


def source_authority_from_evidence(evidence: Any) -> SourceAuthority:
    if isinstance(evidence, Mapping):
        source = str(evidence.get("source") or evidence.get("reporting_origin") or "")
    else:
        source = str(getattr(evidence, "source", "") or "")
    return source_authority((source,))


def source_authority_zh(value: str | None) -> str:
    """The reader's word for one code-owned source authority; an unknown value reads as itself."""

    return SOURCE_AUTHORITY_ZH.get(str(value or ""), str(value or ""))


__all__ = [
    "IPTC_CODEBOOK_SHA256",
    "IPTC_MEDIA_TOPICS_VERSION",
    "IPTC_SUBJECT_CODEBOOK",
    "IPTC_SUBJECT_CODES",
    "IPTC_SUBJECT_LABELS_ZH",
    "SOURCE_AUTHORITIES",
    "SourceAuthority",
    "source_authority",
    "source_authority_from_evidence",
    "source_authority_zh",
]
