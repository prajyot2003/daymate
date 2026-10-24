"""DayMate: a multi-agent day planner for ASI:One.

orchestrator (chat + payment protocol, mailbox) -> weather agent (Open-Meteo)
                                                -> places agent  (OpenStreetMap)
                                                -> events agent  (allevents.in)
ASI1 parses intent and writes a structured, weather-aware itinerary; the orchestrator turns it into
navigation / calendar / ride links, travel times, a budget split and an interactive card.

Run:   python daymate.py
Check: python daymate.py --check "Plan my Saturday in Jaipur, I love forts and street food"
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AsyncOpenAI
from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

import tools
from tools import CATEGORIES

load_dotenv()

ASI1_API_KEY = os.getenv("ASI1_API_KEY")
if not ASI1_API_KEY:
    sys.exit("ASI1_API_KEY is not set. Get one at https://asi1.ai/dashboard/api-keys and put it in .env")
llm = AsyncOpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI1_API_KEY)

# Pro (multi-day) plans are paid via Stripe when keys are set; without them Pro is unlocked for free.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
PRO_PRICE = int(os.getenv("PRO_PRICE", "49"))
PRO_CURRENCY = os.getenv("PRO_CURRENCY", "inr")
if STRIPE_SECRET_KEY:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

INDOOR = {"museum", "mall", "cafe", "restaurant", "temple", "street_food", "nightlife"}
FOOD = {"cafe", "restaurant", "street_food"}
FORM_INTERESTS = [("heritage", "Forts & palaces"), ("museum", "Museums"), ("street_food", "Street food"),
                  ("cafe", "Cafes"), ("market", "Markets & shopping"), ("park", "Parks & nature"),
                  ("temple", "Temples"), ("nightlife", "Nightlife")]
SYNONYMS = {"fort": "heritage", "palace": "heritage", "food": "street_food", "shopping": "market",
            "bazaar": "market", "garden": "park", "bar": "nightlife", "pub": "nightlife", "coffee": "cafe",
            "museums": "museum", "markets": "market", "parks": "park", "temples": "temple", "event": "event"}


# ---------- messages between agents ----------

class WeatherRequest(Model):
    lat: float
    lon: float
    start: str  # YYYY-MM-DD
    days: int


class WeatherReport(Model):
    ok: bool
    weather: dict


class PlacesRequest(Model):
    city: str
    lat: float
    lon: float
    categories: list[str]
    per_cat: int = 6


class PlacesReport(Model):
    ok: bool
    places: list[dict]


class EventsRequest(Model):
    city: str
    days: list[str]


class EventsReport(Model):
    ok: bool
    events: list[dict]


# ---------- ASI1: intent + itinerary ----------

async def ask_llm(system: str, user: str, max_tokens: int = 2048) -> str:
    r = await llm.chat.completions.create(
        model="asi1",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
    )
    return r.choices[0].message.content or ""


def extract_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}


def normalize_intent(intent: dict) -> dict:
    today = date.today()
    try:
        day = date.fromisoformat(str(intent.get("day")))
    except ValueError:
        day = today
    intent["day"] = max(day, today).isoformat()
    intent["days"] = min(max(int(intent.get("days") or 1), 1), 3)
    intent["group_size"] = max(int(intent.get("group_size") or 1), 1)
    budget = intent.get("budget_pp")
    intent["budget_pp"] = int(budget) if str(budget or "").strip().isdigit() and int(budget) > 0 else None
    intent["interests"] = [c for c in intent.get("interests", []) if c in CATEGORIES] or ["attraction", "restaurant"]
    if not FOOD & set(intent["interests"]):
        intent["interests"].append("restaurant")
    intent.setdefault("lang", "English")
    intent.setdefault("notes", "")
    return intent


async def parse_intent(text: str, previous: dict | None) -> dict:
    today = date.today()
    system = (
        f"Today is {today.isoformat()} ({today:%A}). Extract a trip request as JSON only, no prose: "
        '{"city": str|null, "day": "YYYY-MM-DD", "days": int, "interests": [str], "group_size": int, '
        '"budget_pp": int|null, "lang": str, "notes": str}. '
        f"interests: choose 3-5 from {list(CATEGORIES)}, always include one food option. "
        "day = first day, default today; resolve words like 'tomorrow' or 'Saturday'. days = trip length 1-3. "
        "group_size = number of people (default 1). budget_pp = budget per person in INR if mentioned. "
        "lang = the language/style the user writes in, e.g. English, Hindi, Hinglish. "
        "notes = pace, dietary needs or anything else. "
        "If a previous request is given, treat the message as a modification of it and keep unchanged fields."
    )
    user = text if not previous else f"Previous request: {json.dumps(previous)}\nNew message: {text}"
    return normalize_intent(extract_json(await ask_llm(system, user, 400)))


async def compose_plan(intent: dict, geo: dict, weather: dict | None, catalogue: list[dict]) -> dict:
    days = [(date.fromisoformat(intent["day"]) + timedelta(days=i)).isoformat() for i in range(intent["days"])]
    system = (
        "You are DayMate, a practical local trip planner. Return JSON only, no prose:\n"
        '{"title": str, "summary": str, "days": [{"date": "YYYY-MM-DD", "stops": [{"id": str, "start": "HH:MM", '
        '"end": "HH:MM", "why": str, "cost_inr": int, "indoor": bool}]}], "tips": str}\n'
        f"Rules: one entry per date in {days}. Use ONLY ids from the catalogue, never repeat a place across days. "
        "5-7 stops per day including lunch and dinner. Put outdoor stops in the best weather hours; move indoors "
        "when rain chance > 50% or it is above 34°C, and say so in 'why'. Crowd-aware: put the most popular sights "
        "at opening time or late afternoon and mention it. Respect opening hours. Group nearby stops using lat/lon "
        "and leave 15-40 minutes between stops for travel. An event (id starting with 'e') may be a stop at its "
        "start time if it suits the interests. cost_inr = realistic per-person cost (entry + food) in "
        f"{geo['name']}; keep the day within budget_pp if given. 'why' is at most 12 words. summary = one sentence "
        f"on the weather and strategy. tips = one sentence on what to pack/know. title = max 6 words with one emoji. Write title, summary, why and tips in {intent['lang']}."
    )
    compact = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items() if k != "url"} for c in catalogue]
    user = (f"Request: {json.dumps(intent)}\nLocation: {geo['name']}, {geo.get('country', '')}\n"
            f"Weather:\n{(weather or {}).get('summary', 'unavailable')}\n"
            f"Catalogue:\n{json.dumps(compact, ensure_ascii=False)}")
    return extract_json(await ask_llm(system, user, 1500 + 1000 * intent["days"]))


def normalize_plan(plan: dict, catalogue: list[dict]) -> dict:
    known, used = {c["id"] for c in catalogue}, set()
    days = []
    for d in plan.get("days") or []:
        stops = []
        for s in d.get("stops") or []:
            if s.get("id") not in known or s["id"] in used or not re.fullmatch(r"\d\d:\d\d", str(s.get("start", ""))):
                continue
            if not re.fullmatch(r"\d\d:\d\d", str(s.get("end", ""))):
                s["end"] = s["start"]
            used.add(s["id"])
            s["cost_inr"] = max(int(s.get("cost_inr") or 0), 0)
            stops.append(s)
        if stops:
            days.append({"date": d.get("date"), "stops": sorted(stops, key=lambda s: s["start"])})
    return {"title": plan.get("title") or "Your DayMate plan", "summary": plan.get("summary", ""),
            "tips": plan.get("tips", ""), "days": days}


# ---------- rendering: links, travel, budget, card ----------

def numbered(state: dict) -> list[tuple[int, dict, dict]]:
    """(n, day, stop) across all days, numbered from 1."""
    out = []
    for d in state["plan"]["days"]:
        for s in d["stops"]:
            out.append((len(out) + 1, d, s))
    return out


def stop_weather(state: dict, day: str, start: str) -> tuple[str, bool]:
    """('☀️ 25°', rainy?) for the hour a stop starts."""
    v = ((state.get("weather") or {}).get("hourly") or {}).get(f"{day}T{start[:2]}:00")
    if not v:
        return "", False
    rainy = v[1] >= 40 or int(v[2]) >= 51
    return f"{tools.weather_icon(int(v[2]), v[1])} {round(v[0])}°" + (f" · {v[1]:.0f}% rain" if rainy else ""), rainy


def short(text: str, limit: int = 80) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def rupees(n: int) -> str:
    return f"₹{n:,}"


async def render(state: dict, note: str = "") -> tuple[str, dict]:
    """Compact chat summary (one line per stop + one action row per day) and a detailed itinerary card."""
    plan, intent, geo = state["plan"], state["intent"], state["geo"]
    cat = {c["id"]: c for c in state["catalogue"]}
    tz = geo.get("timezone", "Asia/Kolkata")
    daily = (state.get("weather") or {}).get("daily") or {}
    legs_by_day = await asyncio.gather(*(tools.route_legs([(cat[s["id"]]["lat"], cat[s["id"]]["lon"]) for s in d["stops"]])
                                         for d in plan["days"]))
    per_person = sum(s["cost_inr"] for d in plan["days"] for s in d["stops"])
    stops_total = sum(len(d["stops"]) for d in plan["days"])
    first = date.fromisoformat(plan["days"][0]["date"])
    when = f"{first:%a %d %b}" + (f" + {len(plan['days']) - 1} more day(s)" if len(plan["days"]) > 1 else "")

    md = []
    if note:
        md += [note, ""]
    md += [f"### {plan['title']}", f"{when} · {stops_total} stops · ~{rupees(per_person)}/person", "",
           short(plan["summary"], 200), ""]
    card_children, n = [], 0
    for di, (d, legs) in enumerate(zip(plan["days"], legs_by_day), 1):
        w = daily.get(d["date"])
        wline = f" · {tools.weather_icon(w['code'], w['rain'])} {w['min']:.0f}–{w['max']:.0f}°" if w else ""
        head = f"Day {di} · {date.fromisoformat(d['date']):%a %d %b}{wline}"
        if len(plan["days"]) > 1:
            md.append(f"**{head}**")
        items = []
        for i, s in enumerate(d["stops"]):
            n += 1
            p = cat[s["id"]]
            wx, rainy = stop_weather(state, d["date"], s["start"])
            md.append(f"`{s['start']}` **{p['name']}** – {short(s.get('why'), 70)} [📍]({tools.maps_link(p)})"
                      + (f" [🎟️]({p['url']})" if p.get("url") else ""))
            leg = legs[i] if i < len(legs) else None
            badges = [b for b in (
                {"type": "badge", "label": wx, "variant": "warning" if rainy else "info"} if wx else None,
                {"type": "badge", "label": f"~{rupees(s['cost_inr'])}" if s["cost_inr"] else "Free", "variant": "success"},
                {"type": "badge", "label": "🚶 walk next" if leg["km"] < 0.6 else f"🚗 {leg['min']} min next", "variant": "info"}
                if leg else None,
            ) if b]
            items.append({"children": [
                {"type": "group", "direction": "row", "gap": 8, "children": [
                    {"type": "text", "value": f"{s['start']}–{s['end']}", "style": "emphasis"},
                    {"type": "heading", "value": f"{n}. {p['name']}", "level": 3}]},
                {"type": "text", "value": short(s.get("why"), 110), "style": "muted"},
                {"type": "group", "direction": "row", "gap": 6, "children": [
                    *badges, {"type": "button", "label": "🔁 Swap", "action": {"selection": {"action": "swap", "stop": n}}}]},
            ]})
        stops = [cat[s["id"]] for s in d["stops"]]
        day_cal = tools.calendar_link(
            f"DayMate: {plan['title']}" + (f" (Day {di})" if len(plan["days"]) > 1 else ""), d["date"],
            d["stops"][0]["start"], d["stops"][-1]["end"], tz, geo["name"],
            "\n".join(f"{s['start']} {cat[s['id']]['name']} – {tools.maps_link(cat[s['id']])}" for s in d["stops"]))
        md += ["", f"🧭 [Navigate]({tools.route_link(stops)}) · 📅 [Add to calendar]({day_cal}) · "
                   f"🚕 [Cab to first stop]({tools.ride_link(None, stops[0])})", ""]
        card_children += [{"type": "heading", "value": head, "level": 3}, {"type": "list", "items": items}]

    group = intent["group_size"]
    money = f"💰 ~{rupees(per_person)} per person" + (f" · {rupees(per_person * group)} for {group}" if group > 1 else "")
    if intent.get("budget_pp"):
        cap = intent["budget_pp"] * intent["days"]
        money += " · ✅ within budget" if per_person <= cap else f" · ⚠️ {rupees(per_person - cap)} over budget"
    md.append(money)
    used = {s["id"] for d in plan["days"] for s in d["stops"]}
    extra = [e for e in state["catalogue"] if e["id"].startswith("e") and e["id"] not in used][:2]
    if extra:
        md.append("🎟️ Also on: " + " · ".join(f"[{short(e['name'], 40)}]({e['url']})" for e in extra))
    if plan.get("tips"):
        md.append(f"🎒 {short(plan['tips'], 160)}")
    md += ["", "_Tap **Swap** on any stop in the card, or just type what to change._"]

    buttons = [{"type": "button", "label": "🌧️ Rain alerts", "primary": True, "action": {"selection": {"action": "watch"}}}]
    if intent["days"] == 1:
        buttons.append({"type": "button", "label": "➕ Make it 3 days", "action": {"selection": {"action": "pro"}}})
    buttons.append({"type": "button", "label": "✨ New plan", "action": {"selection": {"action": "new"}}})
    card_children += [{"type": "divider"}, {"type": "group", "direction": "row", "gap": 8, "children": buttons}]
    card = {"root": {"type": "section", "title": plan["title"],
                     "subtitle": f"{geo['name']} · {when} · ~{rupees(per_person)}/person", "children": card_children}}
    return "\n".join(md), card


BUDGETS = [("700", "💸 Budget (~₹700/day)"), ("1500", "⚖️ Comfort (~₹1,500/day)"), ("4000", "✨ Splurge (~₹4,000/day)")]
GROUPS = [("1", "Just me"), ("2", "2 people"), ("4", "3–4 people"), ("6", "5+ people")]
TILES = {"heritage": "🏰", "museum": "🖼️", "street_food": "🍢", "cafe": "☕", "market": "🛍️",
         "park": "🌳", "temple": "🛕", "nightlife": "🌙"}


def form_card() -> dict:
    """Tap-to-pick planner: one text box, four dropdowns and interest tiles."""
    today = date.today()
    days = [today + timedelta(days=i) for i in range(7)]
    day_label = lambda i, d: "Today" if i == 0 else "Tomorrow" if i == 1 else f"{d:%a %d %b}"
    select = lambda name, label, opts: {"type": "input", "name": name, "kind": "select", "label": label,
                                        "options": [{"value": v, "label": l} for v, l in opts]}
    return {"root": {"type": "section", "title": "Where to? 🌍", "subtitle": "Pick a few things – I'll plan the rest", "children": [
        {"type": "input", "name": "city", "kind": "text", "label": "City", "required": True, "placeholder": "e.g. Jaipur"},
        {"type": "group", "direction": "row", "gap": 8, "children": [
            select("day", "When", [(d.isoformat(), day_label(i, d)) for i, d in enumerate(days)]),
            select("days", "How long", [("1", "1 day"), ("2", "2 days · Pro"), ("3", "3 days · Pro")])]},
        {"type": "group", "direction": "row", "gap": 8, "children": [
            select("group_size", "Who", GROUPS), select("budget_pp", "Budget per person", BUDGETS)]},
        {"type": "text", "value": "What do you enjoy? (pick any)", "style": "emphasis"},
        {"type": "choice_grid", "name": "interests", "multi": True,
         "choices": [{"value": c, "label": f"{TILES[c]} {label}"} for c, label in FORM_INTERESTS]},
        {"type": "button", "label": "✨ Plan my day", "primary": True, "action": {"selection": {"action": "plan"}}},
    ]}}


def intent_from_form(sel: dict) -> dict:
    picked = sel.get("interests") or []
    if isinstance(picked, str):
        picked = re.split(r"[,\s]+", picked)
    truthy = {True, "true", "on", "1", 1, "yes"}
    picked = [c for c in picked if c in CATEGORIES] + [c for c, _ in FORM_INTERESTS if sel.get(f"like_{c}") in truthy]
    return normalize_intent({
        "city": str(sel.get("city") or "").strip() or None, "day": sel.get("day"), "days": sel.get("days") or 1,
        "group_size": sel.get("group_size") or 1, "budget_pp": sel.get("budget_pp"),
        "interests": picked or ["heritage", "street_food", "museum"], "lang": "English", "notes": ""})


def message(text: str, card_kind: str | None = None, payload: dict | None = None) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if card_kind:
        content.append(MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1", "requires_card_interaction": "true", "card_kind": card_kind,
            "card_payload": json.dumps(payload, ensure_ascii=False), "preferred_drawer_width_px": "560"}))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


# ---------- swap + rain watch ----------

def category_in(text: str) -> str | None:
    words = re.findall(r"[a-z_]+", text.lower().replace("street food", "street_food"))
    for w in words:
        if w in CATEGORIES or w == "event":
            return w
        if w in SYNONYMS:
            return SYNONYMS[w]
    return None


def find_target(state: dict, sel: dict, text: str) -> int | None:
    stops = numbered(state)
    if str(sel.get("stop", "")).isdigit():
        return int(sel["stop"])
    if m := re.search(r"\b(\d{1,2})\b", text):
        return int(m.group(1))
    cat = {c["id"]: c for c in state["catalogue"]}
    before = text.lower().split(" with ")[0]
    for n, _, s in stops:
        if cat[s["id"]]["name"].lower() in before:
            return n
    want = category_in(before)
    return next((n for n, _, s in stops if cat[s["id"]]["category"] == want), None)


def swap_stop(state: dict, n: int, new_cat: str | None, exact: bool = False) -> tuple[dict, dict] | None:
    """Replace stop n with the nearest unused catalogue entry of new_cat (or the same category).
    Unless exact, falls back to anything of the same kind (food vs sight)."""
    stops = numbered(state)
    if not 1 <= n <= len(stops):
        return None
    cat = {c["id"]: c for c in state["catalogue"]}
    _, _, stop = stops[n - 1]
    old = cat[stop["id"]]
    want = new_cat or old["category"]
    used = {s["id"] for _, _, s in stops}
    pool = [c for c in state["catalogue"] if c["id"] not in used and c["category"] == want]
    if not pool and not exact:
        pool = [c for c in state["catalogue"] if c["id"] not in used and (c["category"] in FOOD) == (want in FOOD)]
    if not pool:
        return None
    new = min(pool, key=lambda c: (c["lat"] - old["lat"]) ** 2 + (c["lon"] - old["lon"]) ** 2)
    stop.update(id=new["id"], why=f"Swapped in for {old['name']}.", indoor=new["category"] in INDOOR)
    return old, new


def at_risk(stops: list[dict], hourly: dict, now_local: str) -> list[tuple[dict, float]]:
    risky = []
    for s in stops:
        if s["indoor"] or f"{s['date']} {s['end']}" < now_local:
            continue
        hours = range(int(s["start"][:2]), int(s["end"][:2]) + 1)
        rain = max((hourly.get(f"{s['date']}T{h:02d}:00", [0, 0, 0])[1] for h in hours), default=0)
        if rain >= 50:
            risky.append((s, rain))
    return risky


# ---------- agents ----------

# Separate ports so the Agentverse inspector on :8001 sees only the orchestrator;
# same process, so agent-to-agent messages are delivered in memory.
orchestrator = Agent(name="daymate", seed=os.environ["ORCHESTRATOR_SEED"], port=8001, mailbox=True,
                     publish_agent_details=True)
weather_agent = Agent(name="daymate-weather", seed=os.environ["WEATHER_SEED"], port=8002)
places_agent = Agent(name="daymate-places", seed=os.environ["PLACES_SEED"], port=8003)
events_agent = Agent(name="daymate-events", seed=os.getenv("EVENTS_SEED") or os.environ["PLACES_SEED"] + "-events",
                     port=8004)


@weather_agent.on_message(WeatherRequest, replies=WeatherReport)
async def on_weather(ctx: Context, sender: str, req: WeatherRequest):
    try:
        await ctx.send(sender, WeatherReport(ok=True, weather=await tools.fetch_weather(req.lat, req.lon, req.start, req.days)))
    except Exception:
        ctx.logger.exception("weather failed")
        await ctx.send(sender, WeatherReport(ok=False, weather={}))


@places_agent.on_message(PlacesRequest, replies=PlacesReport)
async def on_places(ctx: Context, sender: str, req: PlacesRequest):
    try:
        places = await tools.fetch_places(req.city, req.lat, req.lon, req.categories, req.per_cat)
        await ctx.send(sender, PlacesReport(ok=True, places=places))
    except Exception:
        ctx.logger.exception("places failed")
        await ctx.send(sender, PlacesReport(ok=False, places=[]))


@events_agent.on_message(EventsRequest, replies=EventsReport)
async def on_events(ctx: Context, sender: str, req: EventsRequest):
    try:
        await ctx.send(sender, EventsReport(ok=True, events=await tools.fetch_events(req.city, req.days)))
    except Exception:
        ctx.logger.exception("events failed")
        await ctx.send(sender, EventsReport(ok=False, events=[]))


async def build_state(ctx: Context, intent: dict) -> dict | str:
    """Fan out to the specialist agents and compose a plan. Returns state, or an error message."""
    geo = await tools.geocode(intent["city"])
    if not geo:
        return f"I couldn't find **{intent['city']}** on the map. Could you check the spelling?"
    days = [(date.fromisoformat(intent["day"]) + timedelta(days=i)).isoformat() for i in range(intent["days"])]
    ctx.logger.info(f"intent={intent}")
    (w, _), (p, _), (e, _) = await asyncio.gather(
        ctx.send_and_receive(weather_agent.address, WeatherRequest(
            lat=geo["latitude"], lon=geo["longitude"], start=intent["day"], days=intent["days"]), WeatherReport, timeout=45),
        ctx.send_and_receive(places_agent.address, PlacesRequest(
            city=geo["name"], lat=geo["latitude"], lon=geo["longitude"], categories=intent["interests"],
            per_cat=4 + 2 * intent["days"]), PlacesReport, timeout=60),
        ctx.send_and_receive(events_agent.address, EventsRequest(city=geo["name"], days=days), EventsReport, timeout=30),
    )
    weather = w.weather if w and w.ok else None
    catalogue = (p.places if p and p.ok else []) + (e.events if e and e.ok else [])
    if not catalogue:
        return f"I couldn't find places in **{geo['name']}** right now. Please try again in a minute."
    plan = normalize_plan(await compose_plan(intent, geo, weather, catalogue), catalogue)
    if not plan["days"]:
        return "I couldn't put a plan together this time. Please try again, or tell me a bit more about what you like."
    return {"intent": intent, "catalogue": catalogue, "plan": plan, "weather": weather,
            "geo": {k: geo.get(k) for k in ("name", "country", "latitude", "longitude", "timezone")}}


async def deliver_plan(ctx: Context, sender: str, intent: dict, note: str = ""):
    state = await build_state(ctx, intent)
    if isinstance(state, str):
        await ctx.send(sender, message(state))
        return
    text, card = await render(state, note)
    ctx.storage.set(sender, state)
    await ctx.send(sender, message(text, "custom", card))


def create_checkout(sender: str, intent: dict) -> dict:
    session = stripe.checkout.Session.create(
        ui_mode="embedded", redirect_on_completion="if_required", payment_method_types=["card"], mode="payment",
        return_url="https://agentverse.ai/payment-success?session_id={CHECKOUT_SESSION_ID}",
        line_items=[{"quantity": 1, "price_data": {"currency": PRO_CURRENCY, "unit_amount": PRO_PRICE * 100,
                     "product_data": {"name": f"DayMate Pro · {intent['days']}-day trip to {intent['city']}"}}}],
        metadata={"user_address": sender, "service": "daymate_pro"})
    return {"ui_mode": "embedded", "publishable_key": STRIPE_PUBLISHABLE_KEY, "client_secret": session.client_secret,
            "checkout_session_id": session.id, "currency": PRO_CURRENCY, "amount_cents": str(PRO_PRICE * 100)}


async def plan_or_charge(ctx: Context, sender: str, intent: dict):
    if intent["days"] == 1:
        await deliver_plan(ctx, sender, intent)
        return
    if not STRIPE_SECRET_KEY:
        await deliver_plan(ctx, sender, intent, "🗓️ DayMate Pro (multi-day) unlocked for free – payments aren't configured on this agent.")
        return
    checkout = await asyncio.to_thread(create_checkout, sender, intent)
    ctx.storage.set(f"pending:{checkout['checkout_session_id']}", {"sender": sender, "intent": intent})
    await ctx.send(sender, message(f"🗓️ {intent['days']}-day trips are **DayMate Pro** ({PRO_CURRENCY.upper()} {PRO_PRICE}). "
                                   "Complete the checkout and I'll build your full trip. Decline to get a free 1-day plan instead."))
    await ctx.send(sender, RequestPayment(
        accepted_funds=[Funds(currency=PRO_CURRENCY.upper(), amount=f"{PRO_PRICE}.00", payment_method="stripe")],
        recipient=str(ctx.agent.address), deadline_seconds=1800, reference=checkout["checkout_session_id"],
        description=f"DayMate Pro: {intent['days']}-day plan for {intent['city']}",
        metadata={"stripe": checkout, "service": "daymate_pro"}))


async def do_swap(ctx: Context, sender: str, state: dict, sel: dict, text: str):
    n = find_target(state, sel, text)
    if n is None:
        await ctx.send(sender, message("Which stop should I swap? Reply e.g. **swap 3**."))
        return
    new_cat = category_in(text.lower().split(" with ", 1)[1]) if " with " in text.lower() else None
    result = swap_stop(state, n, new_cat, exact=True)
    if result is None and new_cat != "event":
        # nothing unused of that category yet: ask the places agent for a deeper search
        geo, cat_ = state["geo"], new_cat or {c["id"]: c for c in state["catalogue"]}[numbered(state)[n - 1][2]["id"]]["category"]
        p, _ = await ctx.send_and_receive(places_agent.address, PlacesRequest(
            city=geo["name"], lat=geo["latitude"], lon=geo["longitude"], per_cat=15,
            categories=[cat_] + sorted(FOOD - {cat_}) if cat_ in FOOD else [cat_]),
            PlacesReport, timeout=30)
        known = {c["name"].lower() for c in state["catalogue"]}
        for c in (p.places if p and p.ok else []):
            if c["name"].lower() not in known:
                state["catalogue"].append({**c, "id": f"p{len(state['catalogue']) + 1}"})
        result = swap_stop(state, n, new_cat)
    if result is None:
        used = {s["id"] for _, _, s in numbered(state)}
        left = sorted({c["category"].replace("_", " ") for c in state["catalogue"] if c["id"] not in used})
        await ctx.send(sender, message(f"I couldn't find another {(new_cat or 'option').replace('_', ' ')} in "
                                       f"{state['geo']['name']} for stop {n}."
                                       + (f" I still have unused: {', '.join(left)}. Try *swap {n} with a {left[0]}*." if left else "")))
        return
    old, new = result
    picked = "" if not new_cat or new["category"] == new_cat else \
        f" (no other {new_cat.replace('_', ' ')} found nearby, so I picked a {new['category'].replace('_', ' ')})"
    text_md, card = await render(state, f"🔁 Swapped **{old['name']}** → **{new['name']}**{picked}")
    ctx.storage.set(sender, state)
    await ctx.send(sender, message(text_md, "custom", card))


def local_now(state: dict) -> str:
    return datetime.now(ZoneInfo(state["geo"].get("timezone") or "Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")


async def do_watch(ctx: Context, sender: str, state: dict):
    cat = {c["id"]: c for c in state["catalogue"]}
    stops = [{"n": n, "date": d["date"], "start": s["start"], "end": s["end"], "name": cat[s["id"]]["name"],
              "indoor": bool(s.get("indoor")) or cat[s["id"]]["category"] in INDOOR} for n, d, s in numbered(state)]
    watches = ctx.storage.get("watches") or {}
    watches[sender] = {"geo": state["geo"], "stops": stops, "alerted": []}
    ctx.storage.set("watches", watches)
    risky = at_risk(stops, (state.get("weather") or {}).get("hourly") or {}, local_now(state))
    now = ("Right now the forecast looks fine for every outdoor stop. ☀️" if not risky else
           "Already at risk: " + "; ".join(f"**{s['name']}** ({rain:.0f}% rain) – reply **swap {s['n']}**" for s, rain in risky))
    await ctx.send(sender, message(f"👀 Watching your plan. I'll message you here if rain threatens an outdoor stop.\n\n{now}"))


@orchestrator.on_interval(period=1800.0)
async def rain_watch(ctx: Context):
    watches = ctx.storage.get("watches") or {}
    for sender, w in list(watches.items()):
        geo, today = w["geo"], local_now(w)[:10]
        upcoming = [s for s in w["stops"] if s["date"] >= today]
        if not upcoming:
            del watches[sender]
            continue
        days = sorted({s["date"] for s in upcoming})
        try:
            weather = await tools.fetch_weather(geo["latitude"], geo["longitude"], days[0],
                                                (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1)
        except Exception:
            ctx.logger.exception("rain watch: weather failed")
            continue
        for s, rain in at_risk(upcoming, weather["hourly"], local_now(w)):
            key = f"{s['date']} {s['start']}"
            if key in w["alerted"]:
                continue
            w["alerted"].append(key)
            await ctx.send(sender, message(
                f"🌧️ Heads-up: {rain:.0f}% chance of rain around {s['start']} on {s['date']} during **{s['name']}**. "
                f"Reply **swap {s['n']} with a museum** (or a mall/cafe) and I'll re-plan that stop."))
    ctx.storage.set("watches", watches)


chat = Protocol(spec=chat_protocol_spec)
GREETINGS = {"hi", "hello", "hey", "start", "help", "menu", "new", "new plan", "plan my day"}


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    sel = extract_json(text) if text.startswith("{") else {}  # card selections arrive as JSON on direct chats
    low = text.lower()
    action = sel.get("action") or ("swap" if re.search(r"\b(swap|replace)\b", low) else
                                   "watch" if re.search(r"\b(watch|alert|notify)\b", low) else
                                   "new" if low.strip("!. ") in GREETINGS else None)
    state = ctx.storage.get(sender) or {}
    try:
        if action == "new":
            await ctx.send(sender, message("Hi, I'm **DayMate** 👋 Pick a few things below, or just type something like "
                                           "*\"Saturday in Jaipur, forts and street food\"*.",
                                           "custom", form_card()))
            return
        if action in ("swap", "watch") and not state:
            await ctx.send(sender, message("Let's make a plan first!", "custom", form_card()))
            return
        if action == "swap":
            await do_swap(ctx, sender, state, sel, text)
            return
        if action == "watch":
            await do_watch(ctx, sender, state)
            return
        if action == "plan":
            intent = intent_from_form(sel)
        elif action == "pro" and state:
            intent = {**state["intent"], "days": 3}
        else:
            intent = await parse_intent(text, state.get("intent"))
        if not intent.get("city"):
            await ctx.send(sender, message("Which city should I plan for? e.g. *Plan Saturday in Jaipur, I love forts "
                                           "and street food* – or fill this in:", "custom", form_card()))
            return
        await plan_or_charge(ctx, sender, intent)
    except Exception:
        ctx.logger.exception("planning failed")
        await ctx.send(sender, message("Something went wrong while planning. Please try again in a moment."))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


payments = Protocol(spec=payment_protocol_spec, role="seller")


@payments.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    pending = ctx.storage.get(f"pending:{msg.transaction_id}")
    paid = False
    if pending and msg.funds.payment_method == "stripe" and STRIPE_SECRET_KEY:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, msg.transaction_id)
        paid = getattr(session, "payment_status", None) == "paid"
    if not paid:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                             reason="Payment not completed yet. Please finish checkout and pay again."))
        return
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    ctx.storage.remove(f"pending:{msg.transaction_id}")
    await deliver_plan(ctx, pending["sender"], pending["intent"], "✅ DayMate Pro unlocked – thanks for your support!")


@payments.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    state = ctx.storage.get(sender) or {}
    intent = state.get("intent")
    await ctx.send(sender, message("No problem – here's a free 1-day plan instead."))
    if intent:
        await deliver_plan(ctx, sender, {**intent, "days": 1})


orchestrator.include(chat, publish_manifest=True)
orchestrator.include(payments, publish_manifest=True)


class _CheckCtx:
    """Just enough of Context for build_state outside the agent network."""
    logger = type("L", (), {"info": staticmethod(print)})()

    async def send_and_receive(self, address, req, _type, timeout):
        if isinstance(req, WeatherRequest):
            return WeatherReport(ok=True, weather=await tools.fetch_weather(req.lat, req.lon, req.start, req.days)), None
        if isinstance(req, PlacesRequest):
            return PlacesReport(ok=True, places=await tools.fetch_places(req.city, req.lat, req.lon, req.categories, req.per_cat)), None
        try:
            return EventsReport(ok=True, events=await tools.fetch_events(req.city, req.days)), None
        except Exception as err:
            print("events failed:", err)
            return None, None


async def check(text: str):
    """End-to-end run of intent -> agents' tools -> plan -> render -> swap, without the agent network."""
    intent = await parse_intent(text, None)
    assert intent.get("city"), intent
    state = await build_state(_CheckCtx(), intent)
    assert isinstance(state, dict), state
    md, card = await render(state)
    assert "google.com/maps/dir" in md and "calendar.google.com" in md and "m.uber.com" in md, md
    assert len(json.dumps(card)) < 64_000
    print(md, f"\n\n[card: {len(json.dumps(card))} bytes, {len(numbered(state))} stops, "
              f"{sum(c['id'].startswith('e') for c in state['catalogue'])} events in catalogue]")
    swapped = swap_stop(state, 1, None)
    assert swapped, "swap found no alternative"
    print(f"[swap 1: {swapped[0]['name']} -> {swapped[1]['name']}]")
    form = intent_from_form({"city": "Jaipur", "days": "2", "group_size": "4", "budget_pp": "800", "like_heritage": True})
    assert form["days"] == 2 and form["group_size"] == 4 and form["budget_pp"] == 800 and "heritage" in form["interests"]
    stops = [{"n": 1, "date": "2099-01-01", "start": "14:00", "end": "15:00", "name": "Fort", "indoor": False},
             {"n": 2, "date": "2099-01-01", "start": "14:00", "end": "15:00", "name": "Museum", "indoor": True}]
    risky = at_risk(stops, {"2099-01-01T14:00": [25, 80, 61]}, "2026-01-01 00:00")
    assert [s["name"] for s, _ in risky] == ["Fort"], risky
    print("[checks passed]")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        asyncio.run(check(" ".join(sys.argv[2:]) or "Plan my Saturday in Jaipur, I love forts and street food"))
    else:
        print(f"Inspector: https://agentverse.ai/inspect/?uri=http%3A//127.0.0.1%3A8001&address={orchestrator.address}")
        # agents bind to the loop that existed when they were created, so reuse it
        orchestrator._loop.run_until_complete(asyncio.gather(*(a.run_async() for a in (
            orchestrator, weather_agent, places_agent, events_agent))))
