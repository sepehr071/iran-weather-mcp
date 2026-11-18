import json
from pathlib import Path

import httpx
import pytest
from mcp import Client

from iran_weather_mcp import http, weather
from iran_weather_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page(name: str):
    """Route handler serving a saved HTML page."""
    body = (FIXTURES / name).read_text(encoding="utf-8")
    return lambda request: httpx.Response(200, text=body)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fresh_http_client(monkeypatch):
    """The shared httpx client is bound to one event loop; each test gets a new loop."""
    monkeypatch.setattr(http, "RETRY_DELAY", 0)
    monkeypatch.setattr(http, "JITTER", (0, 0))
    http.set_transport(None)
    weather._index.clear()
    yield
    http.set_transport(None)


@pytest.fixture
def site():
    """Fake irimo.ir: site["substring of the URL"] = fixture file name or a callable(request) -> httpx.Response.

    The longest matching key wins; unknown URLs return 404. Every request is appended to site.calls.
    """

    class Routes(dict):
        calls: list[httpx.Request]

    routes = Routes()
    routes.calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.calls.append(request)
        url = str(request.url)
        keys = [k for k in routes if k in url]
        if not keys:
            return httpx.Response(404, text="no fake route")
        target = routes[max(keys, key=len)]
        if callable(target):
            return target(request)
        return httpx.Response(200, text=(FIXTURES / target).read_text(encoding="utf-8", errors="replace"))

    http.set_transport(httpx.MockTransport(handler))
    return routes


@pytest.fixture
async def client():
    async with Client(mcp, raise_exceptions=True) as c:
        call_tool = c.call_tool

        async def parsed(*args, **kwargs):
            result = await call_tool(*args, **kwargs)
            if not result.is_error:
                result.structured_content = json.loads(result.content[0].text)
            return result

        c.call_tool = parsed
        yield c
