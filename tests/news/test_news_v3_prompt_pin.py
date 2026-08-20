"""The Triage prompt is pinned to its version (#81).

Before this test the prompt — the artefact that changed most often and mattered most — had no test coverage at
all: `TRIAGE_PROMPT_SHA256` was computed at import time and referenced nowhere outside the consumer, and
`TRIAGE_PROMPT_VERSION` was a hand-written constant with no link to the bytes. Rewriting the whole prompt kept
`make test` green, and every stored `trace.prompt_sha256` was ambiguous about which text produced it.
"""

from __future__ import annotations

from tracefold.news.agents.prompts import (
    TRIAGE_PROMPT_SHA256,
    TRIAGE_PROMPT_SHA256_BY_VERSION,
    TRIAGE_SYSTEM_PROMPT,
    prompt_sha256,
)
from tracefold.news.models import TRIAGE_PROMPT_VERSION


def test_current_prompt_version_names_the_current_prompt_bytes() -> None:
    """Editing the prompt without bumping TRIAGE_PROMPT_VERSION fails here, with the new sha to record."""

    pinned = TRIAGE_PROMPT_SHA256_BY_VERSION.get(TRIAGE_PROMPT_VERSION)
    assert pinned is not None, (
        f"{TRIAGE_PROMPT_VERSION} is missing from TRIAGE_PROMPT_SHA256_BY_VERSION; "
        f"add it with sha {TRIAGE_PROMPT_SHA256}"
    )
    assert pinned == TRIAGE_PROMPT_SHA256, (
        f"the prompt text changed but {TRIAGE_PROMPT_VERSION} still points at {pinned}; "
        f"bump TRIAGE_PROMPT_VERSION and record sha {TRIAGE_PROMPT_SHA256}"
    )


def test_pinned_shas_are_hex_digests_and_unique() -> None:
    shas = list(TRIAGE_PROMPT_SHA256_BY_VERSION.values())
    assert len(set(shas)) == len(shas), "two prompt versions cannot share a sha"
    for version, sha in TRIAGE_PROMPT_SHA256_BY_VERSION.items():
        assert version.startswith("news_triage_prompt_v"), version
        assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), (version, sha)


def test_prompt_sha256_is_the_digest_of_the_frozen_text() -> None:
    assert prompt_sha256(TRIAGE_SYSTEM_PROMPT) == TRIAGE_PROMPT_SHA256
