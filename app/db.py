"""Supabase client singletons.

We expose two clients:
- service_client: full access (used by backend agents / writes)
- anon_client:    public-anon (not really needed in POC since no auth, but kept for parity)
"""
from functools import lru_cache

from supabase import Client, create_client

from app.config import get_settings


@lru_cache
def service_client() -> Client:
    s = get_settings()
    if not s.supabase_url or not s.supabase_service_role_key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return create_client(s.supabase_url, s.supabase_service_role_key)


@lru_cache
def anon_client() -> Client:
    s = get_settings()
    if not s.supabase_url or not s.supabase_anon_key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY."
        )
    return create_client(s.supabase_url, s.supabase_anon_key)
