"""Load every staged paddle file into Supabase.

The scrapers always write a JSONL staging cache under `storage/paddle_staging/`
and only then try the DB, so a scrape is never lost when migration 011 has not
been applied yet. This script replays that cache through the *same* idempotent
upsert path.

Usage (from `backend/`):
    .venv/Scripts/python.exe scripts/paddle_load_staged.py
    .venv/Scripts/python.exe scripts/paddle_load_staged.py --dry-run
    .venv/Scripts/python.exe scripts/paddle_load_staged.py --only reviews_okendo
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.db import service_client
from app.services import paddle_store as ps
from app.services.dsg_catalog import to_product_records as dsg_products

PRODUCT_FILES = ("products_shopify", "products_pbc")
REVIEW_FILES = ("reviews_bazaarvoice", "reviews_okendo", "reviews_judgeme", "reviews_yotpo")
STATS_FILES = (
    "product_stats_bazaarvoice", "product_stats_okendo",
    "product_stats_judgeme", "product_stats_yotpo",
)

MIGRATION_HINT = """
paddle_* tables are missing — migration 011 has not been applied.

Apply it once (project convention: Supabase SQL editor):
  1. open the Supabase dashboard -> SQL editor
  2. paste backend/supabase/migrations/011_paddle_reviews.sql
  3. Run
  4. re-run this script

Nothing is lost meanwhile: every scrape is already staged on disk under
storage/paddle_staging/ and this script replays it.
""".strip()

# paddle_products / paddle_reviews column allow-lists. Staged rows come straight
# from the dataclasses, so an added local field must not break the DB write.
PRODUCT_COLS = {
    "brand_id", "brand", "source", "retailer", "source_product_id", "family_id",
    "canonical_name", "title", "handle", "product_url", "image_url", "price",
    "currency", "review_count", "avg_rating", "rating_distribution", "gtin",
    "is_paddle", "is_active", "last_seen_at",
}
REVIEW_COLS = {
    "brand_id", "source", "external_review_id", "source_product_id", "family_id",
    "canonical_name", "brand", "retailer", "reviewer_name", "reviewer_location",
    "rating", "title", "body", "pros", "cons", "secondary_ratings",
    "context_values", "posted_at", "is_verified", "is_recommended",
    "is_incentivized", "helpful_count", "unhelpful_count",
    "brand_response", "brand_response_at", "media_urls", "language_code",
    "source_sentiment", "content_hash", "scraped_at",
}


def _project(row: dict[str, Any], cols: set[str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k in cols}


def _dedupe(rows: list[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
    """Last-write-wins dedupe on a composite key.

    Required before an upsert: PostgREST rejects a single batch that contains the
    same conflict target twice ("ON CONFLICT DO UPDATE command cannot affect row
    a second time"), which a family-rollup source can easily produce.
    """
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        seen[tuple(row.get(k) for k in key)] = row
    return list(seen.values())


def load_products(db: Any, *, dry_run: bool, only: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in PRODUCT_FILES:
        if only and only != name:
            continue
        rows = [_project(r, PRODUCT_COLS) for r in ps.read_staged(name)]
        rows = _dedupe(rows, ("source", "source_product_id"))
        out[name] = len(rows)
        if rows and not dry_run:
            ps.upsert_products(db, rows)

    if not only or only == "products_dsg":
        dsg = [
            _project(r.to_row(), PRODUCT_COLS)
            for r in dsg_products(ps.brand_id_map(db))
        ]
        dsg = _dedupe(dsg, ("source", "source_product_id"))
        out["products_dsg"] = len(dsg)
        if dsg and not dry_run:
            ps.upsert_products(db, dsg)
    return out


def load_reviews(db: Any, *, dry_run: bool, only: str | None) -> tuple[dict[str, int], int]:
    """Load every review file. Returns `(per_file_counts, syndicated_flagged)`.

    All files are read before any write so cross-source syndication can be
    detected: a retailer copy is only identifiable by comparing its body hash
    against the brand-site sources, which live in a different file.
    """
    per_file: dict[str, list[dict[str, Any]]] = {}
    for name in REVIEW_FILES:
        rows = [_project(r, REVIEW_COLS) for r in ps.read_staged(name)]
        per_file[name] = _dedupe(rows, ("source", "external_review_id"))

    every_row = [r for rows in per_file.values() for r in rows]
    flagged = ps.flag_cross_source_duplicates(every_row)

    out: dict[str, int] = {}
    for name, rows in per_file.items():
        if only and only != name:
            continue
        out[name] = len(rows)
        if rows and not dry_run:
            ps.upsert_reviews(db, rows)
    return out, flagged


def apply_product_stats(db: Any, *, dry_run: bool) -> int:
    """Push per-product review_count / avg_rating onto paddle_products."""
    patched = 0
    for name in STATS_FILES:
        for row in ps.read_staged(name):
            source = row.get("source")
            pid = row.get("source_product_id")
            if not source or not pid:
                continue
            patch = {
                k: row[k]
                for k in ("review_count", "avg_rating", "rating_distribution", "family_id")
                if row.get(k) is not None
            }
            if not patch or dry_run:
                continue
            try:
                (
                    db.table("paddle_products").update(patch)
                    .eq("source", source).eq("source_product_id", str(pid)).execute()
                )
                patched += 1
            except Exception:
                continue
    return patched


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay staged paddle data into Supabase.")
    ap.add_argument("--dry-run", action="store_true", help="count staged rows, write nothing")
    ap.add_argument("--only", default=None, help="load a single staging file by name")
    args = ap.parse_args()

    db = service_client()
    staged = ps.list_staged()
    print(f"staging files present: {staged or '(none)'}")

    if not args.dry_run and not ps.tables_ready(db):
        print(MIGRATION_HINT)
        return 2

    products = load_products(db, dry_run=args.dry_run, only=args.only)
    reviews, syndicated = load_reviews(db, dry_run=args.dry_run, only=args.only)
    stats = apply_product_stats(db, dry_run=args.dry_run)
    linked = 0 if args.dry_run else ps.link_reviews_to_products(db)

    print(json.dumps({
        "dry_run": args.dry_run,
        "products": products,
        "products_total": sum(products.values()),
        "reviews": reviews,
        "reviews_total": sum(reviews.values()),
        "syndicated_flagged": syndicated,
        "product_stats_patched": stats,
        "reviews_linked_to_products": linked,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
