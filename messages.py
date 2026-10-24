"""Messages between the DayMate orchestrator and its specialist agents (shared so schema digests match)."""

from uagents import Model


class WeatherRequest(Model):
    rid: str = ""  # request id, echoed back so replies can be matched without waiting
    lat: float
    lon: float
    start: str  # YYYY-MM-DD
    days: int


class WeatherReport(Model):
    ok: bool
    rid: str = ""
    weather: dict


class PlacesRequest(Model):
    rid: str = ""  # request id, echoed back so replies can be matched without waiting
    city: str
    lat: float
    lon: float
    categories: list[str]
    per_cat: int = 6


class PlacesReport(Model):
    ok: bool
    rid: str = ""
    places: list[dict]


class EventsRequest(Model):
    rid: str = ""  # request id, echoed back so replies can be matched without waiting
    city: str
    days: list[str]


class EventsReport(Model):
    ok: bool
    rid: str = ""
    events: list[dict]
