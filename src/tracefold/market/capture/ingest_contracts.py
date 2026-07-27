from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tracefold.market.capture.evidence_repository import EventRead


@dataclass(frozen=True, slots=True)
class IngestedEvent:
    event: EventRead
    entities: list[dict[str, Any]]
    inserted: bool
    token_intents: list[dict[str, Any]] = field(default_factory=list)
    token_resolutions: list[dict[str, Any]] = field(default_factory=list)
