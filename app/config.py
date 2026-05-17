"""Application configuration loaded from environment / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = Field(default="")
    openai_model_cheap: str = Field(default="gpt-4o-mini")
    openai_model_smart: str = Field(default="gpt-4o")

    # DataForSEO
    dataforseo_login: str = Field(default="")
    dataforseo_password: str = Field(default="")

    # Supabase
    supabase_url: str = Field(default="")
    supabase_anon_key: str = Field(default="")
    supabase_service_role_key: str = Field(default="")
    supabase_db_url: str = Field(default="")

    # Apify (optional)
    apify_token: str = Field(default="")
    apify_enabled: bool = Field(default=False)

    # Google OAuth (for GSC + GA4 integrations — optional)
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_redirect_base_url: str = Field(default="http://localhost:8000")

    # App
    app_env: str = Field(default="local")
    storage_dir: str = Field(default="./storage")
    default_market: str = Field(default="US")
    default_language: str = Field(default="en")
    max_pages_per_crawl: int = Field(default=300)
    max_keywords_per_project: int = Field(default=500)

    # Hard caps (POC safety)
    openai_cost_cap_usd_per_run: float = Field(default=5.0)
    crawl_concurrency: int = Field(default=10)
    crawl_request_timeout_sec: int = Field(default=15)
    user_agent: str = Field(
        default="SEOIntelBot/0.1 (+internal POC; respects robots.txt)"
    )

    @property
    def storage_path(self) -> Path:
        p = Path(self.storage_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
