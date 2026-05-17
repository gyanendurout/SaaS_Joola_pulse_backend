"""Performance tracking: Google Search Console OAuth flow + data fetch."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db import service_client

router = APIRouter(prefix="/api/performance", tags=["performance"])

SCOPES_GSC = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _google_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def _flow(redirect_uri: str):
    """Build a google_auth_oauthlib Flow from config."""
    from google_auth_oauthlib.flow import Flow  # type: ignore[import]
    settings = get_settings()
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uris": [redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES_GSC,
        redirect_uri=redirect_uri,
    )


# ─── Status ──────────────────────────────────────────────────────────────────

@router.get("/status")
async def performance_status(domain: str = Query(...)):
    """Check if GSC is connected for a domain and whether Google OAuth is configured."""
    if not _google_configured():
        return {
            "google_configured": False,
            "gsc_connected": False,
            "message": (
                "Google OAuth is not configured. "
                "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env, "
                "then set GOOGLE_REDIRECT_BASE_URL=http://localhost:8000."
            ),
        }
    db = service_client()
    rows = (
        db.table("integrations")
        .select("provider,token_expiry")
        .eq("domain", domain)
        .execute().data or []
    )
    providers = {r["provider"] for r in rows}
    return {
        "google_configured": True,
        "gsc_connected": "gsc" in providers,
    }


# ─── Connect (OAuth redirect) ─────────────────────────────────────────────────

@router.get("/connect/gsc")
async def connect_gsc(domain: str = Query(...)):
    """Redirect user to Google's OAuth consent screen for Search Console access."""
    if not _google_configured():
        raise HTTPException(
            400,
            "Google OAuth not configured. Add GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET to .env",
        )
    try:
        settings = get_settings()
        redirect_uri = f"{settings.google_redirect_base_url}/api/performance/callback/gsc"
        flow = _flow(redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=domain,
        )
        return RedirectResponse(auth_url)
    except ImportError:
        raise HTTPException(
            500,
            "google-auth-oauthlib not installed. Run: pip install google-auth-oauthlib google-api-python-client",
        )


# ─── OAuth callback ──────────────────────────────────────────────────────────

@router.get("/callback/gsc")
async def callback_gsc(code: str = Query(...), state: str = Query(...)):
    """Handle Google OAuth callback — exchange code for tokens and store them."""
    domain = state
    try:
        settings = get_settings()
        redirect_uri = f"{settings.google_redirect_base_url}/api/performance/callback/gsc"
        flow = _flow(redirect_uri)
        flow.fetch_token(code=code)
        creds = flow.credentials
        db = service_client()
        db.table("integrations").upsert(
            {
                "domain": domain,
                "provider": "gsc",
                "access_token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
                "extra": {"scopes": list(creds.scopes or [])},
            },
            on_conflict="domain,provider",
        ).execute()
        # Redirect back to the frontend performance tab
        return RedirectResponse(f"http://localhost:3000?gsc_connected=1&domain={domain}")
    except Exception as e:
        raise HTTPException(500, f"OAuth callback error: {e}")


# ─── Disconnect ──────────────────────────────────────────────────────────────

@router.delete("/disconnect/gsc")
async def disconnect_gsc(domain: str = Query(...)):
    db = service_client()
    db.table("integrations").delete().eq("domain", domain).eq("provider", "gsc").execute()
    db.table("performance_cache").delete().eq("domain", domain).eq("provider", "gsc").execute()
    return {"ok": True}


# ─── GSC data ────────────────────────────────────────────────────────────────

@router.get("/{domain}/gsc")
async def get_gsc_data(domain: str, date_range: str = Query("last_28_days")):
    """Fetch Search Console performance: queries, pages, daily trend. Cached 6 h."""
    db = service_client()

    # Serve from cache if fresh (< 6 hours)
    cache_res = (
        db.table("performance_cache")
        .select("data,fetched_at")
        .eq("domain", domain)
        .eq("provider", "gsc")
        .eq("date_range", date_range)
        .execute().data
    )
    if cache_res:
        fetched_at = datetime.fromisoformat(cache_res[0]["fetched_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - fetched_at < timedelta(hours=6):
            return cache_res[0]["data"]

    # Load stored token
    token_res = (
        db.table("integrations")
        .select("*")
        .eq("domain", domain)
        .eq("provider", "gsc")
        .execute().data
    )
    if not token_res:
        raise HTTPException(
            404,
            "GSC not connected for this domain. Click 'Connect GSC' first.",
        )
    token_row = token_res[0]

    try:
        from google.oauth2.credentials import Credentials  # type: ignore[import]
        from googleapiclient.discovery import build  # type: ignore[import]

        settings = get_settings()
        creds = Credentials(
            token=token_row.get("access_token"),
            refresh_token=token_row.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        service = build("webmasters", "v3", credentials=creds)

        ranges_days = {"last_7_days": 7, "last_28_days": 28, "last_90_days": 90}
        days = ranges_days.get(date_range, 28)
        end = date.today()
        start = end - timedelta(days=days)

        def _query(dimensions: list[str], limit: int = 50) -> list[dict]:
            resp = service.searchanalytics().query(
                siteUrl=f"sc-domain:{domain}",
                body={
                    "startDate": str(start),
                    "endDate": str(end),
                    "dimensions": dimensions,
                    "rowLimit": limit,
                    "orderBy": [{"fieldName": "clicks", "sortOrder": "descending"}],
                },
            ).execute()
            return resp.get("rows", [])

        kw_rows = _query(["query"], 50)
        page_rows = _query(["page"], 20)
        daily_rows = _query(["date"], 90)

        total_clicks = sum(r.get("clicks", 0) for r in daily_rows)
        total_impr = sum(r.get("impressions", 0) for r in daily_rows)
        avg_ctr = round(total_clicks / total_impr * 100, 2) if total_impr > 0 else 0
        avg_pos = round(
            sum(r.get("position", 0) for r in daily_rows) / len(daily_rows), 1
        ) if daily_rows else 0

        data = {
            "domain": domain,
            "date_range": date_range,
            "start_date": str(start),
            "end_date": str(end),
            "totals": {
                "clicks": total_clicks,
                "impressions": total_impr,
                "ctr": avg_ctr,
                "position": avg_pos,
            },
            "top_keywords": kw_rows,
            "top_pages": page_rows,
            "daily_trend": daily_rows,
        }

        # Store in cache
        try:
            db.table("performance_cache").upsert(
                {
                    "domain": domain,
                    "provider": "gsc",
                    "date_range": date_range,
                    "data": data,
                },
                on_conflict="domain,provider,date_range",
            ).execute()
        except Exception:
            pass

        return data

    except ImportError:
        raise HTTPException(
            500,
            "google-api-python-client not installed. Run: pip install google-api-python-client google-auth-oauthlib",
        )
    except Exception as e:
        raise HTTPException(500, f"GSC API error: {e}")
