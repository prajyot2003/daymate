"""Shared DayMate logic: ASI1 prompts, plan rendering, cards, templates, swap, rain checks, Stripe.

Used by the local multi-agent runner (daymate.py) and the Agentverse hosted agents (hosted/).
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

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


ASI1_API_KEY = os.getenv("ASI1_API_KEY")
if not ASI1_API_KEY:
    raise RuntimeError("ASI1_API_KEY is not set (local: .env, hosted: Agent Secrets)")
# Agentverse injects ASI1_API_KEY / ASI1_BASE_URL into hosted agents; locally they come from .env
llm = AsyncOpenAI(base_url=os.getenv("ASI1_BASE_URL") or "https://api.asi1.ai/v1", api_key=ASI1_API_KEY)

# Pro (multi-day) plans are paid via Stripe when keys are set; without them Pro is unlocked for free.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
PRO_PRICE = int(os.getenv("PRO_PRICE", "49"))
PRO_CURRENCY = os.getenv("PRO_CURRENCY", "inr")

INDOOR = {"museum", "mall", "cafe", "restaurant", "temple", "street_food", "nightlife"}
FOOD = {"cafe", "restaurant", "street_food"}
FORM_INTERESTS = [("heritage", "Forts & palaces"), ("museum", "Museums"), ("street_food", "Street food"),
                  ("cafe", "Cafes"), ("market", "Markets & shopping"), ("park", "Parks & nature"),
                  ("temple", "Temples"), ("nightlife", "Nightlife")]
SYNONYMS = {"fort": "heritage", "palace": "heritage", "food": "street_food", "shopping": "market",
            "bazaar": "market", "garden": "park", "bar": "nightlife", "pub": "nightlife", "coffee": "cafe",
            "museums": "museum", "markets": "market", "parks": "park", "temples": "temple", "event": "event"}


from messages import *  # noqa: E402,F401,F403  WeatherRequest/Report, PlacesRequest/Report, EventsRequest/Report


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
    tweaks = follow_ups(state)
    md += ["", "💡 **Try:** " + " · ".join(f"*{label}*" for label, _ in tweaks)
           + " — or tap **Swap** on a stop, **⭐ Save** it as a template, or type any change."]

    suggest = [{"type": "button", "label": label, "action": {"selection": {"action": "tweak", "text": text}}}
               for label, text in tweaks]
    buttons = [{"type": "button", "label": "⭐ Save as template", "primary": True, "action": {"selection": {"action": "save"}}},
               {"type": "button", "label": "🌧️ Rain alerts", "action": {"selection": {"action": "watch"}}}]
    if intent["days"] == 1:
        buttons.append({"type": "button", "label": "➕ Make it 3 days", "action": {"selection": {"action": "pro"}}})
    buttons.append({"type": "button", "label": "✨ New plan", "action": {"selection": {"action": "new"}}})
    card_children += [{"type": "divider"}, {"type": "text", "value": "💡 Suggestions", "style": "emphasis"},
                      {"type": "group", "direction": "row", "gap": 8, "children": suggest},
                      {"type": "group", "direction": "row", "gap": 8, "children": buttons}]
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


# ---------- suggestions: ready-made templates, saved trips, follow-ups ----------

TEMPLATES = [
    {"id": "jaipur-royal", "title": "🏰 Royal Jaipur weekend", "city": "Jaipur", "lat": 26.91962, "lon": 75.78781,
     "days": 2, "weekend": True, "budget_pp": 1500, "interests": ["heritage", "museum", "street_food", "market"],
     "blurb": "Forts, palaces, bazaars & street food", "image": ("Hawa Mahal",)},
    {"id": "goa-chill", "title": "🏖️ Goa sunsets & cafes", "city": "Calangute", "lat": 15.5439, "lon": 73.7553,
     "days": 1, "weekend": True, "budget_pp": 2000, "interests": ["viewpoint", "cafe", "nightlife", "restaurant"],
     "blurb": "Sunset points, cafe-hopping and nightlife", "image": ("Calangute", "Goa")},
    {"id": "delhi-student", "title": "🎒 Student day in Delhi", "city": "New Delhi", "lat": 28.63576, "lon": 77.22445,
     "days": 1, "weekend": False, "budget_pp": 600, "interests": ["monument", "street_food", "market", "park"],
     "blurb": "Monuments, chaat and markets on a small budget", "image": ("India Gate",)},
    {"id": "mumbai-indoor", "title": "☔ Rain-proof Mumbai", "city": "Mumbai", "lat": 19.07283, "lon": 72.88261,
     "days": 1, "weekend": False, "budget_pp": 1500, "interests": ["museum", "mall", "cafe", "restaurant"],
     "blurb": "Museums, cafes and malls – no getting wet", "image": ("Gateway of India",)},
    {"id": "varanasi-soul", "title": "🛕 Soulful Varanasi", "city": "Varanasi", "lat": 25.31668, "lon": 82.97391,
     "days": 1, "weekend": False, "budget_pp": 1000, "interests": ["temple", "heritage", "street_food", "viewpoint"],
     "blurb": "Ghats, temples and the evening aarti", "image": ("Dashashwamedh Ghat",)},
    {"id": "blr-nights", "title": "🌙 Bengaluru cafes & nights", "city": "Bengaluru", "lat": 12.97194, "lon": 77.59369,
     "days": 1, "weekend": True, "budget_pp": 2000, "interests": ["cafe", "park", "nightlife", "restaurant"],
     "blurb": "Parks, cafe-hopping and craft beer", "image": ("Cubbon Park",)},
]


def upcoming_weekend() -> str:
    t = date.today()
    return (t if t.weekday() >= 5 else t + timedelta(days=5 - t.weekday())).isoformat()


def template_intent(t: dict) -> dict:
    return normalize_intent({"city": t["city"], "day": upcoming_weekend() if t["weekend"] else date.today().isoformat(),
                             "days": t["days"], "interests": list(t["interests"]), "group_size": 1,
                             "budget_pp": t["budget_pp"], "lang": "English", "notes": t["blurb"]})


async def live_badge(t: dict) -> dict | None:
    try:
        w = (await tools.fetch_weather(t["lat"], t["lon"], date.today().isoformat(), 1))["daily"]
        d = next(iter(w.values()))
        rainy = d["rain"] >= 50
        return {"label": f"{tools.weather_icon(d['code'], d['rain'])} {d['max']:.0f}° today" + (" · rain" if rainy else ""),
                "variant": "warning" if rainy else "info"}
    except Exception:
        return None


def describe(intent: dict) -> str:
    likes = ", ".join(i.replace("_", " ") for i in intent["interests"][:3])
    return f"{intent['city']} · {intent['days']} day{'s' if intent['days'] > 1 else ''} · {likes}"


async def welcome(ctx: Context, sender: str) -> tuple[str, dict, list[dict]]:
    """Carousel of personal + ready-made trip ideas with live weather. Returns (text, card, menu actions)."""
    saved = ctx.storage.get(f"tpl:{sender}") or []
    last = (ctx.storage.get(sender) or {}).get("intent")
    entries = []  # (item, action)
    for i in reversed(range(len(saved))):
        s = saved[i]
        entries.append(({"title": f"⭐ {s['name']}", "subtitle": describe(s["intent"]),
                         "badges": [{"label": "Your template", "variant": "success"}],
                         "image": await tools.wiki_image(s["intent"]["city"])}, {"action": "template", "saved": i}))
    if last and all(s["intent"]["city"] != last["city"] for s in saved):
        entries.append(({"title": f"🔁 {last['city']} again", "subtitle": describe(last),
                         "badges": [{"label": "Recent", "variant": "info"}],
                         "image": await tools.wiki_image(last["city"])}, {"action": "again"}))
    badges = await asyncio.gather(*(live_badge(t) for t in TEMPLATES))
    images = await asyncio.gather(*(tools.wiki_image(*t["image"], t["city"]) for t in TEMPLATES))
    for t, badge, img in zip(TEMPLATES, badges, images):
        tags = [b for b in (badge, {"label": "Pro", "variant": "warning"} if t["days"] > 1 else None) if b]
        entries.append(({"title": t["title"], "subtitle": f"{t['blurb']} · {t['days']} day{'s' if t['days'] > 1 else ''}",
                         "badges": tags, "secondary_text": f"~₹{t['budget_pp']:,}/person/day", "image": img},
                        {"action": "template", "id": t["id"]}))
    entries.append(({"title": "✨ Create your own", "subtitle": "Pick any city, dates, budget and interests",
                     "badges": [{"label": "Custom", "variant": "info"}], "image": await tools.wiki_image("Taj Mahal")},
                    {"action": "custom"}))
    fallback = await tools.wiki_image("Taj Mahal", "India")
    items = []
    for k, (item, action) in enumerate(entries, 1):
        item = {k_: v for k_, v in {**item, "image": item.get("image") or fallback}.items() if v}
        items.append({"id": f"idea{k}", **item,
                      "primary_cta": {"label": "Create" if action["action"] == "custom" else "Plan this", "selection": action}})
    lines = "\n".join(f"{k}. {item['title']} – {item.get('subtitle', '')}" for k, (item, _) in enumerate(entries, 1))
    text = ("Hi, I'm **DayMate** 👋 Here are some ideas for you – tap one to plan it, or create your own.\n\n"
            f"{lines}\n\n_Reply with a number, or just type a trip like *\"Saturday in Udaipur, lakes and cafes\"*._")
    card = {"title": "Ideas for you", "subtitle": f"Live weather · {date.today():%a %d %b}", "items": items}
    return text, card, [a for _, a in entries]


def follow_ups(state: dict) -> list[tuple[str, str]]:
    """Context-aware tweaks for the current plan: (button label, instruction)."""
    plan, intent = state["plan"], state["intent"]
    per_person = sum(s["cost_inr"] for d in plan["days"] for s in d["stops"])
    hourly = (state.get("weather") or {}).get("hourly") or {}
    rainy = any((hourly.get(f"{d['date']}T{s['start'][:2]}:00") or [0, 0, 0])[1] >= 40 and not s.get("indoor")
                for d in plan["days"] for s in d["stops"])
    used = {s["id"] for d in plan["days"] for s in d["stops"]}
    event = next((e for e in state["catalogue"] if e["id"].startswith("e") and e["id"] not in used), None)
    out = []
    if intent.get("budget_pp") and per_person > intent["budget_pp"] * intent["days"]:
        out.append(("💸 Fit my budget", "make it cheaper so it fits my budget"))
    elif per_person > 1200 * intent["days"]:
        out.append(("💸 Cheaper version", "make it cheaper, more free and low-cost stops"))
    if rainy:
        out.append(("☔ Rain-proof it", "rain is likely, move outdoor stops indoors"))
    if event:
        out.append(("🎟️ Add an event", f"add the event '{event['name']}' to the plan"))
    out += [("🐢 Slower pace", "slower pace with fewer stops and longer breaks"),
            ("🍽️ More food", "add more food stops, local specialities"),
            ("👨‍👩‍👧 Family-friendly", "make it family-friendly with kids")]
    return out[:3]


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


def local_now(state: dict) -> str:
    try:
        tz = ZoneInfo(state["geo"].get("timezone") or "Asia/Kolkata")
    except Exception:  # no tz database on the host: IST covers DayMate's home market
        tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")


# ---------- Stripe over REST (the stripe SDK isn't available to Agentverse hosted agents) ----------

async def create_checkout(sender: str, intent: dict) -> dict:
    """Embedded Checkout session for DayMate Pro; returns the metadata the ASI:One / inspector UI renders."""
    r = await tools.http.post("https://api.stripe.com/v1/checkout/sessions", auth=(STRIPE_SECRET_KEY, ""), data={
        # current Stripe API: "embedded_page" (formerly "embedded"); payment methods come from the dashboard
        "ui_mode": "embedded_page", "redirect_on_completion": "if_required", "mode": "payment",
        "return_url": "https://agentverse.ai/payment-success?session_id={CHECKOUT_SESSION_ID}",
        "line_items[0][quantity]": 1,
        "line_items[0][price_data][currency]": PRO_CURRENCY,
        "line_items[0][price_data][unit_amount]": PRO_PRICE * 100,
        "line_items[0][price_data][product_data][name]": f"DayMate Pro · {intent['days']}-day trip to {intent['city']}",
        "metadata[user_address]": sender, "metadata[service]": "daymate_pro"})
    r.raise_for_status()
    s = r.json()
    return {"ui_mode": "embedded", "publishable_key": STRIPE_PUBLISHABLE_KEY, "client_secret": s["client_secret"],
            "checkout_session_id": s["id"], "currency": PRO_CURRENCY, "amount_cents": str(PRO_PRICE * 100)}


async def checkout_paid(session_id: str) -> bool:
    r = await tools.http.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}", auth=(STRIPE_SECRET_KEY, ""))
    return r.status_code == 200 and r.json().get("payment_status") == "paid"


def payment_request(agent_address: str, checkout: dict, intent: dict) -> RequestPayment:
    return RequestPayment(
        accepted_funds=[Funds(currency=PRO_CURRENCY.upper(), amount=f"{PRO_PRICE}.00", payment_method="stripe")],
        recipient=agent_address, deadline_seconds=1800, reference=checkout["checkout_session_id"],
        description=f"DayMate Pro: {intent['days']}-day plan for {intent['city']}",
        metadata={"stripe": checkout, "service": "daymate_pro"})


from answers import answer_events, answer_places, answer_weather  # noqa: E402,F401


def plan_requests(intent: dict, geo: dict, rid: str = "") -> tuple[WeatherRequest, PlacesRequest, EventsRequest]:
    days = [(date.fromisoformat(intent["day"]) + timedelta(days=i)).isoformat() for i in range(intent["days"])]
    return (WeatherRequest(rid=rid, lat=geo["latitude"], lon=geo["longitude"], start=intent["day"], days=intent["days"]),
            PlacesRequest(rid=rid, city=geo["name"], lat=geo["latitude"], lon=geo["longitude"],
                          categories=intent["interests"], per_cat=4 + 2 * intent["days"]),
            EventsRequest(rid=rid, city=geo["name"], days=days))


def slim_geo(geo: dict) -> dict:
    return {k: geo.get(k) for k in ("name", "country", "latitude", "longitude", "timezone")}


async def finish_state(intent: dict, geo: dict, weather: dict | None, places: list, events: list) -> dict | str:
    """Compose a plan from the specialists' answers. Returns state, or a message explaining what went wrong."""
    catalogue = (places or []) + (events or [])
    if not catalogue:
        return f"I couldn't find places in **{geo['name']}** right now. Please try again in a minute."
    plan = normalize_plan(await compose_plan(intent, geo, weather, catalogue), catalogue)
    if not plan["days"]:
        return "I couldn't put a plan together this time. Please try again, or tell me a bit more about what you like."
    return {"intent": intent, "catalogue": catalogue, "plan": plan, "weather": weather, "geo": slim_geo(geo)}


async def send_plan(ctx: Context, sender: str, state: dict | str, note: str = ""):
    if isinstance(state, str):
        await ctx.send(sender, message(state))
        return
    text, card = await render(state, note)
    ctx.storage.set(sender, state)
    await ctx.send(sender, message(text, "custom", card))


# ---------- conversation (deliver = how to fan out to specialists; more_places = deeper places search) ----------

GREETINGS = {"hi", "hello", "hey", "start", "help", "menu", "new", "new plan", "plan my day"}


async def plan_or_charge(ctx: Context, sender: str, intent: dict, deliver):
    if intent["days"] == 1:
        await deliver(ctx, sender, intent, "")
        return
    if not STRIPE_SECRET_KEY:
        await deliver(ctx, sender, intent, "🗓️ DayMate Pro (multi-day) unlocked for free – payments aren't configured on this agent.")
        return
    checkout = await create_checkout(sender, intent)
    ctx.storage.set(f"pending:{checkout['checkout_session_id']}", {"sender": sender, "intent": intent})
    await ctx.send(sender, message(f"🗓️ {intent['days']}-day trips are **DayMate Pro** ({PRO_CURRENCY.upper()} {PRO_PRICE}). "
                                   "Complete the checkout and I'll build your full trip. Decline to get a free 1-day plan instead."))
    await ctx.send(sender, payment_request(str(ctx.agent.address), checkout, intent))


async def do_swap(ctx: Context, sender: str, state: dict, sel: dict, text: str, more_places):
    n = find_target(state, sel, text)
    if n is None:
        await ctx.send(sender, message("Which stop should I swap? Reply e.g. **swap 3**."))
        return
    new_cat = category_in(text.lower().split(" with ", 1)[1]) if " with " in text.lower() else None
    result = swap_stop(state, n, new_cat, exact=True)
    if result is None and new_cat != "event":
        # nothing unused of that category yet: ask the places agent for a deeper search
        cat_ = new_cat or {c["id"]: c for c in state["catalogue"]}[numbered(state)[n - 1][2]["id"]]["category"]
        found = await more_places(ctx, state["geo"], [cat_] + sorted(FOOD - {cat_}) if cat_ in FOOD else [cat_], 15)
        known = {c["name"].lower() for c in state["catalogue"]}
        for c in found:
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
    await send_plan(ctx, sender, state, f"🔁 Swapped **{old['name']}** → **{new['name']}**{picked}")


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


async def rain_watch_tick(ctx: Context):
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


async def handle_chat(ctx: Context, sender: str, msg: ChatMessage, deliver, more_places):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    sel = extract_json(text) if text.startswith("{") else {}  # card selections arrive as JSON on direct chats
    low = text.lower()
    menu = ctx.storage.get(f"menu:{sender}") or []
    if not sel:  # planner-mediated selections arrive as prose; map them back to actions
        tpl = next((t for t in TEMPLATES if t["id"] in low or t["title"][2:].strip().lower() in low), None)
        if low.strip(". ").isdigit() and 1 <= int(low.strip(". ")) <= len(menu):
            sel = menu[int(low.strip(". ")) - 1]
        elif re.search(r"\b(swap|replace)\b", low):
            sel = {"action": "swap"}
        elif re.search(r"\bsave\b.*\b(template|plan|trip|this|it)\b|^save\W*$", low):
            sel = {"action": "save"}
        elif re.search(r"\b(watch|alert|notify)\b", low):
            sel = {"action": "watch"}
        elif re.search(r"create your own|\bcustom\b", low):
            sel = {"action": "custom"}
        elif tpl:
            sel = {"action": "template", "id": tpl["id"]}
        elif re.search(r"\b(again|same trip|repeat)\b", low) and len(low) < 40:
            sel = {"action": "again"}
        elif low.strip("!. ") in GREETINGS:
            sel = {"action": "new"}
    action = sel.get("action")
    state = ctx.storage.get(sender) or {}
    try:
        if action == "new" or (action in ("swap", "watch", "save", "again", "tweak") and not state):
            text_, card, actions = await welcome(ctx, sender)
            ctx.storage.set(f"menu:{sender}", actions)
            await ctx.send(sender, message(text_, "carousel", card))
            return
        if action == "custom":
            await ctx.send(sender, message("Let's build your own 🛠️ Fill in what matters, I'll handle the rest:",
                                           "custom", form_card()))
            return
        if action == "swap":
            await do_swap(ctx, sender, state, sel, text, more_places)
            return
        if action == "watch":
            await do_watch(ctx, sender, state)
            return
        if action == "save":
            saved = [s for s in ctx.storage.get(f"tpl:{sender}") or [] if s["name"] != state["plan"]["title"]]
            saved = (saved + [{"name": state["plan"]["title"], "intent": state["intent"]}])[-6:]
            ctx.storage.set(f"tpl:{sender}", saved)
            await ctx.send(sender, message(f"⭐ Saved **{state['plan']['title']}** as a template. Say **hi** any time "
                                           "and it'll be first in your ideas, re-planned with fresh weather."))
            return
        if action == "plan":
            intent = intent_from_form(sel)
        elif action == "template":
            saved = ctx.storage.get(f"tpl:{sender}") or []
            if "saved" in sel and str(sel["saved"]).isdigit() and int(sel["saved"]) < len(saved):
                intent = normalize_intent({**saved[int(sel["saved"])]["intent"], "day": date.today().isoformat()})
            else:
                intent = template_intent(next((t for t in TEMPLATES if t["id"] == sel.get("id")), TEMPLATES[0]))
        elif action == "again":
            intent = normalize_intent({**state["intent"], "day": date.today().isoformat()})
        elif action == "pro" and state:
            intent = {**state["intent"], "days": 3}
        elif action == "tweak":
            intent = await parse_intent(str(sel.get("text") or text), state["intent"])
        else:
            intent = await parse_intent(text, state.get("intent"))
        if not intent.get("city"):
            text_, card, actions = await welcome(ctx, sender)
            ctx.storage.set(f"menu:{sender}", actions)
            await ctx.send(sender, message("Which city should I plan for? " + text_.split("\n", 1)[1].lstrip(),
                                           "carousel", card))
            return
        ctx.logger.info(f"intent={intent}")
        await plan_or_charge(ctx, sender, intent, deliver)
    except Exception:
        ctx.logger.exception("planning failed")
        await ctx.send(sender, message("Something went wrong while planning. Please try again in a moment."))


async def handle_commit(ctx: Context, sender: str, msg: CommitPayment, deliver):
    pending = ctx.storage.get(f"pending:{msg.transaction_id}")
    paid = bool(pending and msg.funds.payment_method == "stripe" and STRIPE_SECRET_KEY
                and await checkout_paid(msg.transaction_id))
    if not paid:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                             reason="Payment not completed yet. Please finish checkout and pay again."))
        return
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    ctx.storage.remove(f"pending:{msg.transaction_id}")
    await deliver(ctx, pending["sender"], pending["intent"], "✅ DayMate Pro unlocked – thanks for your support!")


async def handle_reject(ctx: Context, sender: str, msg: RejectPayment, deliver):
    intent = (ctx.storage.get(sender) or {}).get("intent")
    await ctx.send(sender, message("No problem – here's a free 1-day plan instead."))
    if intent:
        await deliver(ctx, sender, {**intent, "days": 1}, "")
