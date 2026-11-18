from pathlib import Path

import httpx
import pytest

from iran_weather_mcp import http

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures"

TOOLS = {
    "wx_cities",
    "wx_weather",
    "wx_province",
    "wx_observations",
    "wx_warnings",
    "wx_warning",
    "wx_road",
    "wx_airports",
    "wx_bulletins",
}
HOME_PAGE_2 = "index.php?form_29902s=14"
CITY = "module=web_directory&wd_id=701&id="
WARNINGS = "/far/wd/592-"


@pytest.fixture
def routes(site):
    site["/far/index.php"] = "idx1.html"
    row = (
        '<td class="_met_td1"><a href="https://www.irimo.ir/far/wd/701-x.html?id=17555"> مشهد </a></td>'
        '<td class="_met_td2" id="info_1"></td>'
    )
    site[HOME_PAGE_2] = lambda request: httpx.Response(200, text=row)
    site["index.php?form_29902s=28"] = lambda request: httpx.Response(200, text="")
    site[CITY] = "tehran.html"  # every other main city's page
    site[CITY + "17561"] = "tehran.html"
    site[CITY + "17555"] = "mashhad.html"
    site[CITY + "118"] = "kash.html"
    site[WARNINGS] = "warn.html"
    site["/services/news/1183446-"] = "w1.html"
    return site


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name


async def test_cities(client, routes):
    r = (await client.call_tool("wx_cities", {})).structured_content
    assert "27 آبان" in r["day"]
    by = {c["name"]: c for c in r["cities"]}
    assert by["تهران"] == {
        "id": 17561,
        "name": "تهران",
        "condition": by["تهران"]["condition"],
        "max_c": 16,
        "min_c": 10,
    }
    assert by["بندر عباس"]["max_c"] == 30 and by["بندر عباس"]["min_c"] == 19
    assert "max_c" not in by["اراک"]  # no data ('-') is dropped
    # paging links of the list are followed
    assert any("form_29902s=14" in str(c.url) for c in routes.calls)


async def test_observations(client, routes):
    r = (await client.call_tool("wx_observations", {})).structured_content
    by = {s["name"]: s for s in r["stations"]}
    assert by["تبریز"]["temp_c"] == 13 and by["تبریز"]["humidity_pct"] == 50 and by["تبریز"]["wind_kmh"] == 4
    assert by["رشت فرودگاه"]["wind_kmh"] == 0  # calm


async def test_weather_by_name_and_id(client, routes):
    r = (await client.call_tool("wx_weather", {"city": "تهران"})).structured_content
    assert r["id"] == 17561 and r["name"] == "تهران"
    cur = r["current"]
    assert (cur["temp_c"], cur["humidity_pct"], cur["wind_kmh"], cur["visibility_m"]) == (15, 33, 7, 6000)
    assert cur["rain_24h_mm"] == 0 and cur["max_24h_c"] == 17 and cur["min_24h_c"] == 10
    assert cur["updated"] == "1405/08/27 10:30"
    assert [(d["day"], d["min_c"], d["max_c"]) for d in r["forecast"]] == [(1, 10, 16), (2, 10, 18), (3, 15, 20)]
    assert "stations" not in r
    again = (await client.call_tool("wx_weather", {"city": "17561"})).structured_content
    assert again["current"] == cur


async def test_weather_station_without_fresh_report(client, routes):
    routes[CITY + "17561"] = "tehran_nodata.html"  # the site shows "-" for every current value
    r = (await client.call_tool("wx_weather", {"city": "17561"})).structured_content
    # missing values are dropped, the 24 h extremes the site still shows are kept, no forecast block
    cur = r["current"]
    assert (cur["max_24h_c"], cur["min_24h_c"]) == (17, 10)
    assert not {"temp_c", "condition", "wind_kmh", "humidity_pct", "updated"} & cur.keys()
    assert "forecast" not in r


async def test_weather_visibility_in_km(client, routes):
    r = (await client.call_tool("wx_weather", {"city": "کاشمر"})).structured_content
    assert r["current"]["visibility_m"] == 10000 and r["current"]["visibility_over"] is True


async def test_province(client, routes):
    r = (await client.call_tool("wx_province", {"city": "تهران"})).structured_content
    assert r["province"] == "تهران"
    by = {s["name"]: s for s in r["stations"]}
    assert by["آبعلی"]["temp_c"] == 5 and by["آبعلی"]["wind_kmh"] == 14 and by["آبعلی"]["wind_from"] == "غربی"
    assert by["دوشان تپه"] == {"id": 405, "name": "دوشان تپه"}
    r = (await client.call_tool("wx_province", {"city": "مشهد"})).structured_content
    assert r["province"] == "خراسان رضوی"


async def test_unknown_city_is_an_error(client, routes):
    r = await client.call_tool(
        "wx_weather",
        {"city": "شهر نامعلوم"},
    )
    assert r.is_error and "wx_province" in r.content[0].text


async def test_warnings_and_kind_filter(client, routes):
    r = (await client.call_tool("wx_warnings", {"limit": 3})).structured_content
    first = r["warnings"][0]
    assert first["id"] == 1183446 and first["kind"] == "pollution" and first["level"] == "yellow"
    assert first["number"] == 3 and first["date"] == "1405/08/27"
    r = (await client.call_tool("wx_warnings", {"kind": "marine", "limit": 2})).structured_content
    assert [w["kind"] for w in r["warnings"]] == ["marine", "marine"]


async def test_warnings_next_page_uses_form_params(client, routes):
    await client.call_tool("wx_warnings", {"page": 2})
    last = str(routes.calls[-1].url)
    assert "form_32533s=20" in last and "form_32533a=4637" in last


async def test_warning_detail(client, routes):
    r = (await client.call_tool("wx_warning", {"warning_id": 1183446})).structured_content
    assert r["title"].startswith("هشدار آلودگی-سطح زرد شماره 3")
    assert r["published"] == "1405/08/27"
    assert "text" not in r and len(r["details"]) == 7
    assert "زمان شروع: صبح شنبه 1405/08/30" in r["details"]
    assert any(d.startswith("منطقه اثر: تهران، کرج") for d in r["details"])
    assert r["files"][0].endswith(".pdf") and r["map_image"].endswith(".jpg")


async def test_warning_with_two_systems_keeps_every_block(client, routes):
    routes["/services/news/1183409-"] = "w2.html"
    r = (await client.call_tool("wx_warning", {"warning_id": 1183409})).structured_content
    labels = [d.split(":")[0] for d in r["details"]]
    assert labels.count("زمان شروع") == 2 and "توصیف سامانه دوم" in labels
    assert any("دریای خزر" in d for d in r["details"])


async def test_http_errors_are_actionable(client, site):
    site["/far/index.php"] = lambda request: httpx.Response(503)
    r = await client.call_tool("wx_cities", {})
    assert r.is_error and "IRAN_WEATHER_MCP_PROXY" in r.content[0].text


async def test_host_allowlist():
    with pytest.raises(http.ApiError):
        await http.fetch("https://example.com/")


async def test_weather_trailing_dash_field_is_not_a_number(client, routes):
    # last caption "کمینه دما" is a bare "-": it must not pick up the update date (1405) as a temperature
    routes[CITY + "17662"] = "bakharz.html"
    cur = (await client.call_tool("wx_weather", {"city": "17662"})).structured_content["current"]
    assert cur["max_24h_c"] == 14 and "min_24h_c" not in cur
    assert cur["wind_kmh"] == 0 and cur["rain_24h_mm"] == 0  # "آرام" is calm


async def test_warning_detail_drops_inline_scripts(client, routes):
    body = (FIXTURES / "w1.html").read_text(encoding="utf-8", errors="replace")
    script = "<script>var _uploadFileTypes = new Array('avi'); function uploadFile_x() {}</script>"
    marker = "فایل پیوست" if "فایل پیوست" in body else "</form>"
    routes["/services/news/1183446-"] = lambda request: httpx.Response(
        200, text=body.replace(marker, script + marker, 1)
    )
    r = (await client.call_tool("wx_warning", {"warning_id": 1183446})).structured_content
    assert "_uploadFileTypes" not in " ".join(r["details"]) and r["details"][-1].startswith("توصیه:")


async def test_warnings_agriculture_kind(client, routes):
    routes[WARNINGS] = lambda request: httpx.Response(
        200,
        text='<a href="https://www.irimo.ir/far/services/news/1183193-x.html" title="هشدار هواشناسی کشاورزی-سطح نارنجی '
        'شماره 1 چهارشنبه مورخ 1405/08/13">x</a>',
    )
    r = (await client.call_tool("wx_warnings", {"kind": "agriculture"})).structured_content
    assert [(w["id"], w["kind"], w["level"], w["date"]) for w in r["warnings"]] == [
        (1183193, "agriculture", "orange", "1405/08/13")
    ]


ROADS = "wd/731-"
ROAD = "wd/732-"


async def test_road_by_name_and_id(client, site):
    site[ROADS] = "roads1.html"
    site[ROAD] = "road.html"
    r = (await client.call_tool("wx_road", {"route": "آبادان-تهران"})).structured_content
    assert r["id"] == 388 and r["name"] == "آبادان-تهران" and r["count"] == 11
    by = {s["name"]: s for s in r["stations"]}
    assert by["آبادان"]["temp_c"] == 29 and by["آبادان"]["visibility_m"] == 6000 and "pressure_hpa" not in by["آبادان"]
    assert by["اراک فرودگاه"]["pressure_hpa"] == 1019 and by["اراک فرودگاه"]["visibility_over"] is True
    assert by["فرودگاه امام(رباط کریم)"]["wind_kmh"] == 0  # calm
    assert by["دزفول"] == {"id": 206, "name": "دزفول"}  # no report: only the id and name remain
    assert (await client.call_tool("wx_road", {"route": "388"})).structured_content["name"] == "آبادان-تهران"
    # ASCII digits, spaces instead of the hyphen
    assert (await client.call_tool("wx_road", {"route": "آبادان تهران"})).structured_content["id"] == 388


async def test_road_ambiguous_and_unknown(client, site):
    site[ROADS] = "roads1.html"
    site[ROAD] = "road.html"
    r = (await client.call_tool("wx_road", {"route": "اراک"})).structured_content
    assert r["count"] == len(r["routes"]) >= 2 and "stations" not in r
    r = await client.call_tool("wx_road", {"route": "شهر نامعلوم"})
    assert r.is_error and "No route" in r.content[0].text
    assert (await client.call_tool("wx_road", {"route": " "})).is_error


async def test_airports(client, site):
    site["wd/1218-"] = "avi.html"
    r = (await client.call_tool("wx_airports", {})).structured_content
    assert r["count"] == 3 and all("taf" not in a for a in r["airports"])
    by = {a["icao"]: a for a in r["airports"]}
    assert by["OIMM"] == {"icao": "OIMM", "name": "Mashhad", "station_id": 17555, "flight_category": "mvfr"}
    one = (await client.call_tool("wx_airports", {"airport": "oimm"})).structured_content["airports"][0]
    assert one["taf"].startswith("TAF OIMM 180530Z 1806/1912") and "\n" not in one["taf"]
    assert (await client.call_tool("wx_airports", {"airport": "abadan"})).structured_content["airports"][0][
        "icao"
    ] == "OIAA"
    assert by["OIAA"]["flight_category"] == "ifr"
    assert (await client.call_tool("wx_airports", {"airport": "Paris"})).is_error


async def test_bulletins(client, site):
    site["wd/1062-"] = "b5.html"
    site["wd/10425-"] = "bw.html"
    r = (await client.call_tool("wx_bulletins", {})).structured_content
    assert r["kind"] == "five_day" and r["published"] == "1405/08/26"
    assert r["pdf"].startswith("https://www.irimo.ir/far/index.php?module=cdk") and "&amp;" not in r["pdf"]
    r = (await client.call_tool("wx_bulletins", {"kind": "weekly"})).structured_content
    assert r["published"] == "1405/08/25" and r["pdf"].endswith("KH.pdf") and " " not in r["pdf"]


async def test_bulletin_title_belongs_to_the_pdf(client, site):
    # the seasonal page lists map titles before the bulletin's own block
    site["wd/1061-"] = lambda request: httpx.Response(
        200,
        text='<td class="saman_title" align="center"> نقشه بارش </td><span class="data">۱۴۰۵/۰۸/۰۱</span>'
        '<a href="https://www.irimo.ir//x/seasonal.pdf"><img></a>'
        '<td class="saman_title" align="center"> خبرنامه پیش بینی فصلی - آبان 1405 </td>'
        '<span class="caption">تاریخ انتشار:</span><span class="data">۱۴۰۵/۰۸/۰۲</span>',
    )
    r = (await client.call_tool("wx_bulletins", {"kind": "seasonal"})).structured_content
    assert r["title"] == "خبرنامه پیش بینی فصلی - آبان 1405" and r["published"] == "1405/08/02"
