"""Agent B — On-Page SEO & Issue Detection. STUB. Implemented in week 2."""
from __future__ import annotations

from uuid import UUID

# 15 rule codes (see Artifact 5)
ISSUE_CODES = (
    "MISSING_TITLE",
    "TITLE_TOO_SHORT",
    "TITLE_TOO_LONG",
    "DUPLICATE_TITLE",
    "MISSING_META_DESC",
    "META_DESC_TOO_LONG",
    "DUPLICATE_META_DESC",
    "MISSING_H1",
    "MULTIPLE_H1",
    "THIN_CONTENT",
    "MISSING_CANONICAL",
    "NOINDEX_ON_IMPORTANT_PAGE",
    "BROKEN_INTERNAL_LINK",
    "IMAGES_MISSING_ALT",
    "MISSING_SCHEMA_FOR_PAGE_TYPE",
)


async def run_issues(run_id: UUID) -> dict:
    """Reads `pages` for the run, applies 15 deterministic rules, writes `issues`."""
    raise NotImplementedError("Agent B is a stub — implementation comes in week 2.")
