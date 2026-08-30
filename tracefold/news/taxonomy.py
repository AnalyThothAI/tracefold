"""Versioned News taxonomy facts and code-owned provenance classification (#117).

The model owns four text judgments.  ``source_authority`` is deliberately absent
from :class:`ModelTaxonomyV1`: it is assembled from structured source evidence by
code, so a model cannot promote its own answer to first-party or filing status.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifact_identity import canonical_sha

TAXONOMY_VERSION: Final = "news_taxonomy_v1"
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
IPTCCodebookSha = Literal["6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac"]
IPTC_CODEBOOK_SHA256: Final[IPTCCodebookSha] = "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac"
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
EVENT_FAMILY_ZH: Final[dict[str, str]] = {
    "financial_results": "财务业绩",
    "guidance_outlook": "指引与展望",
    "product_service_change": "产品/服务变化",
    "corporate_transaction": "公司交易",
    "financing_capital_allocation": "融资与资本配置",
    "leadership_governance": "领导层与治理",
    "regulatory_legal": "监管与法律",
    "security_operational_incident": "安全/运营事故",
    "market_access": "市场准入",
    "market_flow_price": "市场流量与价格",
    "macro_policy_data": "宏观政策与数据",
    "geopolitical_conflict": "地缘冲突",
    "other": "其他",
}
CHANGE_STATE_ZH: Final[dict[str, str]] = {
    "announced": "已宣布",
    "scheduled": "已排期",
    "effective": "已生效",
    "reported": "已报告",
    "updated": "已更新",
    "delayed": "已延期",
    "cancelled": "已取消",
    "recalled": "已召回",
    "unknown": "状态未知",
}
SOURCE_AUTHORITY_ZH: Final[dict[str, str]] = {
    "regulatory_filing": "监管申报",
    "issuer_first_party": "发行方一手来源",
    "reputable_secondary": "可信二手来源",
    "unknown": "来源权威未知",
}
ASSERTION_STATUS_ZH: Final[dict[str, str]] = {
    "confirmed": "已确认",
    "claimed": "单方声称",
    "rumor": "传闻",
    "conflicted": "来源冲突",
    "unknown": "断言状态未知",
}

EVENT_FAMILIES: Final[tuple[str, ...]] = tuple(EVENT_FAMILY_ZH)
CHANGE_STATES: Final[tuple[str, ...]] = tuple(CHANGE_STATE_ZH)
SOURCE_AUTHORITIES: Final[tuple[str, ...]] = tuple(SOURCE_AUTHORITY_ZH)
ASSERTION_STATUSES: Final[tuple[str, ...]] = tuple(ASSERTION_STATUS_ZH)

SubjectCode = Literal[
    "medtop:04000000",
    "medtop:20000174",
    "medtop:20000175",
    "medtop:20000177",
    "medtop:20000178",
    "medtop:20000180",
    "medtop:20000183",
    "medtop:20000186",
    "medtop:20000187",
    "medtop:20000189",
    "medtop:20000190",
    "medtop:20000192",
    "medtop:20000195",
    "medtop:20000196",
    "medtop:20000197",
    "medtop:20000199",
    "medtop:20000200",
    "medtop:20000204",
    "medtop:20000205",
    "medtop:20000207",
    "medtop:20000208",
    "medtop:20000344",
    "medtop:20000346",
    "medtop:20000350",
    "medtop:20000359",
    "medtop:20000365",
    "medtop:20000370",
    "medtop:20000371",
    "medtop:20000373",
    "medtop:20000379",
    "medtop:20000384",
    "medtop:20000385",
    "medtop:20001164",
    "medtop:20001279",
    "medtop:16000000",
]
EventFamily = Literal[
    "financial_results",
    "guidance_outlook",
    "product_service_change",
    "corporate_transaction",
    "financing_capital_allocation",
    "leadership_governance",
    "regulatory_legal",
    "security_operational_incident",
    "market_access",
    "market_flow_price",
    "macro_policy_data",
    "geopolitical_conflict",
    "other",
]
ChangeState = Literal[
    "announced",
    "scheduled",
    "effective",
    "reported",
    "updated",
    "delayed",
    "cancelled",
    "recalled",
    "unknown",
]
SourceAuthority = Literal["regulatory_filing", "issuer_first_party", "reputable_secondary", "unknown"]
AssertionStatus = Literal["confirmed", "claimed", "rumor", "conflicted", "unknown"]


class _ExactTaxonomyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelTaxonomyV1(_ExactTaxonomyModel):
    """The four taxonomy axes the EventSemantics Predictor may emit."""

    subject_codes: tuple[SubjectCode, ...] = Field(default=(), max_length=3)
    event_family: EventFamily
    change_state: ChangeState
    assertion_status: AssertionStatus

    @field_validator("subject_codes", mode="before")
    @classmethod
    def canonical_subject_codes(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            return value
        if not all(isinstance(code, str) and code in IPTC_SUBJECT_CODES for code in value):
            return value
        present = set(value)
        return tuple(code for code in IPTC_SUBJECT_CODES if code in present)

    @model_validator(mode="after")
    def no_parent_child_duplicates(self) -> ModelTaxonomyV1:
        if "medtop:04000000" in self.subject_codes and any(
            code.startswith("medtop:2000") for code in self.subject_codes
        ):
            raise ValueError("news_taxonomy_parent_child_duplicate")
        return self


class NewsTaxonomyV1(ModelTaxonomyV1):
    """The complete persisted taxonomy, including the code-owned axis."""

    taxonomy_version: Literal["news_taxonomy_v1"] = TAXONOMY_VERSION
    source_authority: SourceAuthority
    codebook_sha256: IPTCCodebookSha = IPTC_CODEBOOK_SHA256

    @classmethod
    def issue(cls, model: ModelTaxonomyV1, *, source_authority: SourceAuthority) -> NewsTaxonomyV1:
        return cls(**model.model_dump(mode="json"), source_authority=source_authority)


SOURCE_AUTHORITY_CLASSIFIER_VERSION: Final = "news_source_authority_v2"
_REGULATORY_SOURCE_NAMES: Final = frozenset({"sec", "edgar", "securities and exchange commission"})
_REGULATORY_HOSTNAMES: Final = frozenset({"sec.gov", "www.sec.gov", "edgar.sec.gov"})
_ISSUER_SOURCE_NAMES: Final = frozenset(
    {
        "aave",
        "binance",
        "bybit",
        "chainlink",
        "coinbase",
        "ethereum",
        "hyperliquid",
        "kraken",
        "nasdaq",
        "nyse",
        "okx",
        "solana",
        "tesla",
        "tron dao",
        "upbit",
    }
)
_ISSUER_HANDLES: Final = _ISSUER_SOURCE_NAMES
_SECONDARY_SOURCE_NAMES: Final = frozenset(
    {
        "associated press",
        "ap",
        "bloomberg",
        "cnbc",
        "coindesk",
        "financial times",
        "reuters",
        "the block",
        "the wall street journal",
        "wall street journal",
        "wsj",
    }
)
_SECONDARY_HOSTNAMES: Final = frozenset(
    {
        "bloomberg.com",
        "www.bloomberg.com",
        "cnbc.com",
        "www.cnbc.com",
        "coindesk.com",
        "www.coindesk.com",
        "ft.com",
        "www.ft.com",
        "reuters.com",
        "www.reuters.com",
        "theblock.co",
        "www.theblock.co",
        "wsj.com",
        "www.wsj.com",
    }
)
_SOURCE_AUTHORITY_REGISTRY: Final = {
    "regulatory_filing": {
        "names": sorted(_REGULATORY_SOURCE_NAMES),
        "handles": [],
        "hostnames": sorted(_REGULATORY_HOSTNAMES),
    },
    "issuer_first_party": {
        "names": sorted(_ISSUER_SOURCE_NAMES),
        "handles": sorted(_ISSUER_HANDLES),
        "hostnames": [],
    },
    "reputable_secondary": {
        "names": sorted(_SECONDARY_SOURCE_NAMES),
        "handles": [],
        "hostnames": sorted(_SECONDARY_HOSTNAMES),
    },
}
SOURCE_AUTHORITY_REGISTRY_SHA256: Final = canonical_sha(
    {
        "classifier_version": SOURCE_AUTHORITY_CLASSIFIER_VERSION,
        "registry": _SOURCE_AUTHORITY_REGISTRY,
    }
)


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


def source_authority(values: Sequence[str]) -> SourceAuthority:
    """Classify exact structured source identities; uncertainty stays unknown."""

    identities = {identity for value in values if (identity := _source_identity(value)) is not None}
    for authority in ("regulatory_filing", "issuer_first_party", "reputable_secondary"):
        registry = _SOURCE_AUTHORITY_REGISTRY[authority]
        if any(value in registry[kind] for kind, value in identities):
            return authority
    return "unknown"


def source_authority_from_evidence(evidence: Any) -> SourceAuthority:
    if isinstance(evidence, Mapping):
        source = str(evidence.get("source") or evidence.get("reporting_origin") or "")
    else:
        source = str(getattr(evidence, "source", "") or "")
    return source_authority((source,))


def taxonomy_public(value: Mapping[str, Any] | NewsTaxonomyV1 | None) -> dict[str, Any] | None:
    """Versioned server-owned current API projection."""

    if value is None:
        return None
    taxonomy = value if isinstance(value, NewsTaxonomyV1) else NewsTaxonomyV1.model_validate(value)
    codes = list(taxonomy.subject_codes)
    return taxonomy.model_dump(mode="json") | {
        "subject_labels_zh": [IPTC_SUBJECT_LABELS_ZH[code] for code in codes],
        "event_family_zh": EVENT_FAMILY_ZH[taxonomy.event_family],
        "change_state_zh": CHANGE_STATE_ZH[taxonomy.change_state],
        "source_authority_zh": SOURCE_AUTHORITY_ZH[taxonomy.source_authority],
        "assertion_status_zh": ASSERTION_STATUS_ZH[taxonomy.assertion_status],
    }


def event_family_zh(value: str | None) -> str:
    return EVENT_FAMILY_ZH.get(str(value or ""), str(value or ""))


__all__ = [
    "ASSERTION_STATUSES",
    "CHANGE_STATES",
    "EVENT_FAMILIES",
    "IPTC_CODEBOOK_SHA256",
    "IPTC_MEDIA_TOPICS_VERSION",
    "IPTC_SUBJECT_CODEBOOK",
    "IPTC_SUBJECT_CODES",
    "IPTC_SUBJECT_LABELS_ZH",
    "SOURCE_AUTHORITIES",
    "SOURCE_AUTHORITY_CLASSIFIER_VERSION",
    "SOURCE_AUTHORITY_REGISTRY_SHA256",
    "TAXONOMY_VERSION",
    "AssertionStatus",
    "ChangeState",
    "EventFamily",
    "IPTCCodebookSha",
    "ModelTaxonomyV1",
    "NewsTaxonomyV1",
    "SourceAuthority",
    "SubjectCode",
    "event_family_zh",
    "source_authority",
    "source_authority_from_evidence",
    "taxonomy_public",
]
