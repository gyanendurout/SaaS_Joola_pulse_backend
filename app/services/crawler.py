"""httpx-based fetcher (Tier 1).

Returns the final URL after redirects, the status code, the redirect chain,
and the raw body bytes (decoded as text where possible).

Concurrency is enforced by an outer semaphore at the agent level.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger()


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    redirect_chain: list[str] = field(default_factory=list)
    text: str = ""
    content_type: str = ""
    fetcher: str = "httpx"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    @property
    def needs_js_render(self) -> bool:
        """Heuristic: did Tier 1 likely fail to capture content?

        - HTML response, but body is suspiciously thin
        - Or contains markers of a client-rendered SPA
        """
        if not self.ok or "html" not in self.content_type.lower():
            return False
        body = self.text or ""
        if len(body) < 2000:
            return True
        lower = body.lower()
        spa_markers = (
            'id="__next"',                # Next.js SSR-less
            'id="root"></div>',           # generic CRA / Vite
            "ng-version=",                # Angular
            "data-reactroot",             # legacy React
            "<noscript>you need to enable javascript",
        )
        return any(m in lower for m in spa_markers) and len(body) < 5000


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)),
)
async def fetch(url: str, client: httpx.AsyncClient) -> FetchResult:
    try:
        r = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as e:
        log.info("fetch_error", url=url, error=str(e))
        return FetchResult(url=url, final_url=url, status=0, error=str(e))

    redirect_chain = [str(h.url) for h in r.history]
    content_type = r.headers.get("content-type", "")
    text = ""
    if "html" in content_type.lower() or "xml" in content_type.lower():
        # httpx auto-decodes; if it can't, fall back to utf-8 ignore.
        try:
            text = r.text
        except Exception:
            text = r.content.decode("utf-8", errors="ignore")

    return FetchResult(
        url=url,
        final_url=str(r.url),
        status=r.status_code,
        redirect_chain=redirect_chain,
        text=text,
        content_type=content_type,
    )


def make_client(user_agent: str, timeout_sec: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=httpx.Timeout(timeout_sec, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        http2=True,
    )
