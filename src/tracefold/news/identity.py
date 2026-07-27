from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tracefold.news.models import (
    ARTICLE_IDENTITY_VERSION,
    STORY_IDENTITY_VERSION,
    STORY_MEMBER_SEMANTICS_VERSION,
    STORY_SCORING_VERSION,
    AdmittedFeedObservation,
    ArticleIdentityFeatures,
    MemberSemantics,
    NewsFeedEntry,
    NewsSourceDefinition,
    StoryCandidate,
    StoryIdentityDecision,
)

ARTICLE_MAX_AGE_MS = 96 * 60 * 60 * 1000
ARTICLE_FUTURE_SKEW_MS = 60 * 60 * 1000
URL_REUSE_MIN_SOURCE_TIME_GAP_MS = 12 * 60 * 60 * 1000
LEXICAL_CANDIDATE_WINDOW_MS = 96 * 60 * 60 * 1000
ANCHORED_CANDIDATE_WINDOW_MS = 14 * 24 * 60 * 60 * 1000
NAMED_EVENT_CANDIDATE_WINDOW_MS = 30 * 24 * 60 * 60 * 1000
STORY_MATCH_THRESHOLD = 0.62
STORY_STRONG_THRESHOLD = 0.83
STORY_RUNNER_UP_MARGIN = 0.08

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_TRACKING_QUERY_PREFIXES = ("utm_", "ref_", "campaign")
_TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "source",
    "spm",
    "ocid",
}
_SOURCE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|:]\s*)?(?:reuters|associated press|ap news|bbc(?: news)?|"
    r"al jazeera|financial times|cnbc|bloomberg|the guardian|cnn|"
    r"路透(?:社)?|美联社|新华社|央视新闻)\s*$",
    re.IGNORECASE,
)
_STATIC_PATHS = {
    "",
    "/",
    "/home",
    "/world",
    "/business",
    "/markets",
    "/news",
    "/latest",
    "/breaking-news",
}
_STATIC_TITLE_HINTS = (
    "breaking news & views",
    "latest news",
    "world news",
    "business news",
    "homepage",
    "home page",
    "新闻首页",
    "最新新闻",
)
_OPINION_HINTS = (
    "opinion",
    "commentary",
    "editorial",
    "column:",
    "analysis:",
    "观点",
    "评论",
    "社论",
    "专栏",
)
_ANALYSIS_HINTS = ("analysis", "explainer", "what it means", "深度", "解析", "解读")
_LIVE_HINTS = ("live:", "live updates", "liveblog", "实时更新", "直播")
_CORRECTION_HINTS = ("correction", "corrected", "retract", "更正", "勘误", "撤回")
_BACKGROUND_HINTS = ("explainer", "background", "timeline", "what to know", "背景", "回顾")
_RETROSPECTIVE_HINTS = ("anniversary", "one year on", "retrospective", "周年", "复盘")
_ATTRIBUTION_HINTS = (
    "according to reuters",
    "source: reuters",
    "according to ap",
    "source: ap",
    "据路透",
    "路透称",
    "据美联社",
    "来源：",
    "原文",
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "says",
    "said",
    "the",
    "to",
    "with",
    "after",
    "amid",
    "new",
    "latest",
    "breaking",
    "news",
    "update",
    "updates",
    "live",
    "if",
}

_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "us": ("united states", "u.s.", "u.s ", "america", "美国", "美方"),
    "china": (
        "people's republic of china",
        "prc",
        "chinese",
        "中国",
        "中方",
        "北京",
    ),
    "russia": (
        "russian federation",
        "russian",
        "俄罗斯",
        "俄方",
        "莫斯科",
    ),
    "ukraine": ("ukrainian", "乌克兰", "基辅"),
    "israel": ("以色列",),
    "iran": ("iranian", "伊朗", "德黑兰"),
    "chile": ("智利",),
    "peru": ("秘鲁",),
    "nigeria": ("尼日利亚",),
    "kenya": ("肯尼亚",),
    "lebanon": ("黎巴嫩",),
    "venezuela": ("委内瑞拉",),
    "turkey": ("türkiye", "turkiye", "土耳其"),
    "argentina": ("阿根廷",),
    "brazil": (
        "brazilian",
        "巴西",
    ),
    "mexico": ("mexican", "墨西哥"),
    "kazakhstan": ("kazakh", "哈萨克斯坦"),
    "saudi_arabia": (
        "saudi",
        "沙特阿拉伯",
        "沙特",
    ),
    "japan": ("日本", "东京"),
    "eu": ("european union", "欧盟", "brussels", "布鲁塞尔"),
    "uk": ("united kingdom", "britain", "英国", "伦敦"),
    "fed": ("federal reserve", "fomc", "美联储", "联储"),
    "ecb": ("european central bank", "欧洲央行"),
    "boj": ("bank of japan", "日本央行"),
    # Bare ``央行`` means "central bank" and must not be treated as PBOC: it
    # appears inside names such as ``欧洲央行`` and would create a false entity
    # conflict across languages.
    "pboc": ("people's bank of china", "中国人民银行"),
    "un": ("united nations", "联合国"),
    "nato": ("北约",),
    "apple": ("苹果公司", "apple inc"),
    "google": ("alphabet", "谷歌"),
    "microsoft": ("微软",),
    "nvidia": ("英伟达",),
    "openai": ("open ai",),
    "opec": ("opec+", "欧佩克"),
    "hezbollah": ("真主党",),
    "houthis": ("houthi", "胡塞武装", "胡塞"),
}

_ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "increase": (
        "raise",
        "raises",
        "raised",
        "hike",
        "hikes",
        "increase",
        "increases",
        "加息",
        "提高",
        "上调",
    ),
    "decrease": (
        "cut",
        "cuts",
        "lower",
        "lowers",
        "decrease",
        "降息",
        "降低",
        "下调",
        "fall",
        "falls",
        "fell",
    ),
    "hold": ("hold", "holds", "held", "steady", "维持", "不变"),
    "propose": ("propose", "proposes", "proposal", "plan", "draft", "提议", "计划", "草案"),
    "approve": (
        "approve",
        "approves",
        "approved",
        "pass",
        "passes",
        "adopt",
        "通过",
        "批准",
    ),
    "reject": ("reject", "rejects", "rejected", "veto", "否决", "拒绝"),
    "sign": (
        "sign",
        "signs",
        "signed",
        "签署",
    ),
    "implement": (
        "implement",
        "implements",
        "effective",
        "takes effect",
        "enforce",
        "实施",
        "生效",
        "执行",
    ),
    "sanction": (
        "sanction",
        "sanctions",
        "制裁",
    ),
    "attack": ("attack", "attacks", "strike", "strikes", "bomb", "袭击", "打击", "空袭"),
    "retaliate": (
        "retaliate",
        "retaliates",
        "retaliated",
        "retaliation",
        "responds with strikes",
        "报复",
        "反击",
    ),
    "ceasefire": (
        "ceasefire",
        "truce",
        "停火",
    ),
    "elect": ("elect", "elected", "wins election", "当选", "赢得选举"),
    "resign": ("resign", "resigns", "辞职", "下台"),
    "acquire": ("acquire", "acquires", "buyout", "merger", "收购", "并购"),
    "default": ("default", "defaults", "bankrupt", "bankruptcy", "违约", "破产"),
    "disrupt": ("disrupt", "halt", "shutdown", "suspend", "中断", "暂停", "关闭"),
    "confirm": ("confirm", "confirms", "confirmed", "announce", "announces", "确认", "宣布"),
    "correct": ("correction", "corrected", "retract", "更正", "撤回"),
    "threaten": ("threaten", "threatens", "threatened", "warn", "warns", "威胁", "警告"),
    "close": ("close", "closes", "closed", "closure", "封锁", "关闭"),
    "seize": ("seize", "seizes", "seized", "扣押", "夺取"),
    "impose": ("impose", "imposes", "imposed", "加征", "施加"),
    "lift": (
        "lift",
        "lifts",
        "lifted",
        "lifting",
        "remove",
        "removes",
        "解除",
        "取消",
    ),
    "recall": ("recall", "recalls", "recalled", "召回"),
    "allow": ("allow", "allows", "allowed", "permit", "permits", "允许"),
    "kill": ("kill", "kills", "killed", "致死", "击毙"),
    "order": ("order", "orders", "ordered", "下令"),
    "sell_off": ("sell-off", "sell off", "selloff", "抛售"),
    "react": ("reaction", "react", "reacts", "responds to", "回应"),
    "deny": ("deny", "denies", "denied", "拒发", "拒绝"),
    "freeze": ("freeze", "freezes", "frozen", "冻结"),
    "investigate": (
        "investigate",
        "investigates",
        "investigated",
        "under investigation",
        "调查",
    ),
    "fine": (
        "fine",
        "fines",
        "fined",
        "penalty",
        "penalties",
        "罚款",
        "处罚",
    ),
    "transfer": (
        "transfer",
        "move to",
        "agrees move",
        "join",
        "joins",
        "to join",
        "转会",
    ),
    "end": ("end", "ends", "ended", "终止", "结束"),
    "earthquake": ("earthquake", "earthquakes", "quake", "地震"),
    "unveil": ("unveil", "unveils", "unveiled", "发布", "推出"),
    "protest": ("protest", "protests", "protested", "抗议"),
}

_EVENT_OBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "interest_rates": (
        "interest rate",
        "interest rates",
        "rates",
        "policy rate",
        "borrowing costs",
        "利率",
        "政策利率",
    ),
    "tariffs": ("tariff", "tariffs", "关税"),
    "exports": ("export", "exports", "出口"),
    "imports": ("import", "imports", "进口"),
    "inflation": (
        "inflation",
        "consumer prices",
        "cpi",
        "通胀",
        "消费者价格",
    ),
    "gdp": ("gdp", "gross domestic product", "国内生产总值"),
    "employment": (
        "employment",
        "jobs",
        "payrolls",
        "unemployment",
        "就业",
        "非农",
        "失业",
    ),
    "oil_output": (
        "oil output",
        "oil production",
        "crude output",
        "原油产量",
        "石油产量",
    ),
    "oil_prices": ("oil price", "oil prices", "crude prices", "油价"),
    "semiconductors": ("semiconductor", "semiconductors", "chips", "芯片", "半导体"),
    "airspace": ("airspace", "领空"),
    "border": ("border", "边境"),
    "embassy": ("embassy", "consulate", "大使馆", "领事馆"),
    "ambassador": ("ambassador", "envoy", "大使"),
    "bonds": ("bond", "bonds", "债券"),
    "museum_exhibits": ("smithsonian", "museum exhibit", "museum exhibits", "博物馆展品"),
    "visas": ("visa", "visas", "签证"),
    "military_expansion": (
        "army expansion",
        "military expansion",
        "expand the army",
        "扩军",
    ),
    "football_transfer": (
        "transfer",
        "move to",
        "agrees move",
        "join",
        "to join",
        "转会",
    ),
    "securities_regulator": (
        "securities regulator",
        "securities regulatory",
        "证券监管",
    ),
    "monopoly": ("monopoly", "monopoly abuses", "垄断"),
}

_STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "expected": ("expected", "forecast", "likely", "预计", "预期", "可能"),
    "proposed": ("proposal", "proposed", "draft", "提议", "草案"),
    "approved": ("approved", "passed", "adopted", "通过", "获批"),
    "signed": ("signed", "签署"),
    "implemented": ("implemented", "effective", "takes effect", "实施", "生效"),
    "result": (
        "result",
        "actual",
        "came in",
        "comes in",
        "recorded",
        "wins",
        "elected",
        "结果",
        "实际",
        "录得",
        "当选",
    ),
}

_ACTION_CONFLICTS = {
    frozenset(("increase", "decrease")),
    frozenset(("approve", "reject")),
    frozenset(("attack", "retaliate")),
    frozenset(("propose", "implement")),
    frozenset(("hold", "increase")),
    frozenset(("hold", "decrease")),
    frozenset(("seize", "threaten")),
    frozenset(("seize", "close")),
    frozenset(("impose", "lift")),
    frozenset(("impose", "react")),
    frozenset(("allow", "kill")),
}
_EVENT_OBJECT_CONFLICTS = {
    frozenset(("exports", "imports")),
    frozenset(("inflation", "gdp")),
    frozenset(("inflation", "employment")),
    frozenset(("gdp", "employment")),
    frozenset(("interest_rates", "inflation")),
    frozenset(("interest_rates", "employment")),
    frozenset(("interest_rates", "gdp")),
    frozenset(("oil_output", "oil_prices")),
}
_STAGE_ORDER = {
    "expected": 0,
    "proposed": 1,
    "approved": 2,
    "signed": 3,
    "implemented": 4,
    "result": 5,
}
_MISSION_TERMS = {
    "policy": (
        "policy",
        "government",
        "parliament",
        "congress",
        "election",
        "sanction",
        "diplomacy",
        "政策",
        "政府",
        "议会",
        "选举",
        "制裁",
        "外交",
    ),
    "macro": (
        "inflation",
        "gdp",
        "growth",
        "employment",
        "rates",
        "central bank",
        "trade",
        "tariff",
        "通胀",
        "经济增长",
        "就业",
        "央行",
        "利率",
        "贸易",
        "关税",
    ),
    "market": (
        "market",
        "stocks",
        "bonds",
        "currency",
        "oil",
        "gas",
        "commodity",
        "bank",
        "金融",
        "市场",
        "股票",
        "债券",
        "汇率",
        "原油",
        "天然气",
        "大宗商品",
        "银行",
    ),
    "geopolitical": (
        "war",
        "attack",
        "military",
        "ceasefire",
        "nuclear",
        "border",
        "战争",
        "袭击",
        "军事",
        "停火",
        "核",
        "边境",
    ),
    "systemic": (
        "default",
        "bankruptcy",
        "bank run",
        "financial stability",
        "supply chain",
        "emergency",
        "违约",
        "破产",
        "挤兑",
        "金融稳定",
        "供应链",
        "紧急状态",
    ),
}


@dataclass(frozen=True, slots=True)
class StoryProjection:
    representative_revision_id: str
    title: str
    snippet: str
    languages: tuple[str, ...]
    event_core: dict[str, object]
    first_seen_at_ms: int
    last_material_evidence_at_ms: int
    material_evolution_state: str
    lifecycle: str
    breaking: bool
    impact_profile: dict[str, object]
    priority_profile: dict[str, object]
    impact_score: int
    priority_score: int
    evidence_posture: str
    evidence_factors: dict[str, object]
    article_count: int
    primary_member_count: int
    contextual_member_count: int
    reporting_origin_count: int
    independent_origin_count: int
    syndicated_article_count: int
    material_evidence_hash: str
    presentation_state_hash: str
    brief_eligible: bool
    brief_eligibility_reason: dict[str, object]
    profile: dict[str, object]
    profile_hash: str


def admit_feed_entry(
    *,
    source: NewsSourceDefinition,
    entry: NewsFeedEntry,
    observed_at_ms: int,
) -> tuple[AdmittedFeedObservation | None, str | None]:
    title = normalize_display_text(entry.title or "")
    if not title:
        return None, "invalid_title"
    normalized_url = normalize_url(entry.link)
    if normalized_url is None:
        return None, "invalid_url"
    if _is_static_entry(normalized_url=normalized_url, title=title):
        return None, "static_or_section_page"
    published_at_ms = entry.published_at_ms
    if published_at_ms is None:
        return None, "missing_source_time"
    if published_at_ms > observed_at_ms + ARTICLE_FUTURE_SKEW_MS:
        return None, "future_source_time"
    if observed_at_ms - published_at_ms > ARTICLE_MAX_AGE_MS:
        return None, "stale_source_time"
    if not _normalizable_identity(entry.guid, normalized_url, title, published_at_ms):
        return None, "identity_unavailable"
    summary = normalize_display_text(entry.summary)
    language = normalize_language(entry.language or source.default_language)
    source_entry_key = _source_entry_key(entry.guid, normalized_url)
    raw_entry = dict(entry.raw)
    revision_payload = {
        "url": normalized_url,
        "title": title,
        "summary": summary,
        "published_at_ms": published_at_ms,
        "language": language,
    }
    observation_revision_hash = sha256_json(revision_payload)
    observation_id = deterministic_id(
        "observation",
        source.source_id,
        source_entry_key,
        observation_revision_hash,
    )
    return (
        AdmittedFeedObservation(
            observation_id=observation_id,
            source_id=source.source_id,
            source_entry_key=source_entry_key,
            observation_revision_hash=observation_revision_hash,
            source_guid=normalize_optional_text(entry.guid),
            raw_url=str(entry.link).strip(),
            normalized_url=normalized_url,
            title=title,
            summary=summary,
            source_published_at_ms=published_at_ms,
            observed_at_ms=observed_at_ms,
            language=language,
            raw_entry=raw_entry,
        ),
        None,
    )


def article_id_for(
    *,
    publisher_organization_id: str,
    normalized_url: str,
    first_observation_id: str,
) -> tuple[str, str]:
    incarnation_key = deterministic_id("incarnation", first_observation_id)
    return (
        deterministic_id(
            "article",
            ARTICLE_IDENTITY_VERSION,
            publisher_organization_id,
            normalized_url,
            incarnation_key,
        ),
        incarnation_key,
    )


def article_revision_id(
    *,
    article_id: str,
    content_hash: str,
) -> str:
    return deterministic_id("article-revision", article_id, content_hash)


def extract_identity_features(
    *,
    revision_id: str,
    article_id: str,
    title: str,
    snippet: str,
    language: str,
) -> ArticleIdentityFeatures:
    cleaned_title = normalize_match_text(_SOURCE_SUFFIX_RE.sub("", title))
    cleaned_lead = normalize_match_text(snippet)
    combined = f"{cleaned_title} {cleaned_lead}".strip()
    tokens = tuple(sorted(_tokens(combined)))
    bigrams = tuple(sorted(_bigrams(cleaned_title)))
    chargrams = tuple(sorted(_chargrams(cleaned_title)))
    ordered_entities = _canonical_matches_in_order(combined, _ENTITY_ALIASES)
    entities = tuple(sorted(ordered_entities))
    actions = _identity_actions(combined)
    if "earthquake" in actions and "attack" in actions:
        actions = tuple(action for action in actions if action != "attack")
    if "protest" in actions and "attack" in actions:
        actions = tuple(action for action in actions if action != "attack")
    actor_entities: tuple[str, ...]
    target_entities: tuple[str, ...]
    if actions == ("earthquake",):
        actor_entities = ()
        target_entities = ()
    else:
        actor_entities = ordered_entities[:1] if actions and ordered_entities else ()
        target_entities = ordered_entities[1:] if actions and len(ordered_entities) > 1 else ()
    event_objects = _canonical_matches(combined, _EVENT_OBJECT_ALIASES)
    stages = _canonical_matches(combined, _STAGE_ALIASES)
    locations = tuple(
        entity
        for entity in entities
        if entity
        in {
            "us",
            "china",
            "russia",
            "ukraine",
            "israel",
            "iran",
            "turkey",
            "argentina",
            "brazil",
            "mexico",
            "kazakhstan",
            "saudi_arabia",
            "japan",
            "chile",
            "peru",
            "nigeria",
            "kenya",
            "lebanon",
            "venezuela",
            "eu",
            "uk",
        }
    )
    quantities = tuple(_quantities(combined))
    named_event_keys = tuple(sorted(_named_event_keys(combined)))
    temporal_episode_keys = tuple(sorted(_temporal_episode_keys(cleaned_title)))
    event_parts = (
        *actor_entities[:2],
        *actions[:2],
        *event_objects[:2],
        *target_entities[:2],
        *locations[:2],
        *stages[:1],
        *named_event_keys[:2],
        *temporal_episode_keys[:1],
    )
    event_key = "|".join(event_parts)
    if not event_key:
        event_key = "|".join(tokens[:8])
    lexical_signature = " ".join(tokens)
    content_fingerprint = deterministic_id("content", cleaned_title, cleaned_lead)
    feature_payload = {
        "identity_version": ARTICLE_IDENTITY_VERSION,
        "normalized_title": cleaned_title,
        "normalized_lead": cleaned_lead,
        "entities": entities,
        "actor_entities": actor_entities,
        "target_entities": target_entities,
        "actions": actions,
        "event_objects": event_objects,
        "locations": locations,
        "stages": stages,
        "quantities": quantities,
        "tokens": tokens,
        "bigrams": bigrams,
        "chargrams": chargrams,
        "named_event_keys": named_event_keys,
        "temporal_episode_keys": temporal_episode_keys,
        "event_key": event_key,
    }
    return ArticleIdentityFeatures(
        revision_id=revision_id,
        article_id=article_id,
        language=normalize_language(language),
        normalized_title=cleaned_title,
        normalized_lead=cleaned_lead,
        content_fingerprint=content_fingerprint,
        lexical_signature=lexical_signature,
        event_key=event_key,
        named_event_keys=named_event_keys,
        temporal_episode_keys=temporal_episode_keys,
        entities=entities,
        actor_entities=actor_entities,
        target_entities=target_entities,
        actions=actions,
        event_objects=event_objects,
        locations=locations,
        stages=stages,
        quantities=quantities,
        tokens=tokens,
        bigrams=bigrams,
        chargrams=chargrams,
        extraction_receipt={
            "version": ARTICLE_IDENTITY_VERSION,
            "unknown_entities": not entities,
            "unknown_actions": not actions,
            "source_suffix_removed": cleaned_title != normalize_match_text(title),
        },
        feature_hash=sha256_json(feature_payload),
    )


def confirmed_url_reuse(
    *,
    current_title: str,
    current_snippet: str,
    current_source_published_at_ms: int,
    current_language: str,
    new_title: str,
    new_snippet: str,
    new_source_published_at_ms: int,
    new_language: str,
) -> bool:
    """Return true only for a high-confidence publisher URL incarnation break.

    A large source-time gap alone is insufficient. The new artifact must also
    carry a deterministic event-identity conflict against the current artifact
    and have very low lexical overlap. Everything else remains a revision and
    is later quarantined if Story identity is ambiguous.
    """

    if new_source_published_at_ms - current_source_published_at_ms < URL_REUSE_MIN_SOURCE_TIME_GAP_MS:
        return False
    current = extract_identity_features(
        revision_id="url-reuse-current",
        article_id="url-reuse-current",
        title=current_title,
        snippet=current_snippet,
        language=current_language,
    )
    incoming = extract_identity_features(
        revision_id="url-reuse-incoming",
        article_id="url-reuse-incoming",
        title=new_title,
        snippet=new_snippet,
        language=new_language,
    )
    current_core = {
        "entities": current.entities,
        "actions": current.actions,
        "locations": current.locations,
        "stages": current.stages,
    }
    conflicts = _hard_conflicts(incoming, current_core)
    lexical_overlap = _jaccard(set(current.tokens), set(incoming.tokens))
    return bool(conflicts) and lexical_overlap < 0.2


def build_story_profile(
    members: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not members:
        raise ValueError("news_story_members_required")
    features = [_feature_mapping(member) for member in members]
    entities = _supported_values(features, "entities")
    actions = _supported_values(features, "actions")
    event_objects = _supported_values(features, "event_objects")
    locations = _supported_values(features, "locations")
    stages = _supported_values(features, "stages")
    named_event_keys = _supported_values(features, "named_event_keys")
    temporal_episode_keys = _supported_values(features, "temporal_episode_keys")
    fingerprints = sorted(
        {
            str(feature.get("content_fingerprint", ""))
            for feature in features
            if str(feature.get("content_fingerprint", ""))
        }
    )
    member_signatures = sorted(
        {str(feature.get("lexical_signature", "")) for feature in features if str(feature.get("lexical_signature", ""))}
    )
    quantities: list[dict[str, object]] = []
    for feature in features:
        for quantity in _dict_sequence(feature.get("quantities")):
            if quantity not in quantities:
                quantities.append(quantity)
    profile: dict[str, object] = {
        "version": STORY_IDENTITY_VERSION,
        "core_anchors": {
            "entities": entities,
            "actor_entities": _supported_values(features, "actor_entities"),
            "target_entities": _supported_values(features, "target_entities"),
            "actions": actions,
            "event_objects": event_objects,
            "locations": locations,
            "stages": stages,
            "named_event_keys": named_event_keys,
            "temporal_episode_keys": temporal_episode_keys,
            "numeric_constraints": quantities,
        },
        "numeric_constraints": quantities,
        "content_fingerprints": fingerprints,
        "member_signatures": member_signatures,
        "member_revision_ids": sorted(str(member["revision_id"]) for member in members),
        "languages": sorted({str(member["language"]) for member in members}),
    }
    profile["profile_hash"] = sha256_json(profile)
    return profile


def decide_story(
    *,
    article: ArticleIdentityFeatures,
    candidates: Sequence[Mapping[str, object]],
) -> StoryIdentityDecision:
    scored = tuple(
        sorted(
            (_score_candidate(article, candidate) for candidate in candidates),
            key=lambda item: (-item.final_score, item.story_id),
        )
    )
    viable = tuple(candidate for candidate in scored if not candidate.hard_conflicts)
    if not scored:
        return StoryIdentityDecision(
            verdict="no_candidate_new_story",
            match_method="seed",
            match_score=0,
            runner_up_margin=1,
            reason={"candidate_count": 0},
        )
    if not viable:
        return StoryIdentityDecision(
            verdict="reject_conflict",
            match_method="hard_conflict",
            match_score=0,
            runner_up_margin=1,
            candidates=scored,
            reason={"conflicts": sorted({item for candidate in scored for item in candidate.hard_conflicts})},
        )
    best = viable[0]
    runner_up_score = viable[1].final_score if len(viable) > 1 else 0.0
    margin = max(0.0, min(1.0, best.final_score - runner_up_score))
    if best.strong_proofs and best.final_score >= STORY_STRONG_THRESHOLD and margin >= STORY_RUNNER_UP_MARGIN:
        return StoryIdentityDecision(
            verdict="accept_strong",
            selected_story_id=best.story_id,
            match_method=best.strong_proofs[0],
            match_score=best.final_score,
            runner_up_margin=margin,
            candidates=scored,
            reason={"strong_proofs": list(best.strong_proofs)},
        )
    if best.final_score >= STORY_MATCH_THRESHOLD and margin >= STORY_RUNNER_UP_MARGIN:
        return StoryIdentityDecision(
            verdict="accept_scored",
            selected_story_id=best.story_id,
            match_method="constraint_scored",
            match_score=best.final_score,
            runner_up_margin=margin,
            candidates=scored,
            reason={
                "threshold": STORY_MATCH_THRESHOLD,
                "required_margin": STORY_RUNNER_UP_MARGIN,
            },
        )
    return StoryIdentityDecision(
        verdict="ambiguous_new_story",
        match_method="ambiguous",
        match_score=best.final_score,
        runner_up_margin=margin,
        candidates=scored,
        reason={
            "threshold": STORY_MATCH_THRESHOLD,
            "required_margin": STORY_RUNNER_UP_MARGIN,
            "best_story_id": best.story_id,
        },
    )


def classify_member_semantics(
    *,
    source: Mapping[str, object],
    revision: Mapping[str, object],
    features: ArticleIdentityFeatures,
    existing_members: Sequence[Mapping[str, object]],
    is_seed: bool,
) -> MemberSemantics:
    text = f"{revision.get('title', '')} {revision.get('snippet', '')}".lower()
    canonical_url = str(revision.get("canonical_url") or "")
    content_form = _content_form(text=text, canonical_url=canonical_url)
    existing_origins = {
        str(member.get("reporting_origin_id") or "")
        for member in existing_members
        if str(member.get("reporting_origin_id") or "")
    }
    publisher = str(source.get("publisher_organization_id") or source.get("source_chain_id") or "")
    same_fingerprint = any(
        str(member.get("content_fingerprint") or "") == features.content_fingerprint for member in existing_members
    )
    attributed = any(hint in text for hint in _ATTRIBUTION_HINTS)
    source_role = str(source.get("source_role") or "")
    reporting_origin_id: str | None
    if is_seed and source_role != "trusted_aggregator":
        origin_relation = "originating"
        origin_confidence = 0.9
        reporting_origin_id = publisher
    elif same_fingerprint or attributed:
        origin_relation = "syndicated"
        origin_confidence = 0.92 if same_fingerprint else 0.78
        reporting_origin_id = _attributed_origin(existing_members) or publisher
    elif source_role == "trusted_aggregator":
        origin_relation = "derived" if attributed else "unresolved"
        origin_confidence = 0.65 if attributed else 0.2
        reporting_origin_id = _attributed_origin(existing_members)
    elif publisher and publisher not in existing_origins:
        origin_relation = "independent" if existing_members else "originating"
        origin_confidence = 0.8
        reporting_origin_id = publisher
    else:
        origin_relation = "unresolved"
        origin_confidence = 0.35
        reporting_origin_id = publisher or None

    if any(hint in text for hint in _CORRECTION_HINTS):
        development_relation = "correction"
    elif any(hint in text for hint in _RETROSPECTIVE_HINTS):
        development_relation = "retrospective"
    elif any(hint in text for hint in _BACKGROUND_HINTS):
        development_relation = "background"
    elif is_seed:
        development_relation = "initial"
    else:
        development_relation = "follow_up"

    if content_form == "opinion":
        epistemic_use = "viewpoint"
    elif content_form in {"analysis"} or development_relation in {"background", "retrospective"}:
        epistemic_use = "context"
    elif content_form == "static" or (content_form == "live" and not features.actions):
        epistemic_use = "non_evidence"
    else:
        epistemic_use = "fact_evidence"

    return MemberSemantics(
        content_form=content_form,
        origin_relation=origin_relation,
        development_relation=development_relation,
        epistemic_use=epistemic_use,
        reporting_origin_id=reporting_origin_id,
        origin_confidence=origin_confidence,
        reason={
            "version": STORY_MEMBER_SEMANTICS_VERSION,
            "same_content_fingerprint": same_fingerprint,
            "explicit_attribution": attributed,
            "source_role": source_role,
            "publisher_organization_id": publisher,
        },
    )


def project_story(
    members: Sequence[Mapping[str, object]],
    *,
    now_ms: int,
    previous: Mapping[str, object] | None = None,
) -> StoryProjection:
    if not members:
        raise ValueError("news_story_members_required")
    ordered = sorted(
        members,
        key=lambda row: (
            _int(row.get("observed_at_ms")),
            str(row.get("revision_id")),
        ),
    )
    profile = build_story_profile(ordered)
    representative = max(ordered, key=_representative_rank)
    primary = [member for member in ordered if str(member.get("membership_kind")) == "primary"]
    contextual = [member for member in ordered if str(member.get("membership_kind")) == "contextual"]
    fact_members = [member for member in primary if str(member.get("epistemic_use")) == "fact_evidence"]
    material_members = [
        member
        for member in fact_members
        if str(member.get("origin_relation")) != "syndicated" or str(member.get("development_relation")) == "correction"
    ]
    origins = {
        str(member.get("reporting_origin_id"))
        for member in fact_members
        if str(member.get("reporting_origin_id") or "")
        and str(member.get("origin_relation")) not in {"syndicated", "derived", "unresolved"}
        and _float(member.get("origin_confidence")) >= 0.7
    }
    reporting_origins = {
        str(member.get("reporting_origin_id"))
        for member in fact_members
        if str(member.get("reporting_origin_id") or "")
    }
    syndicated_count = sum(1 for member in primary if str(member.get("origin_relation")) == "syndicated")
    has_primary_authority = any(
        str(member.get("source_role")) == "official_authority" and str(member.get("epistemic_use")) == "fact_evidence"
        for member in primary
    )
    correction_members = [member for member in primary if str(member.get("development_relation")) == "correction"]
    withdrawn = any(
        "retract" in f"{member.get('title', '')} {member.get('snippet', '')}".lower()
        or "撤回" in f"{member.get('title', '')} {member.get('snippet', '')}"
        for member in correction_members
    )
    conflict_factors = _story_conflicts(fact_members)
    if withdrawn:
        evidence_posture = "withdrawn"
    elif correction_members:
        evidence_posture = "corrected"
    elif conflict_factors:
        evidence_posture = "contested"
    elif has_primary_authority:
        evidence_posture = "primary_source_confirmed"
    elif len(origins) >= 2:
        evidence_posture = "independently_corroborated"
    else:
        evidence_posture = "single_origin_reported"
    evidence_factors: dict[str, object] = {
        "independent_origin_count": len(origins),
        "reporting_origin_count": len(reporting_origins),
        "syndicated_article_count": syndicated_count,
        "has_primary_authority": has_primary_authority,
        "has_material_conflict": bool(conflict_factors),
        "material_conflicts": conflict_factors,
        "has_correction_or_retraction": bool(correction_members),
        "source_quality_distribution": dict(Counter(str(member.get("trust_tier") or "unknown") for member in primary)),
    }
    material_payload = {
        "identity_version": STORY_IDENTITY_VERSION,
        "material_evidence": sorted(
            {
                (
                    str(member.get("article_id")),
                    str(_feature_mapping(member).get("feature_hash") or ""),
                    str(member.get("epistemic_use")),
                    str(member.get("origin_relation")),
                    str(member.get("development_relation")),
                    str(member.get("reporting_origin_id") or ""),
                )
                for member in material_members
            }
        ),
        "material_evidence_factors": {
            "independent_origin_count": len(origins),
            "has_primary_authority": has_primary_authority,
            "material_conflicts": sorted(
                (
                    str(conflict.get("left_article_id") or ""),
                    str(conflict.get("right_article_id") or ""),
                    str(conflict.get("kind") or ""),
                    tuple(str(value) for value in _sequence(conflict.get("values"))),
                )
                for conflict in conflict_factors
            ),
            "correction_evidence": sorted(
                (
                    str(member.get("article_id")),
                    str(_feature_mapping(member).get("feature_hash") or ""),
                )
                for member in correction_members
            ),
            "withdrawn": withdrawn,
        },
    }
    material_evidence_hash = sha256_json(material_payload)
    first_seen_at_ms = min(_int(member.get("observed_at_ms")) for member in ordered)
    last_material_evidence_at_ms = max(_int(member.get("observed_at_ms")) for member in (material_members or primary))
    material_evolution_state = _material_evolution(
        primary=material_members,
        correction=bool(correction_members),
        conflict=bool(conflict_factors),
        independent_origins=len(origins),
    )
    if previous and str(previous.get("material_evidence_hash") or "") == material_evidence_hash:
        last_material_evidence_at_ms = _int(previous.get("last_material_evidence_at_ms"))
        material_evolution_state = str(previous.get("material_evolution_state") or material_evolution_state)
    previous_lifecycle = str(previous.get("lifecycle")) if previous else None
    lifecycle = story_lifecycle(
        last_material_evidence_at_ms=last_material_evidence_at_ms,
        now_ms=now_ms,
        previous_lifecycle=previous_lifecycle,
        material_evolution_state=material_evolution_state,
    )
    combined_text = " ".join(
        f"{member.get('title', '')} {member.get('snippet', '')}" for member in fact_members
    ).lower()
    impact_profile, impact_score = story_impact(
        text=combined_text,
        evidence_factors=evidence_factors,
    )
    priority_profile, priority_score = story_priority(
        impact_score=impact_score,
        lifecycle=lifecycle,
        evidence_posture=evidence_posture,
        last_material_evidence_at_ms=last_material_evidence_at_ms,
        now_ms=now_ms,
        material_evolution_state=material_evolution_state,
    )
    breaking = (
        now_ms - last_material_evidence_at_ms <= 30 * 60 * 1000
        and impact_score >= 70
        and bool(fact_members)
        and evidence_posture != "withdrawn"
    )
    presentation_state_hash = sha256_json(
        {
            "lifecycle": lifecycle,
            "breaking": breaking,
            "priority_profile": priority_profile,
            "representative_revision_id": str(representative["revision_id"]),
        }
    )
    eligibility_reasons: list[str] = []
    if not fact_members:
        eligibility_reasons.append("no_fact_evidence")
    if impact_score < 45:
        eligibility_reasons.append("impact_below_floor")
    if lifecycle == "dormant":
        eligibility_reasons.append("dormant")
    if evidence_posture == "withdrawn":
        eligibility_reasons.append("withdrawn")
    brief_eligible = not eligibility_reasons
    return StoryProjection(
        representative_revision_id=str(representative["revision_id"]),
        title=str(representative["title"]),
        snippet=str(representative.get("snippet") or ""),
        languages=tuple(sorted({str(member.get("language") or "und") for member in ordered})),
        event_core=dict(_mapping(profile["core_anchors"])),
        first_seen_at_ms=first_seen_at_ms,
        last_material_evidence_at_ms=last_material_evidence_at_ms,
        material_evolution_state=material_evolution_state,
        lifecycle=lifecycle,
        breaking=breaking,
        impact_profile=impact_profile,
        priority_profile=priority_profile,
        impact_score=impact_score,
        priority_score=priority_score,
        evidence_posture=evidence_posture,
        evidence_factors=evidence_factors,
        article_count=len({str(member.get("article_id")) for member in ordered}),
        primary_member_count=len(primary),
        contextual_member_count=len(contextual),
        reporting_origin_count=len(reporting_origins),
        independent_origin_count=len(origins),
        syndicated_article_count=syndicated_count,
        material_evidence_hash=material_evidence_hash,
        presentation_state_hash=presentation_state_hash,
        brief_eligible=brief_eligible,
        brief_eligibility_reason={
            "eligible": brief_eligible,
            "reasons": eligibility_reasons,
            "version": STORY_SCORING_VERSION,
        },
        profile=profile,
        profile_hash=str(profile["profile_hash"]),
    )


def story_lifecycle(
    *,
    last_material_evidence_at_ms: int,
    now_ms: int,
    previous_lifecycle: str | None,
    material_evolution_state: str,
) -> str:
    age_ms = max(0, now_ms - last_material_evidence_at_ms)
    if previous_lifecycle == "dormant" and material_evolution_state != "first_report":
        return "reactivated"
    if age_ms <= 30 * 60 * 1000:
        return "emerging" if material_evolution_state == "first_report" else "developing"
    if age_ms <= 6 * 60 * 60 * 1000:
        return "developing"
    if age_ms <= 24 * 60 * 60 * 1000:
        return "stable"
    if age_ms <= ARTICLE_MAX_AGE_MS:
        return "fading"
    return "dormant"


def story_impact(
    *,
    text: str,
    evidence_factors: Mapping[str, object],
) -> tuple[dict[str, object], int]:
    dimensions: dict[str, int] = {}
    for dimension, terms in _MISSION_TERMS.items():
        hits = sum(1 for term in terms if term in text)
        dimensions[dimension] = min(100, hits * 24)
    cross_region = min(
        100,
        25
        * len(
            {
                entity
                for entity, aliases in _ENTITY_ALIASES.items()
                if any(alias in text for alias in (entity, *aliases))
            }
        ),
    )
    dimensions["cross_region_scope"] = cross_region
    dimensions["evidence_quality"] = min(
        100,
        30
        + _int(evidence_factors.get("independent_origin_count")) * 25
        + (20 if evidence_factors.get("has_primary_authority") else 0),
    )
    material_dimensions = [
        dimensions["policy"],
        dimensions["macro"],
        dimensions["market"],
        dimensions["geopolitical"],
        dimensions["systemic"],
    ]
    strongest = sorted(material_dimensions, reverse=True)
    score = min(
        100,
        round(
            (strongest[0] if strongest else 0) * 0.55
            + (strongest[1] if len(strongest) > 1 else 0) * 0.2
            + cross_region * 0.15
            + dimensions["evidence_quality"] * 0.1
        ),
    )
    critical = (
        dimensions["systemic"] >= 48
        or dimensions["geopolitical"] >= 72
        or (dimensions["policy"] >= 48 and max(dimensions["macro"], dimensions["market"]) >= 48)
    )
    if critical:
        score = max(score, 75)
    return (
        {
            "version": STORY_SCORING_VERSION,
            "dimensions": dimensions,
            "critical": critical,
            "mission_relevant": max(material_dimensions, default=0) > 0,
        },
        score,
    )


def story_priority(
    *,
    impact_score: int,
    lifecycle: str,
    evidence_posture: str,
    last_material_evidence_at_ms: int,
    now_ms: int,
    material_evolution_state: str,
) -> tuple[dict[str, object], int]:
    age_hours = max(0.0, (now_ms - last_material_evidence_at_ms) / 3_600_000)
    recency = round(100 * math.exp(-age_hours / 24))
    lifecycle_factor = {
        "emerging": 90,
        "developing": 100,
        "reactivated": 95,
        "stable": 70,
        "fading": 35,
        "dormant": 0,
    }[lifecycle]
    evidence_factor = {
        "primary_source_confirmed": 90,
        "independently_corroborated": 100,
        "single_origin_reported": 55,
        "contested": 70,
        "corrected": 65,
        "withdrawn": 0,
    }[evidence_posture]
    novelty = 90 if material_evolution_state != "first_report" else 70
    score = round(
        impact_score * 0.45 + recency * 0.25 + lifecycle_factor * 0.15 + evidence_factor * 0.1 + novelty * 0.05
    )
    return (
        {
            "version": STORY_SCORING_VERSION,
            "impact": impact_score,
            "recency": recency,
            "lifecycle": lifecycle_factor,
            "evidence": evidence_factor,
            "novelty": novelty,
            "recent_brief_coverage": 0,
        },
        min(100, max(0, score)),
    )


def story_id_for_seed(article_id: str) -> str:
    return deterministic_id("story", STORY_IDENTITY_VERSION, article_id)


def identity_decision_id(revision_id: str) -> str:
    return deterministic_id("story-decision", STORY_IDENTITY_VERSION, revision_id)


def normalize_url(value: str | None) -> str | None:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = hostname
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_NAMES and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
        ],
        doseq=True,
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def normalize_display_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def normalize_match_text(value: object) -> str:
    text = normalize_display_text(value).casefold()
    text = re.sub(r"[^\w\u3400-\u9fff%.$+-]+", " ", text)
    return _SPACE_RE.sub(" ", text).strip()


def normalize_language(value: object) -> str:
    normalized = str(value or "und").strip().lower().replace("_", "-")
    return normalized.split("-", 1)[0] or "und"


def deterministic_id(namespace: str, *parts: object) -> str:
    digest = hashlib.sha256("\n".join((namespace, *(str(part) for part in parts))).encode()).hexdigest()
    return f"{namespace}_{digest[:32]}"


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _score_candidate(
    article: ArticleIdentityFeatures,
    candidate: Mapping[str, object],
) -> StoryCandidate:
    story_id = str(candidate["story_id"])
    profile = _mapping(candidate.get("profile"))
    core = _mapping(profile.get("core_anchors"))
    members = [_feature_mapping(member) for member in _mapping_sequence(candidate.get("member_features"))]
    conflicts = _hard_conflicts(article, core)
    member_scores = [_feature_similarity(article, member) for member in members]
    member_score = max(member_scores, default=0.0)
    core_score = _core_similarity(article, core)
    channel_hits = tuple(sorted(str(item) for item in _sequence(candidate.get("channel_hits"))))
    strong_proofs: list[str] = []
    proof_scores: list[float] = []
    fingerprints = {str(item) for item in _sequence(profile.get("content_fingerprints"))}
    if article.content_fingerprint in fingerprints:
        strong_proofs.append("content_fingerprint")
        proof_scores.append(0.99)
    member_proofs = [_member_proof(article, member) for member in members]
    for proof in member_proofs:
        path = str(proof["path"])
        if path and path not in strong_proofs:
            strong_proofs.append(path)
            proof_scores.append(_float(proof["score"]))
    final_score = min(1.0, member_score * 0.65 + core_score * 0.35)
    if proof_scores:
        final_score = max(final_score, *proof_scores)
    if conflicts:
        final_score = 0.0
    return StoryCandidate(
        story_id=story_id,
        channel_hits=channel_hits,
        member_score=round(member_score, 6),
        core_score=round(core_score, 6),
        final_score=round(final_score, 6),
        hard_conflicts=tuple(conflicts),
        strong_proofs=tuple(strong_proofs),
        reason={
            "member_count": len(members),
            "identity_version": STORY_IDENTITY_VERSION,
            "proof_ladder": member_proofs,
            "hard_conflict_veto": bool(conflicts),
        },
    )


def _member_proof(
    article: ArticleIdentityFeatures,
    member: Mapping[str, object],
) -> dict[str, object]:
    member_language = normalize_language(member.get("language"))
    same_language = normalize_language(article.language) == member_language
    member_title = str(member.get("normalized_title") or "")
    article_title_tokens = _tokens(article.normalized_title)
    member_title_tokens = _tokens(member_title)
    title_containment = _containment(article_title_tokens, member_title_tokens)
    similarity = _feature_similarity(article, member)
    shared = _shared_anchor_families(article, member)
    event_key_equal = bool(article.event_key and article.event_key == str(member.get("event_key") or ""))
    path = ""
    score = 0.0
    if same_language and article.normalized_title and article.normalized_title == member_title:
        path, score = "normalized_title_exact", 0.97
    elif (
        same_language
        and min(len(article_title_tokens), len(member_title_tokens)) >= 4
        and (
            title_containment >= 0.9
            or (
                title_containment >= 0.82
                and bool(
                    {
                        "entities",
                        "actor_entities",
                        "actions",
                        "event_objects",
                        "named_event_keys",
                        "temporal_episode_keys",
                        "quantities",
                    }
                    & shared
                )
            )
        )
    ):
        path, score = "normalized_title_containment", 0.93
    elif same_language and similarity >= 0.83 and (shared or title_containment >= 0.8):
        path, score = "same_language_member_similarity", 0.88
    elif (
        same_language
        and "named_event_keys" in shared
        and bool({"entities", "locations", "actions", "event_objects"} & shared)
    ):
        path, score = "named_event_anchors", 0.91
    elif same_language and title_containment >= 0.65 and {"actions", "event_objects"} <= shared:
        path, score = "action_object_containment", 0.88
    elif (
        same_language
        and event_key_equal
        and "actions" in shared
        and bool(
            {"event_objects", "named_event_keys", "temporal_episode_keys", "quantities", "stages"} & shared
            or {"actor_entities", "target_entities"} <= shared
        )
    ):
        path, score = "deterministic_event_anchors", 0.86
    elif (
        not same_language
        and event_key_equal
        and "actions" in shared
        and len(shared) >= 3
        and bool(
            {"actor_entities", "entities"} & shared
            and {"event_objects", "locations", "named_event_keys", "quantities"} & shared
        )
    ):
        path, score = "cross_language_deterministic_anchors", 0.95
    return {
        "revision_id": str(member.get("revision_id") or ""),
        "same_language": same_language,
        "path": path,
        "score": round(score, 6),
        "member_similarity": round(similarity, 6),
        "title_containment": round(title_containment, 6),
        "shared_anchor_families": sorted(shared),
        "event_key_equal": event_key_equal,
    }


def _shared_anchor_families(
    article: ArticleIdentityFeatures,
    member: Mapping[str, object],
) -> set[str]:
    families: dict[str, tuple[set[str], set[str]]] = {
        "entities": (set(article.entities), _string_set(member.get("entities"))),
        "actor_entities": (
            set(article.actor_entities),
            _string_set(member.get("actor_entities")),
        ),
        "target_entities": (
            set(article.target_entities),
            _string_set(member.get("target_entities")),
        ),
        "actions": (set(article.actions), _string_set(member.get("actions"))),
        "event_objects": (
            set(article.event_objects),
            _string_set(member.get("event_objects")),
        ),
        "locations": (set(article.locations), _string_set(member.get("locations"))),
        "stages": (set(article.stages), _string_set(member.get("stages"))),
        "named_event_keys": (
            set(article.named_event_keys),
            _string_set(member.get("named_event_keys")),
        ),
        "temporal_episode_keys": (
            set(article.temporal_episode_keys),
            _string_set(member.get("temporal_episode_keys")),
        ),
    }
    shared = {family for family, (left, right) in families.items() if left and right and not left.isdisjoint(right)}
    article_quantities = _material_quantities(article.quantities)
    member_quantities = _material_quantities(member.get("quantities"))
    if any(
        not article_quantities[kind].isdisjoint(member_quantities[kind])
        for kind in article_quantities.keys() & member_quantities.keys()
    ):
        shared.add("quantities")
    article_identity_quantities = _identity_quantities(article.quantities)
    member_identity_quantities = _identity_quantities(member.get("quantities"))
    if any(
        not article_identity_quantities[kind].isdisjoint(member_identity_quantities[kind])
        for kind in article_identity_quantities.keys() & member_identity_quantities.keys()
    ):
        shared.add("quantities")
    return shared


def _feature_similarity(
    article: ArticleIdentityFeatures,
    member: Mapping[str, object],
) -> float:
    tokens = _string_set(member.get("tokens"))
    bigrams = _string_set(member.get("bigrams"))
    chargrams = _string_set(member.get("chargrams"))
    entities = _string_set(member.get("entities"))
    actors = _string_set(member.get("actor_entities"))
    targets = _string_set(member.get("target_entities"))
    actions = _string_set(member.get("actions"))
    event_objects = _string_set(member.get("event_objects"))
    stages = _string_set(member.get("stages"))
    lexical = (
        _jaccard(set(article.tokens), tokens) * 0.35
        + _jaccard(set(article.bigrams), bigrams) * 0.25
        + _jaccard(set(article.chargrams), chargrams) * 0.15
    )
    anchor = (
        _jaccard(set(article.entities), entities) * 0.1
        + _jaccard(set(article.actor_entities), actors) * 0.03
        + _jaccard(set(article.target_entities), targets) * 0.02
        + _jaccard(set(article.actions), actions) * 0.08
        + _jaccard(set(article.event_objects), event_objects) * 0.08
        + _jaccard(set(article.stages), stages) * 0.02
    )
    containment = _containment(set(article.tokens), tokens)
    score = min(1.0, lexical + anchor + containment * 0.12)
    if min(len(article.tokens), len(tokens)) >= 4 and containment >= 0.9:
        score = max(score, 0.88)
    return score


def _core_similarity(
    article: ArticleIdentityFeatures,
    core: Mapping[str, object],
) -> float:
    entity = _jaccard(set(article.entities), _string_set(core.get("entities")))
    actor = _jaccard(
        set(article.actor_entities),
        _string_set(core.get("actor_entities")),
    )
    target = _jaccard(
        set(article.target_entities),
        _string_set(core.get("target_entities")),
    )
    action = _jaccard(set(article.actions), _string_set(core.get("actions")))
    event_object = _jaccard(
        set(article.event_objects),
        _string_set(core.get("event_objects")),
    )
    location = _jaccard(set(article.locations), _string_set(core.get("locations")))
    stage = _jaccard(set(article.stages), _string_set(core.get("stages")))
    named = _jaccard(
        set(article.named_event_keys),
        _string_set(core.get("named_event_keys")),
    )
    temporal = _jaccard(
        set(article.temporal_episode_keys),
        _string_set(core.get("temporal_episode_keys")),
    )
    return min(
        1.0,
        entity * 0.25
        + actor * 0.25
        + target * 0.1
        + action * 0.2
        + event_object * 0.1
        + location * 0.05
        + stage * 0.05
        + named * 0.1
        + temporal * 0.05,
    )


def _hard_conflicts(
    article: ArticleIdentityFeatures,
    core: Mapping[str, object],
) -> list[str]:
    conflicts: list[str] = []
    core_entities = _string_set(core.get("entities"))
    core_actors = _string_set(core.get("actor_entities"))
    core_locations = _string_set(core.get("locations"))
    core_actions = _string_set(core.get("actions"))
    core_event_objects = _string_set(core.get("event_objects"))
    core_stages = _string_set(core.get("stages"))
    core_named_events = _string_set(core.get("named_event_keys"))
    core_temporal_episodes = _string_set(core.get("temporal_episode_keys"))
    if article.locations and core_locations and set(article.locations).isdisjoint(core_locations):
        conflicts.append("location_conflict")
    if article.entities and core_entities and set(article.entities).isdisjoint(core_entities):
        conflicts.append("principal_entity_conflict")
    if article.actor_entities and core_actors and set(article.actor_entities).isdisjoint(core_actors):
        conflicts.append("actor_direction_conflict")
    conflicts.extend(
        f"action_conflict:{left}:{right}"
        for left in article.actions
        for right in core_actions
        if frozenset((left, right)) in _ACTION_CONFLICTS
    )
    if set(article.event_objects).isdisjoint(core_event_objects):
        conflicts.extend(
            f"event_object_conflict:{left}:{right}"
            for left in article.event_objects
            for right in core_event_objects
            if frozenset((left, right)) in _EVENT_OBJECT_CONFLICTS
        )
    if article.stages and core_stages:
        article_stage = max((_STAGE_ORDER.get(stage, -1) for stage in article.stages), default=-1)
        core_stage = max((_STAGE_ORDER.get(stage, -1) for stage in core_stages), default=-1)
        if article_stage >= 0 and core_stage >= 0 and abs(article_stage - core_stage) >= 2:
            conflicts.append("event_stage_conflict")
    if article.named_event_keys and core_named_events and set(article.named_event_keys).isdisjoint(core_named_events):
        conflicts.append("named_event_conflict")
    if (
        article.temporal_episode_keys
        and core_temporal_episodes
        and set(article.temporal_episode_keys).isdisjoint(core_temporal_episodes)
    ):
        conflicts.append("temporal_episode_conflict")
    article_quantities = _identity_quantities(article.quantities)
    core_quantities = _identity_quantities(core.get("numeric_constraints"))
    conflicts.extend(
        f"identity_quantity_conflict:{kind}"
        for kind in sorted(article_quantities.keys() & core_quantities.keys())
        if article_quantities[kind].isdisjoint(core_quantities[kind])
    )
    return sorted(set(conflicts))


def _story_conflicts(members: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    for index, left in enumerate(members):
        left_features = _feature_mapping(left)
        left_actions = _string_set(left_features.get("actions"))
        left_quantities = _material_quantities(left_features.get("quantities"))
        for right in members[index + 1 :]:
            right_features = _feature_mapping(right)
            right_actions = _string_set(right_features.get("actions"))
            right_quantities = _material_quantities(right_features.get("quantities"))
            conflicts.extend(
                {
                    "left_article_id": str(left.get("article_id")),
                    "right_article_id": str(right.get("article_id")),
                    "left_revision_id": str(left.get("revision_id")),
                    "right_revision_id": str(right.get("revision_id")),
                    "kind": "action_conflict",
                    "values": [left_action, right_action],
                }
                for left_action in left_actions
                for right_action in right_actions
                if frozenset((left_action, right_action)) in _ACTION_CONFLICTS
            )
            conflicts.extend(
                {
                    "left_article_id": str(left.get("article_id")),
                    "right_article_id": str(right.get("article_id")),
                    "left_revision_id": str(left.get("revision_id")),
                    "right_revision_id": str(right.get("revision_id")),
                    "kind": "numeric_conflict",
                    "quantity_kind": kind,
                    "values": [
                        sorted(left_quantities[kind]),
                        sorted(right_quantities[kind]),
                    ],
                }
                for kind in sorted(left_quantities.keys() & right_quantities.keys())
                if left_quantities[kind].isdisjoint(right_quantities[kind])
            )
    return conflicts


def _material_evolution(
    *,
    primary: Sequence[Mapping[str, object]],
    correction: bool,
    conflict: bool,
    independent_origins: int,
) -> str:
    if len(primary) <= 1:
        return "first_report"
    if correction:
        return "material_correction"
    if conflict:
        return "conflict_detected"
    if independent_origins >= 2:
        return "new_independent_origin"
    return "material_follow_up"


def _representative_rank(member: Mapping[str, object]) -> tuple[object, ...]:
    evidence_rank = {
        "fact_evidence": 4,
        "context": 2,
        "viewpoint": 1,
        "non_evidence": 0,
    }
    origin_rank = {
        "originating": 5,
        "independent": 4,
        "unresolved": 2,
        "derived": 1,
        "syndicated": 0,
    }
    form_rank = {
        "report": 5,
        "analysis": 3,
        "unknown": 2,
        "live": 1,
        "opinion": 0,
        "static": 0,
    }
    trust_rank = {"authoritative": 4, "trusted": 3, "standard": 2, "low": 1}
    feature = _feature_mapping(member)
    specificity = (
        len(_string_set(feature.get("entities")))
        + len(_string_set(feature.get("actions")))
        + len(_string_set(feature.get("stages")))
    )
    return (
        evidence_rank.get(str(member.get("epistemic_use")), 0),
        origin_rank.get(str(member.get("origin_relation")), 0),
        form_rank.get(str(member.get("content_form")), 0),
        trust_rank.get(str(member.get("trust_tier")), 0),
        specificity,
        _int(member.get("observed_at_ms")),
        str(member.get("revision_id")),
    )


def _content_form(*, text: str, canonical_url: str) -> str:
    path = urlsplit(canonical_url).path.lower() if canonical_url else ""
    if path in _STATIC_PATHS or any(hint in text for hint in _STATIC_TITLE_HINTS):
        return "static"
    if any(hint in text for hint in _LIVE_HINTS):
        return "live"
    if any(hint in text for hint in _OPINION_HINTS):
        return "opinion"
    if any(hint in text for hint in _ANALYSIS_HINTS):
        return "analysis"
    if text:
        return "report"
    return "unknown"


def _is_static_entry(*, normalized_url: str, title: str) -> bool:
    parsed = urlsplit(normalized_url)
    path = (parsed.path or "/").rstrip("/") or "/"
    text = normalize_match_text(title)
    return path in _STATIC_PATHS or any(normalize_match_text(hint) in text for hint in _STATIC_TITLE_HINTS)


def _supported_values(features: Sequence[Mapping[str, object]], key: str) -> list[str]:
    counter: Counter[str] = Counter()
    for feature in features:
        counter.update(_string_set(feature.get(key)))
    minimum_support = 1 if len(features) <= 2 else 2
    return sorted(value for value, count in counter.items() if count >= minimum_support)


def _canonical_matches(
    text: str,
    aliases: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    padded = f" {text.casefold()} "
    found = []
    for canonical, values in aliases.items():
        options = (canonical, *values)
        if any(_phrase_in_text(option.casefold(), padded) for option in options):
            found.append(canonical)
    return tuple(sorted(found))


def _canonical_matches_in_order(
    text: str,
    aliases: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    normalized_text = normalize_match_text(text)
    matches: list[tuple[int, str]] = []
    for canonical, values in aliases.items():
        positions = [
            position
            for option in (canonical, *values)
            if (position := _phrase_position(normalize_match_text(option), normalized_text)) is not None
        ]
        if positions:
            matches.append((min(positions), canonical))
    return tuple(canonical for _, canonical in sorted(matches))


def _phrase_position(phrase: str, text: str) -> int | None:
    if not phrase:
        return None
    if any("\u3400" <= char <= "\u9fff" for char in phrase):
        position = text.find(phrase)
        return position if position >= 0 else None
    match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text)
    return match.start() if match is not None else None


def _identity_actions(text: str) -> tuple[str, ...]:
    """Extract event-defining actions without generic reporting verbs.

    ``announce``/``confirm`` is identity-bearing only when it is the sole
    recognized action. When a title also names a material action, retaining the
    reporting verb creates a language-dependent event key (for example,
    ``宣布下调`` versus ``cuts``) and can hide a true candidate.
    """

    actions = _canonical_matches(text, _ACTION_ALIASES)
    if len(actions) > 1 and "confirm" in actions:
        actions = tuple(action for action in actions if action != "confirm")
    if "recall" in actions and "attack" in actions:
        actions = tuple(action for action in actions if action != "attack")
    if "order" in actions:
        actions = tuple(action for action in actions if action not in {"correct", "sign"})
    return actions


def _phrase_in_text(phrase: str, padded_text: str) -> bool:
    if any("\u3400" <= char <= "\u9fff" for char in phrase):
        return phrase in padded_text
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", padded_text) is not None


def _quantities(text: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    pattern = re.compile(
        r"(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>basis points?|bps?|个?基点|%|percent|percentage points?|百分比|"
        r"million|billion|trillion|万美元|万人|万|亿|美元|元|人)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        raw_value = match.group("value")
        unit = (match.group("unit") or "count").lower()
        if unit in {"basis point", "basis points", "bp", "bps", "基点", "个基点"}:
            kind = "basis_points"
        elif unit in {"%", "percent", "percentage point", "percentage points", "百分比"}:
            kind = "percent"
        elif unit in {"million", "billion", "trillion", "万", "亿", "万美元", "美元", "元"}:
            kind = "amount"
        elif unit in {"人", "万人"}:
            kind = "people"
        else:
            kind = "count"
        values.append(
            {
                "value": float(raw_value),
                "unit": unit,
                "kind": kind,
                "identity_defining": False,
            }
        )
    identity_patterns = (
        ("flight", re.compile(r"\b(?:flight|航班)\s*[-#]?\s*(\d{1,6})\b", re.IGNORECASE)),
        (
            "resolution",
            re.compile(r"\b(?:resolution|决议)\s*[-#]?\s*(\d{1,6})\b", re.IGNORECASE),
        ),
        (
            "phase",
            re.compile(r"\b(?:phase|阶段)\s*[-#]?\s*(\d{1,3})\b", re.IGNORECASE),
        ),
        (
            "round",
            re.compile(r"\b(?:round|第)\s*[-#]?\s*(\d{1,3})(?:\s*轮)?\b", re.IGNORECASE),
        ),
        (
            "tranche",
            re.compile(r"\b(?:tranche|批次)\s*[-#]?\s*(\d{1,3})\b", re.IGNORECASE),
        ),
        (
            "magnitude",
            re.compile(
                r"\b(?:magnitude|震级)\s*[-#]?\s*(\d+(?:\.\d+)?)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "magnitude",
            re.compile(
                r"\b(\d+(?:\.\d+)?)[-\s]*magnitude\b",
                re.IGNORECASE,
            ),
        ),
    )
    for identity_kind, identity_pattern in identity_patterns:
        for match in identity_pattern.finditer(text):
            identity_quantity = {
                "value": float(match.group(1)),
                "unit": identity_kind,
                "kind": f"identity_{identity_kind}",
                "identity_defining": True,
            }
            if identity_quantity not in values:
                values.append(identity_quantity)
    return values[:12]


def _material_quantities(value: object) -> dict[str, set[float]]:
    material: dict[str, set[float]] = defaultdict(set)
    for quantity in _dict_sequence(value):
        kind = str(quantity.get("kind") or "")
        unit = str(quantity.get("unit") or "")
        raw_value = _float(quantity.get("value"))
        if kind in {"basis_points", "percent"}:
            material[kind].add(raw_value)
        elif kind == "amount":
            multiplier = {
                "million": 1_000_000,
                "billion": 1_000_000_000,
                "trillion": 1_000_000_000_000,
                "万美元": 10_000,
                "万": 10_000,
                "亿": 100_000_000,
            }.get(unit)
            if multiplier is not None:
                material[kind].add(raw_value * multiplier)
        elif kind == "people":
            material[kind].add(raw_value * (10_000 if unit == "万人" else 1))
    return material


def _identity_quantities(value: object) -> dict[str, set[float]]:
    identity: dict[str, set[float]] = defaultdict(set)
    for quantity in _dict_sequence(value):
        if not bool(quantity.get("identity_defining")):
            continue
        kind = str(quantity.get("kind") or "")
        if not kind:
            continue
        identity[kind].add(_float(quantity.get("value")))
    return identity


def _named_event_keys(text: str) -> set[str]:
    matches = set()
    patterns = (
        r"\b(?:cop|g|brics|nato)\s*-?\d{1,2}\b",
        r"\b(?:fomc|ecb|boj)\s+(?:meeting|decision)\b",
        r"\b(?:hurricane|typhoon)\s+[a-z][a-z-]+\b",
        r"\b[a-z][a-z -]+ act\b",
        r"第?[一二三四五六七八九十\d]+轮选举",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matches.add(normalize_match_text(match.group(0)))
    return matches


def _temporal_episode_keys(title: str) -> set[str]:
    matches: set[str] = set()
    month_names = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    patterns = (
        r"\b20\d{2}[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?\b",
        rf"\b(?:{'|'.join(month_names)})\s+(?:[0-3]?\d(?:st|nd|rd|th)?(?:,\s*)?)?20\d{{2}}\b",
        rf"\b(?:{'|'.join(month_names)})\s+[0-3]?\d(?:st|nd|rd|th)?\b",
        r"\b20\d{2}年(?:1[0-2]|0?[1-9])月(?:[0-3]?\d日)?",
        r"(?<!\d)(?:1[0-2]|0?[1-9])月(?:[0-3]?\d日)?",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, title, re.IGNORECASE):
            matches.add(normalize_match_text(match.group(0)))
    return matches


def _tokens(value: str) -> set[str]:
    return {token for token in _WORD_RE.findall(value) if token not in _STOPWORDS and len(token) > 1}


def _bigrams(value: str) -> set[str]:
    tokens = [token for token in _WORD_RE.findall(value) if token not in _STOPWORDS]
    return {f"{left} {right}" for left, right in pairwise(tokens)}


def _chargrams(value: str, size: int = 3) -> set[str]:
    compact = re.sub(r"\s+", " ", value)
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _containment(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _normalizable_identity(
    guid: str | None,
    normalized_url: str,
    title: str,
    published_at_ms: int,
) -> bool:
    return bool(normalize_optional_text(guid) or normalized_url or (title and published_at_ms >= 0))


def _source_entry_key(guid: str | None, normalized_url: str) -> str:
    normalized_guid = normalize_optional_text(guid)
    return normalized_guid or normalized_url


def normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _attributed_origin(members: Sequence[Mapping[str, object]]) -> str | None:
    origins = [
        str(member.get("reporting_origin_id")) for member in members if str(member.get("reporting_origin_id") or "")
    ]
    return origins[0] if len(set(origins)) == 1 and origins else None


def _feature_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    nested = value.get("features")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key in (
            "revision_id",
            "article_id",
            "language",
            "normalized_title",
            "normalized_lead",
            "content_fingerprint",
            "lexical_signature",
            "event_key",
            "named_event_keys",
            "feature_hash",
        ):
            if value.get(key) is not None:
                merged[key] = value[key]
        return merged
    return value


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dict_sequence(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in _mapping_sequence(value)]


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _string_set(value: object) -> set[str]:
    return {str(item) for item in _sequence(value) if str(item)}


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "ANCHORED_CANDIDATE_WINDOW_MS",
    "ARTICLE_FUTURE_SKEW_MS",
    "ARTICLE_MAX_AGE_MS",
    "LEXICAL_CANDIDATE_WINDOW_MS",
    "NAMED_EVENT_CANDIDATE_WINDOW_MS",
    "STORY_MATCH_THRESHOLD",
    "STORY_RUNNER_UP_MARGIN",
    "STORY_STRONG_THRESHOLD",
    "StoryProjection",
    "admit_feed_entry",
    "article_id_for",
    "article_revision_id",
    "build_story_profile",
    "classify_member_semantics",
    "confirmed_url_reuse",
    "decide_story",
    "deterministic_id",
    "extract_identity_features",
    "identity_decision_id",
    "normalize_display_text",
    "normalize_language",
    "normalize_match_text",
    "normalize_url",
    "project_story",
    "sha256_json",
    "story_id_for_seed",
    "story_impact",
    "story_lifecycle",
    "story_priority",
]
