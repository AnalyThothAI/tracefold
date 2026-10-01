"""Render selected adopted claims without taking notification decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import dspy  # type: ignore[import-untyped]

from ..generation_capacity import NewsGenerationCapacity
from ..notifications.card import card_copy_material
from ..notifications.contracts import CardCopy, ReaderRepairContext
from ..updates.contracts import Claim, Source
from ..updates.identity import canonical_json, identity
from ..updates.judgment import ContractFault
from . import generation
from .generation import ADAPTER_VERSION, references

CARD_INSTRUCTION: Final = """Render the selected adopted claims into Chinese. Each claim_ref gets exactly one
line. Translate its statement faithfully; use its structured fields to preserve the exact actor, action,
object, quantity, period, conditions, speaker, uncertainty and phase. The quote is evidence for that claim;
provenance identifies who reported it and supplies no additional event facts. Do not reinterpret a claim.
A captured underwater drone is not a sunk submarine. Persian زهپاد/پهپاد means a drone, not a submarine.
A second drone is not a second submarine. Reported or
claimed is not verified; proposed or announced is not effective or executing. Do not strengthen the verb,
replace the object, change the count, omit a material qualifier, or add a new actor, event or causal claim.
The headline may compress only these selected facts and must obey the same constraints. Do not classify,
reject or deduplicate claims. The selection is already made. No unselected claims or new source identifiers.
Use the supplied short claim_ref exactly once per claim. Write plain Chinese text with no URLs
(http/https/www), control characters or newline in the headline. Do not copy source links into prose.
Citation source metadata supplies provenance, not additional assertions; preserve the adopted speaker
and uncertainty without upgrading a report to verification. Keep essential actors, locations and objects.
A claim with `earlier` was preceded by a message the reader already received (earlier.delivered_text).
That text only shows what not to repeat. It is never a source of facts, names, terms or numbers: take every
name, term and number from the claim itself, even where the earlier text words the same thing differently.
For render "increment", open the line with "补充：" and write only what the claim adds; do not restate or
name the earlier fact. For render "correction", open the line with "更正：", say which earlier statement is
corrected and state the corrected fact.
"""


class CopySignature(dspy.Signature):  # type: ignore[misc]
    selected_claims_json: str = dspy.InputField(
        desc="Only selected adopted claims with short reference aliases, exact citations and source provenance."
    )
    result: CardCopy = dspy.OutputField(desc="A Chinese headline and exactly one section per selected claim.")


class DspyCardComposer:
    def __init__(
        self,
        lm_factory: Callable[[], Any],
        *,
        model_identity: str,
        generation_capacity: NewsGenerationCapacity | None = None,
    ) -> None:
        self.lm_factory = lm_factory
        self.generation_capacity = generation_capacity
        self.identity = identity(
            "news_card_copy", ADAPTER_VERSION, CARD_INSTRUCTION, CopySignature.model_json_schema(), model_identity
        )

    async def compose(
        self,
        claims: tuple[Claim, ...],
        *,
        sources: Mapping[str, Source],
        earlier: Mapping[str, ReaderRepairContext] | None = None,
    ) -> CardCopy:
        if not claims:
            raise ContractFault("news_empty_card_selection")
        aliases = {claim.ref: f"c{index}" for index, claim in enumerate(claims, 1)}
        selected = [references(row, aliases) for row in card_copy_material(claims, sources, earlier)]
        prediction = await generation.generate(
            CopySignature.with_instructions(CARD_INSTRUCTION),
            self.lm_factory(),
            capacity=self.generation_capacity,
            selected_claims_json=canonical_json(selected),
        )
        copy = CardCopy.model_validate(prediction.result)
        decoded = references(copy.model_dump(mode="json"), {alias: ref for ref, alias in aliases.items()})
        if any(line["claim_ref"] not in aliases for line in decoded["lines"]):
            raise ContractFault("news_card_claim_reference_unknown")
        return CardCopy.model_validate(decoded)
