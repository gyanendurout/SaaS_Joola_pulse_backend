"""Verify your DataForSEO credentials in 2 seconds.

Run from the backend directory:

    python -m scripts.test_dataforseo

It calls the cheapest possible DataForSEO endpoint (user_data)
which just returns your account info — no quota is spent.

Expected output on success:
    OK  login=your-login@example.com  money_left=$XX.XX  rate_limit=...

On 401:
    FAIL  Status=401  -> credentials wrong, see below.
"""
from __future__ import annotations

import json
import sys

import httpx

from app.config import get_settings


def main() -> int:
    s = get_settings()
    login = s.dataforseo_login
    password = s.dataforseo_password
    print(f"Testing credentials: login={login!r}  password=***{password[-3:] if password else ''}")
    if not login or not password:
        print("FAIL  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD missing in .env")
        return 1
    try:
        r = httpx.get(
            "https://api.dataforseo.com/v3/appendix/user_data",
            auth=(login, password),
            timeout=15.0,
        )
    except Exception as e:
        print(f"FAIL  Network error: {e}")
        return 2

    print(f"HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:
        print(r.text[:400])
        return 3

    if r.status_code == 401:
        print("FAIL  401 Unauthorized — your DataForSEO login or password is wrong.")
        print("\nFix:")
        print("  1. Sign in at https://app.dataforseo.com/")
        print("  2. Click your email → 'API Access' (or 'API Dashboard')")
        print("  3. Copy your API Login (an email) and API Password (a generated key,")
        print("     NOT your account login password)")
        print("  4. Replace these two lines in backend/.env:")
        print("       DATAFORSEO_LOGIN=<paste login>")
        print("       DATAFORSEO_PASSWORD=<paste API password>")
        print("  5. Restart the backend (Ctrl-C → start it again)")
        return 4

    if r.status_code != 200:
        print(f"FAIL  Unexpected status {r.status_code}")
        print(json.dumps(body, indent=2)[:600])
        return 5

    tasks = body.get("tasks") or []
    if not tasks:
        print("FAIL  No tasks in response — unexpected payload.")
        print(json.dumps(body, indent=2)[:400])
        return 6

    task = tasks[0]
    if task.get("status_code") and task.get("status_code") != 20000:
        print(f"FAIL  task status={task.get('status_code')} message={task.get('status_message')}")
        return 7

    result = (task.get("result") or [{}])[0]
    money = result.get("money", {}).get("balance")
    rate = result.get("rates", {})
    print(f"OK  login={result.get('login')}  money_left=${money}  rate_per_minute={rate.get('limits', {}).get('minute')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
