from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import regex
from ada_url import URL

from .identity import (
    JAVASCRIPT_WHITESPACE_PATTERN,
    collapse_javascript_whitespace,
    javascript_is_letter_or_number,
    javascript_lower,
    javascript_starts_with_lowercase_letter,
    javascript_starts_with_uppercase_letter,
    javascript_trim,
    parse_javascript_number,
    utf16_length,
    utf16_slice,
    web_usv_string,
)
from .models import (
    NewsBriefSource,
    NewsBriefStory,
    NewsBriefStoryLine,
    NewsBriefSynthesisResult,
)

_CODE_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE | re.ASCII)
_CITATION_RE = re.compile(r"\[([0-9]{1,3})\]")
_LINE_CITATION_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}*\[[0-9]{{1,3}}\]")
_CITATION_WITH_WHITESPACE_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}*\[([0-9]{{1,3}})\]")
_ANCHOR_DELIMS_RE = re.compile(rf"(?:{JAVASCRIPT_WHITESPACE_PATTERN}|[,.!?;:()'\"‘’“”´\\/—–\-\[\]{{}}])+")
_WORD_SPLIT_RE = regex.compile(r"[^\p{L}\p{N}'’\-]+")
_PROPER_SENTENCE_SPLIT_RE = re.compile(rf"[.!?]+{JAVASCRIPT_WHITESPACE_PATTERN}+|\n+")
_LEAD_SENTENCE_SPLIT_RE = re.compile(rf"(?<=[.!?]){JAVASCRIPT_WHITESPACE_PATTERN}+")
_JS_WHITESPACE_PREFIX_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}+")
_DOTTED_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z]\.(?:[A-Z]\.?)+")
_TITLE_PREFIX_STOP = frozenset(
    {
        "President",
        "Prime",
        "Minister",
        "Senator",
        "Representative",
        "Dr",
        "Dr.",
        "Mr",
        "Mr.",
        "Ms",
        "Ms.",
        "Mrs",
        "Mrs.",
        "Acting",
        "Interim",
        "Former",
        "Ex",
        "Chairman",
        "Chairwoman",
        "Chair",
        "Speaker",
        "CEO",
        "Secretary",
        "Defense",
        "Foreign",
        "Ambassador",
        "General",
        "Admiral",
        "Colonel",
        "Captain",
        "Pope",
        "King",
        "Queen",
        "Prince",
        "Princess",
        "Lord",
        "Lady",
        "Sir",
        "Dame",
        "Judge",
        "Justice",
    }
)
_PROPER_NOUN_JOINER = frozenset({"of", "the", "and", "for", "de", "du", "der", "van", "el", "al"})
_ACRONYM_EXPANSIONS = (
    ("WHO", "World Health Organization"),
    ("UN", "United Nations"),
    ("US", "USA", "United States", "United States of America", "America"),
    ("UK", "United Kingdom", "Britain", "Great Britain"),
    ("EU", "European Union"),
    ("IDF", "Israel Defense Forces", "Israeli Defense Forces"),
    ("IMF", "International Monetary Fund"),
    ("WTO", "World Trade Organization"),
    ("NATO", "North Atlantic Treaty Organization"),
    ("OECD",),
    ("OPEC", "Organization of the Petroleum Exporting Countries"),
    ("IAEA", "International Atomic Energy Agency"),
    ("ASEAN",),
    ("ECOWAS",),
    ("BRICS",),
    ("DOJ", "Department of Justice", "Justice Department"),
    ("FBI", "Federal Bureau of Investigation"),
    ("SEC", "Securities and Exchange Commission"),
    ("CIA", "Central Intelligence Agency"),
    ("NSA", "National Security Agency"),
    ("DOD", "Department of Defense", "Defense Department", "Pentagon"),
    ("DR Congo", "Democratic Republic of Congo", "DRC"),
    ("UAE", "United Arab Emirates"),
)
_DEMONYM_TO_NATION = (
    ("Israeli", "Israel"),
    ("Israelis", "Israel"),
    ("American", "United States"),
    ("Americans", "United States"),
    ("Iranian", "Iran"),
    ("Iranians", "Iran"),
    ("Russian", "Russia"),
    ("Russians", "Russia"),
    ("Chinese", "China"),
    ("French", "France"),
    ("German", "Germany"),
    ("Germans", "Germany"),
    ("Japanese", "Japan"),
    ("Lebanese", "Lebanon"),
    ("Syrian", "Syria"),
    ("Syrians", "Syria"),
    ("Saudi", "Saudi Arabia"),
    ("Saudis", "Saudi Arabia"),
    ("Egyptian", "Egypt"),
    ("Egyptians", "Egypt"),
    ("Turkish", "Turkey"),
    ("Turks", "Turkey"),
    ("Indian", "India"),
    ("Indians", "India"),
    ("Pakistani", "Pakistan"),
    ("Pakistanis", "Pakistan"),
    ("British", "United Kingdom"),
    ("Briton", "United Kingdom"),
    ("Britons", "United Kingdom"),
    ("Ukrainian", "Ukraine"),
    ("Ukrainians", "Ukraine"),
    ("Palestinian", "Palestine"),
    ("Palestinians", "Palestine"),
    ("Yemeni", "Yemen"),
    ("Yemenis", "Yemen"),
    ("Iraqi", "Iraq"),
    ("Iraqis", "Iraq"),
    ("Afghan", "Afghanistan"),
    ("Afghans", "Afghanistan"),
    ("Spanish", "Spain"),
    ("Italian", "Italy"),
    ("Italians", "Italy"),
    ("Korean", "Korea"),
    ("Koreans", "Korea"),
    ("Vietnamese", "Vietnam"),
    ("Mexican", "Mexico"),
    ("Mexicans", "Mexico"),
    ("Brazilian", "Brazil"),
    ("Brazilians", "Brazil"),
    ("Canadian", "Canada"),
    ("Canadians", "Canada"),
    ("Australian", "Australia"),
    ("Australians", "Australia"),
    ("Cuban", "Cuba"),
    ("Cubans", "Cuba"),
    ("Venezuelan", "Venezuela"),
    ("Venezuelans", "Venezuela"),
    ("Argentine", "Argentina"),
    ("Argentinian", "Argentina"),
    ("Argentinians", "Argentina"),
    ("Polish", "Poland"),
    ("Dutch", "Netherlands"),
    ("Greek", "Greece"),
    ("Greeks", "Greece"),
    ("Portuguese", "Portugal"),
    ("Swiss", "Switzerland"),
    ("Swedish", "Sweden"),
    ("Swedes", "Sweden"),
    ("Norwegian", "Norway"),
    ("Norwegians", "Norway"),
    ("Finnish", "Finland"),
    ("Finns", "Finland"),
    ("Danish", "Denmark"),
    ("Danes", "Denmark"),
    ("Belgian", "Belgium"),
    ("Belgians", "Belgium"),
    ("Austrian", "Austria"),
    ("Austrians", "Austria"),
    ("Filipino", "Philippines"),
    ("Filipinos", "Philippines"),
    ("Thai", "Thailand"),
    ("Thais", "Thailand"),
    ("Indonesian", "Indonesia"),
    ("Indonesians", "Indonesia"),
    ("Nigerian", "Nigeria"),
    ("Nigerians", "Nigeria"),
    ("Ethiopian", "Ethiopia"),
    ("Ethiopians", "Ethiopia"),
    ("Kenyan", "Kenya"),
    ("Kenyans", "Kenya"),
    ("South Korean", "South Korea"),
    ("South Koreans", "South Korea"),
    ("North Korean", "North Korea"),
    ("North Koreans", "North Korea"),
)
_ACRONYM_NORMALIZE = {variant.lower(): group[0].lower() for group in _ACRONYM_EXPANSIONS for variant in group}
_DEMONYM_NORMALIZE = {demonym.lower(): nation.lower() for demonym, nation in _DEMONYM_TO_NATION}
_SENTENCE_START_AMBIGUOUS = frozenset(
    [
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "it",
        "he",
        "she",
        "they",
        "we",
        "you",
        "i",
        "some",
        "many",
        "most",
        "all",
        "few",
        "several",
        "both",
        "each",
        "every",
        "other",
        "another",
        "such",
        "any",
        "either",
        "neither",
        "there",
        "here",
        "now",
        "today",
        "yesterday",
        "tomorrow",
        "when",
        "where",
        "while",
        "as",
        "after",
        "before",
        "during",
        "since",
        "until",
        "if",
        "because",
        "although",
        "though",
        "unless",
        "whether",
        "how",
        "why",
        "what",
        "which",
        "whose",
        "no",
        "not",
        "yes",
        "breaking",
        "live",
        "updated",
        "latest",
        "exclusive",
        "just",
        "meanwhile",
        "however",
        "moreover",
        "additionally",
        "furthermore",
        "still",
        "with",
        "without",
        "on",
        "in",
        "at",
        "by",
        "for",
        "over",
        "under",
        "about",
    ]
)
_SENTENCE_INITIAL_ALLOWANCE_BLOCKED = frozenset(
    [
        "january",
        "jan",
        "february",
        "feb",
        "march",
        "mar",
        "april",
        "apr",
        "may",
        "june",
        "jun",
        "july",
        "jul",
        "august",
        "aug",
        "september",
        "sept",
        "sep",
        "october",
        "oct",
        "november",
        "nov",
        "december",
        "dec",
        "turkey",
        "china",
        "chad",
        "jordan",
        "georgia",
        "polish",
        "dutch",
        "thai",
        "bill",
        "mark",
        "will",
        "frank",
        "rose",
        "grant",
        "apple",
        "amazon",
        "shell",
        "orange",
        "target",
        "meta",
        "gap",
        "visa",
        "post",
        "guard",
        "state",
        "sun",
        "times",
        "page",
        "ford",
    ]
)
_NUMBER_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1_000,
    "thousands": 1_000,
    "million": 1_000_000,
    "millions": 1_000_000,
    "billion": 1_000_000_000,
    "billions": 1_000_000_000,
    "trillion": 1_000_000_000_000,
    "trillions": 1_000_000_000_000,
    "dozen": 12,
}
_NUMBER_WORDS = sorted((*_NUMBER_WORD_VALUES, "dozens"), key=len, reverse=True)
_NUMBER_WORD_PATTERN = "|".join(re.escape(word) for word in _NUMBER_WORDS)
_NUMBER_WORD_SEQUENCE_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{_NUMBER_WORD_PATTERN})(?:[- ](?:{_NUMBER_WORD_PATTERN}|and))*"
    rf"(?![A-Za-z0-9_])(?:{JAVASCRIPT_WHITESPACE_PATTERN}+percent(?![A-Za-z0-9_]))?",
    re.IGNORECASE | re.ASCII,
)
_DIGIT_FACT_RE = re.compile(
    rf"[0-9][0-9,]*(?:\.[0-9]+)?(?:{JAVASCRIPT_WHITESPACE_PATTERN}*"
    rf"(?:%|percent|thousands?|millions?|billions?|trillions?))?",
    re.IGNORECASE | re.ASCII,
)
_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_DATE_EXPRESSION_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}|"
    rf"[0-9]{{1,2}}[/-][0-9]{{1,2}}(?:[/-][0-9]{{2,4}})?|"
    rf"(?:{_MONTH_PATTERN})\.?{JAVASCRIPT_WHITESPACE_PATTERN}+[0-9]{{1,2}}"
    rf"(?:,?{JAVASCRIPT_WHITESPACE_PATTERN}+[0-9]{{4}})?|"
    rf"[0-9]{{1,2}}{JAVASCRIPT_WHITESPACE_PATTERN}+(?:{_MONTH_PATTERN})\.?"
    rf"{JAVASCRIPT_WHITESPACE_PATTERN}*(?:[0-9]{{4}})?)(?![A-Za-z0-9_])",
    re.IGNORECASE | re.ASCII,
)
_MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_ANCHOR_STOPWORDS = frozenset(
    {
        "president",
        "vice",
        "senator",
        "minister",
        "secretary",
        "chairman",
        "chairwoman",
        "spokesman",
        "spokeswoman",
        "director",
        "general",
        "admiral",
        "colonel",
        "captain",
        "mayor",
        "governor",
        "judge",
        "justice",
        "doctor",
        "professor",
        "pope",
        "rabbi",
        "imam",
        "sheikh",
        "sultan",
        "emir",
        "king",
        "queen",
        "prince",
        "princess",
        "prime",
        "chief",
        "premier",
        "chancellor",
        "speaker",
        "ambassador",
        "envoy",
        "commissioner",
        "attorney",
        "cardinal",
        "archbishop",
        "monsignor",
        "reverend",
        "pastor",
        "bishop",
        "lord",
        "lady",
        "dame",
        "congressman",
        "congresswoman",
        "congressperson",
        "representative",
        "delegate",
        "baron",
        "baroness",
        "officials",
        "officers",
        "leaders",
        "members",
        "people",
        "forces",
        "police",
        "troops",
        "agents",
        "authorities",
        "sources",
        "rebels",
        "militants",
        "protesters",
        "civilians",
        "residents",
        "citizens",
        "workers",
        "voters",
        "senior",
        "junior",
        "former",
        "acting",
        "deputy",
        "assistant",
        "federal",
        "national",
        "international",
        "global",
        "regional",
        "central",
        "local",
        "foreign",
        "domestic",
        "civil",
        "public",
        "private",
        "special",
        "major",
        "armed",
        "after",
        "before",
        "during",
        "while",
        "despite",
        "following",
        "amid",
        "today",
        "yesterday",
        "tomorrow",
        "this",
        "these",
        "those",
        "when",
        "where",
        "what",
        "which",
        "breaking",
        "says",
        "said",
        "told",
        "reports",
        "analysis",
        "opinion",
        "editorial",
        "update",
        "updates",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)


def synthesis_system_prompt(date_iso: str) -> str:
    return (
        f"Current date: {date_iso}.\n\n"
        "You are compiling the WORLD BRIEF from the numbered stories below. Respond with JSON ONLY "
        "(no markdown fences, no commentary):\n"
        '{"lead": "...", "lines": [{"n": 1, "text": "..."}, ...]}\n\n'
        "Rules:\n"
        '- "lead": 2-3 sentences, under 80 words, synthesizing the most consequential 2-3 threads. '
        "Cite every claim with the bracket number of its story, e.g. [1] or [3].\n"
        '- "lines": exactly one entry per numbered story, in order. Each "text" is ONE sentence under 30 '
        "words restating that story, ending with its citation [n].\n"
        "- Use ONLY facts present in the numbered story text. Do not add names, places, dates, numbers, or "
        "context that are not explicitly there.\n"
        "- Do not invent proper nouns (people, organizations, countries) that are not in the story text.\n"
        "- Two numbered stories can describe the SAME event in different words. A lead claim may combine "
        "them, but it MUST carry the citation of EVERY story it drew from — write [3][7], not just [3]. Any "
        "name, place, or number you take from a story you did not cite counts as invented.\n"
        '- Write acronyms WITHOUT periods: "US", "UN", "EU", "UK" — never "U.S.", "U.N.". A trailing '
        "period there reads as the end of a sentence.\n"
        "- Refer to an actor by the name the story uses. Do not swap in a capital city, nickname, or synonym "
        'for it — write "US", not "Washington"; "Iran", not "Tehran" — unless that word is in the story text.\n'
        '- NEVER start with "Breaking news", "Good evening", "Tonight", or TV-style openings.'
    )


def synthesis_user_prompt(stories: Sequence[NewsBriefStory]) -> str:
    rows = [
        f"{index}. {story.primary_title} ({story.primary_source}, {story.unique_source_count} "
        f"source{'s' if story.unique_source_count != 1 else ''})"
        for index, story in enumerate(stories, start=1)
    ]
    return f"Stories:\n{'\n'.join(rows)}\n\nCompile the world brief JSON."


def brief_system_prompt(date_iso: str) -> str:
    return (
        f"Current date: {date_iso}.\n\n"
        "Rewrite the provided headline as 2 concise sentences MAX (under 60 words total).\n"
        "Rules:\n"
        "- Use ONLY facts present in the headline text. Do not add names, places, dates, or context that are "
        "not explicitly in the headline.\n"
        "- Do not invent proper nouns (people, organizations, countries) that are not in the headline.\n"
        "- Include a location, person, or organization ONLY if it appears in the headline. If the headline "
        "has no location, do not add one.\n"
        '- NEVER start with "Breaking news", "Good evening", "Tonight", or TV-style openings.\n'
        "- No bullet points, no meta-commentary, no speculation beyond the headline."
    )


def brief_user_prompt(headline: str) -> str:
    return f"Headline: {headline}\n\nRewrite as 2 sentences using only facts from this headline."


def parse_brief_synthesis(raw_text: str, story_count: int) -> tuple[str, tuple[tuple[int, str], ...]] | None:
    if not isinstance(raw_text, str) or not raw_text or "\x00" in raw_text:
        return None
    text = javascript_trim(_CODE_FENCE_RE.sub("", web_usv_string(raw_text)))
    start = text.find("{")
    if start < 0:
        return None
    end = _balanced_object_end(text, start)
    if end is None:
        return None
    try:
        parsed = json.loads(text[start : end + 1], parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    lead_value = parsed.get("lead")
    lead = javascript_trim(web_usv_string(lead_value)) if isinstance(lead_value, str) else ""
    if "\x00" in lead or not 40 <= utf16_length(lead) <= 700:
        return None
    raw_lines = parsed.get("lines")
    by_index: dict[int, str] = {}
    if isinstance(raw_lines, list):
        for entry in raw_lines:
            if not isinstance(entry, Mapping):
                continue
            n = _line_index(entry.get("n"))
            line_value = entry.get("text")
            line = javascript_trim(web_usv_string(line_value)) if isinstance(line_value, str) else ""
            if (
                n is None
                or not 1 <= n <= story_count
                or "\x00" in line
                or not 15 <= utf16_length(line) <= 260
                or n in by_index
            ):
                continue
            by_index[n] = line
    if len(by_index) < math.ceil(story_count / 2):
        return None
    return lead, tuple(sorted(by_index.items()))


def compose_l1_brief(
    raw_text: str,
    stories: Sequence[NewsBriefStory],
    *,
    provider: str,
    model: str,
) -> NewsBriefSynthesisResult | None:
    if not stories or not any(is_brief_lead_eligible(story) for story in stories):
        return None
    parsed = parse_brief_synthesis(raw_text, len(stories))
    if parsed is None:
        return None
    lead, parsed_lines = parsed
    cleaned_lead, stripped_citations = verify_citation_indexes(lead, len(stories))
    if not _lead_is_grounded(cleaned_lead, stories):
        return None

    by_index = dict(parsed_lines)
    lines: list[NewsBriefStoryLine] = []
    line_fallbacks: list[int] = []
    for index, story in enumerate(stories, start=1):
        line = by_index.get(index)
        headline = _sanitize_title(story.primary_title)
        if line is None:
            lines.append(NewsBriefStoryLine(n=index, text=f"{headline} [{index}]"))
            continue
        bare = javascript_trim(_LINE_CITATION_RE.sub("", line))
        if not _proper_nouns_grounded(bare, _story_ground_text(story)):
            line_fallbacks.append(index)
            lines.append(NewsBriefStoryLine(n=index, text=f"{headline} [{index}]"))
            continue
        lines.append(NewsBriefStoryLine(n=index, text=f"{bare} [{index}]"))

    return NewsBriefSynthesisResult(
        brief_kind="l1",
        quality="ok",
        world_brief=cleaned_lead,
        brief_story_lines=tuple(lines),
        sources=tuple(_l1_source(story) for story in stories),
        provider=provider,
        model=model,
        validation={
            "failure_code": None,
            "stripped_citations": stripped_citations,
            "line_fallbacks": line_fallbacks,
        },
    )


def compose_l2_brief(
    text: str,
    story: NewsBriefStory,
    *,
    provider: str,
    model: str,
    failure_code: str,
) -> NewsBriefSynthesisResult:
    world_brief = javascript_trim(web_usv_string(text))
    noun_valid, _hallucinated = validate_no_hallucinated_proper_nouns(world_brief, story.primary_title)
    headline_fallback = not noun_valid
    if headline_fallback:
        world_brief = _sanitize_title(story.primary_title)
        provider = f"{provider}+headline-fallback"
    source = _linked_source(story)
    return NewsBriefSynthesisResult(
        brief_kind="l2",
        quality="degraded",
        world_brief=world_brief,
        brief_story_lines=(),
        sources=(source,) if source is not None else (),
        provider=provider,
        model=model,
        validation={
            "failure_code": failure_code,
            "headline_fallback": headline_fallback,
        },
    )


def compose_none_brief(
    story: NewsBriefStory | None,
    *,
    failure_code: str,
) -> NewsBriefSynthesisResult:
    source = _linked_source(story) if story is not None else None
    return NewsBriefSynthesisResult(
        brief_kind="none",
        quality="degraded",
        world_brief="",
        brief_story_lines=(),
        sources=(source,) if source is not None else (),
        provider="",
        model="",
        validation={"failure_code": failure_code},
    )


def is_brief_lead_eligible(story: NewsBriefStory) -> bool:
    return story.unique_source_count >= 2 or story.entity_corroboration


def verify_citation_indexes(text: str, source_count: int) -> tuple[str, int]:
    if not isinstance(text, str) or not text:
        return (text if isinstance(text, str) else ""), 0
    maximum = math.floor(source_count) if source_count > 0 else 0
    stripped = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal stripped
        index = int(match.group(1))
        if 1 <= index <= maximum:
            return match.group(0)
        stripped += 1
        return ""

    return _CITATION_WITH_WHITESPACE_RE.sub(replace, text), stripped


def selection_fingerprint(snapshot: Mapping[str, Any]) -> str:
    canonical = {
        "projection_revision": snapshot["projection_revision"],
        "selector_evaluated_at_ms": snapshot["selector_evaluated_at_ms"],
        "top_stories": snapshot["top_stories"],
        "selection_stats": snapshot["selection_stats"],
        "selector_version": snapshot["selector_version"],
        "identity_version": snapshot["identity_version"],
    }
    return _canonical_hash(canonical)


def publication_id(payload: Mapping[str, Any]) -> str:
    return _canonical_hash({key: value for key, value in payload.items() if key != "publication_id"})


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = web_usv_string(serialized).encode()
    return hashlib.sha256(encoded).hexdigest()


def _balanced_object_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _line_index(value: Any) -> int | None:
    number = _javascript_number(value)
    return int(number) if math.isfinite(number) and number.is_integer() else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _javascript_number(value: Any) -> float:
    """Coerce one JSON value with JavaScript ``Number(value)`` semantics."""

    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return -math.inf if value < 0 else math.inf
    if isinstance(value, list):
        value = ",".join(_javascript_array_item_string(item) for item in value)
    if not isinstance(value, str):
        return math.nan
    return parse_javascript_number(value)


def _javascript_array_item_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(_javascript_array_item_string(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, str):
        return value
    return "[object Object]"


def _story_ground_text(story: NewsBriefStory) -> str:
    return " — ".join((story.primary_title, *story.member_titles))


def _proper_nouns_grounded(summary: str, ground_text: str) -> bool:
    return validate_no_hallucinated_proper_nouns(summary, ground_text)[0]


def validate_no_hallucinated_proper_nouns(summary: str, ground_text: str) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(summary, str) or not summary or not isinstance(ground_text, str) or not ground_text:
        return True, ()
    try:
        summary_entries = [
            (_normalize_sequence(tokens), sentence_initial, all_caps, first_sentence)
            for tokens, sentence_initial, all_caps, first_sentence in _extract_proper_noun_entries(summary)
        ]
        headline_sequences = [_normalize_sequence(tokens) for tokens in _extract_proper_noun_sequences(ground_text)]
        headline_tokens = _ground_token_set(ground_text)
    except (TypeError, ValueError):
        return True, ()
    if not summary_entries:
        return True, ()
    for summary_sequence, sentence_initial, all_caps, first_sentence in summary_entries:
        found = any(
            _contains_subsequence(headline_sequence, summary_sequence)
            or (len(summary_sequence) == 1 and summary_sequence[0] in headline_sequence)
            for headline_sequence in headline_sequences
        )
        if (
            not found
            and sentence_initial
            and first_sentence
            and not all_caps
            and len(summary_sequence) == 1
            and summary_sequence[0] not in _SENTENCE_INITIAL_ALLOWANCE_BLOCKED
            and summary_sequence[0] in headline_tokens
        ):
            found = True
        if not found:
            return False, summary_sequence
    return True, ()


def _normalize_dotted_acronyms(text: str) -> str:
    return _DOTTED_ACRONYM_RE.sub(lambda match: match.group(0).replace(".", ""), text)


def _extract_proper_noun_sequences(text: str) -> list[tuple[str, ...]]:
    return [entry[0] for entry in _extract_proper_noun_entries(text)]


def _extract_proper_noun_entries(text: str) -> list[tuple[tuple[str, ...], bool, bool, bool]]:
    if not isinstance(text, str) or not text:
        return []
    preprocessed = _normalize_dotted_acronyms(text)
    sequences: list[tuple[tuple[str, ...], bool, bool, bool]] = []
    sentence_ordinal = -1
    for raw_sentence in _PROPER_SENTENCE_SPLIT_RE.split(preprocessed):
        sentence = javascript_trim(raw_sentence)
        if not sentence:
            continue
        sentence_ordinal += 1
        tokens = [token for token in _WORD_SPLIT_RE.split(sentence) if token]
        if not tokens:
            continue
        current: list[str] = []
        current_started_sentence = False
        current_all_caps = False
        current_first_sentence = False
        bridge_buffer: list[str] = []
        first_token = True
        for token in tokens:
            stripped = re.sub(r"[.,;:'’]+$", "", token)
            stripped = re.sub(r"['’]s$", "", stripped, flags=re.IGNORECASE | re.ASCII)
            token_for_lookup = stripped or token
            is_title_prefix = stripped in _TITLE_PREFIX_STOP
            lowered_token = javascript_lower(token)
            is_joiner = lowered_token in _PROPER_NOUN_JOINER
            is_capitalized = len(token) >= 2 and token[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            is_all_caps = bool(re.fullmatch(r"[A-Z]{2,6}", token))
            ambiguous_start = first_token and not is_all_caps and lowered_token in _SENTENCE_START_AMBIGUOUS
            if is_title_prefix and not current:
                first_token = False
                continue
            if ambiguous_start:
                first_token = False
                continue
            at_sentence_start = first_token
            first_token = False
            if is_joiner:
                if current:
                    bridge_buffer.append(lowered_token)
                continue
            if is_capitalized or is_all_caps:
                if current and bridge_buffer:
                    current.extend(bridge_buffer)
                bridge_buffer = []
                if not current:
                    current_started_sentence = at_sentence_start
                    current_all_caps = is_all_caps
                    current_first_sentence = sentence_ordinal == 0
                current.append(javascript_lower(token_for_lookup))
                continue
            if current:
                sequences.append((tuple(current), current_started_sentence, current_all_caps, current_first_sentence))
                current = []
                current_started_sentence = False
                current_all_caps = False
                current_first_sentence = False
            bridge_buffer = []
        if current:
            sequences.append((tuple(current), current_started_sentence, current_all_caps, current_first_sentence))
    return sequences


def _normalize_token(token: str) -> str:
    lowered = javascript_lower(token)
    return _ACRONYM_NORMALIZE.get(lowered, _DEMONYM_NORMALIZE.get(lowered, lowered))


def _normalize_sequence(sequence: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    index = 0
    while index < len(sequence):
        matched = False
        for span in range(min(5, len(sequence) - index), 1, -1):
            candidate = javascript_lower(" ".join(sequence[index : index + span]))
            if candidate in _ACRONYM_NORMALIZE:
                normalized.append(_ACRONYM_NORMALIZE[candidate])
                index += span
                matched = True
                break
        if matched:
            continue
        normalized.append(_normalize_token(sequence[index]))
        index += 1
    return tuple(normalized)


def _ground_token_set(text: str) -> set[str]:
    tokens: set[str] = set()
    if not isinstance(text, str) or not text:
        return tokens
    for raw in _WORD_SPLIT_RE.split(_normalize_dotted_acronyms(text)):
        if not raw:
            continue
        stripped = re.sub(r"[.,;:'’]+$", "", raw)
        stripped = re.sub(r"['’]s$", "", stripped, flags=re.IGNORECASE | re.ASCII)
        value = stripped or raw
        token = javascript_lower(value)
        if not token:
            continue
        if not javascript_starts_with_uppercase_letter(value) and (
            token in _ACRONYM_NORMALIZE or token in _DEMONYM_NORMALIZE
        ):
            continue
        tokens.add(_normalize_token(token))
    return tokens


def _contains_subsequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle:
        return True
    return any(haystack[index : index + len(needle)] == needle for index in range(len(haystack) - len(needle) + 1))


def _lead_is_grounded(lead: str, stories: Sequence[NewsBriefStory]) -> bool:
    gate_view = _normalize_mid_sentence_acronyms_for_split(lead)
    sentences = [
        javascript_trim(sentence) for sentence in _LEAD_SENTENCE_SPLIT_RE.split(gate_view) if javascript_trim(sentence)
    ]
    if not sentences:
        return False
    for sentence in sentences:
        cited = [int(value) for value in _CITATION_RE.findall(sentence) if 1 <= int(value) <= len(stories)]
        if not cited:
            return False
        ground_text = " — ".join(_story_ground_text(stories[index - 1]) for index in cited)
        if not _proper_nouns_grounded(sentence, ground_text):
            return False
        if not _facts_grounded(sentence, ground_text):
            return False
    anchors = {javascript_lower(token) for story in stories[:8] for token in _anchor_tokens(story.primary_title)}
    if not anchors:
        return True
    lead_tokens = {javascript_lower(token) for token in _ANCHOR_DELIMS_RE.split(lead) if utf16_length(token) >= 4}
    threshold = 2 if len(anchors) >= 4 else 1
    return len(anchors & lead_tokens) >= threshold


def _normalize_mid_sentence_acronyms_for_split(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        remainder = text[match.end() :]
        whitespace = _JS_WHITESPACE_PREFIX_RE.match(remainder)
        if whitespace is None:
            return match.group(0)
        following = remainder[whitespace.end() :]
        if javascript_starts_with_lowercase_letter(following) or re.match(r"(?:\[[0-9]{1,3}\])+(?:[.!?]|$)", following):
            return match.group(0).replace(".", "")
        return match.group(0)

    return _DOTTED_ACRONYM_RE.sub(replace, text)


def _anchor_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in _ANCHOR_DELIMS_RE.split(value)
        if utf16_length(token) >= 4
        and token[:1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        and javascript_lower(token) not in _ANCHOR_STOPWORDS
    )


def _facts_grounded(summary: str, ground_text: str) -> bool:
    return _numeric_facts(summary).issubset(_numeric_facts(ground_text))


def _numeric_facts(text: str) -> set[str]:
    facts: set[str] = set()
    if not isinstance(text, str) or not text:
        return facts
    remaining = _CITATION_RE.sub(" ", text)

    def replace_date(match: re.Match[str]) -> str:
        _add_date_expression_facts(facts, match.group(0))
        return " " * len(match.group(0))

    remaining = _DATE_EXPRESSION_RE.sub(replace_date, remaining)

    def replace_digit(match: re.Match[str]) -> str:
        before = _utf16_adjacent_code_unit(remaining, match.start(), before=True)
        after = _utf16_adjacent_code_unit(remaining, match.end(), before=False)
        if _is_unicode_letter_or_number(before) or _is_unicode_letter_or_number(after):
            return match.group(0)
        facts.add(_normalize_digit_fact(match.group(0)))
        return " " * len(match.group(0))

    remaining = _DIGIT_FACT_RE.sub(replace_digit, remaining)
    for match in _NUMBER_WORD_SEQUENCE_RE.finditer(remaining):
        facts.add(_normalize_number_word_fact(match.group(0)))
    return facts


def _utf16_adjacent_code_unit(text: str, index: int, *, before: bool) -> str:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    offset = utf16_length(text[:index]) * 2
    start = offset - 2 if before else offset
    if start < 0 or start + 2 > len(encoded):
        return ""
    return encoded[start : start + 2].decode("utf-16-le", errors="surrogatepass")


def _is_unicode_letter_or_number(value: str) -> bool:
    return javascript_is_letter_or_number(value)


def _format_number(value: float | int) -> str:
    """Format one IEEE-754 double like JavaScript ``String(number)``."""

    try:
        number = float(value)
    except OverflowError:
        number = -math.inf if value < 0 else math.inf
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "-Infinity" if number < 0 else "Infinity"
    if number == 0:
        return "0"

    sign = "-" if number < 0 else ""
    raw = repr(abs(number)).lower()
    if "e" in raw:
        coefficient, exponent_text = raw.split("e", 1)
        integer, _, fraction = coefficient.partition(".")
        digits = (integer + fraction).rstrip("0")
        decimal_position = len(integer) + int(exponent_text)
    else:
        integer, _, fraction = raw.partition(".")
        if integer != "0":
            digits = (integer + fraction).rstrip("0")
            decimal_position = len(integer)
        else:
            leading_zeroes = len(fraction) - len(fraction.lstrip("0"))
            digits = fraction.lstrip("0").rstrip("0")
            decimal_position = -leading_zeroes

    digit_count = len(digits)
    if digit_count <= decimal_position <= 21:
        formatted = digits + ("0" * (decimal_position - digit_count))
    elif 0 < decimal_position <= 21:
        formatted = f"{digits[:decimal_position]}.{digits[decimal_position:]}"
    elif -6 < decimal_position <= 0:
        formatted = f"0.{('0' * -decimal_position)}{digits}"
    else:
        tail = f".{digits[1:]}" if digit_count > 1 else ""
        exponent = decimal_position - 1
        formatted = f"{digits[0]}{tail}e{'+' if exponent >= 0 else ''}{exponent}"
    return sign + formatted


def _normalize_digit_fact(raw: str) -> str:
    normalized_raw = javascript_trim(raw).lower().replace(",", "")
    match = re.fullmatch(
        rf"([0-9]+(?:\.[0-9]+)?)(?:{JAVASCRIPT_WHITESPACE_PATTERN}*"
        rf"(%|percent|thousands?|millions?|billions?|trillions?))?",
        normalized_raw,
        flags=re.ASCII,
    )
    if match is None:
        return f"number:{javascript_trim(raw).lower()}"
    value = float(match.group(1))
    if not math.isfinite(value):
        return f"number:{javascript_trim(raw).lower()}"
    unit = match.group(2)
    if unit is None:
        return f"number:{_format_number(value)}"
    if unit in {"%", "percent"}:
        return f"number:{_format_number(value)}%"
    scale = _NUMBER_WORD_VALUES.get(unit.removesuffix("s"))
    return f"number:{_format_number(value * scale)}" if scale else f"number:{_format_number(value)} {unit}"


def _normalize_number_word_fact(raw: str) -> str:
    words = [word for word in collapse_javascript_whitespace(raw.lower().replace("-", " ")).split(" ") if word]
    percent = bool(words and words[-1] == "percent")
    if percent:
        words.pop()
    if "dozen" in words or "dozens" in words:
        return f"word:{' '.join(words)}"
    total = 0
    current = 0
    saw_value = False
    for word in words:
        if word == "and":
            continue
        value = _NUMBER_WORD_VALUES.get(word)
        if value is None:
            return f"word:{' '.join(words)}"
        saw_value = True
        if value >= 100:
            if current == 0:
                current = 1
            if value >= 1_000:
                total += current * value
                current = 0
            else:
                current *= value
        else:
            current += value
    if not saw_value:
        return f"word:{' '.join(words)}"
    suffix = "%" if percent else ""
    return f"number:{_format_number(total + current)}{suffix}"


def _add_date_fact(facts: set[str], year: str | None, month: str | int | None, day: str | int | None) -> None:
    try:
        month_number = int(month) if month is not None else 0
        day_number = int(day) if day is not None else 0
    except (TypeError, ValueError):
        return
    if not 1 <= month_number <= 12 or not 1 <= day_number <= 31:
        return
    month_part = f"{month_number:02d}"
    day_part = f"{day_number:02d}"
    facts.update({f"date:{month_part}-{day_part}", f"number:{month_number}", f"number:{day_number}"})
    if year is not None and year != "":
        try:
            year_number = int(year)
        except (TypeError, ValueError):
            return
        facts.update({f"date:{year_number}-{month_part}-{day_part}", f"number:{year_number}"})


def _add_date_expression_facts(facts: set[str], raw: str) -> None:
    normalized = collapse_javascript_whitespace(raw)
    match = re.fullmatch(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})", normalized)
    if match:
        _add_date_fact(facts, match.group(1), match.group(2), match.group(3))
        return
    match = re.fullmatch(r"([0-9]{1,2})[/-]([0-9]{1,2})(?:[/-]([0-9]{2,4}))?", normalized)
    if match:
        _add_date_fact(facts, match.group(3), match.group(1), match.group(2))
        return
    match = re.fullmatch(
        rf"([A-Za-z]+)\.?{JAVASCRIPT_WHITESPACE_PATTERN}+([0-9]{{1,2}})"
        rf"(?:,?{JAVASCRIPT_WHITESPACE_PATTERN}+([0-9]{{4}}))?",
        normalized,
    )
    if match:
        _add_date_fact(facts, match.group(3), _MONTH_NUMBERS.get(match.group(1).lower()), match.group(2))
        return
    match = re.fullmatch(
        rf"([0-9]{{1,2}}){JAVASCRIPT_WHITESPACE_PATTERN}+([A-Za-z]+)\.?"
        rf"{JAVASCRIPT_WHITESPACE_PATTERN}*(?:{JAVASCRIPT_WHITESPACE_PATTERN}+([0-9]{{4}}))?",
        normalized,
    )
    if match:
        _add_date_fact(facts, match.group(3), _MONTH_NUMBERS.get(match.group(2).lower()), match.group(1))


def _sanitize_title(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", re.sub(r"<[^>]*>", "", value))
    return javascript_trim(web_usv_string(utf16_slice(cleaned, 500)))


def _valid_http_url(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    normalized = javascript_trim(value)
    try:
        parsed = URL(normalized)
    except ValueError:
        return ""
    if parsed.protocol not in {"http:", "https:"} or not parsed.hostname:
        return ""
    return parsed.href


def _clip_text(value: str, maximum: int) -> str:
    text = collapse_javascript_whitespace(value)
    if utf16_length(text) <= maximum:
        return web_usv_string(text)
    clipped = javascript_trim(web_usv_string(utf16_slice(text, maximum - 1)))
    return f"{clipped}..."


def _l1_source(story: NewsBriefStory) -> NewsBriefSource:
    source = _linked_source(story)
    if source is not None:
        return source
    return NewsBriefSource(
        title=_sanitize_title(story.primary_title) or "Untitled",
        source=story.primary_source or "Unknown",
        url="",
    )


def _linked_source(story: NewsBriefStory) -> NewsBriefSource | None:
    url = _valid_http_url(story.primary_link)
    title = _clip_text(story.primary_title, 160)
    source = _clip_text(story.primary_source, 80)
    if not url or not title or not source:
        return None
    return NewsBriefSource(
        title=title,
        source=source,
        url=url,
        published_at_ms=story.primary_published_at_ms,
    )


__all__ = [
    "brief_system_prompt",
    "brief_user_prompt",
    "compose_l1_brief",
    "compose_l2_brief",
    "compose_none_brief",
    "is_brief_lead_eligible",
    "parse_brief_synthesis",
    "publication_id",
    "selection_fingerprint",
    "synthesis_system_prompt",
    "synthesis_user_prompt",
    "validate_no_hallucinated_proper_nouns",
    "verify_citation_indexes",
]
