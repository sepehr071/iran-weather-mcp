"""Weather tools: city forecasts and current observations, province stations, warnings (irimo.ir HTML pages)."""

from __future__ import annotations

import html
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import BASE, SITE, ApiError, fetch
from .registry import tool

CITY_URL = f"{BASE}/index.php?module=web_directory&wd_id=701&id="
WARNINGS_URL = f"{BASE}/wd/592-warnings.html"
WARNING_URL = f"{BASE}/services/news/"
PAGE_SIZE = 20  # warnings per list page
LEVELS = {"سفید": "white", "زرد": "yellow", "نارنجی": "orange", "قرمز": "red"}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NORM = str.maketrans({"ي": "ی", "ك": "ک", "\u200c": "", " ": "", "ـ": ""})
WARNING_LABELS = re.compile(
    r"(توصیف سامانه[^:\n]{0,10}|زمان شروع|زمان پایان|نوع مخاطره|منطقه اثر|اثر مخاطره|توصیه)\s*:"
)
_index: dict[int, str] = {}  # station id -> name, filled lazily by _resolve from province pages


def _d(s: str) -> str:
    """ASCII digits."""
    return s.translate(_DIGITS)


def _text(s: str) -> str:
    """Strip tags, decode entities, collapse whitespace, ASCII digits."""
    return _d(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s))).strip())


def _num(s: str | None) -> float | int | None:
    """First number in a string ('۱۵° c' -> 15); None for '-' or '--' (no data)."""
    m = re.search(r"-?\d+(?:\.\d+)?", _d(s or ""))
    if not m:
        return None
    v = float(m.group())
    return int(v) if v.is_integer() else v


def _val(s: str) -> str | None:
    """Cleaned text; None when the site shows a dash (no data)."""
    s = _text(s)
    return None if s in ("", "-", "--") else s


def _key(s: str) -> str:
    return html.unescape(s).translate(_NORM)


# ---------- parsers ----------


def _station_blocks(page: str) -> list[dict[str, Any]]:
    """Map markers: a link with the station id followed by its tooltip (name, humidity, wind, temperature)."""
    out = []
    pat = (
        r"href='[^']*?[?&]id=(\d+)[^']*'(?:(?!href=).)*?class='centertitle'>(.*?)</div>"
        r"<div class='centerdetail'>(.*?)</div></div>(?:(?!href=).)*?numwether fontYk'>(.*?)</div>"
    )
    for m in re.finditer(pat, page, re.S):
        sid, name, detail, temp = m.groups()
        if not _text(name):  # a few stations on the site have a blank name and no data
            continue
        detail_t = _text(detail.replace("<br />", " | "))
        hum = re.search(r"نم نسبی\s*%\s*(\d+)", detail_t)
        wind = re.search(r"(\d+)\s*km/h", detail_t)
        direction = re.search(r"جهت باد\s*([^|]+)", detail_t)
        out.append(
            {
                "id": int(sid),
                "name": _text(name),
                "temp_c": _num(temp),
                "humidity_pct": int(hum.group(1)) if hum else None,
                "wind_kmh": int(wind.group(1)) if wind else (0 if "آرام" in detail_t else None),
                "wind_from": direction.group(1).strip() if direction else None,
            }
        )
    return out


def parse_home(page: str) -> dict[str, Any]:
    """Home page: today's forecast (condition, max, min) for the main cities, plus the country map observations."""
    day = re.search(r"پیش بینی وضع هوا\s*<span[^>]*>\|([^<]+)</span>", page)
    cond = {m.group(1): m.group(2) for m in re.finditer(r"\$\('#img_(\d+)'\)\.html\('<img[^>]*?alt=\"([^\"]*)\"", page)}
    temps = {
        m.group(1): [_num(x) for x in re.findall(r">([^<>]*?)&nbsp;&nbsp;<img", m.group(2))]
        for m in re.finditer(r"\$\('#info_(\d+)'\)\.html\('(.*?)'\);", page, re.S)
    }
    cities = []
    row = (
        r'<td class="_met_td1">\s*<a href="[^"]*?[?&]id=(\d+)">\s*(.*?)\s*</a>\s*</td>\s*'
        r'<td class="_met_td2" id="info_(\d+)">'
    )
    for m in re.finditer(row, page, re.S):
        cid, name, key = m.groups()
        hi, lo = (temps.get(key) or [None, None])[:2]
        cities.append({"id": int(cid), "name": _text(name), "condition": cond.get(key), "max_c": hi, "min_c": lo})
    return {"day": _text(day.group(1)) if day else None, "cities": cities, "stations": _station_blocks(page)}


def parse_city(page: str) -> dict[str, Any]:
    """City page: current observation, 3 day forecast and the observations of the province's stations."""
    name = re.search(r"<h1[^>]*>\s*وضع هوای (.*?)</h1>", page, re.S)
    out: dict[str, Any] = {"name": _text(name.group(1)) if name else None}
    cur = re.search(r'id="divCurrentWeather".*?(?=id="divFutureWeather"|$)', page, re.S)
    if cur:
        c = cur.group()
        t = re.search(r'font-size:48px;direction:ltr">(.*?)</div>', c, re.S)
        cond = re.search(r'font-size:18px;">(.*?)</div>', c, re.S)
        fields = {
            _text(k): _text(v) for k, v in re.findall(r"wet_s_wblk_caption'>(.*?)</span>(.*?)(?=<br|</div>|$)", c, re.S)
        }  # a value is plain "-" (no <span>) while the station has no fresh report
        hour = re.search(r"ساعت:\s*([\d:۰-۹]+)", page)
        upd = re.search(r"آخرین بروز رسانی:\s*([^<]+)", c)
        wind = fields.get("سرعت باد:", "")
        ms, kmh = re.search(r"(\d+)\s*متر", wind), re.search(r"(\d+)\s*کیلومتر", wind)
        direction = re.search(r"جهت باد:\s*([^<]+)", c)
        vis_t = fields.get("دید افقی:", "")
        vis = _num(vis_t)
        if vis is not None and "کیلومتر" in vis_t:
            vis *= 1000  # "بیش از ۱۰ کیلومتر" (more than 10 km) is how the site reports good visibility
        rain = fields.get("بارندگی 24 ساعت گذشته:", "")
        out["current"] = {
            "temp_c": _num(t.group(1)) if t else None,
            "condition": _val(cond.group(1)) if cond else None,
            "wind_kmh": int(kmh.group(1)) if kmh else (0 if "آرام" in wind else None),
            "wind_ms": int(ms.group(1)) if ms else None,
            "wind_from": _val(direction.group(1)) if direction else None,
            "humidity_pct": _num(fields.get("نم نسبی:")),
            "visibility_m": vis,
            "visibility_over": "بیش از" in vis_t or None,
            "rain_24h_mm": 0 if "صفر" in rain else _num(rain),
            "max_24h_c": _num(fields.get("بیشینه دما در 24 ساعت گذشته:")),
            "min_24h_c": _num(fields.get("کمینه دما در 24 ساعت گذشته:")),
            "observed_at": _d(hour.group(1)) if hour else None,
            "updated": _val(upd.group(1)) if upd else None,
        }
    fut = re.search(r'id="divFutureWeather".*?</table>(.*?</div>\s*</div>)?', page, re.S)
    if fut:
        f = fut.group()
        days = []
        parts = re.split(r"24 ساعت (اول|دوم|سوم)</div>", f)
        for label, seg in zip(parts[1::2], parts[2::2], strict=False):
            cond = re.search(r'height:70px">(.*?)</div>', seg, re.S)
            lo = re.search(r'direction:ltr">([^<]*?)°[^<]*(?:&nbsp;)?<img[^>]*downL', seg, re.S)
            hi = re.search(r'upL\.png" >&nbsp;([^<]*?)°', seg, re.S)
            days.append(
                {
                    "day": {"اول": 1, "دوم": 2, "سوم": 3}[label],
                    "condition": _text(cond.group(1)) if cond else None,
                    "min_c": _num(lo.group(1)) if lo else None,
                    "max_c": _num(hi.group(1)) if hi else None,
                }
            )
        if days:
            out["forecast"] = days
        upd = re.search(r"آخرین بروز رسانی:\s*([^<]+)", f)
        if upd:
            out["forecast_updated"] = _text(upd.group(1))
    prov = re.search(r'top_current_title">وضع هوای فعلی استان ([^<]+)<', page)
    if prov:
        out["province"] = _text(prov.group(1))
    out["stations"] = _station_blocks(page)
    return out


def parse_warnings(page: str) -> dict[str, Any]:
    """Warning list page: entries plus the paging parameters of the list form."""
    items = []
    for m in re.finditer(r'<a href="[^"]*?/services/news/(\d+)-[^"]*" title="([^"]*)"', page):
        wid, title = m.groups()
        title = _text(title)
        level = re.search(r"سطح\s*(\S+)", title)
        number = re.search(r"شماره\s*(\d+)", title)
        date = re.search(r"(1[34]\d\d/\d\d/\d\d)", title)
        items.append(
            {
                "id": int(wid),
                "title": title,
                "kind": "marine"
                if "دریایی" in title
                else "pollution"
                if "آلودگی" in title
                else "agriculture"
                if "کشاورزی" in title
                else "weather",
                "level": LEVELS.get(level.group(1)) if level else None,
                "number": int(number.group(1)) if number else None,
                "date": date.group(1) if date else None,
            }
        )
    form = re.search(r"form_(\d+)s=\d+&amp;form_\1a=(\d+)", page)
    return {"items": items, "form": form.groups() if form else None}


def parse_warning(page: str) -> dict[str, Any]:
    """Warning page: title, publish date, the labelled body lines (system, start, end, hazard, area...), files."""
    title = re.search(r'class="saman_title">(.*?)</div>', page, re.S)
    date = re.search(r"تاریخ انتشار:\s*</span>\s*([\d/]+)", page)
    start = page.find('class="saman_description"')
    end = min(
        (i for i in (page.find(m, start) for m in ("فایل پیوست", "تصاویر مرتبط", "</form>")) if i > 0),
        default=len(page),
    )
    region = page[start:end].partition(">")[2] if start >= 0 else ""
    body = re.sub(r'<div class="saman_pic">.*?</div>|<(script|style)\b.*?</\1>', "", region, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]*>", " ", re.sub(r"<br\s*/?>|</p>|</li>", "\n", body)))
    text = _d(re.sub(r"[ \t\r\xa0]+", " ", text))
    # every labelled field starts a new entry; lines without a label (sub-areas by day) stay with the previous one
    parts = re.split(WARNING_LABELS, text)
    details = [f"{k.strip()}: {' '.join(v.split())}" for k, v in zip(parts[1::2], parts[2::2], strict=False)]
    out: dict[str, Any] = {
        "title": _text(title.group(1)) if title else None,
        "published": date.group(1) if date else None,
    }
    if parts[0].strip():
        out["text"] = " ".join(parts[0].split())
    if details:
        out["details"] = details
    files = re.findall(r'href="([^"]+\.(?:pdf|docx?))"', page[start:] if start >= 0 else page, re.I)
    if files:
        out["files"] = list(dict.fromkeys(u.replace(" ", "%20") for u in files))
    img = re.search(r'class="saman_pic">\s*<a href="([^"]+)"', page)
    if img:
        out["map_image"] = img.group(1)
    return out


def _visibility(s: str) -> tuple[float | int | None, bool | None]:
    """Metres from the site's visibility text; 'بیش از ۱۰ کیلومتر' (more than 10 km) -> (10000, True)."""
    v = _num(s)
    if v is not None and "کیلومتر" in s:
        v *= 1000
    return v, ("بیش از" in s) or None


def parse_roads(page: str) -> dict[str, Any]:
    """Road list page: route ids and names, plus the paging parameters of the list form."""
    items = [
        {"id": int(i), "name": _text(n)}
        for i, n in re.findall(r'<a title="[^"]*" [^>]*?href="[^"]*/wd/732-[^"]*\?road=(\d+)">(.*?)</a>', page)
    ]
    form = re.search(r"form_(\d+)s=\d+&amp;form_\1a=(\d+)", page)
    offsets = list(dict.fromkeys(re.findall(r"form_\d+s=(\d+)&amp;form_\d+a=\d+", page)))
    return {"items": items, "form": form.groups() if form else None, "offsets": [int(o) for o in offsets]}


def parse_road(page: str) -> dict[str, Any]:
    """Road page: the current weather of every city along the route, in route order."""
    name = re.search(r"<title>[^<]*?\(راه\)\s*([^<|:]+?)\s*(?:[|:<])", page)
    stations = []
    for row in re.findall(r"<tr class='met-(?:odd|even)'>(.*?)</tr>", page, re.S):
        c = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(c) < 7:
            continue
        link = re.search(r"[?&]id=(\d+)", c[0])
        when = re.search(r"([\d:]+)\s+(\d{4}/\d\d/\d\d)", _text(c[1].replace("&nbsp;", " ")))
        cond = re.search(r"alt='([^']*)'", c[2])
        vis, over = _visibility(_text(c[4]))
        wind = _text(c[6].replace("<br/>", " | "))
        ms, kmh = re.search(r"(\d+)\s*mps", wind), re.search(r"(\d+)\s*km/h", wind)
        direction = wind.split("|")[0].strip()
        pressure = _num(_text(c[5]))
        stations.append(
            {
                "id": int(link.group(1)) if link else None,
                "name": _text(c[0]),
                "observed_at": f"{when.group(2)} {when.group(1)}" if when else None,
                "condition": _val(cond.group(1)) if cond else None,
                "temp_c": _num(_text(c[3])),
                "visibility_m": vis,
                "visibility_over": over,
                "pressure_hpa": pressure or None,  # the site prints 0 when the sensor has no value
                "wind_from": None if "آرام" in direction or "mps" in direction else _val(direction),
                "wind_ms": int(ms.group(1)) if ms else None,
                "wind_kmh": int(kmh.group(1)) if kmh else (0 if "آرام" in wind else None),
            }
        )
    return {"name": _text(name.group(1)) if name else None, "stations": stations}


def parse_airports(page: str) -> list[dict[str, Any]]:
    """Aviation page: one entry per airport with its TAF forecast text, flight category and station id."""
    cats = {
        m.group(3): (m.group(1), m.group(2).lower())
        for m in re.finditer(
            r"""href=['"][^'"]*[?&]id=(\d+)[^'"]*['"][^>]*><img[^>]*?TAFOR_(\w+)\.png[^>]*?id=['"](\w{4})['"]""", page
        )
    }
    out = []
    for m in re.finditer(
        r"""id=['"]stat_info_\d+_(\w{4})['"].*?headerTooltip[^>]*><div[^>]*>(.*?)</div>.*?bodytooltip[^>]*>(.*?)</div>""",
        page,
        re.S,
    ):
        icao, head, body = m.groups()
        sid, cat = cats.get(icao, (None, None))
        out.append(
            {
                "icao": icao,
                "name": re.sub(rf"^{icao}-\s*", "", _text(head)) or None,
                "station_id": int(sid) if sid else None,
                "flight_category": "vfr" if cat == "vrf" else cat,  # the site's icon file is spelled TAFOR_vrf
                "taf": re.sub(r"\s+", " ", html.unescape(body)).strip() or None,
            }
        )
    return out


BULLETINS = {  # kind -> (page id, what it is)
    "five_day": 1062,
    "weekly": 10425,
    "seasonal": 1061,
    "tehran": 1639,
    "air_quality": 4754,
}


def parse_bulletin(page: str) -> dict[str, Any]:
    """Bulletin page: title, publish date and PDF link of the latest bulletin."""
    href = re.search(r'href="([^"]*(?:pdf_name=[^"]*|\.pdf))"', page)
    after = page[href.start() :] if href else page  # the seasonal page lists map titles before the bulletin's own
    title = re.search(r'class="saman_title"[^>]*>(.*?)</td>', after, re.S)
    date = re.search(r'class="data">\s*([\d/۰-۹]+)\s*<', after)
    url = html.unescape(href.group(1)).replace(" ", "%20") if href else None
    if url and url.startswith("/"):
        url = SITE + url
    if not date and url and (m := re.search(r"_(\d{4})(\d\d)(\d\d)\.pdf", url)):  # file name carries the Jalali date
        published = "/".join(m.groups())
    else:
        published = _d(date.group(1)) if date else None
    return {"title": _text(title.group(1)) if title else None, "published": published, "pdf": url}


# ---------- city resolution ----------


async def _home() -> dict[str, Any]:
    """Home page data; the city list is paged (14 per page), so the other pages are merged in."""
    page = await fetch(f"{BASE}/index.php")
    home = parse_home(page)
    for form, offset, a in dict.fromkeys(re.findall(r"index\.php&amp;form_(\d+)s=(\d+)&amp;form_\1a=(\d+)", page)):
        more = parse_home(await fetch(f"{BASE}/index.php?form_{form}s={offset}&form_{form}a={a}"))
        home["cities"] += more["cities"]
    return home


async def _resolve(city: str) -> int:
    """Station id from a number, a main city name, or any station name in a province page."""
    q = city.strip()
    if q.isdigit() or (_d(q).isdigit()):
        return int(_d(q))
    k = _key(q)
    if not k:
        raise ApiError("Give a city name or a station id.")
    home = await _home()
    for c in home["cities"]:
        if _key(c["name"]) == k:
            return c["id"]
    stations = {s["id"]: s["name"] for s in home["stations"]}
    stations.update(_index)
    hit = [i for i, n in stations.items() if _key(n) == k] or [i for i, n in stations.items() if k in _key(n)]
    if hit:
        return hit[0]
    for c in home["cities"]:  # not on the home page: look through the province pages, one per main city
        if k in _key(c["name"]):
            return c["id"]
    for c in home["cities"]:
        if c["id"] in _index:  # its province page was already read
            continue
        found = parse_city(await fetch(CITY_URL + str(c["id"])))["stations"]
        _index.update({s["id"]: s["name"] for s in found})
        _index[c["id"]] = c["name"]
        for s in found:
            if k in _key(s["name"]):
                return s["id"]
    raise ApiError(
        f"No station named '{city}'. Use wx_cities for the main cities or wx_province to list a province's stations."
    )


async def _city(city: str) -> tuple[int, dict[str, Any]]:
    cid = await _resolve(city)
    data = parse_city(await fetch(CITY_URL + str(cid)))
    if not data.get("name"):
        raise ApiError(f"Station {cid} not found. Use wx_cities or wx_province for valid ids.")
    for s in data["stations"]:
        _index[s["id"]] = s["name"]
    return cid, data


# ---------- tools ----------


@tool("Main cities forecast")
async def wx_cities() -> dict[str, Any]:
    """Today's forecast (condition, max and min temperature) for the main cities (about 35), with the ids wx_weather accepts.

    Start here for "what is the weather in Iran today". Temperatures are in °C; `day` is the Persian date label
    the site shows.
    """
    home = await _home()
    return {"day": home["day"], "count": len(home["cities"]), "cities": home["cities"]}


@tool("Weather at a city")
async def wx_weather(
    city: Annotated[
        str,
        Field(
            description="City or station name in Persian (تهران, مشهد, کاشمر) or a station id from wx_cities / wx_province"
        ),
    ],
) -> dict[str, Any]:
    """Current observation and 3-day forecast for one city or weather station.

    Current: temperature, wind, humidity, visibility, rain and min/max over the last 24 h, with the observation time
    and the update time (Jalali, Tehran time). Forecast: three 24 h periods with condition and min/max. Not every
    station has a forecast; small stations only have observations.
    """
    cid, d = await _city(city)
    d.pop("stations", None)
    d.pop("province", None)
    return {"id": cid, **d}


@tool("Province stations now")
async def wx_province(
    city: Annotated[str, Field(description="Any city or station of the province (تهران, مشهد) or its station id")],
) -> dict[str, Any]:
    """Latest observations of every weather station in the province of a city.

    Each station has an id (usable in wx_weather), temperature in °C, humidity, wind speed and the direction it
    blows from. Stations without a reading show no temperature.
    """
    cid, d = await _city(city)
    return {"province": d.get("province"), "count": len(d["stations"]), "stations": d["stations"]}


@tool("Country observations")
async def wx_observations() -> dict[str, Any]:
    """Current temperature, humidity and wind at the ~37 stations on the national map of irimo.ir.

    Compact whole-country snapshot (main cities, airports, islands). For one city's full detail use wx_weather.
    """
    home = await _home()
    return {"count": len(home["stations"]), "stations": home["stations"]}


async def _warning_pages(kind: str, limit: int, page: int) -> list[dict[str, Any]]:
    first = parse_warnings(await fetch(WARNINGS_URL))
    form, items = first["form"], first["items"]
    out: list[dict[str, Any]] = []
    for p in range(page, page + 5):  # filtered lists may need several pages to fill the limit
        if p > 1:
            if not form:
                break
            items = parse_warnings(
                await fetch(f"{WARNINGS_URL}?form_{form[0]}s={(p - 1) * PAGE_SIZE}&form_{form[0]}a={form[1]}")
            )["items"]
        out += [w for w in items if kind == "all" or w["kind"] == kind]
        if len(out) >= limit or not items:
            break
    return out[:limit]


@tool("Weather warnings")
async def wx_warnings(
    kind: Annotated[
        Literal["all", "weather", "marine", "pollution", "agriculture"],
        Field(
            description="Warning type: weather (rain, wind, snow, heat), marine (sea), pollution (air quality), "
            "agriculture (frost, farming)"
        ),
    ] = "all",
    limit: Annotated[int, Field(ge=1, le=40)] = 10,
    page: Annotated[int, Field(ge=1, description="List page, 20 warnings per page, newest first")] = 1,
) -> dict[str, Any]:
    """Latest official warnings of the Iran Meteorological Organization, newest first.

    Each has an id for wx_warning, a type, a level (yellow, orange, red) and a number. Warnings are issued per
    event and nothing marks them expired: check the dates in wx_warning.
    """
    items = await _warning_pages(kind, limit, page)
    return {"count": len(items), "warnings": items}


ROADS_URL = f"{BASE}/wd/731-roads.html"
ROAD_URL = f"{BASE}/wd/732-road.html?road="
AVIATION_URL = f"{BASE}/wd/1218-aviation.html"
_roads: dict[int, str] = {}  # route id -> name, read once from the paged route list


async def _road_list() -> dict[int, str]:
    if not _roads:
        first = parse_roads(await fetch(ROADS_URL))
        found = {r["id"]: r["name"] for r in first["items"]}
        for off in first["offsets"]:
            if first["form"]:
                page = await fetch(
                    f"{ROADS_URL}?form_{first['form'][0]}s={off}&form_{first['form'][0]}a={first['form'][1]}"
                )
                found.update({r["id"]: r["name"] for r in parse_roads(page)["items"]})
        _roads.update(found)
    return _roads


@tool("Road weather")
async def wx_road(
    route: Annotated[
        str,
        Field(description="Route as 'origin-destination' in Persian (تهران-مشهد, آبادان تهران) or a route id"),
    ],
) -> dict[str, Any]:
    """Current weather of every city along a main road, from the road-weather service of irimo.ir.

    One row per city in route order: time, condition, temperature, visibility, pressure, wind. If the route name is
    not exact, the matching routes are returned (`routes`) to pick from. Only the routes the site lists (about 400).
    """
    q = route.strip()
    if _d(q).isdigit():
        rid = int(_d(q))
    else:
        tokens = [_key(t) for t in re.split(r"[-–—،,]|\s+", q) if _key(t)]
        if not tokens:
            raise ApiError("Give a route like 'تهران-مشهد' or a route id.")
        names = await _road_list()
        hits = [(i, n) for i, n in names.items() if all(t in _key(n) for t in tokens)]
        exact = [(i, n) for i, n in hits if _key(n).replace("-", "") == "".join(tokens)]
        if len(exact) == 1 or len(hits) == 1:
            rid = (exact or hits)[0][0]
        elif hits:
            return {"routes": [{"id": i, "name": n} for i, n in hits[:30]], "count": len(hits)}
        else:
            raise ApiError(f"No route matching '{route}'. Try fewer words, e.g. just the two city names.")
    d = parse_road(await fetch(ROAD_URL + str(rid)))
    if not d["stations"]:
        raise ApiError(f"Route {rid} not found or has no stations.")
    return {"id": rid, **d, "count": len(d["stations"])}


@tool("Airport forecasts")
async def wx_airports(
    airport: Annotated[
        str | None,
        Field(description="ICAO code (OIII, OIMM) or part of the English name (Mashhad); empty lists all airports"),
    ] = None,
) -> dict[str, Any]:
    """Aviation forecasts (TAF) of about 29 Iranian airports with their flight category (vfr, mvfr, ifr, lifr).

    Without `airport` only the list with categories is returned; with it, the full TAF text of the match. TAF is
    the standard coded airport forecast (wind, visibility, cloud, TEMPO/BECMG changes, UTC times). `station_id` works
    in wx_weather for the city's ground observation.
    """
    items = parse_airports(await fetch(AVIATION_URL))
    if not items:
        raise ApiError("The aviation page shows no airports right now.")
    q = (airport or "").strip().lower()
    if not q:
        return {"count": len(items), "airports": [{k: v for k, v in a.items() if k != "taf"} for a in items]}
    hit = [a for a in items if q == a["icao"].lower()] or [
        a for a in items if q in a["icao"].lower() or q in (a["name"] or "").lower()
    ]
    if not hit:
        raise ApiError(f"No airport matching '{airport}'. Call wx_airports without arguments for the list.")
    return {"count": len(hit), "airports": hit}


@tool("Forecast bulletins")
async def wx_bulletins(
    kind: Annotated[
        Literal["five_day", "weekly", "seasonal", "tehran", "air_quality"],
        Field(description="five_day, weekly (about 6 weeks), seasonal, tehran (province) or air_quality (Tehran)"),
    ] = "five_day",
) -> dict[str, Any]:
    """Link to the latest official forecast bulletin (PDF): 5-day, weekly, seasonal, Tehran province or Tehran air quality.

    The site publishes these long-range forecasts only as PDF files, so this returns the title, publish date and the
    PDF link, not the text.
    """
    d = parse_bulletin(await fetch(f"{BASE}/wd/{BULLETINS[kind]}-bulletin.html"))
    if not d["pdf"]:
        raise ApiError("No bulletin file is published on the page right now.")
    return {"kind": kind, **d}


@tool("Warning detail")
async def wx_warning(
    warning_id: Annotated[int, Field(description="Warning id from wx_warnings")],
) -> dict[str, Any]:
    """Full text of one warning: system description, start and end time, hazard, affected area, impact, advice.

    `details` lists the labelled lines the site prints (Persian, as written; a warning for two systems repeats the
    labels). `files` holds the PDF bulletin and `map_image` the affected-area map.
    """
    d = parse_warning(await fetch(f"{WARNING_URL}{warning_id}-w.html"))
    if not d.get("title"):
        raise ApiError(f"Warning {warning_id} not found.")
    return {"id": warning_id, **d, "url": f"{SITE}/far/services/news/{warning_id}-w.html"}
