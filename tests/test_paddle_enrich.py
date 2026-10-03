"""Unit tests for enrichment result normalisation. No network, no DB, no LLM."""
from __future__ import annotations

from typing import Any

import pytest

from app.services.paddle_enrich import (
    COMPLAINT_CATEGORIES,
    TOPIC_VOCAB,
    _clamp_score,
    enrich_batch,
    normalise_result,
)


def test_normalise_happy_path():
    out = normalise_result({
        "sentiment_label": "negative",
        "sentiment_score": -0.8,
        "topics": ["durability", "quality_defect"],
        "is_crisis": True,
        "is_opportunity": False,
        "complaint_category": "delamination",
        "mentioned_competitors": ["Selkirk"],
    })
    assert out["sentiment_label"] == "negative"
    assert out["sentiment_score"] == -0.8
    assert out["topics"] == ["durability", "quality_defect"]
    assert out["is_crisis"] is True
    assert out["complaint_category"] == "delamination"
    assert out["mentioned_competitors"] == ["Selkirk"]


def test_unknown_labels_fall_back_to_safe_defaults():
    """A hallucinated label must never reach the DB."""
    out = normalise_result({
        "sentiment_label": "extremely thrilled",
        "complaint_category": "paddle exploded",
        "topics": ["vibes", "power", "not_a_topic"],
    })
    assert out["sentiment_label"] == "neutral"
    assert out["complaint_category"] == "none"
    assert out["topics"] == ["power"]


def test_topics_are_capped_at_four():
    out = normalise_result({"topics": list(TOPIC_VOCAB)})
    assert len(out["topics"]) == 4


def test_topic_casing_and_spacing_normalised():
    out = normalise_result({"topics": ["Price Value", "  SPIN "]})
    assert out["topics"] == ["price_value", "spin"]


def test_unknown_competitors_dropped_and_deduped():
    out = normalise_result({"mentioned_competitors": ["selkirk", "Selkirk", "Acme Paddles"]})
    assert out["mentioned_competitors"] == ["Selkirk"]


def test_missing_keys_produce_full_row():
    out = normalise_result({})
    assert set(out) == {
        "sentiment_label", "sentiment_score", "topics", "is_crisis",
        "is_opportunity", "complaint_category", "mentioned_competitors",
    }
    assert out["sentiment_label"] == "neutral"
    assert out["sentiment_score"] == 0.0
    assert out["topics"] == []
    assert out["is_crisis"] is False


def test_score_is_clamped_to_db_range():
    """sentiment_score is NUMERIC(4,3) — an out-of-range value would error."""
    assert _clamp_score(9.9) == 1.0
    assert _clamp_score(-42) == -1.0
    assert _clamp_score("not a number") == 0.0
    assert _clamp_score(None) == 0.0


def test_complaint_category_vocab_matches_none_sentinel():
    assert "none" in COMPLAINT_CATEGORIES


# ---------------------------------------------------- batch alignment (regression)

def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"r{i}", "source": "yotpo", "external_review_id": f"e{i}", "body": f"b{i}"}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_results_are_paired_by_echoed_index_not_position(monkeypatch):
    """Regression: the model dropped an item on real input and every later
    result shifted up a slot, writing "positive" onto a 1-star complaint.
    Pairing must follow the echoed "i", never list position."""
    rows = _rows(3)
    # Model answers out of order and omits i=1 entirely.
    payload = {"results": [
        {"i": 2, "sentiment_label": "negative"},
        {"i": 0, "sentiment_label": "positive"},
    ]}

    async def fake_chat_json(**_kwargs):
        return payload

    monkeypatch.setattr("app.services.paddle_enrich.chat_json", fake_chat_json)
    patches = await enrich_batch(rows)

    got = {p["external_review_id"]: p["sentiment_label"] for p in patches}
    assert got == {"e0": "positive", "e2": "negative"}   # e1 skipped, not shifted


@pytest.mark.asyncio
async def test_result_without_usable_index_is_discarded(monkeypatch):
    async def fake_chat_json(**_kwargs):
        return {"results": [{"sentiment_label": "positive"}, {"i": "x"}, {"i": 99}]}

    monkeypatch.setattr("app.services.paddle_enrich.chat_json", fake_chat_json)
    assert await enrich_batch(_rows(2)) == []


@pytest.mark.asyncio
async def test_patches_carry_the_upsert_conflict_target(monkeypatch):
    async def fake_chat_json(**_kwargs):
        return {"results": [{"i": 0, "sentiment_label": "neutral"}]}

    monkeypatch.setattr("app.services.paddle_enrich.chat_json", fake_chat_json)
    patch = (await enrich_batch(_rows(1)))[0]
    assert patch["source"] == "yotpo" and patch["external_review_id"] == "e0"
