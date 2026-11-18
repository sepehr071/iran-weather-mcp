import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_cities(client):
    r = (await client.call_tool("wx_cities", {})).structured_content
    names = {c["name"] for c in r["cities"]}
    assert r["count"] >= 30 and {"تهران", "مشهد", "شیراز"} <= names
    assert any(c.get("max_c") is not None for c in r["cities"])


async def test_live_observations(client):
    r = (await client.call_tool("wx_observations", {})).structured_content
    assert r["count"] >= 30 and any(s.get("temp_c") is not None for s in r["stations"])


async def test_live_weather(client):
    r = (await client.call_tool("wx_weather", {"city": "مشهد"})).structured_content
    assert r["name"] == "مشهد" and r["current"]["observed_at"]  # any station can show "-" while its feed is stale
    assert len(r["forecast"]) == 3 and all(
        d["max_c"] >= d["min_c"] for d in r["forecast"] if "max_c" in d and "min_c" in d
    )


async def test_live_small_station_by_name(client):
    r = (await client.call_tool("wx_weather", {"city": "کاشمر"})).structured_content
    assert r["name"] == "کاشمر" and r["current"]["observed_at"]


async def test_live_province(client):
    r = (await client.call_tool("wx_province", {"city": "اصفهان"})).structured_content
    assert r["count"] >= 10 and r["stations"][0]["id"]


async def test_live_warnings(client):
    r = (await client.call_tool("wx_warnings", {"limit": 5})).structured_content
    assert r["count"] == 5
    d = (await client.call_tool("wx_warning", {"warning_id": r["warnings"][0]["id"]})).structured_content
    assert d["title"] == r["warnings"][0]["title"] and d.get("details")


async def test_live_road(client):
    r = (await client.call_tool("wx_road", {"route": "آبادان-تهران"})).structured_content
    assert r["id"] == 388 and r["count"] >= 8 and any(s.get("temp_c") is not None for s in r["stations"])


async def test_live_airports(client):
    r = (await client.call_tool("wx_airports", {})).structured_content
    assert r["count"] >= 20 and {a["flight_category"] for a in r["airports"]} <= {"vfr", "mvfr", "ifr", "lifr"}
    one = (await client.call_tool("wx_airports", {"airport": "OIII"})).structured_content["airports"][0]
    assert one["taf"].startswith("TAF")


async def test_live_bulletins(client):
    r = (await client.call_tool("wx_bulletins", {"kind": "weekly"})).structured_content
    assert r["pdf"].endswith(".pdf") and r["published"].startswith("14")
