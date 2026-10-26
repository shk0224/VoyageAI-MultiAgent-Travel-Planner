"""Form tab: /api/plan skips LLM extraction; /api/airports autocomplete; hotel preferences."""
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import app as app_module
import backend
from conftest import TODAY, FakeLLM, FakeSession, FakeTavily
from core.models import TripRequest
from core.trip_parser import validate_request
from test_pipeline import GOOD_DRAFT
from tools.airports import resolve_airports, search_airports
from tools.hotel_tool import search_hotels

FORM = {
    "origin_city": "Atlanta", "origin_state": "Georgia",
    "destination_city": "San Francisco", "destination_state": "California",
    "departure_date": "2026-12-10", "return_date": "2026-12-17", "one_way": False,
    "adults": 2, "children": 0, "budget_usd": 4000, "nonstop_only": False,
    "travel_class": "economy", "min_hotel_rating": 4.0,
    "allow_rentals": False, "allow_hostels": False, "interests": ["Food", "Museums"],
}


@pytest.fixture
def setup(monkeypatch):
    # No TripRequest answers queued: if the form path called the LLM for extraction, it would fail.
    llm = FakeLLM({"ItineraryDraft": [GOOD_DRAFT, GOOD_DRAFT], "TripRequest": [dict(nonstop_only=True)]})
    graph = backend.build_graph(backend.Services(llm=llm, http_session=FakeSession(),
                                                 tavily_client=FakeTavily(), today=lambda: TODAY), MemorySaver())
    monkeypatch.setattr(backend, "_graph", graph)
    monkeypatch.setattr(app_module, "limiter", app_module.RateLimiter(limit=100))
    return TestClient(app_module.app), llm


def test_form_plan_skips_llm_extraction(setup):
    client, llm = setup
    r = client.post("/api/plan", json=FORM)
    data = r.json()
    assert r.status_code == 200 and data["success"], data
    assert "## Flights" in data["answer"] and "## Budget" in data["answer"]
    assert "TripRequest" not in [name for name, _ in llm.calls]        # extraction never called
    assert data["trip"]["adults"] == 2 and data["trip"]["allow_rentals"] is False
    assert "Hotel preferences:** rated 4+, no vacation rentals, no hostels" in data["answer"]
    assert "Interests:** Food, Museums" in data["answer"]
    assert data["summary"].startswith("📝 Trip form: Atlanta → San Francisco")


def test_chat_follow_up_after_form_keeps_trip(setup):
    client, _ = setup
    client.post("/api/plan", json=FORM)
    r = client.post("/api/travel", json={"message": "make it nonstop only"})
    trip = r.json()["trip"]
    assert trip["destination_label"] == "San Francisco" and trip["nonstop_only"] is True
    history = client.get("/api/history").json()["messages"]
    assert history[0]["content"].startswith("📝 Trip form:")


def test_form_rejects_bad_values(setup):
    client, _ = setup
    assert client.post("/api/plan", json={**FORM, "adults": 0}).status_code == 422
    assert client.post("/api/plan", json={**FORM, "departure_date": "next friday"}).status_code == 422
    assert client.post("/api/plan", json={**FORM, "travel_class": "rocket"}).status_code == 422


def test_form_past_date_gets_a_question(setup):
    client, _ = setup
    data = client.post("/api/plan", json={**FORM, "departure_date": "2026-01-05"}).json()
    assert data["questions"] and "past" in data["questions"][0]


def test_airport_autocomplete(setup):
    client, _ = setup
    results = client.get("/api/airports", params={"q": "port"}).json()["results"]
    labels = [r["label"] for r in results]
    assert labels[0] == "Portland, Oregon (PDX)" and labels[1] == "Portland, Maine (PWM)"
    assert client.get("/api/airports", params={"q": "a"}).json()["results"] == []


def test_every_suggestion_resolves_to_airports():
    for q in ("new", "wash", "port", "mia", "atl", "hono", "bos", "jfk", "den"):
        for item in search_airports(q):
            res = resolve_airports(item["city"], item["state"])
            assert res.codes and not res.question, (q, item)


def test_hotel_preferences_filter():
    trip, _ = validate_request(TripRequest(origin_city="Atlanta", destination_city="San Francisco",
                                           departure_date="2026-12-10", return_date="2026-12-17",
                                           min_hotel_rating=4.2), TODAY)
    names = [h["name"] for h in search_hotels(trip, session=FakeSession())["offers"]]
    assert names == ["Union Square Hotel"]           # only the 4.4-rated one passes 4.2+

    strict, _ = validate_request(TripRequest(origin_city="Atlanta", destination_city="San Francisco",
                                             departure_date="2026-12-10", return_date="2026-12-17",
                                             min_hotel_rating=5.0), TODAY)
    result = search_hotels(strict, session=FakeSession())
    assert result["status"] == "empty" and "relaxing" in result["message"]
