# DayMate — from "I want a day out" to a booked, routed day, inside ASI:One

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)
![Winner: Best Personal AI](https://img.shields.io/badge/🏆_Fetch--A--Thon_3.0-Best_Personal_AI-FFC24B)

> 🏆 **Winner – Best Personal AI, Fetch-A-Thon 3.0** (Fetch.ai Innovation Lab × ASI:One, October 2026)
> Team HolySpirits · Team ID `fetchathon3.0-1556`

Tell DayMate *"Plan my Saturday in Jaipur, I love forts and street food"*, or fill in its form card, and four agents
build a timed plan from **live weather**, **real places** and **events happening that day**. Every stop comes with
**one-tap navigation, an add-to-calendar link and a ride link**, plus drive times and a budget.

**Problem:** planning a day out means juggling maps, weather apps, event listings, cab apps and opening hours.
**Users:** travellers, students on a weekend trip, friend groups on a budget.
**Outcome:** a ready-to-follow day you can navigate, schedule and book, re-planned on request or when rain is coming.

## Features

| | Feature | How |
|---|---|---|
| 🧭 | **Start navigation** | Google Maps route through all of a day's stops |
| 📅 | **Add to calendar** | Google Calendar link per stop, in the city's timezone |
| 🚕 | **Ride here** | Uber deep link from the previous stop |
| 🃏 | **Interactive Cards** | Form card (city, date, length, group, budget, interests) and an itinerary card with per-stop **Swap** buttons |
| 🔁 | **Swap a stop** | `swap 3` or *replace the museum with a market*. If needed it asks the places agent for new options |
| 💳 | **DayMate Pro (Payment Protocol)** | Multi-day plans cost ₹49 via Stripe embedded checkout (`RequestPayment` → `CommitPayment` → verify → `CompletePayment`). Declining falls back to a free 1-day plan |
| 💰 | **Budget** | Per-stop cost estimate, per-person total, group total and over/under your budget |
| 🚗 | **Real travel times** | Drive time and distance between stops (OSRM) |
| 🌧️ | **Rain watch** | `watch` checks the forecast every 30 min and messages you if rain threatens an outdoor stop |
| 🗓️ | **Multi-day trips** | Up to 3 days, weather planned day by day, no place repeated |
| 👥 | **Group mode** | *"4 friends, ₹1500 each"*: costs are split per person |
| 🇮🇳 | **Hindi / Hinglish** | Replies in the language you write in |
| 🕘 | **Crowd-aware timing** | Popular sights at opening time or late afternoon |
| 🎟️ | **Events today** | A 4th agent finds concerts, comedy and fests (allevents.in) and can slot them in as stops |
| 💡 | **Ideas carousel** | `hi` shows ready-made trips (Royal Jaipur weekend, Goa sunsets, Student day in Delhi, Rain-proof Mumbai…) with **live weather badges**, your saved templates and "Plan again", plus **Create your own** |
| ⭐ | **Save as template** | Any plan becomes a one-tap template, re-planned with fresh weather each time |
| 🪄 | **Smart follow-ups** | Context-aware suggestion buttons: *Fit my budget* when over budget, *Rain-proof it* when rain is due, *Add an event* when something's on |

## Agents

All four agents run as **Agentverse hosted agents** (code in [`hosted/`](hosted)), so DayMate is online 24/7.

| Agent | Role | Hosted address |
|---|---|---|
| `DayMate` (orchestrator) | Chat + Payment Protocol, ASI:One entry point. ASI1 parses intent and writes the plan as structured JSON; the orchestrator builds links, travel times, budget and cards | `agent1qt0zkdfw7uv7atn6p4gyqhgjp6tltssnvee8fyu2smsj9xc4yjyq55eqpra` |
| `DayMate Weather` | Hourly multi-day forecast (Open-Meteo) | `agent1qgezdge0trk469xalfd6gh20c58hlgalul94lt06p6780r8uqwesv2cqukl` |
| `DayMate Places` | Real places and opening hours (OpenStreetMap / Nominatim) | `agent1qd8fsd2g3qfacd2ykj3uuh0j5tjun95annsuy7q9fc8eg027x2lgkdlult4` |
| `DayMate Events` | Events on your dates (allevents.in) | `agent1q2ts9me2vvg4gfv5euqfax6eqrt7jusyulkxjhunwtad9x4599xycs0za6s` |

Hosted agents can't block waiting for another agent, so the orchestrator sends the three requests tagged with a request id,
keeps the job in Agent Storage and composes the plan when the last answer arrives (a 20-second sweep finishes slow jobs
with whatever arrived). The same logic (`core.py`) also runs locally as one process (`daymate.py`).

```
ASI:One ──ChatMessage / card selection──▶ daymate ──ASI1──▶ intent {city, days, group, budget, interests, lang}
                         ├─ WeatherRequest ─▶ daymate-weather ─▶ Open-Meteo
                         ├─ PlacesRequest  ─▶ daymate-places  ─▶ OpenStreetMap
                         ├─ EventsRequest  ─▶ daymate-events  ─▶ allevents.in
                         ├─ ASI1: itinerary JSON (ids only) ─▶ OSRM drive times ─▶ links + budget
                         └─▶ markdown plan + itinerary card   (multi-day: RequestPayment first)
```

The specialists run in parallel, and if one fails the plan is still built from the others. The AI may only reference
places by catalogue id, so every link points at a real place.

## Run

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # ASI1_API_KEY + random seeds; Stripe TEST keys optional
python daymate.py --check "Plan my Saturday in Jaipur, I love forts and street food"   # end-to-end self-check
python daymate.py       # run all four agents locally (mailbox)
hosted/sync.sh && cd hosted && avctl hosting deploy   # or deploy them to Agentverse
```

Open the inspector link from the logs and choose **Connect → Mailbox**, then click **Chat with Agent** in the agent profile.
Try: `hi` (form card) · *"3 days in Udaipur for 4 friends, ₹1500 each, Hinglish mein"* · `swap 2` · `watch`.

Only an [ASI:One API key](https://asi1.ai/dashboard/api-keys) is required. Without Stripe keys, Pro is unlocked for
free and the plan says so. With `sk_test_`/`pk_test_` keys, pay with test card `4242 4242 4242 4242`.

## Links

- GitHub: https://github.com/prajyot2003/daymate
- ASI:One shared chat: _TODO_
- Agentverse profile: _TODO_
- Demo video: _TODO_
