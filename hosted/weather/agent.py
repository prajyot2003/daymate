"""DayMate weather agent – Agentverse hosted specialist. Answers WeatherRequest from the DayMate orchestrator."""

from uagents import Agent, Context

from answers import answer_weather
from messages import WeatherReport, WeatherRequest

agent = Agent()


@agent.on_message(WeatherRequest, replies=WeatherReport)
async def on_request(ctx: Context, sender: str, req: WeatherRequest):
    await ctx.send(sender, await answer_weather(req))


if __name__ == "__main__":
    agent.run()
