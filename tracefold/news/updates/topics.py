"""IPTC navigation topics projected from Noul answers, and the code-owned key topic families."""

from __future__ import annotations

from typing import Final

from ..taxonomy import IPTC_CODEBOOK_SHA256, IPTC_SUBJECT_CODEBOOK

# The canonical, pinned News codebook. Topics are navigation; they do not decide news value on their own.
CODEBOOK: Final[tuple[tuple[str, str], ...]] = IPTC_SUBJECT_CODEBOOK
CODEBOOK_SHA256: Final = IPTC_CODEBOOK_SHA256
MAX_TOPICS: Final = 3
