"""Local filesystem storage for crawl HTML snapshots.

Layout:
  storage/{run_id}/pages/{page_id}.html.gz

Switch to Supabase Storage later by replacing this module's two functions
without changing callers.
"""
from __future__ import annotations

import gzip
from pathlib import Path

from app.config import get_settings


def page_html_path(run_id: str, page_id: str) -> Path:
    base = get_settings().storage_path / run_id / "pages"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{page_id}.html.gz"


def write_html(run_id: str, page_id: str, html: str) -> str:
    path = page_html_path(run_id, page_id)
    with gzip.open(path, "wb") as fh:
        fh.write(html.encode("utf-8", errors="ignore"))
    return str(path)


def read_html(run_id: str, page_id: str) -> str | None:
    path = page_html_path(run_id, page_id)
    if not path.exists():
        return None
    with gzip.open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors="ignore")
