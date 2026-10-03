# Allow `from scripts.x import y` in tests — scripts/ is a directory, not a
# package, so its parent has to be on sys.path for the import to resolve.
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# E402: these must follow the sys.path shim above, by definition.
import httpx  # noqa: E402
import pytest  # noqa: E402

_API_BASE = "http://localhost:8000"


def _server_up() -> bool:
    try:
        httpx.get(f"{_API_BASE}/docs", timeout=2.0)
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(items: list) -> None:
    """Auto-skip all @pytest.mark.live tests when uvicorn isn't running."""
    if _server_up():
        return
    skip = pytest.mark.skip(reason="uvicorn not running at localhost:8000 — start the server first")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def api() -> httpx.Client:
    """Session-scoped httpx client pointed at the local API."""
    with httpx.Client(base_url=_API_BASE, timeout=15.0) as client:
        yield client
