"""Reset all SEO Intel run data.

Deletes every row from every table so the website starts fresh.
Run from the backend directory:

    python -m scripts.reset_db
"""
from __future__ import annotations

import sys

from app.db import service_client

# Tables in dependency order (children first, parents last).
# Most child tables cascade-delete from `runs`, but we delete explicitly
# so the script works even if a future migration drops cascade.
TABLES = [
    "gap_analyses",
    "backlinks_summary",
    "competitor_domains",
    "domain_ranked_keywords",
    "serp_results",
    "keywords",
    "entities",
    "issues",
    "pages",
    "jobs",
    "runs",
]


def reset() -> None:
    db = service_client()
    print("Resetting database — deleting all rows.")
    for table in TABLES:
        try:
            res = db.table(table).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
            count = len(res.data) if res.data else 0
            print(f"  - {table:30s}  cleared ({count} rows reported)")
        except Exception as e:
            print(f"  ! {table:30s}  failed: {e}")
    print("Done. All tables cleared.")


if __name__ == "__main__":
    confirm = input("This will DELETE ALL ANALYSIS DATA. Type 'yes' to continue: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(1)
    reset()
