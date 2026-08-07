from __future__ import annotations

import json

from tracefold.news import brief as brief_module
from tracefold.news import brief_store as brief_store_module
from tracefold.news.brief import (
    compose_l1_brief,
    compose_l2_brief,
    parse_brief_synthesis,
    publication_id,
    selection_fingerprint,
    synthesis_system_prompt,
    synthesis_user_prompt,
    target_fingerprint,
    validate_no_hallucinated_proper_nouns,
    verify_citation_indexes,
)
from tracefold.news.models import NewsBriefStory


def _story(
    story_id: str,
    title: str,
    source: str,
    *,
    link: str | None,
    sources: tuple[str, ...],
    member_titles: tuple[str, ...] = (),
    entity_corroboration: bool = False,
) -> NewsBriefStory:
    return NewsBriefStory(
        story_id=story_id,
        primary_title=title,
        primary_source=source,
        primary_link=link,
        primary_published_at_ms=1_786_928_400_000,
        source_count=len(member_titles) + 1,
        unique_source_count=len(sources),
        sources=sources,
        last_updated_ms=1_786_928_400_000,
        member_titles=member_titles,
        source_tier=1,
        upstream_importance_score=90,
        entity_corroboration=entity_corroboration,
        corroboration_source_count=2 if entity_corroboration else 0,
        importance_score=240,
        effective_importance_score=230,
        is_alert=True,
        threat_level="high",
        category="economic",
    )


def test_public_l1_prompt_and_composer_keep_story_order_and_empty_source_slot() -> None:
    stories = (
        _story(
            "story-1",
            "Iran threatens to close Strait of Hormuz",
            "Reuters",
            link=None,
            sources=("Reuters", "BBC"),
        ),
        _story(
            "story-2",
            "Turkey hikes interest rates to 50%",
            "Bloomberg",
            link="https://example.test/turkey",
            sources=("Bloomberg",),
        ),
    )
    prompt = synthesis_system_prompt("2026-08-07")
    assert prompt == (
        "Current date: 2026-08-07.\n\n"
        "You are compiling the WORLD BRIEF from the numbered stories below. Respond with JSON ONLY "
        "(no markdown fences, no commentary):\n"
        '{"lead": "...", "lines": [{"n": 1, "text": "..."}, ...]}\n\n'
        "Rules:\n"
        '- "lead": 2-3 sentences, under 80 words, synthesizing the most consequential 2-3 threads. Cite every '
        "claim with the bracket number of its story, e.g. [1] or [3].\n"
        '- "lines": exactly one entry per numbered story, in order. Each "text" is ONE sentence under 30 words '
        "restating that story, ending with its citation [n].\n"
        "- Use ONLY facts present in the numbered story text. Do not add names, places, dates, numbers, or context "
        "that are not explicitly there.\n"
        "- Do not invent proper nouns (people, organizations, countries) that are not in the story text.\n"
        "- Two numbered stories can describe the SAME event in different words. A lead claim may combine them, but "
        "it MUST carry the citation of EVERY story it drew from — write [3][7], not just [3]. Any name, place, or "
        "number you take from a story you did not cite counts as invented.\n"
        '- Write acronyms WITHOUT periods: "US", "UN", "EU", "UK" — never "U.S.", "U.N.". A trailing period '
        "there reads as the end of a sentence.\n"
        "- Refer to an actor by the name the story uses. Do not swap in a capital city, nickname, or synonym for it "
        '— write "US", not "Washington"; "Iran", not "Tehran" — unless that word is in the story text.\n'
        '- NEVER start with "Breaking news", "Good evening", "Tonight", or TV-style openings.'
    )
    assert synthesis_user_prompt(stories) == (
        "Stories:\n"
        "1. Iran threatens to close Strait of Hormuz (Reuters, 2 sources)\n"
        "2. Turkey hikes interest rates to 50% (Bloomberg, 1 source)\n\n"
        "Compile the world brief JSON."
    )

    raw = (
        "```json\n"
        + json.dumps(
            {
                "lead": "Iran raises the stakes around Hormuz [1] while Turkey delivers a dramatic rate hike [2].",
                "lines": [
                    {"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."},
                    {"n": 2, "text": "Turkey raises interest rates to 50% [1]."},
                ],
            }
        )
        + "\n``` trailing prose with a stray }"
    )
    result = compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert result.brief_kind == "l1"
    assert result.quality == "ok"
    assert tuple(line.text for line in result.brief_story_lines) == (
        "Iran threatens to close the Strait of Hormuz. [1]",
        "Turkey raises interest rates to 50%. [2]",
    )
    assert tuple(source.url for source in result.sources) == ("", "https://example.test/turkey")


def test_l1_rejects_numeric_fact_borrowed_from_an_uncited_story() -> None:
    stories = (
        _story(
            "story-1",
            "Russia hits Ukrainian capital with ballistic missiles and drones",
            "AP News",
            link="https://example.test/1",
            sources=("AP News", "Wire"),
        ),
        _story(
            "story-2",
            "Nine killed in strikes on Kyiv, as Ukraine sinks Russian convoy",
            "BBC World",
            link="https://example.test/2",
            sources=("BBC World", "Wire"),
        ),
    )
    raw = json.dumps(
        {
            "lead": (
                "Russia struck the Ukrainian capital, killing nine people [1]. Russia used missiles and drones [1]."
            ),
            "lines": [
                {"n": 1, "text": "Russia hits the Ukrainian capital with missiles and drones [1]."},
                {"n": 2, "text": "Nine were killed in strikes on Kyiv [2]."},
            ],
        }
    )

    assert compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b") is None


def test_fact_gate_uses_javascript_number_strings_without_precision_collapse() -> None:
    assert not brief_module._facts_grounded("Rate reaches 12.34561%.", "Rate reaches 12.34562%.")
    assert brief_module._numeric_facts(
        "Values 0.0000001, 0.000001, 100000000000000000000 and 1000000000 trillion."
    ) == {
        "number:1e-7",
        "number:0.000001",
        "number:100000000000000000000",
        "number:1e+21",
    }


def test_fact_gate_uses_javascript_ascii_word_boundaries_next_to_cjk() -> None:
    assert not brief_module._facts_grounded("死亡two人", "死亡one人")


def test_digit_fact_adjacency_reads_javascript_utf16_code_units() -> None:
    assert brief_module._numeric_facts("𝔸12") == {"number:12"}
    assert brief_module._numeric_facts("12𐐀") == {"number:12"}


def test_proper_noun_gate_uses_javascript_lowercase_not_unicode_casefold() -> None:
    assert validate_no_hallucinated_proper_nouns("Strasse halted talks.", "Straße halted talks.") == (
        False,
        ("strasse",),
    )


def test_source_url_and_text_clamps_use_pinned_web_semantics() -> None:
    story = _story(
        "story-url",
        "😀" * 80 + " title",
        "Reuters",
        link="https://EXAMPLE.com:443",
        sources=("Reuters", "AP News"),
    )

    source = brief_module._linked_source(story)

    assert source is not None
    assert source.url == "https://example.com/"
    assert (
        source.title == brief_module.web_usv_string(brief_module.utf16_slice(story.primary_title, 159)).strip() + "..."
    )
    assert brief_module._valid_http_url("https://example.com:bad/path") == ""


def test_l1_proper_noun_gate_accepts_pinned_acronym_expansion() -> None:
    stories = (
        _story(
            "story-1",
            "WHO declares global health emergency after new outbreak",
            "Reuters",
            link="https://example.test/who",
            sources=("Reuters", "AP News"),
        ),
    )
    raw = json.dumps(
        {
            "lead": "The World Health Organization declares a global health emergency after the outbreak [1].",
            "lines": [
                {
                    "n": 1,
                    "text": "The World Health Organization declares a global health emergency after the outbreak [1].",
                }
            ],
        }
    )

    result = compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert result.brief_story_lines[0].text.startswith("The World Health Organization")


def test_l1_sentence_gate_does_not_split_a_mid_clause_dotted_acronym() -> None:
    stories = (
        _story(
            "story-1",
            "GCC condemns Iranian attacks on Kuwait",
            "The National",
            link="https://example.test/gcc",
            sources=("The National", "Reuters"),
        ),
        _story(
            "story-2",
            "U.S. embassies urge citizens to consider leaving the region",
            "The Hindu",
            link="https://example.test/embassies",
            sources=("The Hindu", "AP News"),
        ),
    )
    raw = json.dumps(
        {
            "lead": (
                "The GCC condemned Iranian attacks on Kuwait [1], while U.S. embassies urged citizens to consider "
                "leaving the region [2]."
            ),
            "lines": [
                {"n": 1, "text": "GCC condemns Iranian attacks on Kuwait [1]"},
                {"n": 2, "text": "U.S. embassies urge citizens to consider leaving the region [2]"},
            ],
        }
    )

    result = compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert "U.S. embassies" in result.world_brief


def test_l1_dotted_acronym_uses_javascript_boundary_after_cjk() -> None:
    story = _story(
        "story-1",
        "Iran met 中U.S. officials during regional security talks",
        "Reuters",
        link="https://example.test/talks",
        sources=("Reuters", "AP News"),
    )
    raw = json.dumps(
        {
            "lead": (
                "Iran met 中U.S. officials during regional security talks [1]. "
                "Iran held talks with 中U.S. officials [1]."
            ),
            "lines": [{"n": 1, "text": "Iran met 中U.S. officials during regional security talks [1]."}],
        }
    )

    result = compose_l1_brief(raw, (story,), provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert "中U.S. officials" in result.world_brief


def test_l1_sentence_scope_uses_javascript_whitespace() -> None:
    stories = (
        _story(
            "story-1",
            "Iran announced major military operations today",
            "Reuters",
            link="https://example.test/iran",
            sources=("Reuters", "AP News"),
        ),
        _story(
            "story-2",
            "Russia announced major military operations today",
            "BBC",
            link="https://example.test/russia",
            sources=("BBC", "AFP"),
        ),
    )
    raw = json.dumps(
        {
            "lead": "Russia announced major operations [1].\ufeffIran announced major operations [2].",
            "lines": [
                {"n": 1, "text": "Iran announced major military operations today [1]."},
                {"n": 2, "text": "Russia announced major military operations today [2]."},
            ],
        }
    )

    assert compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b") is None


def test_l1_anchor_gate_uses_utf16_length_and_ascii_capitalization() -> None:
    astral_story = _story(
        "story-astral",
        "A𝔸B crisis deepens across markets",
        "Reuters",
        link="https://example.test/astral",
        sources=("Reuters", "AP News"),
    )
    missing_astral_anchor = json.dumps(
        {
            "lead": "The crisis deepens across markets as pressure builds [1].",
            "lines": [{"n": 1, "text": "A𝔸B crisis deepens across markets [1]."}],
        }
    )
    assert compose_l1_brief(missing_astral_anchor, (astral_story,), provider="ollama", model="llama3.1:8b") is None

    non_ascii_story = _story(
        "story-non-ascii",
        "Éclair officials announce routine policy change affecting markets",
        "Reuters",
        link="https://example.test/eclair",
        sources=("Reuters", "AP News"),
    )
    no_ascii_anchor = json.dumps(
        {
            "lead": "Officials announce routine policy change affecting markets today [1].",
            "lines": [{"n": 1, "text": "Éclair officials announce routine policy change affecting markets [1]."}],
        }
    )
    assert compose_l1_brief(no_ascii_anchor, (non_ascii_story,), provider="ollama", model="llama3.1:8b")


def test_l1_anchor_gate_requires_two_matches_when_the_primary_corpus_has_four() -> None:
    stories = (
        _story(
            "story-1",
            "Iran threatens Strait of Hormuz closure",
            "Reuters",
            link="https://example.test/1",
            sources=("Reuters", "AP News"),
        ),
        _story(
            "story-2",
            "Turkey raises interest rates sharply",
            "Bloomberg",
            link="https://example.test/2",
            sources=("Bloomberg",),
        ),
    )
    raw = json.dumps(
        {
            "lead": "Iran faces pressure as regional tensions continue to rise [1].",
            "lines": [
                {"n": 1, "text": "Iran threatens the Strait of Hormuz closure [1]."},
                {"n": 2, "text": "Turkey raises interest rates sharply [2]."},
            ],
        }
    )

    assert compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b") is None


def test_public_l2_applies_only_proper_noun_headline_fallback_and_filters_bad_link() -> None:
    story = _story(
        "story-1",
        "Iran threatens to close Strait of Hormuz",
        "Reuters",
        link="javascript:alert(1)",
        sources=("Reuters", "AP News"),
    )

    result = compose_l2_brief(
        "President Macron says the Strait of Hormuz may close soon.",
        story,
        provider="groq",
        model="llama-3.3-70b-versatile",
        failure_code="INSIGHTS_SYNTHESIS_GATE",
    )

    assert result.brief_kind == "l2"
    assert result.quality == "degraded"
    assert result.world_brief == story.primary_title
    assert result.provider == "groq+headline-fallback"
    assert result.brief_story_lines == ()
    assert result.sources == ()
    assert result.validation == {
        "failure_code": "INSIGHTS_SYNTHESIS_GATE",
        "headline_fallback": True,
    }


def test_parser_keeps_first_valid_line_and_enforces_inclusive_bounds_and_half_floor() -> None:
    def payload(lead_length: int, lines: list[dict[str, object]]) -> str:
        return json.dumps({"lead": "L" * lead_length, "lines": lines})

    line = {"n": 1, "text": "A grounded story line [1]."}
    assert parse_brief_synthesis(payload(40, [line]), 1) is not None
    assert parse_brief_synthesis(payload(700, [line]), 1) is not None
    assert parse_brief_synthesis(payload(39, [line]), 1) is None
    assert parse_brief_synthesis(payload(701, [line]), 1) is None
    assert parse_brief_synthesis(payload(40, [line]), 3) is None

    parsed = parse_brief_synthesis(
        payload(
            40,
            [
                line,
                {"n": 1, "text": "A later duplicate must not replace it [1]."},
                {"n": 2, "text": "A second grounded story line [2]."},
            ],
        ),
        3,
    )
    assert parsed is not None
    assert parsed[1][0] == (1, "A grounded story line [1].")


def test_parser_matches_json_parse_number_coercion_and_utf16_lengths() -> None:
    lead = "😀" * 20
    line = "😀" * 8
    for index in (True, [1], "0x1"):
        parsed = parse_brief_synthesis(json.dumps({"lead": lead, "lines": [{"n": index, "text": line}]}), 1)
        assert parsed == (lead, ((1, line),))

    assert parse_brief_synthesis('{"lead":"' + ("L" * 40) + '","lines":[],"extra":NaN}', 1) is None
    huge_index = "9" * 400
    assert (
        parse_brief_synthesis(
            '{"lead":"' + ("L" * 40) + f'","lines":[{{"n":[{huge_index}],"text":"A grounded story [1]."}}]}}',
            1,
        )
        is None
    )
    scalarized = parse_brief_synthesis(
        json.dumps(
            {
                "lead": ("L" * 39) + "\ud83d",
                "lines": [{"n": 1, "text": ("T" * 14) + "\ud83d"}],
            },
            ensure_ascii=True,
        ),
        1,
    )
    assert scalarized == (("L" * 39) + "\ufffd", ((1, ("T" * 14) + "\ufffd"),))
    assert (
        parse_brief_synthesis(
            json.dumps({"lead": ("L" * 40) + "\x00", "lines": [{"n": 1, "text": "T" * 15}]}),
            1,
        )
        is None
    )
    huge_hex_index = "0x" + ("f" * 400)
    assert (
        parse_brief_synthesis(
            json.dumps(
                {
                    "lead": "L" * 40,
                    "lines": [{"n": huge_hex_index, "text": "A grounded story [1]."}],
                }
            ),
            1,
        )
        is None
    )


def test_arabic_indic_digits_are_not_javascript_citations_or_number_facts() -> None:
    assert verify_citation_indexes("Claim [١].", 1) == ("Claim [١].", 0)
    assert brief_module._numeric_facts("Digits ١٢ here.") == set()


def test_out_of_range_citations_are_stripped_before_scoped_gates() -> None:
    stories = (
        _story(
            "story-1",
            "Iran threatens to close Strait of Hormuz",
            "Reuters",
            link="https://example.test/1",
            sources=("Reuters", "AP News"),
        ),
    )
    lines = [{"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."}]
    with_real_citation = json.dumps(
        {
            "lead": "Iran threatens to close the Strait of Hormuz amid rising pressure [999] [1].",
            "lines": lines,
        }
    )
    invalid_only = json.dumps(
        {
            "lead": "Iran threatens to close the Strait of Hormuz amid rising pressure [999].",
            "lines": lines,
        }
    )

    result = compose_l1_brief(with_real_citation, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert result.validation["stripped_citations"] == 1
    assert "[999]" not in result.world_brief
    assert compose_l1_brief(invalid_only, stories, provider="ollama", model="llama3.1:8b") is None
    assert verify_citation_indexes("Claim [123], year [2026], source [1].", 2) == (
        "Claim, year [2026], source [1].",
        1,
    )


def test_multi_citation_unions_only_the_cited_story_fact_scopes() -> None:
    stories = (
        _story(
            "story-1",
            "Russia hits Ukrainian capital with ballistic missiles and drones",
            "AP News",
            link="https://example.test/1",
            sources=("AP News", "Wire"),
        ),
        _story(
            "story-2",
            "Nine killed in strikes on Kyiv, as Ukraine sinks Russian convoy",
            "BBC World",
            link="https://example.test/2",
            sources=("BBC World", "Wire"),
        ),
    )
    lines = [
        {"n": 1, "text": "Russia hits the Ukrainian capital with missiles and drones [1]."},
        {"n": 2, "text": "Nine were killed in strikes on Kyiv [2]."},
    ]
    correctly_scoped = json.dumps(
        {
            "lead": "Russia struck the Ukrainian capital with missiles and drones, killing nine people [1][2].",
            "lines": lines,
        }
    )

    result = compose_l1_brief(correctly_scoped, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert "[1][2]" in result.world_brief


def test_story_lines_use_only_the_pinned_noun_gate_and_canonical_source_index() -> None:
    stories = (
        _story(
            "story-1",
            "Iran threatens to close Strait of Hormuz",
            "Reuters",
            link="https://example.test/1",
            sources=("Reuters", "AP News"),
        ),
        _story(
            "story-2",
            "Turkey hikes interest rates to 50%",
            "Bloomberg",
            link="https://example.test/2",
            sources=("Bloomberg",),
        ),
    )
    raw = json.dumps(
        {
            "lead": "Iran raises the stakes around Hormuz [1] while Turkey delivers a dramatic rate hike [2].",
            "lines": [
                {"n": 1, "text": "President Macron predicts 999 closures [2]."},
                {"n": 2, "text": "Turkey raises interest rates to 999% [1]."},
            ],
        }
    )

    result = compose_l1_brief(raw, stories, provider="ollama", model="llama3.1:8b")

    assert result is not None
    assert result.validation["line_fallbacks"] == [1]
    assert result.brief_story_lines[0].text == "Iran threatens to close Strait of Hormuz [1]"
    assert result.brief_story_lines[1].text == "Turkey raises interest rates to 999%. [2]"


def test_dotted_acronym_citation_collapse_is_narrow_and_fail_closed() -> None:
    stories = (
        _story(
            "story-1",
            "GCC condemns Iranian attacks on Kuwait",
            "The National",
            link="https://example.test/gcc",
            sources=("The National", "Reuters"),
        ),
        _story(
            "story-2",
            "U.S. Embassies Urge Citizens to Consider Leaving the Region",
            "The Hindu",
            link="https://example.test/embassies",
            sources=("The Hindu", "AP News"),
        ),
    )
    lines = [
        {"n": 1, "text": "GCC condemns Iranian attacks on Kuwait [1]"},
        {"n": 2, "text": "U.S. Embassies urge citizens to consider leaving the region [2]"},
    ]

    def compose(lead: str):
        return compose_l1_brief(
            json.dumps({"lead": lead, "lines": lines}),
            stories,
            provider="ollama",
            model="llama3.1:8b",
        )

    assert (
        compose("GCC states condemned Iranian attacks on Kuwait [1]. The region was pressured by the U.S. [2].")
        is not None
    )
    assert (
        compose("GCC states condemned Iranian attacks on Kuwait [1]. The region was pressured by the U.S. [1].") is None
    )
    assert (
        compose(
            "Citizens were urged to leave the region by the U.S. [1] "
            "GCC states condemned Iranian attacks on Kuwait [2]."
        )
        is None
    )
    assert (
        compose("GCC states condemned Iranian attacks on Kuwait [1]. Citizens were urged to leave by the U.S. [1][2].")
        is not None
    )


def test_source_url_and_all_three_content_identities_are_canonical() -> None:
    story = _story(
        "story-1",
        "Iran threatens to close Strait of Hormuz",
        "Reuters",
        link="https://example.test",
        sources=("Reuters", "AP News"),
    )
    result = compose_l2_brief(
        "Iran may close the Strait of Hormuz as regional pressure builds.",
        story,
        provider="ollama",
        model="llama3.1:8b",
        failure_code="INSIGHTS_SYNTHESIS_PARSE",
    )
    assert result.sources[0].url == "https://example.test/"

    snapshot = {
        "projection_revision": "a" * 64,
        "selector_evaluated_at_ms": 1_786_928_400_000,
        "top_stories": [{"story_id": "story-1", "primary_title": "Iran threatens Strait closure"}],
        "selection_stats": {"brief_eligible_considered": 1, "brief_eligible_promoted": False},
        "selector_version": "worldmonitor_public_insights_0e8785c4",
        "identity_version": "worldmonitor_story_identity_f73de5b7",
    }
    selection = selection_fingerprint(snapshot)
    target = target_fingerprint(selection)
    payload = {
        "selection_fingerprint": selection,
        "target_fingerprint": target,
        "brief_kind": "l1",
        "quality": "ok",
        "world_brief": "Iran threatens the Strait of Hormuz [1].",
        "brief_story_lines": [{"n": 1, "text": "Iran threatens Strait closure [1]"}],
        "sources": [
            {
                "title": "Iran threatens Strait closure",
                "source": "Reuters",
                "url": "https://example.test/1",
            }
        ],
        "provider": "ollama",
        "model": "llama3.1:8b",
        "validation": {"failure_code": None},
        "created_at_ms": 1,
        "run_id": "volatile",
    }

    assert selection == "926dbe3c1c0dafae621cfe01cf1b0404c55d6eac99dbe43f060f1b1f81ebaa58"
    assert target == "be82f7a22b4391bc3f89d25444f5889a4bb1aa1793919d48cd6e459092f7234a"
    assert publication_id(payload) == "6f92895293a6d60af126a88948a3645e72c176b27d032fed1723206981296126"
    assert publication_id({**payload, "created_at_ms": 999, "run_id": "another"}) == publication_id(payload)


def test_get_brief_reads_current_publication_and_run_from_one_database_snapshot() -> None:
    target = "t" * 64
    publication = {
        "publication_id": "publication",
        "selection_fingerprint": "s" * 64,
        "target_fingerprint": target,
        "quality": "ok",
        "brief_kind": "l1",
        "world_brief": "Public brief [1].",
        "brief_story_lines": [],
        "top_stories": [],
        "selected_story_ids": [],
        "sources": [],
        "source_age_range": {"newest_ms": 1, "oldest_ms": 1},
        "provider": "fixture",
        "model": "fixture",
        "prompt_version": "prompt",
        "workflow_version": "workflow",
        "composer_version": "composer",
        "schema_version": "schema",
        "selector_version": "selector",
        "identity_version": "identity",
        "locale": "en",
        "validation": {"failure_code": None, "stripped_citations": 0, "line_fallbacks": []},
        "provenance": {},
        "published_at_ms": 1,
        "created_at_ms": 1,
    }
    run = {
        "run_id": "run",
        "target_fingerprint": target,
        "selection_fingerprint": "s" * 64,
        "status": "published",
        "model_outcome": "ok",
        "pointer_action": "advance_ok",
        "failure_count": 0,
        "next_due_at_ms": None,
        "lease_expires_at_ms": None,
        "last_error_code": None,
        "created_at_ms": 1,
        "updated_at_ms": 1,
        "last_attempt_at_ms": 1,
        "completed_at_ms": 1,
    }
    joined = {
        "current_row": {
            "target_fingerprint": target,
            "pending_due_at_ms": None,
        },
        "publication_row": publication,
        "run_row": run,
    }

    class _Cursor:
        def fetchone(self):
            return joined

    class _Connection:
        calls = 0

        def execute(self, statement):
            self.calls += 1
            assert self.calls == 1
            assert "to_jsonb" in statement
            return _Cursor()

    connection = _Connection()
    result = brief_store_module.get_brief(type("Repository", (), {"conn": connection})(), now_ms=2)

    assert connection.calls == 1
    assert result == {
        "state": "current",
        "target_fingerprint": target,
        "pending_due_at_ms": None,
        "publication": publication,
        "latest_run": run,
    }


def test_grounding_uses_pinned_unicode_17_token_boundaries() -> None:
    assert brief_module._numeric_facts("Rate is 12%.") == {"number:12%"}
    assert brief_module._numeric_facts("Rate is \ua7ce12%.") == set()
    assert validate_no_hallucinated_proper_nouns("Alpha moved.", "\U00011db7Alpha moved.") == (
        False,
        ("alpha",),
    )
    assert validate_no_hallucinated_proper_nouns("Foo\ua7ceBar announced.", "Foo Bar announced.") == (
        False,
        ("foo\ua7cfbar",),
    )
