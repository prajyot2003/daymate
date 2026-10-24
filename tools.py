"""Keyless data sources and link builders used by the DayMate agents."""

import asyncio
import json
import re
from datetime import date, timedelta
from urllib.parse import quote, urlencode

import httpx

http = httpx.AsyncClient(timeout=30, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 DayMate/1.0 (Fetch-A-Thon 3.0)"})

# Interest -> Nominatim search phrase. ASI1 may only pick from these keys.
CATEGORIES = {
    "museum": "museum", "attraction": "attraction", "viewpoint": "viewpoint", "heritage": "fort",
    "monument": "monument", "temple": "temple", "park": "park", "cafe": "cafe", "restaurant": "restaurant",
    "street_food": "fast food", "market": "market", "mall": "mall", "nightlife": "bar",
}

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 80: "rain showers", 81: "rain showers", 82: "violent showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def weather_icon(code: int, rain: float) -> str:
    if code >= 95:
        return "⛈️"
    if rain >= 50 or code >= 51:
        return "🌧️"
    return "☀️" if code <= 1 else "⛅" if code == 2 else "☁️"


async def geocode(city: str) -> dict | None:
    r = await http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1})
    results = r.json().get("results")
    return results[0] if results else None


async def fetch_weather(lat: float, lon: float, start: str, days: int) -> dict:
    """Returns {"summary": text for the LLM, "daily": {date: {...}}, "hourly": {"YYYY-MM-DDTHH:00": [temp, rain%, code]}}."""
    end = (date.fromisoformat(start) + timedelta(days=days - 1)).isoformat()
    r = await http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "start_date": start, "end_date": end,
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,weather_code",
    })
    r.raise_for_status()
    d = r.json()
    dl, hl = d["daily"], d["hourly"]
    daily = {day: {"min": dl["temperature_2m_min"][i], "max": dl["temperature_2m_max"][i],
                   "rain": dl["precipitation_probability_max"][i] or 0, "code": dl["weather_code"][i],
                   "sunrise": dl["sunrise"][i][-5:], "sunset": dl["sunset"][i][-5:]}
             for i, day in enumerate(dl["time"])}
    hourly = {t: [hl["temperature_2m"][i], hl["precipitation_probability"][i] or 0, hl["weather_code"][i]]
              for i, t in enumerate(hl["time"]) if 7 <= int(t[11:13]) <= 22}
    lines = []
    for day, w in daily.items():
        lines.append(f"{day}: {w['min']}-{w['max']}°C, max rain {w['rain']}%, sunrise {w['sunrise']}, sunset {w['sunset']}")
        lines += [f"  {t[11:]} {v[0]}°C rain {v[1]}% {WMO.get(v[2], '?')}"
                  for t, v in hourly.items() if t.startswith(day) and int(t[11:13]) % 2 == 0]
    return {"summary": "\n".join(lines), "daily": daily, "hourly": hourly}


async def fetch_places(city: str, lat: float, lon: float, categories: list[str], per_cat: int = 6) -> list[dict]:
    # ponytail: Nominatim, sequential at its 1 req/s limit; swap for a paid places API if it needs ratings/reviews
    places, seen = [], set()
    box = f"{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}"  # ~30 km around the city centre
    for i, cat in enumerate(c for c in categories if c in CATEGORIES):
        if i:
            await asyncio.sleep(1.1)
        try:
            r = await http.get("https://nominatim.openstreetmap.org/search", params={
                "q": f"{CATEGORIES[cat]} in {city}", "format": "jsonv2", "limit": max(12, per_cat * 2), "extratags": 1,
                "viewbox": box, "bounded": 1, "accept-language": "en"})
            r.raise_for_status()
        except httpx.HTTPError:
            continue  # one failed category shouldn't sink the plan
        added = 0
        for x in sorted(r.json(), key=lambda x: x.get("importance", 0), reverse=True):
            key = x.get("name", "").lower()
            if (len(key) <= 3 or key in seen or key == CATEGORIES[cat] or added == per_cat
                    or x.get("category") in ("highway", "boundary", "place")
                    or re.match(r"(to|way to|road to|path to) ", key)):
                continue
            lat_, lon_ = float(x["lat"]), float(x["lon"])
            if any(p["category"] == cat and abs(p["lat"] - lat_) + abs(p["lon"] - lon_) < 0.005 for p in places):
                continue  # duplicate OSM entry for the same spot (~500 m)
            seen.add(key)
            added += 1
            places.append({"id": f"p{len(places) + 1}", "name": x["name"], "category": cat,
                           "hours": (x.get("extratags") or {}).get("opening_hours", ""),
                           "lat": lat_, "lon": lon_})
    return places


async def fetch_events(city: str, days: list[str]) -> list[dict]:
    """Public events from allevents.in city pages (schema.org JSON-LD)."""
    r = await http.get(f"https://allevents.in/{quote(city.lower().replace(' ', '-'))}/all")
    r.raise_for_status()
    events = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for e in data if isinstance(data, list) else [data]:
            if not isinstance(e, dict) or e.get("@type") != "Event" or not e.get("startDate"):
                continue
            start, end = e["startDate"][:10], (e.get("endDate") or e["startDate"])[:10]
            geo = (e.get("location") or {}).get("geo") or {}
            if not geo.get("latitude") or not any(start <= d <= end for d in days):
                continue
            events.append({"id": f"e{len(events) + 1}", "name": e.get("name", "Event"), "category": "event",
                           "starts": e["startDate"], "venue": (e.get("location") or {}).get("name", ""),
                           "url": e.get("url", ""), "lat": float(geo["latitude"]), "lon": float(geo["longitude"])})
    return events[:8]


async def route_legs(points: list[tuple[float, float]]) -> list[dict]:
    """Driving time/distance between consecutive (lat, lon) points via the public OSRM server."""
    if len(points) < 2:
        return []
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        r = await http.get(f"https://router.project-osrm.org/route/v1/driving/{coords}", params={"overview": "false"})
        r.raise_for_status()
        return [{"min": round(leg["duration"] / 60), "km": round(leg["distance"] / 1000, 1)}
                for leg in r.json()["routes"][0]["legs"]]
    except (httpx.HTTPError, KeyError, IndexError):
        return []


# ---------- action links ----------

def maps_link(p: dict) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={p['lat']},{p['lon']}"


def route_link(stops: list[dict]) -> str:
    """Google Maps directions through every stop (Maps allows 9 waypoints)."""
    pts = [f"{s['lat']},{s['lon']}" for s in stops]
    q = {"api": 1, "origin": pts[0], "destination": pts[-1], "travelmode": "driving"}
    if len(pts) > 2:
        q["waypoints"] = "|".join(pts[1:-1][:9])
    return "https://www.google.com/maps/dir/?" + urlencode(q)


def calendar_link(title: str, day: str, start: str, end: str, tz: str, location: str, details: str) -> str:
    d = day.replace("-", "")
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": title, "ctz": tz, "location": location, "details": details,
        "dates": f"{d}T{start.replace(':', '')}00/{d}T{end.replace(':', '')}00"})


def ride_link(frm: dict | None, to: dict) -> str:
    q = {"action": "setPickup", "dropoff[latitude]": to["lat"], "dropoff[longitude]": to["lon"],
         "dropoff[nickname]": to["name"]}
    if frm:
        q |= {"pickup[latitude]": frm["lat"], "pickup[longitude]": frm["lon"], "pickup[nickname]": frm["name"]}
    else:
        q["pickup"] = "my_location"
    return "https://m.uber.com/ul/?" + urlencode(q)
