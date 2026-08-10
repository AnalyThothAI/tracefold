from __future__ import annotations

import re
from typing import Any

from .scoring_common import cap, contribution, safe_float, score_payload

CA_RE = re.compile(r"0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}")
MARKET_RE = re.compile(
    r"\b(mcap|market cap|liquidity|volume|holder|holders|pool|launch|listing|pump|breakout|chart|ath|new)\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://|[a-z0-9-]+\.[a-z]{2,}", re.IGNORECASE)


def post_text_features(text: str | None) -> dict[str, Any]:
    clean = (text or "").strip()
    cashtags = re.findall(r"\$[A-Za-z][A-Za-z0-9_]{1,20}", clean)
    has_ca = bool(CA_RE.search(clean))
    has_market = bool(MARKET_RE.search(clean))
    has_url = bool(URL_RE.search(clean))
    is_short = len(clean) < 18
    repeated_cashtags = len(cashtags) >= 5
    informative = bool(has_ca or has_market or has_url or len(clean.split()) >= 8)
    return {
        "has_contract_address": has_ca,
        "has_market_context": has_market,
        "has_url": has_url,
        "is_short": is_short,
        "repeated_cashtags": repeated_cashtags,
        "informative": informative,
    }


def post_quality_score(features: dict[str, Any]) -> dict[str, Any]:
    text_features = post_text_features(str(features.get("text") or ""))
    attribution_status = str(features.get("attribution_status") or "")
    mention_source = str(features.get("mention_source") or features.get("source") or "")
    confidence = safe_float(features.get("attribution_confidence"))
    weight = safe_float(features.get("attribution_weight"))

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    risk_caps: list[dict[str, Any]] = []
    contributions: list[dict[str, Any]] = []

    direct_points = 25.0 if attribution_status == "direct" or "payload" in mention_source else 12.0
    score += direct_points
    reasons.append("direct_token_evidence" if direct_points >= 25 else "selected_token_evidence")
    contributions.append(contribution("post.direct_evidence", direct_points, "direct_token_evidence"))

    confidence_points = min(20.0, max(confidence, weight) * 20.0)
    score += confidence_points
    contributions.append(contribution("post.attribution_confidence", confidence_points, "attribution_confidence"))

    if text_features["has_contract_address"]:
        score += 18.0
        reasons.append("contains_contract_address")
        contributions.append(contribution("post.contract_address", 18.0, "contains_contract_address"))
    if text_features["has_market_context"]:
        score += 18.0
        reasons.append("market_context_present")
        contributions.append(contribution("post.market_context", 18.0, "market_context_present"))
    if text_features["has_url"]:
        score += 7.0
        reasons.append("reference_link_present")
        contributions.append(contribution("post.reference_link", 7.0, "reference_link_present"))
    if text_features["is_short"]:
        risks.append("low_information_post")
        risk_caps.append(cap("low_information_post", 65))
    if text_features["repeated_cashtags"]:
        risks.append("cashtag_spam_pattern")
        risk_caps.append(cap("cashtag_spam_pattern", 55))
    if confidence and confidence < 0.55:
        risks.append("attribution_confidence_low")
        risk_caps.append(cap("attribution_confidence_low", 60))

    return score_payload(
        score_version="post_quality_v1",
        score=score,
        reasons=reasons,
        risks=risks,
        contributions=contributions,
        risk_caps=risk_caps,
        extra={"text_features": text_features},
    )
