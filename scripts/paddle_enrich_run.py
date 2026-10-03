"""Run AI enrichment over every unenriched paddle review.

Resumable by construction: work is selected by `sentiment_label IS NULL`, so a
kill or a crash loses at most the batches in flight. Re-running picks up exactly
where it stopped.

Usage (from `backend/`):
    .venv/Scripts/python.exe scripts/paddle_enrich_run.py
    .venv/Scripts/python.exe scripts/paddle_enrich_run.py --max-rows 500
    .venv/Scripts/python.exe scripts/paddle_enrich_run.py --concurrency 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from app.db import service_client
from app.services.paddle_enrich import (
    BATCH_SIZE,
    apply_patches,
    enrich_batch,
    fetch_unenriched,
)

# gpt-4o-mini list price. Only used for a rough spend estimate in the log — the
# real number comes off the OpenAI dashboard.
USD_PER_1K_INPUT = 0.00015
USD_PER_1K_OUTPUT = 0.00060
EST_INPUT_TOKENS_PER_REVIEW = 260
EST_OUTPUT_TOKENS_PER_REVIEW = 70


def _remaining(db: Any) -> int:
    try:
        return (
            db.table("paddle_reviews").select("id", count="exact")
            .is_("sentiment_label", "null").limit(1).execute().count
        ) or 0
    except Exception:
        return 0


def _estimate_usd(rows: int) -> float:
    inp = rows * EST_INPUT_TOKENS_PER_REVIEW / 1000 * USD_PER_1K_INPUT
    out = rows * EST_OUTPUT_TOKENS_PER_REVIEW / 1000 * USD_PER_1K_OUTPUT
    return round(inp + out, 2)


async def run(
    db: Any, *, concurrency: int, max_rows: int | None, batch_size: int
) -> dict[str, Any]:
    started = time.monotonic()
    at_start = _remaining(db)
    print(f"unenriched at start: {at_start}  (est. ${_estimate_usd(at_start)})", flush=True)

    updated = 0
    empty_rounds = 0
    fetch_size = batch_size * concurrency

    while True:
        if max_rows is not None and updated >= max_rows:
            break
        rows = fetch_unenriched(db, limit=fetch_size)
        if not rows:
            # A round can come back empty while rows remain only if every
            # remaining row has an empty body; stop after two to avoid spinning.
            empty_rounds += 1
            if empty_rounds >= 2:
                break
            continue
        empty_rounds = 0

        if max_rows is not None:
            rows = rows[: max_rows - updated]

        batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
        results = await asyncio.gather(
            *(enrich_batch(b) for b in batches), return_exceptions=True
        )

        patches: list[dict[str, Any]] = []
        failed_batches = 0
        for res in results:
            if isinstance(res, Exception):
                failed_batches += 1
                continue
            patches.extend(res)

        if not patches:
            # Every batch in this round failed. Bail rather than hammer the API;
            # the rows stay NULL and a re-run retries them.
            print(f"aborting: {failed_batches} consecutive batch failures", flush=True)
            break

        updated += apply_patches(db, patches)
        elapsed = time.monotonic() - started
        rate = updated / elapsed * 60 if elapsed else 0
        left = _remaining(db)
        eta_min = round(left / rate, 1) if rate else None
        print(
            f"  updated {updated:>6} | remaining {left:>6} | "
            f"{rate:>5.0f} rows/min | eta {eta_min} min", flush=True,
        )

    return {
        "rows_enriched": updated,
        "unenriched_at_start": at_start,
        "unenriched_now": _remaining(db),
        "minutes": round((time.monotonic() - started) / 60, 1),
        "est_cost_usd": _estimate_usd(updated),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich paddle reviews with the cheap model.")
    ap.add_argument("--concurrency", type=int, default=5, help="batches in flight")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="reviews per LLM call")
    ap.add_argument("--max-rows", type=int, default=None, help="stop after N rows")
    args = ap.parse_args()

    db = service_client()
    report = await run(
        db,
        concurrency=max(1, args.concurrency),
        max_rows=args.max_rows,
        batch_size=max(1, args.batch_size),
    )
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
