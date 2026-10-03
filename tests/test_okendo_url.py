"""Regression guard for the Okendo pagination URL bug (plan §2.2, §8).

Okendo returns `nextUrl` relative to the API root but WITHOUT the `/v1`
version segment. Resolving it against `https://api.okendo.io` alone produces a
URL the gateway answers with **403 Forbidden**, which looks like a scraper
block rather than a bad path — so this failure mode burns hours if it silently
regresses. Every assertion here is pure: no network, no fixtures, no DB.
"""
from __future__ import annotations

import pytest

from app.services.okendo import (
    API_BASE,
    API_ROOT,
    SELKIRK_STORE_ID,
    resolve_next_url,
    reviews_url,
)

# Verbatim shape of a real `nextUrl` captured from the live API on 2026-08-18.
LIVE_NEXT_URL = (
    "/stores/51eb4f5f-5c6e-4e06-9280-1145c4fb7894"
    "/products/shopify-8000169181286/reviews?limit=100"
    "&lastEvaluated=%7B%22reviewId%22%3A%22fb73b31e-239e-477c-9c76-dff19e724602%22%7D"
)


class TestResolveNextUrl:
    def test_reattaches_the_v1_prefix_that_okendo_strips(self) -> None:
        """THE BUG: the /v1 segment must be re-added or the API returns 403."""
        resolved = resolve_next_url(LIVE_NEXT_URL)

        assert resolved == API_BASE + LIVE_NEXT_URL
        assert resolved is not None
        assert resolved.startswith("https://api.okendo.io/v1/stores/")

    def test_does_not_produce_the_403_url_missing_v1(self) -> None:
        """Guards the specific wrong answer: joining against the bare root."""
        resolved = resolve_next_url(LIVE_NEXT_URL)

        assert resolved != API_ROOT + LIVE_NEXT_URL
        assert resolved is not None
        assert "/v1/stores/" in resolved

    def test_preserves_the_lastevaluated_cursor_untouched(self) -> None:
        """Re-encoding the cursor would break paging just as badly as a 403."""
        resolved = resolve_next_url(LIVE_NEXT_URL)

        assert resolved is not None
        assert "lastEvaluated=%7B%22reviewId%22" in resolved
        assert "limit=100" in resolved

    def test_does_not_double_prefix_an_already_versioned_path(self) -> None:
        """If Okendo ever starts including /v1, /v1/v1/... must not appear."""
        resolved = resolve_next_url("/v1/stores/abc/products/shopify-1/reviews")

        assert resolved == "https://api.okendo.io/v1/stores/abc/products/shopify-1/reviews"
        assert "/v1/v1/" not in resolved

    def test_adds_a_missing_leading_slash(self) -> None:
        resolved = resolve_next_url("stores/abc/reviews")

        assert resolved == "https://api.okendo.io/v1/stores/abc/reviews"

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_no_next_page_resolves_to_none(self, empty: str | None) -> None:
        """Absent/blank cursor is the normal termination signal, not an error."""
        assert resolve_next_url(empty) is None

    def test_accepts_an_absolute_on_host_versioned_cursor(self) -> None:
        absolute = "https://api.okendo.io/v1/stores/abc/reviews?limit=100"

        assert resolve_next_url(absolute) == absolute

    def test_rejects_an_absolute_on_host_cursor_missing_v1(self) -> None:
        """That URL is exactly the one that 403s — refuse it rather than fetch it."""
        assert resolve_next_url("https://api.okendo.io/stores/abc/reviews") is None

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example.com/stores/abc/reviews",
            "http://api.okendo.io.evil.example.com/v1/stores/abc/reviews",
        ],
    )
    def test_refuses_off_host_cursors(self, hostile: str) -> None:
        """A cursor is data from the network; it must not steer us to another host."""
        assert resolve_next_url(hostile) is None


class TestReviewsUrl:
    def test_first_page_url_is_versioned_and_shopify_prefixed(self) -> None:
        url = reviews_url(SELKIRK_STORE_ID, "8000169181286")

        assert url == (
            f"https://api.okendo.io/v1/stores/{SELKIRK_STORE_ID}"
            "/products/shopify-8000169181286/reviews?limit=100"
        )

    def test_limit_is_configurable(self) -> None:
        assert reviews_url("s", "1", limit=25).endswith("reviews?limit=25")
