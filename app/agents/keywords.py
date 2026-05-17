"""Agent D — Keyword Discovery & Enrichment. STUB. Implemented in week 3."""
from __future__ import annotations

from uuid import UUID


async def run_keywords(run_id: UUID) -> dict:
    """DataForSEO Keyword Ideas + Related; deduped, capped at MAX_KEYWORDS_PER_PROJECT.

    Then a deterministic keyword-to-page mapping pass (token-overlap on title/h1/url).
    """
    raise NotImplementedError("Agent D is a stub — implementation comes in week 3.")
