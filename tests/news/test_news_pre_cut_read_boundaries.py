"""The two read boundaries that carry the ledger's pre-cut history across #675 PR-2.

`news_judgment_v3` deleted `magnitude`, `audience` and the whole `TradeRelevanceV1` object, and
`news_editorial_v4` deleted `relevance`. None of those rows is migrated: a 30-day verdict retention
means most of the learning corpus, the release metric's production judgments and every recorded
`TriageContext` still carry them, and `storage.learning` selects `news_judgment_v2` rows by name so the
history stays reachable.

Both contracts forbid unknown keys, so without an explicit adaptation the whole pre-cut ledger becomes
unreadable the moment the image ships -- silently, in the learning plane's case, because
`dataset._selected_context` answered a `ValidationError` with ``None`` and the episode simply left the
corpus. The fixture is copied verbatim out of production for exactly that reason: a hand-written v2
document proves only that the adaptation reads what the test author imagined the workers wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TriageContext

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "pre_cut_judgment_and_context.json"


def _fixture() -> dict[str, Any]:
    return dict(json.loads(_FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def stored() -> dict[str, Any]:
    payload = _fixture()
    judgment = dict(payload["judgment"])
    # The fixture is only evidence if it is actually the shape the cut deleted.
    assert judgment["judgment_contract_version"] == "news_judgment_v2"
    assert {"magnitude", "audience"} <= set(judgment["verdict"])
    assert judgment["editorial"]["editorial_contract_version"] == "news_editorial_v3"
    assert "relevance" in judgment["editorial"]
    return payload


def test_a_recorded_v2_judgment_reads_into_the_current_contract_and_keeps_its_stored_digest(
    stored: dict[str, Any],
) -> None:
    """The property the release metric and the frozen corpus both depend on.

    `ScoredJudgment.from_stored` hashes the two columns *in the shape they are stored in*, which is what
    makes the result comparable with the `judgment_sha256` column beside them -- validating them into the
    current models first would hash the adapted shape and no stored row would ever match again. The
    retired fields are dropped only after that digest is verified.
    """

    judgment = stored["judgment"]
    scored = ScoredJudgment.from_stored(
        judgment_contract_version=judgment["judgment_contract_version"],
        verdict=judgment["verdict"],
        editorial=judgment["editorial"],
    )
    # The digest the writer computed over the stored document, unchanged.
    assert scored.scored_judgment_sha256 == judgment["scored_judgment_sha256"]
    assert scored.reconstructed_from == "news_judgment_v2"
    # Read in the current shape: no retired field survives, and the envelope is the v4 projection.
    assert scored.verdict.fact_kind is None and scored.verdict.evidence_ref == ""
    assert not hasattr(scored.verdict, "magnitude") and not hasattr(scored.verdict, "audience")
    assert scored.editorial.editorial_contract_version == "news_editorial_v4"
    assert not hasattr(scored.editorial, "relevance")
    assert scored.editorial.source_authority == judgment["editorial"]["source_authority"]


def test_the_reconstruction_round_trips_through_the_frozen_corpus(stored: dict[str, Any]) -> None:
    """`dataset` stores `scored.model_dump()` and `metric` validates it back, so the dump has to reload.

    Without the `reconstructed_from` marker on the dump this is the failure that would only appear on the
    second read: the reloaded document says `news_judgment_v2` but carries the adapted verdict, so a
    digest recomputed from it would address neither the stored row nor anything else.
    """

    judgment = stored["judgment"]
    scored = ScoredJudgment.from_stored(
        judgment_contract_version=judgment["judgment_contract_version"],
        verdict=judgment["verdict"],
        editorial=judgment["editorial"],
    )
    frozen = scored.model_dump(mode="json")
    assert frozen["reconstructed_from"] == "news_judgment_v2"
    reloaded = ScoredJudgment.model_validate(frozen)
    assert reloaded.scored_judgment_sha256 == scored.scored_judgment_sha256
    assert reloaded.model_dump(mode="json") == frozen


def test_a_corrupted_pre_cut_document_is_still_refused(stored: dict[str, Any]) -> None:
    """Dropping the retired fields is an adaptation, not an amnesty: the digest is verified first."""

    judgment = stored["judgment"]
    tampered = {**judgment["verdict"], "headline_zh": "改写过的标题"}
    payload = {
        "judgment_contract_version": "news_judgment_v2",
        "verdict": tampered,
        "editorial": judgment["editorial"],
        "verdict_sha256": canonical_sha(dict(judgment["verdict"])),
    }
    with pytest.raises(ValidationError, match="news_scored_judgment_identity_mismatch"):
        ScoredJudgment.model_validate({**payload, "scored_judgment_sha256": canonical_sha(payload)})

    envelope = {**judgment["editorial"], "source_authority": "official_primary"}
    with pytest.raises(ValidationError, match="news_editorial_hash_mismatch"):
        EditorialEnvelope.model_validate(envelope)


def test_a_recorded_context_is_read_as_a_reconstruction_rather_than_dropped(stored: dict[str, Any]) -> None:
    """#679 review 2. A told entry projects the verdict, so every archived one carries a `magnitude`.

    `ToldLedgerEntry` and `_ModelVisibleToldEntry` both forbid unknown keys, so the recorded context
    fails validation outright -- and the learning plane's answer to that was `None`, which would have
    quietly emptied the corpus of its entire pre-cut history rather than failing loudly.
    """

    document = stored["context"]
    assert document["told"]["entries"], "the fixture has to carry told entries to be evidence"
    assert all("magnitude" in entry for entry in document["told"]["entries"])
    with pytest.raises(ValidationError, match="magnitude"):
        TriageContext.model_validate(document)

    adapted, reconstructed_from = TriageContext.adapt_archived(document)
    assert reconstructed_from == "news_judgment_v2"
    context = TriageContext.model_validate(adapted)
    assert len(context.told.entries) == len(document["told"]["entries"])
    # Nothing but the retired key moved: the ledger the model saw is the ledger the replay sees.
    assert [entry.event_id for entry in context.told.entries] == [
        entry["event_id"] for entry in document["told"]["entries"]
    ]
    assert [entry.headline_zh for entry in context.told.entries] == [
        entry["headline_zh"] for entry in document["told"]["entries"]
    ]


def test_a_context_that_needs_no_adaptation_is_not_marked_as_one(stored: dict[str, Any]) -> None:
    """A native recording carries no marker, so `reconstructed_from` means what it says."""

    adapted, _ = TriageContext.adapt_archived(stored["context"])
    again, reconstructed_from = TriageContext.adapt_archived(adapted)
    assert reconstructed_from is None
    assert again == adapted
