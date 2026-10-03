"""Paddle reviews orchestrator — Stages 1-10 of PADDLE_REVIEWS_PLAN.md §5.

    STAGE 1  CATALOG      Shopify products.json (5 brands)
    STAGE 2  BAZAARVOICE  joola.com
    STAGE 3  OKENDO       selkirk.com
    STAGE 4  JUDGEME      paddletek.com + crbnpickleball.com (+ Six Zero ratings-only)
    STAGE 5  PBC CATALOG  pickleballcentral.com ids via the public Searchspring feed
    STAGE 6  YOTPO        pickleballcentral.com reviews (cross-brand retailer)
    STAGE 7  DICKS        catalog from the robots-declared sitemap harvest
    STAGE 8  NORMALISE    dedupe · family rollup · link reviews to products
    STAGE 9  ENRICH       services/paddle_enrich.py
    STAGE 10 UPSERT       Supabase, idempotent

Every stage stages its output to disk BEFORE touching the DB, so a scrape
survives an unapplied migration or a transient Supabase error and can be
replayed with `scripts/paddle_load_staged.py`.

Progress is published to the in-process event bus for SSE, exactly like
`app/agents/news_scraper.py`.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.config import get_settings
from app.db import service_client
from app.services import event_bus
from app.services import paddle_store as ps
from app.services.shopify_catalog import (
    BRANDS,
    UA,
    fetch_brand_catalog,
    to_product_record,
)

log = structlog.get_logger()

REQUEST_TIMEOUT = 30.0
INTER_PRODUCT_SLEEP = 0.4          # 0.35-1.0s hit zero blocks during research

# Unattended-run guards. A scheduled job must always terminate, so every stage is
# bounded and every failure is recorded rather than raised.
STAGE_TIMEOUT_SEC = 60 * 45        # Selkirk (~6.7k reviews) is the slowest source
RETRY_BACKOFF_SEC = 20
ENRICH_ROUND_SIZE = 500            # rows fetched per enrichment round


@dataclass
class StageResult:
    source: str
    ok: bool = False
    products: int = 0
    reviews_found: int = 0
    reviews_unique: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "products": self.products,
            "reviews_found": self.reviews_found,
            "reviews_unique": self.reviews_unique,
            "skipped_reason": self.skipped_reason,
            "errors": self.errors[:5],
        }


# Stage registry. Keys are the `sources` filter values accepted by the API.
STAGES: tuple[str, ...] = (
    "catalog", "bazaarvoice", "okendo", "judgeme", "pbc_catalog", "yotpo",
    "dicks", "dicks_reviews", "amazon",
)


def _utcnow() -> str:
    return ps.utcnow_iso()


def _store_error(
    db: Any, run_id: str, source: str, message: str,
    *, stage: str = "", target: str = "", error_type: str = "scrape_error",
) -> None:
    try:
        db.table("paddle_review_errors").insert({
            "run_id": run_id,
            "source": source,
            "stage": stage,
            "target": target,
            "error_type": error_type,
            "error_message": message[:500],
        }).execute()
    except Exception:
        # The error log is best-effort; never let it mask the real failure.
        pass


def _staged_products(source: str) -> list[dict[str, Any]]:
    """Staged catalog rows for one storefront source."""
    return [
        r for r in ps.read_staged("products_shopify")
        if r.get("source") == source and r.get("is_paddle")
    ]


# ============================================================================ #
# Stage 1 — catalog                                                              #
# ============================================================================ #

async def stage_catalog(db: Any, run_id: str) -> StageResult:
    res = StageResult(source="catalog")
    bmap = ps.brand_id_map(db)
    rows: list[dict[str, Any]] = []

    headers = {"User-Agent": UA, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True
    ) as client:
        for brand in BRANDS:
            try:
                raw = await fetch_brand_catalog(client, brand, paddles_only=True)
                rows.extend(
                    to_product_record(brand, p, bmap.get(brand.slug)).to_row()
                    for p in raw
                )
                event_bus.publish(
                    run_id, "stage_detail", stage="catalog",
                    brand=brand.slug, products=len(raw),
                )
            except Exception as exc:
                res.errors.append(f"{brand.slug}: {exc}")
                _store_error(db, run_id, "catalog", str(exc), stage="catalog", target=brand.slug)
            await asyncio.sleep(INTER_PRODUCT_SLEEP)

    ps.write_staged("products_shopify", rows)
    res.products = len(rows)
    res.ok = bool(rows)

    if ps.tables_ready(db):
        try:
            ps.upsert_products(db, [_product_row_for_db(r) for r in rows])
        except Exception as exc:
            res.errors.append(f"upsert: {exc}")
    return res


def _product_row_for_db(row: dict[str, Any]) -> dict[str, Any]:
    """Drop any local-only key so an added dataclass field can't break the write."""
    allowed = {
        "brand_id", "brand", "source", "retailer", "source_product_id", "family_id",
        "canonical_name", "title", "handle", "product_url", "image_url", "price",
        "currency", "review_count", "avg_rating", "rating_distribution", "gtin",
        "is_paddle", "is_active", "last_seen_at",
    }
    return {k: v for k, v in row.items() if k in allowed}


# ============================================================================ #
# Stages 2-5 — review sources                                                    #
# ============================================================================ #

async def stage_bazaarvoice(db: Any, run_id: str) -> StageResult:
    """JOOLA via Bazaarvoice.

    `productid` filtering is family-expanded — sibling variants return
    overlapping reviews — so the module dedupes on review Id across the whole
    run. Naive per-product sums over-count by ~2x (§2.1, D7).
    """
    from app.services import bazaarvoice

    products = _staged_products("joola_shopify")
    if not products:
        return StageResult(source="bazaarvoice", skipped_reason="no staged JOOLA products")

    outcome = await bazaarvoice.scrape_catalog(products)
    return _finish_review_stage(
        db, run_id,
        source="bazaarvoice",
        staging_name="reviews_bazaarvoice",
        stats_name="product_stats_bazaarvoice",
        records=outcome.records,
        stats=outcome.product_stats,
        products_scraped=outcome.products_scraped,
        errors=[str(e) for e in outcome.errors],
    )


async def stage_okendo(db: Any, run_id: str) -> StageResult:
    """Selkirk via Okendo cursor paging."""
    from app.services import okendo

    products = _staged_products("selkirk_shopify")
    if not products:
        return StageResult(source="okendo", skipped_reason="no staged Selkirk products")

    outcome = await okendo.scrape_products(products)
    return _finish_review_stage(
        db, run_id,
        source="okendo",
        staging_name="reviews_okendo",
        stats_name="product_stats_okendo",
        records=outcome.reviews,
        stats=[vars(s) for s in outcome.stats],
        products_scraped=outcome.products_scraped,
        errors=[f"{outcome.errors} product error(s)"] if outcome.errors else [],
    )


async def stage_judgeme(db: Any, run_id: str) -> StageResult:
    """Paddletek + CRBN via the public Judge.me widget endpoint.

    Six Zero runs the Judge.me v3 React widget, which exposes aggregates but no
    review text, so it lands as `text_unavailable` with ratings only (D13).
    """
    from app.services import judgeme

    products: list[dict[str, Any]] = []
    for source in ("paddletek_shopify", "crbn_shopify", "sixzero_shopify"):
        products.extend(_staged_products(source))
    if not products:
        return StageResult(source="judgeme", skipped_reason="no staged Judge.me products")

    results = await judgeme.scrape_products(products)
    records = [rec for r in results for rec in r.reviews]
    stats = [
        {
            "source": r.source,
            "source_product_id": r.source_product_id,
            "review_count": r.review_count,
            "avg_rating": r.avg_rating,
        }
        for r in results
    ]
    errors = [f"{r.source_product_id}: {r.error}" for r in results if r.error]
    parse_failures = sum(r.rating_parse_failures for r in results)
    if parse_failures:
        errors.append(f"{parse_failures} rating parse failure(s)")

    res = _finish_review_stage(
        db, run_id,
        source="judgeme",
        staging_name="reviews_judgeme",
        stats_name="product_stats_judgeme",
        records=records,
        stats=stats,
        products_scraped=sum(1 for r in results if r.status == "ok"),
        errors=errors,
    )
    text_unavailable = sum(1 for r in results if r.status == "text_unavailable")
    if text_unavailable:
        res.skipped_reason = f"{text_unavailable} product(s) ratings-only (Judge.me v3)"
    return res


async def stage_pbc_catalog(db: Any, run_id: str) -> StageResult:
    """Pickleball Central catalog — the BigCommerce ids the Yotpo stage consumes.

    The storefront 403s plain `httpx` and its category grids are client-rendered,
    so ids come from the public Searchspring feed that populates the grid rather
    than from DOM scraping. No evasion of the edge protection (D14/D17).
    """
    from app.services import bigcommerce_catalog as bc

    res = StageResult(source="pbc_catalog")
    brand_ids = ps.brand_id_map(db)
    rows: list[dict[str, Any]] = []

    catalogs = await bc.fetch_all_products(paddles_only=True)
    for brand in bc.TRACKED_BRANDS:
        for item in catalogs.get(brand.slug, []):
            record = bc.to_product_record(item, brand_ids, fallback=brand)
            if record:
                rows.append(_product_row_for_db(record.to_row()))
        event_bus.publish(
            run_id, "stage_detail", stage="pbc_catalog",
            brand=brand.slug, products=len(catalogs.get(brand.slug, [])),
        )

    # Dedupe: one paddle can surface under more than one brand facet.
    unique = {r["source_product_id"]: r for r in rows}
    rows = list(unique.values())

    ps.write_staged("products_pbc", rows)
    res.products = len(rows)
    res.ok = bool(rows)
    if not rows:
        res.skipped_reason = "Pickleball Central returned no products"

    if rows and ps.tables_ready(db):
        try:
            ps.upsert_products(db, rows)
        except Exception as exc:
            res.errors.append(f"upsert: {exc}")
            res.ok = False
    return res


async def stage_yotpo(db: Any, run_id: str) -> StageResult:
    """Pickleball Central via Yotpo — the cross-brand retailer feed.

    Depends on the PBC catalog being staged first, since BigCommerce product ids
    are what the Yotpo API consumes.
    """
    from app.services import yotpo

    products = [r for r in ps.read_staged("products_pbc") if r.get("source_product_id")]
    if not products:
        return StageResult(
            source="yotpo",
            skipped_reason="no staged Pickleball Central catalog (run the PBC catalog step first)",
        )

    records, stats, errors = await yotpo.fetch_reviews_for_products(products)
    return _finish_review_stage(
        db, run_id,
        source="yotpo",
        staging_name="reviews_yotpo",
        stats_name="product_stats_yotpo",
        records=records,
        stats=stats,
        products_scraped=len(stats),
        errors=[f"{e.get('source_product_id')}: {e.get('error')}" for e in errors],
    )


def _finish_review_stage(
    db: Any,
    run_id: str,
    *,
    source: str,
    staging_name: str,
    stats_name: str,
    records: list[Any],
    stats: list[dict[str, Any]],
    products_scraped: int,
    errors: list[str],
) -> StageResult:
    """Shared tail: finalise, dedupe, stage to disk, then upsert.

    Staging happens BEFORE the DB write so a scrape survives an unapplied
    migration or a transient Supabase error and can be replayed with
    `scripts/paddle_load_staged.py`.
    """
    res = StageResult(source=source, products=products_scraped, errors=list(errors))
    res.reviews_found = len(records)

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for rec in records:
        rec = rec.finalise()
        if not rec.external_review_id or rec.external_review_id in seen:
            continue
        seen.add(rec.external_review_id)
        rows.append(rec.to_row())

    res.reviews_unique = len(rows)
    ps.write_staged(staging_name, rows)
    if stats:
        ps.write_staged(stats_name, stats)
    res.ok = True

    for message in errors:
        _store_error(db, run_id, source, message, stage=source)

    if rows and ps.tables_ready(db):
        try:
            ps.upsert_reviews(db, [_review_row_for_db(r) for r in rows])
        except Exception as exc:
            res.errors.append(f"upsert: {exc}")
            res.ok = False
    return res


def _review_row_for_db(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "brand_id", "source", "external_review_id", "source_product_id", "family_id",
        "canonical_name", "brand", "retailer", "reviewer_name", "reviewer_location",
        "rating", "title", "body", "pros", "cons", "secondary_ratings",
        "context_values", "posted_at", "is_verified", "is_recommended",
        "is_incentivized", "helpful_count", "unhelpful_count",
        "brand_response", "brand_response_at", "media_urls", "language_code",
        "source_sentiment", "content_hash", "scraped_at",
    }
    return {k: v for k, v in row.items() if k in allowed}


# ============================================================================ #
# Stage 6 — Dick's catalog (review text gated on plan Open Question O3)           #
# ============================================================================ #

async def stage_dicks(db: Any, run_id: str) -> StageResult:
    """Load the sitemap-harvested Dick's catalog. Makes no network request.

    Review TEXT stays out of scope until DSG's public Bazaarvoice client id and
    `Bv-Bfd-Token` are bootstrapped from one PDP fetch — an explicit human
    decision because it spends Apify credit (plan O3). We never evade the edge
    block (D14/D17).
    """
    from app.services.dsg_catalog import to_product_records

    res = StageResult(source="dicks")
    records = to_product_records(ps.brand_id_map(db))
    rows = [_product_row_for_db(r.to_row()) for r in records]
    ps.write_staged("products_dsg", rows)
    res.products = len(rows)
    res.ok = bool(rows)
    res.skipped_reason = "review text gated on plan O3 (BV key bootstrap)"

    if rows and ps.tables_ready(db):
        try:
            ps.upsert_products(db, rows)
        except Exception as exc:
            res.errors.append(f"upsert: {exc}")
            res.ok = False
    return res


# ============================================================================ #
# Gated sources — wired but off until a credential/flag lands                     #
# ============================================================================ #

async def stage_dicks_reviews(db: Any, run_id: str) -> StageResult:
    """Dick's review text via their Bazaarvoice tenant. Gated on plan O3.

    The catalog is already solved (301 paddles, `stage_dicks`). Reviews need three
    public client-side values that live in one PDP's HTML: DSG's BV client id, its
    `Bv-Bfd-Token`, and the `Origin` header (the last one is missing from the plan
    and is what makes JOOLA's tenant 401 without it).

    Akamai 403s a genuine browser on that PDP, and we do not evade access controls
    (D14/D17), so the one page has to come through Apify. That spends third-party
    credit, so it stays behind `APIFY_ENABLED` as an explicit human decision.
    """
    settings = get_settings()
    if not settings.apify_enabled or not settings.apify_token:
        return StageResult(
            source="dicks_reviews",
            skipped_reason=(
                "APIFY_ENABLED=false — set it true (plan O3) to bootstrap DSG's "
                "Bazaarvoice client id + Bv-Bfd-Token + Origin from one PDP fetch"
            ),
        )
    if not (settings.dsg_bv_client and settings.dsg_bv_token):
        return StageResult(
            source="dicks_reviews",
            skipped_reason=(
                "Apify enabled but DSG_BV_CLIENT / DSG_BV_TOKEN are unset — run the "
                "one-off PDP bootstrap, then store both in .env"
            ),
        )
    try:
        from app.services import dsg_reviews
    except ImportError:
        return StageResult(
            source="dicks_reviews",
            skipped_reason="credentials present but app/services/dsg_reviews.py not built yet",
        )

    products = [r for r in ps.read_staged("products_dsg") if r.get("source_product_id")]
    records, stats, errors = await dsg_reviews.fetch_reviews_for_products(products)
    return _finish_review_stage(
        db, run_id,
        source="dicks", staging_name="reviews_dicks",
        stats_name="product_stats_dicks",
        records=records, stats=stats,
        products_scraped=len(stats), errors=[str(e) for e in errors],
    )


async def stage_amazon(db: Any, run_id: str) -> StageResult:
    """Amazon reviews. Gated on plan O1.

    Amazon's full review history is behind a login wall, which we do not attempt
    to defeat (D8). Two lawful paths, neither available yet:
      * Brand Registry / Seller Central export for JOOLA's own ASINs
      * Apify's public-data actor for competitor aggregates + recent review text
    """
    settings = get_settings()
    if not settings.apify_enabled or not settings.apify_token:
        return StageResult(
            source="amazon",
            skipped_reason=(
                "APIFY_ENABLED=false and no Brand Registry export configured "
                "(plan O1). Amazon full review history is login-walled; we do not "
                "bypass that."
            ),
        )
    try:
        from app.services import amazon_reviews
    except ImportError:
        return StageResult(
            source="amazon",
            skipped_reason="Apify enabled but app/services/amazon_reviews.py not built yet",
        )

    records, stats, errors = await amazon_reviews.fetch_reviews()
    return _finish_review_stage(
        db, run_id,
        source="amazon", staging_name="reviews_amazon",
        stats_name="product_stats_amazon",
        records=records, stats=stats,
        products_scraped=len(stats), errors=[str(e) for e in errors],
    )


# ============================================================================ #
# Main orchestrator                                                              #
# ============================================================================ #

_STAGE_FNS: dict[str, Callable[[Any, str], Awaitable[StageResult]]] = {
    "catalog": stage_catalog,
    "bazaarvoice": stage_bazaarvoice,
    "okendo": stage_okendo,
    "judgeme": stage_judgeme,
    "pbc_catalog": stage_pbc_catalog,
    "yotpo": stage_yotpo,
    "dicks": stage_dicks,
    "dicks_reviews": stage_dicks_reviews,
    "amazon": stage_amazon,
}


async def _run_stage_guarded(
    db: Any, run_id: str, name: str, *, attempts: int = 2
) -> StageResult:
    """Run one stage with a timeout and a retry, never raising.

    A single flaky source must not be able to abort or hang an unattended run,
    so every failure mode becomes a recorded StageResult instead of an exception.
    """
    last: StageResult | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(
                _STAGE_FNS[name](db, run_id), timeout=STAGE_TIMEOUT_SEC
            )
        except ImportError as exc:
            # A source module that is not installed is a config fact, not a
            # transient error — retrying cannot help.
            return StageResult(source=name, skipped_reason=f"module unavailable: {exc}")
        except TimeoutError:
            msg = f"timed out after {STAGE_TIMEOUT_SEC}s (attempt {attempt}/{attempts})"
            last = StageResult(source=name, errors=[msg])
            log.warning("stage_timeout", stage=name, attempt=attempt)
            _store_error(db, run_id, name, msg, stage=name, error_type="timeout")
        except Exception as exc:
            msg = f"{exc} (attempt {attempt}/{attempts})"
            last = StageResult(source=name, errors=[msg])
            log.warning("stage_failed", stage=name, attempt=attempt, error=str(exc)[:200])
            _store_error(db, run_id, name, str(exc), stage=name, error_type="stage_error")

        if attempt < attempts:
            await asyncio.sleep(RETRY_BACKOFF_SEC * attempt)

    return last or StageResult(source=name, errors=["unknown failure"])


async def sync_all_sources(
    run_id: str,
    sources: list[str] | None = None,
    enrich: bool = True,
) -> dict[str, Any]:
    """Full pipeline, unattended-safe. Returns a summary dict.

    Used both by `POST /api/paddles/sync` (as a BackgroundTask) and by
    `scripts/paddle_sync_all.py` (scheduled runs). Every step is idempotent, so a
    re-run after any failure converges rather than duplicating.
    """
    db = service_client()
    log_ctx = log.bind(run_id=run_id)
    wanted = [s for s in (sources or STAGES) if s in _STAGE_FNS]

    db.table("paddle_review_runs").update({
        "status": "running", "started_at": _utcnow(), "stages_total": len(wanted),
    }).eq("id", run_id).execute()
    event_bus.publish(run_id, "start", message="Paddle review sync started", stages=wanted)

    results: list[StageResult] = []
    for i, name in enumerate(wanted):
        event_bus.publish(run_id, "stage", stage=name, status="running", index=i)
        result = await _run_stage_guarded(db, run_id, name)
        results.append(result)
        event_bus.publish(
            run_id, "stage", stage=name,
            status="done" if result.ok else "error", **result.as_dict(),
        )
        _write_progress(db, run_id, results, stages_done=i + 1)

    tables = ps.tables_ready(db)

    # NORMALISE — flag retailer copies, patch product aggregates, adopt orphans,
    # then link reviews to products. Order matters: orphan product rows must
    # exist before linking runs, or their reviews stay unlinked for a whole cycle.
    syndicated = _flag_staged_syndication()
    stats_patched = orphans_created = linked = 0
    if tables:
        event_bus.publish(run_id, "stage", stage="normalise", status="running")
        syndicated += _flag_db_syndication(db)
        stats_patched = _patch_product_stats(db)
        orphans_created = await _adopt_orphan_products(db)
        linked = ps.link_reviews_to_products(db)
        event_bus.publish(
            run_id, "stage", stage="normalise", status="done",
            linked=linked, syndicated=syndicated,
            product_stats_patched=stats_patched, orphan_products_created=orphans_created,
        )

    # ENRICH — loops until nothing is left with a NULL sentiment_label.
    enriched = 0
    if enrich and tables:
        event_bus.publish(run_id, "stage", stage="enrich", status="running")
        try:
            enriched = await _enrich_until_done(db, run_id)
            event_bus.publish(run_id, "stage", stage="enrich", status="done", enriched=enriched)
        except Exception as exc:
            log_ctx.warning("enrich_failed", error=str(exc))
            event_bus.publish(run_id, "stage", stage="enrich", status="error", error=str(exc))
            _store_error(db, run_id, "enrich", str(exc), stage="enrich", error_type="enrich_error")

    totals = _totals(results)
    failed = [r.source for r in results if not r.ok and not r.skipped_reason]
    status = "done" if not failed else "partial"

    summary = {
        "run_id": run_id,
        "status": status,
        "stages": {r.source: r.as_dict() for r in results},
        "failed_sources": failed,
        "skipped_sources": {r.source: r.skipped_reason for r in results if r.skipped_reason},
        "syndicated_flagged": syndicated,
        "product_stats_patched": stats_patched,
        "orphan_products_created": orphans_created,
        "reviews_linked": linked,
        "reviews_enriched": enriched,
        "staged_only": not tables,
        **totals,
    }

    db.table("paddle_review_runs").update({
        "status": status,
        "finished_at": _utcnow(),
        "stages_done": len(results),
        "reviews_enriched": enriched,
        "error_message": f"failed sources: {', '.join(failed)}" if failed else None,
        **totals,
        "per_source_stats": summary["stages"],
    }).eq("id", run_id).execute()

    event_bus.publish(run_id, "done", **{
        k: v for k, v in summary.items() if k not in ("stages", "skipped_sources")
    })
    log_ctx.info("paddle_sync_complete", status=status, **totals, enriched=enriched)
    return summary


async def _enrich_until_done(db: Any, run_id: str, *, max_rounds: int = 200) -> int:
    """Enrich in rounds until no NULL sentiment_label remains.

    `enrich_reviews` intentionally handles a bounded slice per call, so an
    unattended full run has to loop. Bounded by `max_rounds` so a permanently
    failing row can never spin forever.
    """
    from app.services.paddle_enrich import enrich_reviews

    total = 0
    for round_no in range(max_rounds):
        done = await enrich_reviews(db, limit=ENRICH_ROUND_SIZE)
        total += done
        if done == 0:
            break
        event_bus.publish(
            run_id, "stage_detail", stage="enrich", round=round_no + 1, enriched=total,
        )
    return total


def _flag_db_syndication(db: Any) -> int:
    """Flag retailer reviews whose body already exists on a brand site.

    The staged-file pass only sees this cycle's scrape; this pass compares against
    everything already in the table, which is what catches a retailer publishing a
    copy months after the original.
    """
    # Paged: a plain select silently returns only the first 1000 rows, which
    # would make this scan miss most of the table.
    canon = ps.fetch_all_rows(
        db, "paddle_reviews", "content_hash",
        modifier=lambda q: q.in_("source", list(ps.CANONICAL_SOURCES)),
    )
    canon_hashes = {r["content_hash"] for r in canon if r.get("content_hash")}
    if not canon_hashes:
        return 0
    retail = ps.fetch_all_rows(
        db, "paddle_reviews", "id, content_hash",
        modifier=lambda q: (
            q.in_("source", list(ps.RETAILER_SOURCES)).neq("is_syndicated", True)
        ),
    )

    ids = [r["id"] for r in retail if r.get("content_hash") in canon_hashes]
    flagged = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        try:
            db.table("paddle_reviews").update({"is_syndicated": True}) \
                .in_("id", chunk).execute()
            flagged += len(chunk)
        except Exception as e:
            log.warning("syndication_flag_failed", error=str(e)[:160])
    return flagged


def _patch_product_stats(db: Any) -> int:
    """Push staged per-product review_count / avg_rating onto paddle_products."""
    patched = 0
    for name in (
        "product_stats_bazaarvoice", "product_stats_okendo",
        "product_stats_judgeme", "product_stats_yotpo",
    ):
        for row in ps.read_staged(name):
            source, pid = row.get("source"), row.get("source_product_id")
            if not source or not pid:
                continue
            patch = {
                k: row[k]
                for k in ("review_count", "avg_rating", "rating_distribution", "family_id")
                if row.get(k) is not None
            }
            if not patch:
                continue
            try:
                db.table("paddle_products").update(patch) \
                    .eq("source", source).eq("source_product_id", str(pid)).execute()
                patched += 1
            except Exception:
                continue
    return patched


async def _adopt_orphan_products(db: Any) -> int:
    """Create product rows for reviews whose product is missing from the catalog.

    Delegates to `app.services.paddle_orphans`; the logic used to live in
    `scripts/` and silently no-opped with "No module named 'scripts'" whenever the
    pipeline ran as a script rather than through the API.
    """
    from app.services.paddle_orphans import adopt_orphan_products

    try:
        report = await adopt_orphan_products(db)
    except Exception as e:
        log.warning("orphan_adopt_failed", error=str(e)[:200])
        return 0
    if report.get("per_source"):
        log.info("orphan_adopt", **report)
    return int(report.get("created") or 0)



def _flag_staged_syndication() -> int:
    """Re-mark retailer copies across every staged review file, then rewrite them.

    Cross-source detection is only possible with all sources in hand, so it runs
    here rather than inside an individual source stage.
    """
    files = ("reviews_bazaarvoice", "reviews_okendo", "reviews_judgeme", "reviews_yotpo")
    loaded = {name: ps.read_staged(name) for name in files}
    every_row = [row for rows in loaded.values() for row in rows]
    flagged = ps.flag_cross_source_duplicates(every_row)
    if flagged:
        for name, rows in loaded.items():
            if rows:
                ps.write_staged(name, rows)
    return flagged


def _totals(results: list[StageResult]) -> dict[str, int]:
    return {
        "products_found": sum(r.products for r in results),
        "reviews_found": sum(r.reviews_found for r in results),
        "reviews_new": sum(r.reviews_unique for r in results),
        "sources_ok": sum(1 for r in results if r.ok),
        "sources_failed": sum(1 for r in results if not r.ok and not r.skipped_reason),
    }


def _write_progress(
    db: Any, run_id: str, results: list[StageResult], *, stages_done: int
) -> None:
    try:
        db.table("paddle_review_runs").update({
            "stages_done": stages_done,
            **_totals(results),
            "per_source_stats": {r.source: r.as_dict() for r in results},
        }).eq("id", run_id).execute()
    except Exception:
        pass
