"""DayMate: a multi-agent day planner for ASI:One.

orchestrator (chat protocol, mailbox) -> weather agent (Open-Meteo)
                                      -> places agent  (OpenStreetMap Overpass)
ASI1 parses intent and writes the final weather-aware itinerary.

Run:   python daymate.py
Check: python daymate.py --check "Plan my Saturday in Jaipur, I love forts and street food"
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

load_dotenv()

ASI1_API_KEY = os.getenv("ASI1_API_KEY")
if not ASI1_API_KEY:
    sys.exit("ASI1_API_KEY is not set. Get one at https://asi1.ai/dashboard/api-keys and put it in .env")

llm = AsyncOpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI1_API_KEY)
http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "DayMate/1.0 (Fetch-A-Thon 3.0)"})

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


# ---------- messages between agents ----------

class WeatherRequest(Model):
    lat: float
    lon: float
    day: str  # YYYY-MM-DD


class WeatherReport(Model):
    ok: bool
    summary: str


class PlacesRequest(Model):
    city: str
    categories: list[str]


class PlacesReport(Model):
    ok: bool
    places: list[dict]


# ---------- tools ----------

async def ask_llm(system: str, user: str) -> str:
    r = await llm.chat.completions.create(
        model="asi1",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2048,
    )
    return r.choices[0].message.content or ""


async def parse_intent(text: str, previous: dict | None) -> dict:
    system = (
        f"Today is {date.today().isoformat()} ({date.today():%A}). Extract a day-trip request as JSON only, "
        'no prose: {"city": str|null, "day": "YYYY-MM-DD", "interests": [str], "notes": str}. '
        f"interests must be chosen from {list(CATEGORIES)} (pick 3-5, always include one food option). "
        "day defaults to today; resolve words like 'tomorrow' or 'Saturday'. "
        "notes = budget, group, pace or anything else the user said. "
        "If a previous request is given, treat the message as a modification of it."
    )
    user = text if not previous else f"Previous request: {json.dumps(previous)}\nNew message: {text}"
    raw = await ask_llm(system, user)
    match = re.search(r"\{.*\}", raw, re.S)
    intent = json.loads(match.group(0)) if match else {}
    intent["interests"] = [c for c in intent.get("interests", []) if c in CATEGORIES] or ["attraction", "restaurant"]
    try:
        date.fromisoformat(intent.get("day", ""))
    except ValueError:
        intent["day"] = date.today().isoformat()
    return intent


async def geocode(city: str) -> dict | None:
    r = await http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1})
    results = r.json().get("results")
    return results[0] if results else None


async def fetch_weather(lat: float, lon: float, day: str) -> str:
    r = await http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "start_date": day, "end_date": day,
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
    })
    r.raise_for_status()
    d = r.json()
    daily, hourly = d["daily"], d["hourly"]
    lines = [
        f"{day}: {daily['temperature_2m_min'][0]}-{daily['temperature_2m_max'][0]}°C, "
        f"max rain chance {daily['precipitation_probability_max'][0]}%, "
        f"sunrise {daily['sunrise'][0][-5:]}, sunset {daily['sunset'][0][-5:]}"
    ]
    for i in range(8, 22, 2):  # 08:00-20:00 every 2h
        lines.append(f"{hourly['time'][i][-5:]} {hourly['temperature_2m'][i]}°C, "
                     f"rain {hourly['precipitation_probability'][i]}%, "
                     f"{WMO.get(hourly['weather_code'][i], 'unknown')}")
    return "\n".join(lines)


async def fetch_places(city: str, categories: list[str]) -> list[dict]:
    # ponytail: Nominatim, sequential at its 1 req/s limit; swap for a paid places API if it needs ratings/reviews
    places, seen = [], set()
    for i, cat in enumerate(c for c in categories if c in CATEGORIES):
        if i:
            await asyncio.sleep(1.1)
        try:
            r = await http.get("https://nominatim.openstreetmap.org/search", params={
                "q": f"{CATEGORIES[cat]} in {city}", "format": "jsonv2", "limit": 10, "extratags": 1})
            r.raise_for_status()
        except httpx.HTTPError:
            continue  # one failed category shouldn't sink the plan
        hits = sorted(r.json(), key=lambda x: x.get("importance", 0), reverse=True)
        added = 0
        for x in hits:
            key = x.get("name", "").lower()
            if len(key) <= 3 or key in seen or added == 6:
                continue
            seen.add(key)
            added += 1
            places.append({
                "name": x["name"], "category": cat, "hours": (x.get("extratags") or {}).get("opening_hours", ""),
                "map": f"https://www.openstreetmap.org/?mlat={x['lat']}&mlon={x['lon']}#map=17/{x['lat']}/{x['lon']}",
            })
    return places


async def compose_plan(intent: dict, place: dict, weather: str | None, places: list[dict]) -> str:
    system = (
        "You are DayMate, a practical local day planner. Write a timed itinerary (morning to evening) in markdown. "
        "Use ONLY places from the provided list and link each one with its map URL as [Name](url). "
        "Use the hourly weather: put outdoor stops in the best hours, move indoors when rain chance > 50% "
        "or it is very hot, and say why in a short line. Group nearby stops, include meals, keep it to 5-7 stops. "
        "Respect opening hours if given. End with a 'Pack/Tips' line. If data is missing, say so briefly and still help."
    )
    user = (
        f"Request: {json.dumps(intent)}\nLocation: {place['name']}, {place.get('country', '')}\n"
        f"Weather:\n{weather or 'unavailable'}\nPlaces:\n{json.dumps(places, ensure_ascii=False)}"
    )
    return await ask_llm(system, user)


# ---------- agents ----------

# Separate ports so the Agentverse inspector on :8001 sees only the orchestrator;
# same process, so agent-to-agent messages are delivered in memory.
orchestrator = Agent(name="daymate", seed=os.environ["ORCHESTRATOR_SEED"], port=8001, mailbox=True,
                     publish_agent_details=True)
weather_agent = Agent(name="daymate-weather", seed=os.environ["WEATHER_SEED"], port=8002)
places_agent = Agent(name="daymate-places", seed=os.environ["PLACES_SEED"], port=8003)


@weather_agent.on_message(WeatherRequest, replies=WeatherReport)
async def on_weather(ctx: Context, sender: str, req: WeatherRequest):
    try:
        await ctx.send(sender, WeatherReport(ok=True, summary=await fetch_weather(req.lat, req.lon, req.day)))
    except Exception as err:
        ctx.logger.exception("weather failed")
        await ctx.send(sender, WeatherReport(ok=False, summary=str(err)))


@places_agent.on_message(PlacesRequest, replies=PlacesReport)
async def on_places(ctx: Context, sender: str, req: PlacesRequest):
    try:
        await ctx.send(sender, PlacesReport(ok=True, places=await fetch_places(req.city, req.categories)))
    except Exception:
        ctx.logger.exception("places failed")
        await ctx.send(sender, PlacesReport(ok=False, places=[]))


chat = Protocol(spec=chat_protocol_spec)


def reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), content=[
        TextContent(type="text", text=text), EndSessionContent(type="end-session")])


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    try:
        previous = ctx.storage.get(sender)
        intent = await parse_intent(text, previous)
        if not intent.get("city"):
            await ctx.send(sender, reply("Which city should I plan for? e.g. *Plan Saturday in Jaipur, I love forts and street food*"))
            return
        place = await geocode(intent["city"])
        if not place:
            await ctx.send(sender, reply(f"I couldn't find **{intent['city']}** on the map. Could you check the spelling?"))
            return
        ctx.storage.set(sender, intent)
        ctx.logger.info(f"intent={intent}")

        (w, _), (p, _) = await asyncio.gather(
            ctx.send_and_receive(weather_agent.address, WeatherRequest(
                lat=place["latitude"], lon=place["longitude"], day=intent["day"]), WeatherReport, timeout=40),
            ctx.send_and_receive(places_agent.address, PlacesRequest(
                city=place["name"], categories=intent["interests"]), PlacesReport, timeout=40),
        )
        weather = w.summary if w and w.ok else None
        places = p.places if p and p.ok else []
        plan = await compose_plan(intent, place, weather, places)
        await ctx.send(sender, reply(plan))
    except Exception:
        ctx.logger.exception("planning failed")
        await ctx.send(sender, reply("Something went wrong while planning. Please try again in a moment."))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


orchestrator.include(chat, publish_manifest=True)


async def check(text: str):
    """End-to-end run of the tool pipeline without the agent network."""
    intent = await parse_intent(text, None)
    assert intent.get("city"), intent
    place = await geocode(intent["city"])
    assert place, intent["city"]
    weather = await fetch_weather(place["latitude"], place["longitude"], intent["day"])
    places = await fetch_places(place["name"], intent["interests"])
    assert places, "no places returned"
    print(intent, f"\n{weather}\n{len(places)} places\n")
    print(await compose_plan(intent, place, weather, places))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        asyncio.run(check(" ".join(sys.argv[2:]) or "Plan my Saturday in Jaipur, I love forts and street food"))
    else:
        print(f"Inspector: https://agentverse.ai/inspect/?uri=http%3A//127.0.0.1%3A8001&address={orchestrator.address}")

        # agents bind to the loop that existed when they were created, so reuse it
        orchestrator._loop.run_until_complete(asyncio.gather(
            orchestrator.run_async(), weather_agent.run_async(), places_agent.run_async()))
