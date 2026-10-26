"""Shared test helpers: fake HTTP, fake Tavily, fake LLM.

The fake API responses follow the field names in SerpApi's documented examples.
They are NOT real prices. They exist so the whole pipeline can be tested
without API keys or network, and with results we know in advance.
"""
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TODAY = date(2026, 10, 26)


def leg(airline, number, frm, to, departs, arrives, minutes):
    return {"airline": airline, "flight_number": number, "duration": minutes,
            "travel_class": "Economy",
            "departure_airport": {"id": frm, "name": f"{frm} airport", "time": departs},
            "arrival_airport": {"id": to, "name": f"{to} airport", "time": arrives}}


OUTBOUND = {
    "best_flights": [
        {"flights": [leg("Delta", "DL 1234", "ATL", "SFO", "2026-12-10 08:00", "2026-12-10 10:35", 335)],
         "total_duration": 335, "price": 520, "type": "Round trip", "departure_token": "tok_dl"},
    ],
    "other_flights": [
        {"flights": [leg("American", "AA 100", "ATL", "DFW", "2026-12-10 07:00", "2026-12-10 08:30", 150),
                     leg("American", "AA 200", "DFW", "SFO", "2026-12-10 09:40", "2026-12-10 11:30", 230)],
         "layovers": [{"id": "DFW", "name": "Dallas", "duration": 70}],
         "total_duration": 450, "price": 430, "type": "Round trip", "departure_token": "tok_aa"},
        # Wrong destination airport (LAX is not a San Francisco airport): must be filtered out.
        {"flights": [leg("United", "UA 7", "ATL", "LAX", "2026-12-10 09:00", "2026-12-10 10:50", 290)],
         "total_duration": 290, "price": 300, "type": "Round trip", "departure_token": "tok_ua"},
        # No price: must be skipped.
        {"flights": [leg("Alaska", "AS 1", "ATL", "SFO", "2026-12-10 12:00", "2026-12-10 14:30", 330)],
         "total_duration": 330, "type": "Round trip"},
    ],
    "price_insights": {"lowest_price": 430, "price_level": "typical"},
}

RETURNS = {
    "tok_dl": {"best_flights": [
        {"flights": [leg("Delta", "DL 1235", "SFO", "ATL", "2026-12-17 13:00", "2026-12-17 20:45", 285)],
         "total_duration": 285, "price": 560}]},
    "tok_aa": {"best_flights": [
        {"flights": [leg("American", "AA 201", "SFO", "DFW", "2026-12-17 12:00", "2026-12-17 17:30", 210),
                     leg("American", "AA 101", "DFW", "ATL", "2026-12-17 18:30", "2026-12-17 21:40", 130)],
         "total_duration": 400, "price": 470}]},
}

HOTELS = {"properties": [
    {"type": "hotel", "name": "Budget Inn SF", "overall_rating": 3.6, "reviews": 800,
     "extracted_hotel_class": 2, "link": "https://example.com/budget",
     "rate_per_night": {"lowest": "$90", "extracted_lowest": 90},
     "total_rate": {"lowest": "$630", "extracted_lowest": 630}},
    {"type": "hotel", "name": "Union Square Hotel", "overall_rating": 4.4, "reviews": 2310,
     "gps_coordinates": {"latitude": 37.7880, "longitude": -122.4075},
     "extracted_hotel_class": 4, "link": "https://example.com/union",
     "rate_per_night": {"lowest": "$210", "extracted_lowest": 210},
     "total_rate": {"lowest": "$1,470", "extracted_lowest": 1470}},
    {"type": "hotel", "name": "Marina Inn", "overall_rating": 4.1, "reviews": 950,
     "gps_coordinates": {"latitude": 37.6200, "longitude": -122.3900},
     "extracted_hotel_class": 3, "link": "https://example.com/marina",
     "rate_per_night": {"lowest": "$180", "extracted_lowest": 180}},            # no total_rate
    {"type": "hotel", "name": "Mystery Hotel", "overall_rating": 4.9},       # no price: skipped
]}

TAVILY = {"results": [
    {"title": "Golden Gate Bridge visitor guide", "url": "https://example.com/goldengate", "content": "Walk or bike across San Francisco's bridge."},
    {"title": "Billboard Hot 100", "url": "https://example.com/billboard", "content": "This week's top songs."},   # junk: must be dropped
    {"title": "Ferry Building Marketplace", "url": "https://example.com/san-francisco/ferry", "content": "Local food stalls on the Embarcadero."},
    {"title": "Alcatraz Island tours from San Francisco", "url": "https://example.com/alcatraz", "content": "Ferry to the former prison."},
]}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def _place(title, lat, lng, kind, rating, reviews, hours=None, **extra):
    week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    item = {"title": title, "place_id": "ChIJ" + "".join(c for c in title if c.isalnum())[:12],
            "gps_coordinates": {"latitude": lat, "longitude": lng}, "type": kind, "types": [kind],
            "rating": rating, "reviews": reviews, "address": f"{title} St, San Francisco, CA 94100"}
    if hours is not None:
        item["operating_hours"] = {d: hours.get(d, hours.get("default")) for d in week}
    item.update(extra)
    return item


# Shaped like SerpApi google_maps local_results. Coordinates are real San Francisco locations.
PLACES = {"local_results": [
    _place("Golden Gate Bridge", 37.8199, -122.4783, "Bridge", 4.8, 90000, {"default": "Open 24 hours"}),
    _place("Ferry Building Marketplace", 37.7955, -122.3937, "Market", 4.7, 30000,
           {"default": "10 AM–7 PM", "sunday": "11 AM–5 PM"}),
    _place("Exploratorium", 37.8017, -122.3973, "Science museum", 4.7, 15000,
           {"default": "10 AM–5 PM", "monday": "Closed"}),
    _place("Coit Tower", 37.8024, -122.4058, "Tourist attraction", 4.5, 20000, {"default": "10 AM–6 PM"}),
    _place("de Young Museum", 37.7715, -122.4687, "Art museum", 4.7, 12000,
           {"default": "9:30 AM–5:15 PM", "monday": "Closed"}),
    _place("Golden Gate Park", 37.7694, -122.4862, "Park", 4.8, 60000, {"default": "Open 24 hours"}),
    _place("Lombard Street", 37.8021, -122.4187, "Tourist attraction", 4.6, 40000),           # no hours listed
    _place("Tadich Grill", 37.7935, -122.3995, "Seafood restaurant", 4.5, 4000,
           {"default": "11 AM–9 PM", "sunday": "Closed"}),
    # Junk that must be filtered out:
    _place("Fairmont San Francisco", 37.7924, -122.4102, "Hotel", 4.6, 9000),               # a hotel
    _place("Tiny Corner Gallery", 37.7800, -122.4100, "Art gallery", 5.0, 3),               # 3 reviews
    _place("Griffith Observatory", 34.1184, -118.3004, "Observatory", 4.8, 70000),          # Los Angeles
    _place("Old Closed Diner", 37.7850, -122.4060, "Diner", 4.5, 900, open_state="Permanently closed"),
    {"title": None, "gps_coordinates": None},                                               # broken item
]}


class FakeSession:
    """Stands in for `requests`. Records every call so tests can inspect params.
    `places=None` returns no Google Maps results, so older tests use the web-source fallback."""

    def __init__(self, outbound=OUTBOUND, returns=RETURNS, hotels=HOTELS, places=None):
        self.outbound, self.returns, self.hotels, self.places = outbound, returns, hotels, places
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        if params["engine"] == "google_maps":
            return FakeResponse(self.places or {})
        if params["engine"] == "google_hotels":
            return FakeResponse(self.hotels)
        if "departure_token" in params:
            return FakeResponse(self.returns.get(params["departure_token"], {}))
        return FakeResponse(self.outbound)


class FakeTavily:
    def search(self, query, max_results=5):
        return TAVILY


class FakeLLM:
    """Returns queued answers per output schema, e.g. {'TripRequest': [obj, ...]}."""

    def __init__(self, answers: dict):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        llm = self

        class Runner:
            def invoke(self, messages):
                llm.calls.append((schema.__name__, messages))
                queue = llm.answers.get(schema.__name__)
                if not queue:
                    raise RuntimeError(f"No fake answer left for {schema.__name__}")
                answer = queue.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):          # a fake that reads its input, like a real planner
                    answer = answer(messages)
                return schema(**answer) if isinstance(answer, dict) else answer

        return Runner()


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
