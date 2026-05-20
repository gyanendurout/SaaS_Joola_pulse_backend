"""One-shot regression: when the user writes a substantive brief AND picks a
top-post signal, the brief's topic must win. The signal becomes a style
reference only.

Repro of the bug the user reported:
  - signal = Razer x JOOLA top post
  - brief  = "Coca-Cola X JOOLA"
  - bad output: about Razer
  - good output: about Coca-Cola, no mention of Razer

Run:
  cd backend && .venv\\Scripts\\python.exe selftest_brief_overrides_signal.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("PULSE_API", "http://127.0.0.1:8005")
SUPABASE_URL = "https://loecyghnkkxyymelgexz.supabase.co"


def load_service_key() -> str:
    with open(".env", "r") as f:
        for line in f:
            if line.startswith("SUPABASE_SERVICE_ROLE_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("Service role key not found in .env")


async def main():
    db = create_client(SUPABASE_URL, load_service_key())

    # Find a JOOLA IG post with "razer" in the caption
    posts = (
        db.table("joola_ig_posts")
        .select("post_id,caption,engagement_rate")
        .order("engagement_rate", desc=True, nullsfirst=False)
        .limit(50)
        .execute()
        .data
    )
    razer = next((p for p in posts if p.get("caption") and "razer" in p["caption"].lower()), None)
    if not razer:
        print("NO RAZER POST FOUND — cannot reproduce")
        return
    print(f"Using IG post {razer['post_id']}")
    print(f'Caption: "{(razer["caption"] or "")[:160]}"')
    print()

    payload = {
        "content_type": "ig_post",
        "tone": "informative",
        "audience": "general_fans",
        "length": "short",
        "instructions": "Coca-Cola X JOOLA",
        "signals_config": {
            "use_top_posts": True,
            "selected_top_post_ids": [razer["post_id"]],
        },
        "created_by": f"selftest-{uuid.uuid4().hex[:6]}@joola.com",
    }

    body_chunks = []
    meta = done = error = None
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream("POST", f"{BASE}/api/content/generate/stream", json=payload) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                if t == "meta":
                    meta = ev
                elif t == "token":
                    body_chunks.append(ev.get("text", ""))
                elif t == "done":
                    done = ev
                elif t == "error":
                    error = ev
                    break

    if error:
        print(f"ERROR: {error}")
        return

    full = (done or {}).get("body") or "".join(body_chunks)
    low = full.lower()

    has_coke = ("coca" in low) or ("coke" in low)
    has_razer = "razer" in low

    print("=" * 78)
    print(f"Model: {(meta or {}).get('model')}")
    print(f"Cost:  ${(done or {}).get('cost_usd')}")
    print()
    print("--- GENERATED ---")
    print(full[:1200])
    print("--- END ---")
    print()
    print(f"contains 'coca'/'coke' : {has_coke}")
    print(f"contains 'razer'       : {has_razer}")
    passed = has_coke and not has_razer
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
