"""DayMate: a multi-agent day planner for ASI:One (local runner).

orchestrator (chat + payment protocol, mailbox) -> weather agent (Open-Meteo)
                                                -> places agent  (OpenStreetMap)
                                                -> events agent  (allevents.in)
Shared logic lives in core.py; hosted/ runs the same agents on Agentverse.

Run:   python daymate.py
Check: python daymate.py --check "Plan my Saturday in Jaipur, I love forts and street food"
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, chat_protocol_spec  # noqa: E402
from uagents_core.contrib.protocols.payment import CommitPayment, RejectPayment, payment_protocol_spec  # noqa: E402

import tools  # noqa: E402
from core import *  # noqa: E402,F401,F403  shared models, prompts, rendering, cards, conversation, Stripe

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
    await ctx.send(sender, await answer_weather(req))


@places_agent.on_message(PlacesRequest, replies=PlacesReport)
async def on_places(ctx: Context, sender: str, req: PlacesRequest):
    await ctx.send(sender, await answer_places(req))


@events_agent.on_message(EventsRequest, replies=EventsReport)
async def on_events(ctx: Context, sender: str, req: EventsRequest):
    await ctx.send(sender, await answer_events(req))


async def build_state(ctx: Context, intent: dict) -> dict | str:
    """Fan out to the specialists in parallel and wait for their answers (fine locally: one process)."""
    geo = await tools.geocode(intent["city"])
    if not geo:
        return f"I couldn't find **{intent['city']}** on the map. Could you check the spelling?"
    wr, pr, er = plan_requests(intent, geo)
    (w, _), (p, _), (e, _) = await asyncio.gather(
        ctx.send_and_receive(weather_agent.address, wr, WeatherReport, timeout=45),
        ctx.send_and_receive(places_agent.address, pr, PlacesReport, timeout=60),
        ctx.send_and_receive(events_agent.address, er, EventsReport, timeout=30))
    return await finish_state(intent, geo, w.weather if w and w.ok else None,
                              p.places if p and p.ok else [], e.events if e and e.ok else [])


async def deliver(ctx: Context, sender: str, intent: dict, note: str):
    await send_plan(ctx, sender, await build_state(ctx, intent), note)


async def more_places(ctx: Context, geo: dict, categories: list[str], per_cat: int) -> list[dict]:
    p, _ = await ctx.send_and_receive(places_agent.address, PlacesRequest(
        city=geo["name"], lat=geo["latitude"], lon=geo["longitude"], categories=categories, per_cat=per_cat),
        PlacesReport, timeout=30)
    return p.places if p and p.ok else []


@orchestrator.on_interval(period=1800.0)
async def rain_watch(ctx: Context):
    await rain_watch_tick(ctx)


chat = Protocol(spec=chat_protocol_spec)


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await handle_chat(ctx, sender, msg, deliver, more_places)


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


payments = Protocol(spec=payment_protocol_spec, role="seller")


@payments.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    await handle_commit(ctx, sender, msg, deliver)


@payments.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    await handle_reject(ctx, sender, msg, deliver)


orchestrator.include(chat, publish_manifest=True)
orchestrator.include(payments, publish_manifest=True)


class _CheckCtx:
    """Just enough of Context for build_state outside the agent network."""
    logger = type("L", (), {"info": staticmethod(print)})()

    async def send_and_receive(self, address, req, _type, timeout):
        answer = {WeatherRequest: answer_weather, PlacesRequest: answer_places, EventsRequest: answer_events}[type(req)]
        return await answer(req), None


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
