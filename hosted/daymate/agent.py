"""DayMate orchestrator – Agentverse hosted agent.

Hosted agents run each handler as a short task and reset globals between invocations, so instead of waiting
for replies this agent fans out WeatherRequest / PlacesRequest / EventsRequest tagged with a request id, keeps
the job in Agent Storage, and finishes the plan when the last answer arrives (or after a timeout sweep).
Specialist addresses come from WEATHER_ADDRESS / PLACES_ADDRESS / EVENTS_ADDRESS (set by avctl at deploy time).
"""

import os
import time

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, chat_protocol_spec
from uagents_core.contrib.protocols.payment import CommitPayment, RejectPayment, payment_protocol_spec

import tools
from core import *  # noqa: F401,F403  shared models, prompts, rendering, cards, conversation, Stripe

agent = Agent()
PARTS = ("weather", "places", "events")
JOB_TIMEOUT = 60  # seconds before a job is finished with whatever answers arrived


def specialists() -> dict[str, str]:
    return {"weather": os.environ["WEATHER_ADDRESS"], "places": os.environ["PLACES_ADDRESS"],
            "events": os.environ["EVENTS_ADDRESS"]}


async def deliver(ctx: Context, sender: str, intent: dict, note: str):
    geo = await tools.geocode(intent["city"])
    if not geo:
        await ctx.send(sender, message(f"I couldn't find **{intent['city']}** on the map. Could you check the spelling?"))
        return
    rid = uuid4().hex[:12]
    ctx.storage.set(f"job:{rid}", {"sender": sender, "intent": intent, "note": note, "geo": slim_geo(geo), "t": time.time()})
    ctx.storage.set("jobs", (ctx.storage.get("jobs") or []) + [rid])
    addr = specialists()
    for part, req in zip(PARTS, plan_requests(intent, geo, rid)):
        await ctx.send(addr[part], req)


async def got_part(ctx: Context, rid: str, part: str, value):
    if not ctx.storage.get(f"job:{rid}"):
        return  # already finished (late answer)
    ctx.storage.set(f"job:{rid}:{part}", {"v": value})
    if all(ctx.storage.get(f"job:{rid}:{p}") is not None for p in PARTS):
        await finish(ctx, rid)


async def finish(ctx: Context, rid: str):
    job = ctx.storage.get(f"job:{rid}")
    if not job:
        return
    ctx.storage.remove(f"job:{rid}")  # claim it first so a racing answer can't finish it twice
    parts = {p: (ctx.storage.get(f"job:{rid}:{p}") or {}).get("v") for p in PARTS}
    for p in PARTS:
        ctx.storage.remove(f"job:{rid}:{p}")
    ctx.storage.set("jobs", [j for j in ctx.storage.get("jobs") or [] if j != rid])
    state = await finish_state(job["intent"], job["geo"], parts["weather"], parts["places"] or [], parts["events"] or [])
    await send_plan(ctx, job["sender"], state, job["note"])


@agent.on_message(WeatherReport)
async def on_weather(ctx: Context, sender: str, msg: WeatherReport):
    await got_part(ctx, msg.rid, "weather", msg.weather if msg.ok else None)


@agent.on_message(PlacesReport)
async def on_places(ctx: Context, sender: str, msg: PlacesReport):
    await got_part(ctx, msg.rid, "places", msg.places if msg.ok else [])


@agent.on_message(EventsReport)
async def on_events(ctx: Context, sender: str, msg: EventsReport):
    await got_part(ctx, msg.rid, "events", msg.events if msg.ok else [])


@agent.on_interval(period=20.0)
async def sweep(ctx: Context):
    """Finish jobs whose specialists never answered, so the user always gets a plan."""
    for rid in ctx.storage.get("jobs") or []:
        job = ctx.storage.get(f"job:{rid}")
        if not job:
            ctx.storage.set("jobs", [j for j in ctx.storage.get("jobs") or [] if j != rid])
        elif time.time() - job["t"] > JOB_TIMEOUT:
            await finish(ctx, rid)


@agent.on_interval(period=1800.0)
async def rain_watch(ctx: Context):
    await rain_watch_tick(ctx)


async def more_places(ctx: Context, geo: dict, categories: list[str], per_cat: int) -> list[dict]:
    # swap's deeper search runs inline: hosted handlers can't wait for another agent's reply
    try:
        return await tools.fetch_places(geo["name"], geo["latitude"], geo["longitude"], categories, per_cat)
    except Exception:
        return []


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


agent.include(chat, publish_manifest=True)
agent.include(payments, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
