"""End-to-end: the whole LangGraph pipeline with fake APIs and a fake LLM."""
from langgraph.checkpoint.memory import MemorySaver

from backend import Services, build_graph, run_travel_agent
from conftest import TODAY, FakeLLM, FakeSession, FakeTavily

REQUEST = dict(origin_city="Atlanta", destination_city="San Francisco", destination_state="California",
               departure_date="2026-12-10", return_date="2026-12-17", budget_usd=4000,
               interests=["food", "views"])

BAD_DRAFT = {"days": [{"day_number": 1, "activities": [
                 {"time_of_day": "Evening", "description": "Seafood dinner for about $250.", "source_id": "S2"}]},
             {"day_number": 2, "activities": [
                 {"time_of_day": "Morning", "description": "Visit Coit Tower.", "source_id": "S7"}]}],
             "tips": ["Get a Clipper card."]}

GOOD_DRAFT = {"days": [
    {"day_number": 1, "activities": [{"time_of_day": "Evening", "description": "Settle in near your hotel.", "source_id": None}]},
    {"day_number": 2, "activities": [
        {"time_of_day": "Morning", "description": "Walk across the Golden Gate Bridge.", "source_id": "S1"},
        {"time_of_day": "Afternoon", "description": "Eat your way through the Ferry Building.", "source_id": "S2"}]},
    {"day_number": 3, "activities": [{"time_of_day": "Afternoon", "description": "Take the ferry to Alcatraz.", "source_id": "S3"}]}],
    "tips": ["Get a Clipper card for transit."]}


def make(llm_answers):
    llm = FakeLLM(llm_answers)
    session = FakeSession()
    graph = build_graph(Services(llm=llm, http_session=session, tavily_client=FakeTavily(),
                                 today=lambda: TODAY), MemorySaver())
    return graph, llm, session


def test_full_plan_with_retry_after_failed_verification():
    graph, llm, _ = make({"TripRequest": [REQUEST], "ItineraryDraft": [BAD_DRAFT, GOOD_DRAFT]})
    out = run_travel_agent("A week in San Francisco from Atlanta, Dec 10-17, budget $4000", "t1", graph=graph)
    answer = out["answer"]

    assert out["llm_calls"] == 3                    # extract + 2 itinerary attempts
    assert "$250" not in answer                     # hallucinated price never shown
    assert "$470" in answer and "$1,260" in answer  # real prices, from data
    assert out["stay"] == {"check_in": "2026-12-10", "check_out": "2026-12-17", "nights": 7, "num_days": 8,
                           "first_day": "2026-12-10", "notes": []}
    assert "### Day 1 — Thu Dec 10" in answer and "### Day 8 — Thu Dec 17" in answer
    assert "return flight departs 2026-12-17 12:00" in answer
    assert "flight arrives 2026-12-10 11:30" in answer
    assert "San Francisco, California" in answer
    assert "[S1]" in answer and "https://example.com/goldengate" in answer
    assert "What was checked" in answer and "Not checked" in answer


def test_unfixable_items_are_removed():
    graph, *_ = make({"TripRequest": [REQUEST], "ItineraryDraft": [BAD_DRAFT, BAD_DRAFT]})
    out = run_travel_agent("San Francisco trip", "t2", graph=graph)
    assert "$250" not in out["answer"] and "Visit Coit Tower." not in out["answer"]
    assert "3 plan item(s) were removed" in out["answer"]   # $250, bad source S7, transit card tip


def test_missing_info_asks_and_searches_nothing():
    graph, _, session = make({"TripRequest": [dict(destination_city="Chicago")]})
    out = run_travel_agent("I want to visit Chicago", "t3", graph=graph)
    assert out["questions"] and session.calls == []
    assert "Which U.S. city are you flying from?" in out["answer"]


def test_follow_up_remembers_trip():
    graph, llm, _ = make({
        "TripRequest": [dict(destination_city="San Francisco", origin_city="Atlanta"),
                        dict(departure_date="2026-12-10", return_date="2026-12-17", adults=2)],
        "ItineraryDraft": [GOOD_DRAFT]})
    first = run_travel_agent("Atlanta to San Francisco", "t4", graph=graph)
    assert first["questions"]
    second = run_travel_agent("Dec 10 to 17, two of us", "t4", graph=graph)
    assert second["questions"] == []
    assert second["trip"]["destination_label"] == "San Francisco" and second["trip"]["adults"] == 2


def test_llm_down_still_returns_verified_data():
    graph, *_ = make({"TripRequest": [REQUEST], "ItineraryDraft": [RuntimeError("Groq down")]})
    out = run_travel_agent("San Francisco trip", "t5", graph=graph)
    assert "$470" in out["answer"] and "couldn't be generated" in out["answer"]


def test_llm_down_during_understanding_gives_clear_message():
    graph, _, session = make({"TripRequest": [RuntimeError("Groq down")]})
    out = run_travel_agent("San Francisco trip", "t6", graph=graph)
    assert out["answer"].startswith("Sorry, I couldn't read that request") and session.calls == []


def test_failed_retry_keeps_earlier_checked_draft():
    incomplete = {"days": [{"day_number": 2, "activities": [
        {"time_of_day": "Morning", "description": "Walk the Golden Gate Bridge.", "source_id": "S1"}]}], "tips": []}
    graph, *_ = make({"TripRequest": [REQUEST], "ItineraryDraft": [incomplete, RuntimeError("Groq down")]})
    out = run_travel_agent("San Francisco trip", "t7", graph=graph)
    assert "Walk the Golden Gate Bridge." in out["answer"] and "couldn't be generated" not in out["answer"]
