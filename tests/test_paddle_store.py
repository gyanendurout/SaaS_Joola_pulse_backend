"""Unit tests for the shared paddle normalisation layer. No network, no DB."""
from __future__ import annotations

import pytest

from app.services.paddle_store import (
    ReviewRecord,
    _scrub_custom_fields,
    content_hash,
    detect_brand,
    flag_cross_source_duplicates,
)
from app.services.shopify_catalog import _is_paddle, is_accessory_title

# ---------------------------------------------------------------- content hash

def test_content_hash_is_whitespace_and_case_insensitive():
    """A retailer republishing the same review must collide on hash."""
    a = content_hash("Great  paddle,\n lots of spin", 5, "Dave K")
    b = content_hash("great paddle, lots of spin", 5, "dave k")
    assert a == b


def test_content_hash_separates_different_ratings():
    same_text = "Great paddle"
    assert content_hash(same_text, 5, "Dave") != content_hash(same_text, 1, "Dave")


def test_content_hash_handles_none_body_and_reviewer():
    assert len(content_hash(None, None, None)) == 64


# ------------------------------------------------------------------- finalise

@pytest.mark.parametrize("raw", ["", "   ", "Anonymous", "anonymous", "ANONYMOUS"])
def test_finalise_nulls_anonymous_reviewers(raw):
    """Bazaarvoice sends null, Judge.me sends "" / "Anonymous" — all mean unknown."""
    rec = ReviewRecord(source="judgeme", external_review_id="x1", reviewer_name=raw).finalise()
    assert rec.reviewer_name is None


def test_finalise_keeps_real_name_and_strips_whitespace():
    rec = ReviewRecord(
        source="okendo", external_review_id="x2", reviewer_name="  Dave K.  "
    ).finalise()
    assert rec.reviewer_name == "Dave K."


def test_finalise_populates_content_hash():
    rec = ReviewRecord(source="yotpo", external_review_id="x3", body="Solid", rating=4).finalise()
    assert rec.content_hash and len(rec.content_hash) == 64


def test_finalise_is_idempotent():
    rec = ReviewRecord(source="yotpo", external_review_id="x4", body="Solid", rating=4).finalise()
    first = rec.content_hash
    assert rec.finalise().content_hash == first


# ------------------------------------------------------------------------- PII

def test_scrub_drops_pii_shaped_keys():
    """Merchant-configurable field bags must never carry PII into the DB."""
    bag = {
        "email": "a@b.com", "Customer Email": "c@d.com", "phone": "555",
        "order_id": "1234", "Order Number": "9", "shipping_address": "x",
        "zip": "94110", "last_name": "Kim",
        "skill_level": "3.5", "playing_style": "banger", "height": "6ft",
    }
    cleaned = _scrub_custom_fields(bag)
    assert set(cleaned) == {"skill_level", "playing_style", "height"}


def test_finalise_scrubs_context_values():
    rec = ReviewRecord(
        source="yotpo", external_review_id="x5",
        context_values={"email": "a@b.com", "skill_level": "4.0"},
    ).finalise()
    assert rec.context_values == {"skill_level": "4.0"}


# --------------------------------------------------------------- brand detect

@pytest.mark.parametrize(
    "title,expected",
    [
        ("JOOLA Perseus Pro IV 16mm", "JOOLA"),
        ("Selkirk LUXX Control Air", "Selkirk"),
        ("Paddletek Bantam ESQ-C", "Paddletek"),
        ("CRBN TruFoam Genesis", "CRBN"),
        ("Six Zero Double Black Diamond", "Six Zero"),
        ("Proton Project Peacock", "other"),
    ],
)
def test_detect_brand(title, expected):
    assert detect_brand(title)[0] == expected


# ------------------------------------------------------- paddle vs accessory

def test_pickleball_substring_does_not_veto_a_paddle():
    """Regression: substring matching saw "ball" inside "Pickleball" and
    dropped every real paddle in the catalog on the first run."""
    product = {"title": "JOOLA Perseus Pro V Pickleball Paddle", "product_type": "Inventory Item"}
    assert _is_paddle(product, curated=True) is True
    assert _is_paddle(product, curated=False) is True


def test_curated_collection_keeps_paddle_without_the_keyword():
    """"SLK ERA Power" is a real paddle whose title never says "paddle"."""
    product = {"title": "SLK ERA Power", "product_type": "Paddle"}
    assert _is_paddle(product, curated=True) is True


def test_accessories_are_vetoed():
    for title in (
        "JOOLA Paddle Cover", "Selkirk SLK Tungsten Pickleball Paddle Tape",
        "Six Zero Cleaning Rubber Eraser", "JOOLA Tour Elite Pickleball Bag",
    ):
        assert is_accessory_title(title) is True, title


def test_non_paddle_product_without_keyword_is_excluded():
    product = {"title": "Gift Card", "product_type": "Gift Cards"}
    assert _is_paddle(product, curated=False) is False

def test_scrub_drops_demographics_but_keeps_play_attributes():
    """Pickleball Central's Yotpo widget collects an age band whose values
    include "Under 18". Demographics about the person are dropped; attributes
    of how they play are analytically useful and kept."""
    bag = {
        "Age": "Under 18", "Gender": "M", "Date of Birth": "2009",
        "Skill Level": "Intermediate (3.5-4.0)", "Frequency": "3x per week",
        "Previous Experience": "Yes",
    }
    cleaned = _scrub_custom_fields(bag)
    assert set(cleaned) == {"Skill Level", "Frequency", "Previous Experience"}


def test_scrub_keeps_words_merely_containing_age():
    """`age` must not swallow legitimate keys like "Average Play Time"."""
    assert "Average Play Time" in _scrub_custom_fields({"Average Play Time": "2h"})


# --------------------------------------------------------------- syndication

def test_retailer_copy_of_a_brand_site_review_is_flagged():
    """Measured live: 225 bodies appear on both a Judge.me brand site and
    Pickleball Central. Yotpo's own source_review_id is null on every row, so
    the content hash is the only detector."""
    shared = content_hash("Great paddle, tons of spin", 5, "Dave K")
    rows = [
        {"source": "judgeme", "content_hash": shared},
        {"source": "yotpo", "content_hash": shared},
        {"source": "yotpo", "content_hash": content_hash("Different body", 4, "Sam")},
    ]
    assert flag_cross_source_duplicates(rows) == 1
    assert rows[0].get("is_syndicated") is None      # brand site stays canonical
    assert rows[1]["is_syndicated"] is True
    assert rows[2].get("is_syndicated") is None


def test_duplicate_between_two_brand_sites_is_not_flagged():
    """Both are canonical sources; neither is a syndicated copy of the other."""
    shared = content_hash("Same words", 5, "Ann")
    rows = [
        {"source": "bazaarvoice", "content_hash": shared},
        {"source": "okendo", "content_hash": shared},
    ]
    assert flag_cross_source_duplicates(rows) == 0


def test_flagging_is_idempotent():
    shared = content_hash("Body", 5, "Kim")
    rows = [
        {"source": "judgeme", "content_hash": shared},
        {"source": "yotpo", "content_hash": shared},
    ]
    assert flag_cross_source_duplicates(rows) == 1
    assert flag_cross_source_duplicates(rows) == 0    # already marked


# ------------------------------------------ derived fields (regression)

def test_scrape_write_path_never_writes_the_derived_syndication_flag():
    """`is_syndicated` is derived from cross-source comparison, so the scrape
    path must not carry it. It used to, which meant every crawl reset the flag
    to False and its final value depended on stage ordering."""
    from app.agents.paddle_reviews import _review_row_for_db
    from scripts.paddle_load_staged import REVIEW_COLS

    row = {"source": "yotpo", "external_review_id": "e1", "is_syndicated": True,
           "rating": 5, "body": "text"}
    assert "is_syndicated" not in _review_row_for_db(row)
    assert "is_syndicated" not in REVIEW_COLS
    # the real payload columns must still survive
    assert _review_row_for_db(row)["rating"] == 5


def test_orphan_adoption_is_importable_from_the_app_package():
    """It lived in `scripts/`, which is not a package, so the orchestrator's
    import silently failed with "No module named 'scripts'" and orphan repair
    never ran in the scheduled pipeline."""
    from app.services.paddle_orphans import (
        SOURCE_TO_BRAND_SLUG,
        adopt_orphan_products,
        fetch_orphans,
        resolve_from_catalog,
    )

    assert SOURCE_TO_BRAND_SLUG["bazaarvoice"] == "joola"
    assert callable(adopt_orphan_products) and callable(fetch_orphans)
    assert callable(resolve_from_catalog)
