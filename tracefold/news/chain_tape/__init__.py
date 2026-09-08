"""Robinhood Chain ingestion; research and digest tasks read its committed PostgreSQL facts.

App composes the three stages independently. Only the ingestion loop is exported here: the research
and digest writers reach admission and storage, so App imports them directly to avoid an import cycle.
"""

from __future__ import annotations

from .loop import ChainTapeLoop

__all__ = ["ChainTapeLoop"]
