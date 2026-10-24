"""DayMate places agent – Agentverse hosted specialist. Answers PlacesRequest from the DayMate orchestrator."""

from uagents import Agent, Context

from answers import answer_places
from messages import PlacesReport, PlacesRequest

agent = Agent()


@agent.on_message(PlacesRequest, replies=PlacesReport)
async def on_request(ctx: Context, sender: str, req: PlacesRequest):
    await ctx.send(sender, await answer_places(req))


if __name__ == "__main__":
    agent.run()
