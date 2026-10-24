"""Specialist answers: the same code runs in the local and the Agentverse-hosted weather/places/events agents."""

import tools
from messages import (EventsReport, EventsRequest, PlacesReport, PlacesRequest,
                      WeatherReport, WeatherRequest)


async def answer_weather(req: WeatherRequest) -> WeatherReport:
    try:
        return WeatherReport(ok=True, rid=req.rid, weather=await tools.fetch_weather(req.lat, req.lon, req.start, req.days))
    except Exception:
        return WeatherReport(ok=False, rid=req.rid, weather={})


async def answer_places(req: PlacesRequest) -> PlacesReport:
    try:
        return PlacesReport(ok=True, rid=req.rid,
                            places=await tools.fetch_places(req.city, req.lat, req.lon, req.categories, req.per_cat))
    except Exception:
        return PlacesReport(ok=False, rid=req.rid, places=[])


async def answer_events(req: EventsRequest) -> EventsReport:
    try:
        return EventsReport(ok=True, rid=req.rid, events=await tools.fetch_events(req.city, req.days))
    except Exception:
        return EventsReport(ok=False, rid=req.rid, events=[])
