"""Offline guard for the Judge.me fragment parser.

The fixture is a verbatim `reviews_for_widget` response fragment captured
2026-08-18 from CRBN product 8555527864472 (page 1 of 214 reviews). It was
chosen because it happens to contain every edge case we depend on: ratings of
1, 4 and 5 (so an all-5s parse cannot pass by accident), three "Anonymous"
authors, and two empty `.jdgm-rev__title` nodes.

No network here on purpose — this test is what makes a Judge.me markup change
fail loudly in CI instead of silently yielding NULL ratings in production.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.judgeme import (
    ProductContext,
    RatingParseError,
    parse_fragment,
)

FIXTURE = Path(__file__).parent / "fixtures" / "judgeme_widget_fragment.html"
EXPECTED_REVIEWS = 10


@pytest.fixture(scope="module")
def fragment() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ctx() -> ProductContext:
    return ProductContext(
        source_product_id="8555527864472",
        brand="CRBN",
        brand_id="test-brand-id",
        canonical_name="CRBN TruFoam Genesis (Square)",
        shop="crbn-pickleball.myshopify.com",
    )


@pytest.fixture(scope="module")
def reviews(fragment: str, ctx: ProductContext):
    return [r.finalise() for r in parse_fragment(fragment, ctx)]


def test_parses_every_review_node(reviews) -> None:
    assert len(reviews) == EXPECTED_REVIEWS


def test_every_review_has_a_rating_in_1_to_5(reviews) -> None:
    for rec in reviews:
        assert rec.rating is not None, f"NULL rating on {rec.external_review_id}"
        assert 1 <= rec.rating <= 5, f"{rec.rating} out of range"


def test_ratings_are_not_uniformly_five(reviews) -> None:
    """Guard against a parser that hardcodes or defaults to 5 stars."""
    assert len({r.rating for r in reviews}) > 1
    assert min(r.rating for r in reviews) == 1


def test_at_least_one_non_empty_body(reviews) -> None:
    assert any(rec.body and rec.body.strip() for rec in reviews)


def test_anonymous_author_becomes_none(reviews) -> None:
    """`.finalise()` must NULL out "" and "Anonymous" (plan §8 PII rule)."""
    anon = [r for r in reviews if r.reviewer_name is None]
    assert anon, "fixture is expected to contain Anonymous authors"
    for rec in reviews:
        assert (rec.reviewer_name or "").lower() != "anonymous"


def test_external_ids_are_judgeme_uuids_and_unique(reviews) -> None:
    ids = [r.external_review_id for r in reviews]
    assert len(set(ids)) == len(ids)
    for rid in ids:
        assert re.fullmatch(r"[0-9a-f-]{36}", rid), rid


def test_structural_fields_are_populated(reviews) -> None:
    assert all(r.source == "judgeme" for r in reviews)
    assert all(r.source_product_id == "8555527864472" for r in reviews)
    assert all(r.brand == "CRBN" for r in reviews)
    assert all(r.content_hash for r in reviews)
    assert any(r.posted_at and r.posted_at.startswith("20") for r in reviews)
    assert any(r.is_verified for r in reviews)


def test_missing_rating_markup_raises(fragment: str, ctx: ProductContext) -> None:
    """The whole point: stripped rating markup must raise, not yield NULL."""
    broken = fragment.replace("jdgm-rev__rating", "jdgm-rev__rating-renamed")
    with pytest.raises(RatingParseError):
        parse_fragment(broken, ctx)


def test_falls_back_to_aria_label_then_star_count(fragment: str, ctx: ProductContext) -> None:
    """data-score is only the first of three carriers of the same number."""
    baseline = [r.rating for r in parse_fragment(fragment, ctx)]

    no_score = re.sub(r"data-score='\d'", "", fragment)
    assert [r.rating for r in parse_fragment(no_score, ctx)] == baseline

    no_score_no_aria = re.sub(r"aria-label='\d star review'", "", no_score)
    assert [r.rating for r in parse_fragment(no_score_no_aria, ctx)] == baseline


def test_stacked_transparency_badges_are_all_read(reviews) -> None:
    """Regression: `select_one` dropped the incentive badge.

    Judge.me stacks badges and puts `review_earned_for_future_purchase` second,
    after `review_collected_via_store_invitation`. Reading only the first badge
    reported zero incentivised reviews across all 5,936 scraped rows.
    """
    incentivized = [r for r in reviews if r.is_incentivized]
    assert len(incentivized) == 8, "fixture has 8 earned-for-future-purchase reviews"
    for rec in incentivized:
        badges = (rec.context_values or {}).get("judgeme_badges") or []
        assert len(badges) > 1
        assert "review_earned_for_future_purchase" in badges
