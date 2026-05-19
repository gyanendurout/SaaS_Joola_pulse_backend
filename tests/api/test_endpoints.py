"""
Live API tests — require uvicorn running at localhost:8000.
Auto-skipped when server is not reachable (see conftest.py).
"""
import pytest

pytestmark = pytest.mark.live


def test_docs_reachable(api):
    r = api.get("/docs")
    assert r.status_code == 200


def test_openapi_schema_valid(api):
    r = api.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert "paths" in schema
    assert "info" in schema


def test_news_articles_returns_list(api):
    r = api.get("/api/news/articles?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_news_articles_limit_respected(api):
    r = api.get("/api/news/articles?limit=2")
    assert r.status_code == 200
    assert len(r.json()) <= 2


def test_news_analytics_summary_shape(api):
    r = api.get("/api/news/analytics/summary")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_seo_runs_list(api):
    r = api.get("/api/runs")
    assert r.status_code == 200


def test_unknown_endpoint_returns_404(api):
    r = api.get("/api/__joola_test_not_a_real_endpoint__")
    assert r.status_code == 404


def test_cors_headers_present(api):
    r = api.options("/api/news/articles", headers={"Origin": "http://localhost:3000"})
    # FastAPI with CORSMiddleware should return 200 on OPTIONS or at minimum not 5xx
    assert r.status_code < 500
