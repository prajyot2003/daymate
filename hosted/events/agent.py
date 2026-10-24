"""DayMate events agent – Agentverse hosted specialist. Answers EventsRequest from the DayMate orchestrator."""

from uagents import Agent, Context

from answers import answer_events
from messages import EventsReport, EventsRequest

agent = Agent()


@agent.on_message(EventsRequest, replies=EventsReport)
async def on_request(ctx: Context, sender: str, req: EventsRequest):
    await ctx.send(sender, await answer_events(req))


if __name__ == "__main__":
    agent.run()
